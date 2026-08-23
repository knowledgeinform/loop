"""
Flat "Raw" MongoDB database for file-level backup.

This is the portable, intentionally dumb half of the LOOP data model. One
collection (``raw_files``) in a separate database (default: ``loop_raw``)
holds a row for every raw file that has been ingested (XRD, SEM, etc.) plus
enough metadata to reconstruct provenance if the primary ``loop`` database is
ever lost or moved.

Every row is keyed by ``file_hash`` (a SHA-256 of the file contents), so
repeated uploads of the same file idempotently upsert without duplication.

The collection lives under the ``"raw"`` MongoEngine alias. That alias is
registered in :mod:`loop.settings` alongside the default connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from mongoengine import (
    DateTimeField,
    DictField,
    Document,
    IntField,
    ListField,
    StringField,
)


RAW_DB_ALIAS = "raw"


def _utc_now() -> datetime:
    """Aware UTC timestamp; mirrors :func:`catalog.documents._utc_now`."""
    return datetime.now(timezone.utc)


class RawFile(Document):
    """One document per ingested raw file.

    The primary ``loop`` database may reference this document by ``file_hash``
    (see :class:`catalog.documents.EmbeddedTrial.file_hash`), but no foreign
    key integrity is enforced at the database layer — the raw DB is a
    portable, self-contained manifest.
    """

    id = StringField(primary_key=True)  # file_hash (sha256 hex)

    original_filename = StringField()
    stored_path = StringField()
    content_type = StringField()
    size_bytes = IntField()

    uploaded_at = DateTimeField(default=_utc_now)
    uploaded_by = StringField()

    # Provenance backlinks into the main catalog. Kept as plain strings so
    # the raw DB stays independently useful without a schema join.
    material_auid = StringField()
    recipe_auid = StringField()
    trial_id = StringField()
    doi = StringField()

    elements = DictField()
    structure_family = StringField()
    notes = StringField()
    tags = ListField(StringField())

    # Processed artifacts derived from this raw file (pattern/overlay/peaks).
    derived_files = ListField(DictField())
    # RAW_UPLOADS_ROOT-relative archive folder for this upload, so lazily-built
    # derived artifacts can be back-filled into a self-contained snapshot.
    archive_folder = StringField()

    meta = {
        "db_alias": RAW_DB_ALIAS,
        "collection": "raw_files",
        "indexes": [
            "material_auid",
            "recipe_auid",
            "trial_id",
            "doi",
            "uploaded_by",
            "-uploaded_at",
        ],
        "ordering": ["-uploaded_at"],
        "strict": False,
    }

    @property
    def file_hash(self) -> str:
        return self.id


def record_raw_file(
    *,
    file_hash: str,
    material_auid: Optional[str] = None,
    recipe_auid: Optional[str] = None,
    trial_id: Optional[str] = None,
    doi: Optional[str] = None,
    original_filename: Optional[str] = None,
    stored_path: Optional[str] = None,
    content_type: Optional[str] = None,
    size_bytes: Optional[int] = None,
    uploaded_by: Optional[str] = None,
    elements: Optional[Dict[str, Any]] = None,
    structure_family: Optional[str] = None,
    notes: Optional[str] = None,
    tags: Optional[list] = None,
    archive_folder: Optional[str] = None,
) -> None:
    """Idempotently upsert a ``raw_files`` row keyed by ``file_hash``.

    Fields left as ``None`` are not written (so the call is additive when a
    later upload of the same hash brings richer metadata).
    """
    if not file_hash:
        raise ValueError("file_hash is required to record a raw file")

    set_fields: Dict[str, Any] = {}
    if material_auid is not None:
        set_fields["set__material_auid"] = material_auid
    if recipe_auid is not None:
        set_fields["set__recipe_auid"] = recipe_auid
    if trial_id is not None:
        set_fields["set__trial_id"] = trial_id
    if doi is not None:
        set_fields["set__doi"] = doi
    if original_filename is not None:
        set_fields["set__original_filename"] = original_filename
    if stored_path is not None:
        set_fields["set__stored_path"] = stored_path
    if content_type is not None:
        set_fields["set__content_type"] = content_type
    if size_bytes is not None:
        set_fields["set__size_bytes"] = size_bytes
    if uploaded_by is not None:
        set_fields["set__uploaded_by"] = uploaded_by
    if elements is not None:
        set_fields["set__elements"] = elements
    if structure_family is not None:
        set_fields["set__structure_family"] = structure_family
    if notes is not None:
        set_fields["set__notes"] = notes
    if tags is not None:
        set_fields["set__tags"] = list(tags)
    if archive_folder is not None:
        set_fields["set__archive_folder"] = archive_folder

    RawFile.objects(id=file_hash).update_one(
        set_on_insert__uploaded_at=_utc_now(),
        upsert=True,
        **set_fields,
    )
    _archive_row(file_hash)


def _archive_row(file_hash: str) -> None:
    """Mirror a ``raw_files`` row into the JSON archive.

    ``QuerySet.update_one`` fires no MongoEngine signals, so the archive hooks
    in :mod:`catalog.archive.hooks` never see these writes. Both functions in
    this module use an upsert (deliberately — it makes repeat uploads of the
    same file idempotent), so they archive explicitly instead.

    Read-after-write rather than reconstructing the row from the arguments:
    these are partial updates that merge with whatever a previous upload
    recorded, so only the stored document knows the full state.
    """
    from catalog.archive import registry, writer

    if not writer.is_enabled():
        return
    row = RawFile.objects(id=file_hash).first()
    if row is None:
        return
    writer.write_payload("raw_file", registry.document_payload(row))


def record_derived_file(
    *,
    file_hash: str,
    kind: str,
    variant: Optional[str],
    stored_path: str,
    url: str,
    size_bytes: int,
    sha256: str,
    generated_at: str,
    content_type: Optional[str] = None,
    artifact_type: Optional[str] = None,
    parent_file_hash: Optional[str] = None,
    analysis_id: Optional[str] = None,
    algorithm_version: Optional[str] = None,
    configuration_version: Optional[str] = None,
    content_hash: Optional[str] = None,
    relative_path: Optional[str] = None,
) -> None:
    """Idempotently record one processed artifact under a raw file's manifest row.

    Deduped by ``(kind, variant, analysis_id)`` so rebuilding one cached artifact
    or one persisted analysis replaces only that logical entry.
    ``kind`` is one of ``"pattern"``, ``"overlay"``, ``"peaks"``; ``variant`` is
    ``None`` for the variant-independent pattern.
    """
    if not file_hash:
        raise ValueError("file_hash is required to record a derived file")

    entry = {
        "kind": kind,
        "variant": variant,
        "stored_path": stored_path,
        "url": url,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "generated_at": generated_at,
    }
    if content_type is not None:
        entry["content_type"] = content_type
    if artifact_type is not None:
        entry["artifact_type"] = artifact_type
    if parent_file_hash is not None:
        entry["parent_file_hash"] = parent_file_hash
    if analysis_id is not None:
        entry["analysis_id"] = analysis_id
    if algorithm_version is not None:
        entry["algorithm_version"] = algorithm_version
    if configuration_version is not None:
        entry["configuration_version"] = configuration_version
    if content_hash is not None:
        entry["content_hash"] = content_hash
    if relative_path is not None:
        entry["relative_path"] = relative_path
    row = RawFile.objects(id=file_hash).first()
    if row is None:
        RawFile.objects(id=file_hash).update_one(
            set_on_insert__uploaded_at=_utc_now(), upsert=True
        )
        row = RawFile.objects(id=file_hash).first()
    if row is None:
        raise RuntimeError(f"Failed to read back RawFile {file_hash} after upsert")

    kept = [
        d for d in (row.derived_files or [])
        if not (
            d.get("kind") == kind
            and d.get("variant") == variant
            and d.get("analysis_id") == analysis_id
        )
    ]
    kept.append(entry)
    row.derived_files = kept
    row.save()


__all__ = ["RAW_DB_ALIAS", "RawFile", "record_raw_file", "record_derived_file"]
