"""Export, verify, rebuild, and replay — the archive's reason to exist.

An archive nobody has restored from is a backup nobody has tested. These four
operations are what make the claim "MongoDB is a rebuildable index" checkable
rather than aspirational:

``export``
    Mongo -> archive. Bootstraps the archive from an existing database and
    repairs drift. Idempotent: a second run writes nothing.

``verify``
    Compare, report, don't touch. Recomputes each document's archive payload
    and diffs it against the file on disk.

``rebuild``
    archive -> Mongo. Drops and repopulates the catalog collections from
    ``records/``, then regenerates the derived ones.

``replay``
    ``journal/`` -> Mongo. Same destination, but reconstructed purely by
    replaying events in order. If ``replay`` and ``rebuild`` agree, the journal
    is genuinely self-sufficient and the current-state tree is an optimization
    rather than a dependency.

Fidelity note
-------------
``rebuild`` writes through ``to_mongo()`` and raw pymongo ``ReplaceOne``, never
``Document.save()``. ``Material.save()`` and ``Recipe.save()`` stamp
``updated_at = now()`` unconditionally, so a save-based rebuild would rewrite
every timestamp in the catalog and quietly destroy the very provenance the
archive exists to protect. ``to_mongo()`` still performs full field coercion —
including parsing the ISO-8601 strings in the JSON back into ``datetime`` — so
nothing is lost by skipping ``save()``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from bson import ObjectId
from pymongo import ReplaceOne

from catalog.canonical import archive_json_bytes, sha256_bytes
from . import registry, writer

logger = logging.getLogger(__name__)

#: Order matters on rebuild: materials before recipes (recipes reference a
#: material), and both before anything that annotates them.
REBUILD_ORDER: tuple[str, ...] = (
    "material",
    "recipe",
    "raw_file",
    "synthesis_parse_cache",
    "synthesis_prediction",
    "model_version",
    "model_feedback",
    "xrd_analysis_job",
    "xrd_analysis_review",
    "user_affiliation",
    "user_precursor",
    "user_protocol",
)


@dataclass
class Counts:
    written: int = 0
    unchanged: int = 0
    deleted: int = 0
    failed: int = 0
    #: Documents that resolved to an archive path another document already
    #: claimed. Means one of them was overwritten and will not survive a
    #: rebuild — a data-loss bug, not a warning.
    collisions: int = 0
    #: Source data that was already gone before the archive could copy it —
    #: distinct from `failed`, which means the archive write itself broke.
    #: Reported loudly but never fatal: a catalog with a historical gap must
    #: still be able to archive everything it does have.
    missing: int = 0

    def merge(self, other: "Counts") -> None:
        self.written += other.written
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        self.failed += other.failed
        self.missing += other.missing
        self.collisions += other.collisions


@dataclass
class VerifyReport:
    """Divergence between Mongo and the archive."""

    missing_on_disk: list[str] = field(default_factory=list)
    missing_in_db: list[str] = field(default_factory=list)
    content_differs: list[str] = field(default_factory=list)
    drift_events: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (
            self.missing_on_disk
            or self.missing_in_db
            or self.content_differs
            or self.drift_events
        )

    def total(self) -> int:
        return (
            len(self.missing_on_disk)
            + len(self.missing_in_db)
            + len(self.content_differs)
            + len(self.drift_events)
        )


# --------------------------------------------------------------------------
# Payload enumeration (Mongo side)
# --------------------------------------------------------------------------

def _iter_payloads(kind_name: str) -> Iterator[dict]:
    """Yield the archive payload for every document of one kind, from Mongo.

    Reads raw BSON rather than MongoEngine objects — see
    :func:`registry.raw_payload` for why that distinction is load-bearing.
    """
    archive_kind = registry.kind(kind_name)
    document_class = archive_kind.document_class()
    collection = document_class._get_collection()

    if kind_name == "recipe":
        for raw in collection.find():
            payload = registry.raw_payload(
                document_class, raw, drop=archive_kind.drop_fields
            )
            payload["trial_ids"] = [
                t.get("trial_id") for t in (raw.get("trials") or [])
            ]
            payload["literature_ids"] = [
                item.get("lit_id") for item in (raw.get("literature") or [])
            ]
            yield payload
        return

    for raw in collection.find():
        yield registry.raw_payload(document_class, raw, drop=archive_kind.drop_fields)


def _iter_recipe_children(kind_name: str) -> Iterator[dict]:
    """Yield trial or literature payloads, flattened out of their recipes."""
    from catalog.documents import EmbeddedLiterature, EmbeddedTrial, Recipe

    attribute = "trials" if kind_name == "trial" else "literature"
    embedded_class = EmbeddedTrial if kind_name == "trial" else EmbeddedLiterature
    for raw in Recipe._get_collection().find():
        recipe_auid = str(raw.get("_id"))
        material_auid = str(raw.get("material_auid") or "")
        for child in raw.get(attribute) or []:
            payload = registry.raw_payload(embedded_class, child)
            payload["recipe_auid"] = recipe_auid
            payload["material_auid"] = material_auid
            yield payload


def iter_kind_payloads(kind_name: str) -> Iterator[dict]:
    if kind_name in ("trial", "literature"):
        return _iter_recipe_children(kind_name)
    return _iter_payloads(kind_name)


ALL_KINDS: tuple[str, ...] = REBUILD_ORDER + ("trial", "literature")


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export(
    kinds: Optional[Iterable[str]] = None,
    *,
    prune: bool = False,
    progress=None,
    progress_every: int = 2000,
) -> dict[str, Counts]:
    """Write every Mongo document into the archive.

    Streams document-by-document (see the loop below for why that matters) and
    runs inside ``bulk_mode()`` so it does not fsync once per record — an
    export is re-runnable, so per-record durability buys nothing.

    ``prune`` additionally removes archive files with no corresponding
    document. Off by default: an unexpected empty database would otherwise
    delete the archive, which is precisely the disaster the archive exists to
    prevent.

    ``progress`` is an optional callable invoked every ``progress_every``
    records, so a long run is distinguishable from a hung one.
    """
    selected = tuple(kinds) if kinds else ALL_KINDS
    results: dict[str, Counts] = {}

    writer.write_schema_version()
    _write_readme()

    for kind_name in selected:
        counts = Counts()
        seen: set[str] = set()
        collisions: list[str] = []
        # Stream: read one document, write it, move on. An earlier version
        # buffered the whole collection first, which was wrong in three ways —
        # it held every material in memory at once, it gave no progress for
        # minutes, and most importantly it opened a staleness window as long as
        # the export itself: a document read at the start could be modified in
        # MongoDB before its (already stale) payload was written, leaving the
        # archive disagreeing with the database the moment it finished.
        with writer.bulk_mode():
            for index, payload in enumerate(iter_kind_payloads(kind_name), 1):
                try:
                    result = writer.write_payload(kind_name, payload)
                except Exception:
                    logger.exception("archive export failed for %s", kind_name)
                    counts.failed += 1
                    continue
                if result is None:
                    counts.failed += 1
                    continue
                if result.path in seen:
                    # Two documents resolved to the same archive file, so one
                    # has silently overwritten the other and only one survives
                    # a rebuild. Always a bug — either a key that is not unique
                    # (empty or duplicated) or a `path_of` that loses
                    # information. Reported rather than raised so the rest of
                    # the export still completes.
                    collisions.append(result.path)
                    logger.error(
                        "archive: %s path collision at %s — a record has been "
                        "overwritten and will be lost on rebuild",
                        kind_name, result.path,
                    )
                seen.add(result.path)
                if result.changed:
                    counts.written += 1
                else:
                    counts.unchanged += 1
                if progress and index % progress_every == 0:
                    progress(f"  {kind_name}: {index} processed...")
            if prune:
                counts.deleted += _prune_kind(kind_name, seen)
        counts.collisions = len(collisions)
        results[kind_name] = counts

    # Blobs and Django accounts are not MongoEngine documents, so they sit
    # outside the kind table. They belong here rather than in the management
    # command: `export()` is the complete operation, and a caller that reaches
    # for the library function should not silently get a partial archive.
    # Skipped for scoped runs so `export(["material"])` stays cheap.
    if not kinds:
        results["blob"] = export_blobs(progress=progress)
        try:
            results["django_auth"] = export_auth()
        except Exception:
            logger.exception("archive: Django account export failed")
            results["django_auth"] = Counts(failed=1)
    return results


def export_documents(kind_name: str, ids: Iterable[str]) -> Counts:
    """Archive a specific set of documents by primary key.

    For write paths that bulk-update MongoDB directly and so fire no signals.
    A full ``export`` would also repair the drift, but walking 35k materials
    after every container start is not something anyone should pay for — this
    touches only what actually changed.
    """
    archive_kind = registry.kind(kind_name)
    document_class = archive_kind.document_class()
    identifiers = [str(i) for i in ids]
    counts = Counts()
    if not identifiers:
        return counts
    collection = document_class._get_collection()
    with writer.bulk_mode():
        for raw in collection.find({"_id": {"$in": identifiers}}):
            payload = registry.raw_payload(
                document_class, raw, drop=archive_kind.drop_fields
            )
            result = writer.write_payload(kind_name, payload)
            if result is None:
                counts.failed += 1
            elif result.changed:
                counts.written += 1
            else:
                counts.unchanged += 1
    return counts


def export_auth() -> Counts:
    """Archive the Django-side accounts: users, groups, and API keys.

    These live in SQLite, not MongoDB, so no signal hook can reach them and
    ``loop_archive rebuild`` cannot restore them. Without this, a full recovery
    brings the entire catalog back and nobody can log in — and the restored
    ``UserAffiliation`` records, which gate who may see what, point at user IDs
    that no longer exist.

    Written as a Django fixture so recovery is ``manage.py loaddata`` with no
    bespoke import path. Password hashes and API-key hashes are included
    because an account roster without them is not a restore; the file is
    written ``0600`` for that reason. Neither is a usable credential — both are
    one-way hashes — but they are still the most sensitive bytes in the archive.
    """
    import os

    from django.contrib.auth.models import Group, User
    from django.core import serializers

    from catalog.models import APIKey

    root = writer.archive_root()
    if root is None:
        return Counts()

    payload = serializers.serialize(
        "json",
        list(User.objects.all().order_by("pk"))
        + list(Group.objects.all().order_by("pk"))
        + list(APIKey.objects.all().order_by("pk")),
        indent=2,
    )
    target = root / "records" / "auth" / "django-auth.json"
    writer._write_bytes(target, payload.encode("utf-8"))
    os.chmod(target, 0o600)

    counts = Counts(written=User.objects.count() + Group.objects.count() + APIKey.objects.count())
    writer.append_journal(
        op="upsert",
        kind_name="auth",
        key="django-auth",
        path="auth/django-auth.json",
        # Deliberately no body: the journal is world-readable within the
        # archive, and inlining password hashes into it would defeat the 0600
        # on the file itself.
        sha256=None,
        body=None,
    )
    return counts


def export_blobs(progress=None) -> Counts:
    """Copy every trial's raw upload into the content-addressed blob store.

    Blobs are normally written at upload time by ``xrd_store.store_raw_file``,
    so this only matters for files that predate the archive — but that is
    precisely the data worth having. Without it, an archive can hold complete
    metadata for years of experiments and not one diffraction pattern to
    re-analyze, while faithfully preserving gigabytes of re-importable AFLOW
    records.

    Uses ``xrd_store.resolve_raw_path``, which already understands the current
    layout and both legacy ones, so trials from every era are found. Linking is
    idempotent and content-addressed: re-running costs a stat per trial.
    """
    from catalog import xrd_store
    from catalog.documents import Recipe

    counts = Counts()
    with writer.bulk_mode():
        for recipe in Recipe.objects.no_cache().only("id", "trials"):
            for trial in recipe.trials or []:
                digest = trial.file_hash or _trial_file_hash(trial)
                if not digest:
                    continue
                try:
                    raw_path = xrd_store.resolve_raw_path(str(recipe.id), trial.trial_id)
                except Exception:
                    logger.exception(
                        "archive: could not resolve raw path for %s/%s",
                        recipe.id, trial.trial_id,
                    )
                    counts.failed += 1
                    continue
                if not raw_path:
                    # The trial records a file hash but the bytes are gone from
                    # MEDIA_ROOT — the pattern was already lost before the
                    # archive existed. Counted as `missing`, not `failed`: the
                    # archive did nothing wrong, and a pre-existing gap must not
                    # stop the rest of the catalog from being archived.
                    logger.warning(
                        "archive: no raw file on disk for %s/%s (hash %s)",
                        recipe.id, trial.trial_id, digest,
                    )
                    counts.missing += 1
                    continue
                # The trial's file_hash is what every reference uses, so the
                # blob is stored under it — but check the bytes actually hash to
                # that. A mismatch means the recorded hash and the file on disk
                # have diverged, and it is far better to learn that now than
                # during a restore, when `restore_blobs` will refuse the blob.
                from catalog.canonical import sha256_file

                try:
                    if sha256_file(raw_path) != digest:
                        logger.error(
                            "archive: %s/%s records file_hash %s but the file on "
                            "disk hashes differently — archiving it anyway, but "
                            "the recorded hash is wrong and restore will refuse it",
                            recipe.id, trial.trial_id, digest,
                        )
                except OSError:
                    pass

                extension = Path(raw_path).suffix
                existing = writer.archive_root() / registry.blob_path(digest, extension)
                already = existing.exists()
                if writer.write_blob(digest, raw_path, ext=extension) is None:
                    counts.failed += 1
                elif already:
                    counts.unchanged += 1
                else:
                    counts.written += 1
                    if progress and counts.written % 25 == 0:
                        progress(f"  blobs: {counts.written} linked...")
    return counts


def restore_blobs(progress=None) -> Counts:
    """Put archived raw uploads back under ``MEDIA_ROOT`` after a rebuild.

    The mirror image of :func:`export_blobs`, and the half of recovery that is
    easy to forget: restoring the documents gives every trial its ``file_hash``
    and a ``raw_data_link``, but with no bytes on disk the detail page has no
    pattern to plot and no analysis can be re-run. The metadata would look
    perfect and the science would be gone.

    Content addressing pays off here — the blob's name *is* the expected
    checksum, so a corrupted or truncated file is caught on the way out rather
    than discovered months later.
    """
    from catalog import xrd_store
    from catalog.canonical import sha256_file
    from catalog.documents import Recipe

    root = writer.archive_root()
    counts = Counts()
    if root is None:
        return counts

    for recipe in Recipe.objects.no_cache().only("id", "trials"):
        for trial in recipe.trials or []:
            digest = trial.file_hash or _trial_file_hash(trial)
            if not digest:
                continue
            shard = root / "blobs" / "sha256" / digest[:2] / digest[2:4]
            matches = sorted(shard.glob(f"{digest}*")) if shard.is_dir() else []
            if not matches:
                logger.warning(
                    "archive: no blob for %s/%s (hash %s)",
                    recipe.id, trial.trial_id, digest,
                )
                counts.missing += 1
                continue
            blob = matches[0]
            try:
                if sha256_file(blob) != digest:
                    logger.error(
                        "archive: blob %s fails its own checksum — refusing to "
                        "restore a corrupted pattern", blob,
                    )
                    counts.failed += 1
                    continue
                folder = xrd_store.trial_dir(str(recipe.id), trial.trial_id)
                target = folder / f"raw{blob.suffix}"
                if target.exists() and sha256_file(target) == digest:
                    counts.unchanged += 1
                    continue
                for stale in folder.glob("raw.*"):
                    if stale != target:
                        stale.unlink(missing_ok=True)
                try:
                    os.link(blob, target)
                except OSError:
                    shutil.copy2(blob, target)
                counts.written += 1
                if progress and counts.written % 25 == 0:
                    progress(f"  raw files: {counts.written} restored...")
            except Exception:
                logger.exception(
                    "archive: could not restore raw file for %s/%s",
                    recipe.id, trial.trial_id,
                )
                counts.failed += 1
    return counts


def _trial_file_hash(trial):
    """Fall back to the hash stashed in ``exp_condition.additional_params``."""
    condition = getattr(trial, "exp_condition", None)
    if condition is None:
        return None
    return (getattr(condition, "additional_params", None) or {}).get("file_hash")


def _prune_kind(kind_name: str, keep_paths: set[str]) -> int:
    """Delete archive files of one kind that Mongo no longer has."""
    root = writer.archive_root()
    if root is None:
        return 0
    removed = 0
    for path, payload in _iter_archive_files(kind_name):
        relative = str(path.relative_to(root / "records"))
        if relative in keep_paths:
            continue
        writer.delete_payload(kind_name, payload)
        removed += 1
    return removed


def _write_readme() -> None:
    """Drop a plain-language description of the format beside the data.

    The archive is meant to outlive this repository. Someone opening the
    directory in five years should not have to reverse-engineer the layout
    from the filenames.
    """
    root = writer.archive_root()
    if root is None:
        return
    text = """# LOOP archive

This directory is the authoritative record of the LOOP catalog. MongoDB is a
rebuildable index over it, not the other way around.

Everything here is plain UTF-8 JSON with sorted keys and two-space indentation,
plus the original uploaded files as opaque bytes. No database, no proprietary
format, and no part of the LOOP codebase is needed to read it.

## Layout

    records/    current state, one file per record
    blobs/      original uploaded files, addressed by SHA-256 of their contents
    journal/    append-only log of every change, one JSONL file per UTC day

`records/` mirrors the catalog hierarchy: a material directory contains its
recipes, each recipe contains its trials and literature. Colons in AUIDs are
written as hyphens because some filesystems reject them.

`journal/` inlines the full body of every create, update, and delete, so it can
reconstruct the catalog at any past moment on its own. Losing `records/` and
losing `journal/` are independent failures.

## Restoring

    python manage.py loop_archive rebuild --yes --target-db loop

Rebuilds MongoDB from `records/`. To rebuild from the event log instead — which
also proves the journal is complete — use `replay`. To check that a live
database still matches this archive, use `verify`.

## What is deliberately absent

Vector embeddings, DOI lookup tables, plot caches, and background job queues
are derived data. They are regenerated after a rebuild rather than archived,
because storing them would double the archive's size and guarantee drift.
"""
    # Format documentation, not user data, and it travels with the offsite copy
    # so whoever restores the archive can read how it is laid out.
    writer._write_bytes(root / "ARCHIVE.md", text.encode("utf-8"), shared=True)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def _iter_archive_files(kind_name: str) -> Iterator[tuple[Path, dict]]:
    """Yield ``(path, payload)`` for every archived file of one kind.

    Finding files by *shape* rather than by a stored index means ``verify``
    genuinely inspects the directory rather than trusting bookkeeping that
    could itself be wrong.
    """
    root = writer.archive_root()
    if root is None:
        return
    records = root / "records"
    if not records.is_dir():
        return

    patterns = {
        "material": "materials/*/material.json",
        "recipe": "materials/*/recipes/*/recipe.json",
        "trial": "materials/*/recipes/*/trials/*/trial.json",
        "literature": "materials/*/recipes/*/literature/*.json",
        "raw_file": "raw-files/*/*/*.json",
        "synthesis_prediction": "synthesis-predictions/*.json",
        "synthesis_parse_cache": "synthesis-cache/*/*/*.json",
        "model_version": "models/versions/*.json",
        "model_feedback": "models/feedback/*.json",
        "xrd_analysis_job": "xrd-analyses/*/job.json",
        "xrd_analysis_review": "xrd-analyses/*/reviews/*.json",
        "user_affiliation": "users/*/affiliations.json",
        "user_precursor": "users/*/precursors/*.json",
        "user_protocol": "users/*/protocols/*.json",
    }
    pattern = patterns.get(kind_name)
    if not pattern:
        return
    for path in sorted(records.glob(pattern)):
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("archive: unreadable record %s", path)


def verify(kinds: Optional[Iterable[str]] = None) -> VerifyReport:
    """Compare Mongo against the archive without changing either."""
    selected = tuple(kinds) if kinds else ALL_KINDS
    report = VerifyReport()
    root = writer.archive_root()
    if root is None:
        return report

    records_root = root / "records"
    for kind_name in selected:
        # Compare one record at a time against its own file, holding only the
        # set of paths seen. The previous version buffered every projected
        # payload *and* every file's bytes for a kind simultaneously, which on
        # a large materials collection meant two full copies of the archive in
        # memory at once.
        seen: set[str] = set()
        for payload in iter_kind_payloads(kind_name):
            relative = registry.kind(kind_name).path_of(payload)
            seen.add(relative)
            target = records_root / relative
            if not target.exists():
                report.missing_on_disk.append(f"{kind_name}:{relative}")
                continue
            if target.read_bytes() != archive_json_bytes(payload):
                report.content_differs.append(f"{kind_name}:{relative}")

        for path, _payload in _iter_archive_files(kind_name):
            relative = str(path.relative_to(records_root))
            if relative not in seen:
                report.missing_in_db.append(f"{kind_name}:{relative}")

    marker = root / ".state" / "drift.json"
    if marker.exists():
        try:
            report.drift_events = json.loads(marker.read_text(encoding="utf-8")).get("events", [])
        except Exception:
            logger.warning("archive: unreadable drift marker")
    return report


# --------------------------------------------------------------------------
# rebuild
# --------------------------------------------------------------------------

def _coerce_document(kind_name: str, payload: dict, children: Optional[dict] = None):
    """Turn an archived payload back into storable BSON.

    Constructing the MongoEngine document does the type coercion for us —
    notably parsing ISO-8601 strings back into ``datetime`` and hex strings
    back into ``ObjectId`` — but we call ``to_mongo()`` rather than ``save()``
    so no ``save()`` override gets to restamp ``updated_at``.
    """
    archive_kind = registry.kind(kind_name)
    document_class = archive_kind.document_class()
    # Restore datetimes that were tagged because they sat inside an untyped
    # DictField. Declared fields are left as ISO strings for MongoEngine to
    # coerce; only the schema-invisible ones need help.
    data = dict(registry.decode_tagged(payload))

    identifier = data.pop("_id", None)
    if archive_kind.id_is_objectid and identifier is not None:
        identifier = ObjectId(str(identifier))

    if kind_name == "recipe":
        data.pop("trial_ids", None)
        data.pop("literature_ids", None)
        # Children are assembled from their own files (rebuild) or journal
        # bodies (replay), so they miss the decode applied to `payload` above
        # and need it here. Trials carry the deepest untyped data in the
        # catalog — `exp_condition.additional_params.xrd_metadata` is
        # instrument output nobody declares a schema for.
        data["trials"] = registry.decode_tagged((children or {}).get("trials", []))
        data["literature"] = registry.decode_tagged((children or {}).get("literature", []))

    document = document_class(**data)
    if identifier is not None:
        document.pk = identifier
    raw = document.to_mongo()
    if identifier is not None:
        raw["_id"] = identifier

    # `to_mongo()` is here for its type coercion — ISO strings back into
    # datetimes, hex back into ObjectIds — but it also materializes every
    # field's default, which would silently add keys the archive never held
    # (`derived_files: []`, `elements: {}`). Restoring must reproduce what was
    # archived, not a MongoEngine-normalized version of it, or an
    # export → rebuild → export cycle never converges.
    allowed = set(data) | {"_id"}
    if kind_name == "recipe":
        allowed |= {"trials", "literature"}
    for key in [k for k in raw if k not in allowed]:
        del raw[key]
    return raw


def _load_recipe_children(root: Path) -> dict[str, dict[str, list]]:
    """Group archived trials and literature by their recipe AUID."""
    grouped: dict[str, dict[str, list]] = {}
    for kind_name, bucket in (("trial", "trials"), ("literature", "literature")):
        for _path, payload in _iter_archive_files(kind_name):
            recipe_auid = payload.get("recipe_auid")
            if not recipe_auid:
                continue
            child = dict(payload)
            child.pop("recipe_auid", None)
            child.pop("material_auid", None)
            grouped.setdefault(recipe_auid, {"trials": [], "literature": []})[bucket].append(child)
    # Trials are numbered "1", "2", ... — sort numerically so a rebuilt recipe
    # presents them in the same order the UI showed before.
    for buckets in grouped.values():
        buckets["trials"].sort(key=lambda t: _trial_sort_key(t.get("trial_id", "")))
        buckets["literature"].sort(key=lambda item: str(item.get("lit_id", "")))
    return grouped


def _trial_sort_key(trial_id: str):
    try:
        return (0, int(trial_id))
    except (TypeError, ValueError):
        return (1, str(trial_id))


def rebuild(
    kinds: Optional[Iterable[str]] = None,
    *,
    drop: bool = True,
    regenerate_derived: bool = True,
    restore_raw_files: bool = True,
    dry_run: bool = False,
    progress=None,
) -> dict[str, int]:
    """Repopulate MongoDB from ``records/``.

    Runs entirely inside ``suspended()``: without that, every write would fire
    the archive hooks and rewrite the files currently being read.
    """
    selected = [k for k in (tuple(kinds) if kinds else REBUILD_ORDER) if k in REBUILD_ORDER]
    root = writer.archive_root()
    if root is None:
        raise RuntimeError("ARCHIVE_ROOT is not configured; nothing to rebuild from")

    results: dict[str, int] = {}
    recipe_children = _load_recipe_children(root)

    with writer.suspended():
        for kind_name in selected:
            archive_kind = registry.kind(kind_name)
            document_class = archive_kind.document_class()
            collection = document_class._get_collection()

            if drop and not dry_run:
                collection.delete_many({})

            operations = []
            for _path, payload in _iter_archive_files(kind_name):
                children = None
                if kind_name == "recipe":
                    children = recipe_children.get(payload.get("_id"))
                try:
                    raw = _coerce_document(kind_name, payload, children)
                except Exception:
                    logger.exception("archive rebuild: bad record in %s", _path)
                    continue
                operations.append(ReplaceOne({"_id": raw["_id"]}, raw, upsert=True))

            if operations and not dry_run:
                for chunk_start in range(0, len(operations), 500):
                    collection.bulk_write(operations[chunk_start:chunk_start + 500], ordered=False)
            results[kind_name] = len(operations)
            if progress:
                verb = "would restore" if dry_run else "restored"
                progress(f"  {kind_name}: {verb} {len(operations)}")

        # After the documents, put the raw uploads back on disk. Order matters:
        # this reads the freshly-restored recipes to know which trials need
        # which blob.
        if restore_raw_files and not dry_run:
            raw = restore_blobs(progress=progress)
            results["raw_files_restored"] = raw.written
            results["raw_files_present"] = raw.unchanged
            if raw.missing:
                results["raw_files_missing"] = raw.missing
            if raw.failed:
                results["raw_files_failed"] = raw.failed

        if regenerate_derived and not dry_run:
            results.update(regenerate_derived_state())
    return results


def regenerate_derived_state() -> dict[str, int]:
    """Rebuild the collections the archive deliberately does not store."""
    from catalog.documents import DOIMapping, Material, Recipe

    counts: dict[str, int] = {}

    # DOI mappings: DOI -> the materials whose recipes cite it.
    DOIMapping.objects.delete()
    mapping: dict[str, dict[str, Any]] = {}
    for recipe in Recipe.objects.no_cache().only("material_auid", "literature"):
        for item in recipe.literature or []:
            doi = (item.doi or "").strip().lower()
            if not doi:
                continue
            entry = mapping.setdefault(doi, {"material_auids": set(), "title": item.title or ""})
            entry["material_auids"].add(recipe.material_auid)
            if not entry["title"] and item.title:
                entry["title"] = item.title
    for doi, entry in mapping.items():
        DOIMapping(
            doi=doi,
            material_auids=sorted(entry["material_auids"]),
            title=entry["title"],
        ).save()
    counts["doi_mappings"] = len(mapping)

    # latest_trial_date: the $max side effect of Recipe.save(), recomputed.
    latest: dict[str, Any] = {}
    for recipe in Recipe.objects.no_cache().only("material_auid", "trials"):
        for trial in recipe.trials or []:
            if trial.trial_date is None:
                continue
            current = latest.get(recipe.material_auid)
            if current is None or trial.trial_date > current:
                latest[recipe.material_auid] = trial.trial_date
    material_collection = Material._get_collection()
    for material_auid, when in latest.items():
        material_collection.update_one(
            {"_id": material_auid}, {"$set": {"latest_trial_date": when}}
        )
    counts["latest_trial_date"] = len(latest)
    return counts


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------

def iter_journal(since: Optional[str] = None) -> Iterator[dict]:
    """Yield journal events in chronological order.

    Files are named by UTC date, so sorting paths sorts events, and lines
    within a file are already in append order.
    """
    root = writer.archive_root()
    if root is None:
        return
    journal = root / "journal"
    if not journal.is_dir():
        return
    for path in sorted(journal.glob("*/*/*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    logger.warning("archive: unparseable journal line in %s", path)
                    continue
                if since and event.get("ts", "") < since:
                    continue
                yield event


def replay(*, since: Optional[str] = None, drop: bool = True) -> dict[str, int]:
    """Rebuild MongoDB from the journal alone.

    This is the proof that the journal is self-sufficient. It reconstructs
    current state by folding every event in order — later events overwrite
    earlier ones, deletes remove — and then materializes the result exactly as
    ``rebuild`` would.
    """
    state: dict[str, dict[str, dict]] = {}
    recipe_children: dict[str, dict[str, dict]] = {}

    for event in iter_journal(since):
        kind_name = event.get("kind")
        key = event.get("key")
        body = event.get("body")
        if kind_name in (None, "blob") or key is None:
            continue

        if kind_name in ("trial", "literature"):
            if body is None:
                continue
            recipe_auid = body.get("recipe_auid")
            if not recipe_auid:
                continue
            bucket = recipe_children.setdefault(recipe_auid, {})
            if event.get("op") == "delete":
                bucket.pop(key, None)
            else:
                bucket[key] = body
            continue

        collection_state = state.setdefault(kind_name, {})
        if event.get("op") == "delete":
            collection_state.pop(key, None)
        elif body is not None:
            collection_state[key] = body

    results: dict[str, int] = {}
    with writer.suspended():
        for kind_name in REBUILD_ORDER:
            archive_kind = registry.kind(kind_name)
            document_class = archive_kind.document_class()
            collection = document_class._get_collection()
            if drop:
                collection.delete_many({})

            operations = []
            for payload in state.get(kind_name, {}).values():
                children = None
                if kind_name == "recipe":
                    grouped = recipe_children.get(payload.get("_id"), {})
                    trials, literature = [], []
                    for child in grouped.values():
                        stripped = dict(child)
                        stripped.pop("recipe_auid", None)
                        stripped.pop("material_auid", None)
                        (trials if "trial_id" in stripped else literature).append(stripped)
                    trials.sort(key=lambda t: _trial_sort_key(t.get("trial_id", "")))
                    literature.sort(key=lambda item: str(item.get("lit_id", "")))
                    children = {"trials": trials, "literature": literature}
                try:
                    raw = _coerce_document(kind_name, payload, children)
                except Exception:
                    logger.exception("archive replay: bad event body for %s/%s", kind_name, payload)
                    continue
                operations.append(ReplaceOne({"_id": raw["_id"]}, raw, upsert=True))

            if operations:
                for chunk_start in range(0, len(operations), 500):
                    collection.bulk_write(operations[chunk_start:chunk_start + 500], ordered=False)
            results[kind_name] = len(operations)

        results.update(regenerate_derived_state())
    return results


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

def status() -> dict:
    """Summarize the archive: counts per kind, journal extent, schema version."""
    root = writer.archive_root()
    if root is None:
        return {"enabled": False}

    counts = {name: sum(1 for _ in _iter_archive_files(name)) for name in ALL_KINDS}
    journal_files = sorted((root / "journal").glob("*/*/*.jsonl")) if (root / "journal").is_dir() else []
    blobs = sum(1 for _ in (root / "blobs").rglob("*")) if (root / "blobs").is_dir() else 0

    last_event = None
    if journal_files:
        try:
            lines = journal_files[-1].read_text(encoding="utf-8").strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                last_event = {
                    "ts": last.get("ts"),
                    "op": last.get("op"),
                    "kind": last.get("kind"),
                    "key": last.get("key"),
                    "actor": last.get("actor"),
                }
        except Exception:
            logger.warning("archive: could not read journal tail")

    drift_marker = root / ".state" / "drift.json"
    drift = 0
    if drift_marker.exists():
        try:
            drift = len(json.loads(drift_marker.read_text(encoding="utf-8")).get("events", []))
        except Exception:
            drift = -1

    return {
        "enabled": True,
        "root": str(root),
        "schema": writer.read_schema_version(),
        "records": counts,
        "total_records": sum(counts.values()),
        "blob_files": blobs,
        "journal_days": len(journal_files),
        "last_event": last_event,
        "drift_events": drift,
    }


__all__ = [
    "ALL_KINDS",
    "Counts",
    "REBUILD_ORDER",
    "VerifyReport",
    "export",
    "iter_journal",
    "iter_kind_payloads",
    "rebuild",
    "regenerate_derived_state",
    "replay",
    "status",
    "verify",
]
