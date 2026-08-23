"""The single choke point for archive writes.

Everything that reaches the archive goes through this module. Callers are
mostly the MongoEngine signal handlers in :mod:`catalog.archive.hooks`, plus
the handful of code paths that write to Mongo without going through a document
(:mod:`catalog.raw_db`, the superuser JSON editor, the AFLOW bulk import).

Ordering is the whole point
---------------------------
An archive write happens **before** the corresponding Mongo write, and raises
on failure. That ordering is what makes the phrase "the archive is the source
of truth" mean something operationally: if the disk write fails, the Mongo
write never happens, and the two can never disagree in the direction that
loses data. The failure is visible to the user as a 500 rather than silently
swallowed.

``ARCHIVE_REQUIRED = False`` downgrades this to log-and-continue for
emergencies (a full disk at 2am shouldn't stop the lab from recording
experiments). That mode records the failure in ``.state/drift.json`` so
``loop_archive verify`` reports it loudly instead of letting the archive
quietly rot.

Unchanged writes are skipped
----------------------------
Before writing, the new bytes are compared against what is already on disk. If
they match, nothing is written and nothing is journaled. This makes ``export``
idempotent for free, and — more importantly — keeps the journal meaningful: a
journal entry means something actually changed, not that a page was rendered.
It matters because ``_upsert_material`` re-saves an unchanged ``Material`` on
every single trial upload.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from django.conf import settings

from catalog.canonical import (
    apply_artifact_mode,
    archive_json_bytes,
    atomic_write_bytes,
    sha256_bytes,
)
from . import ARCHIVE_SCHEMA_VERSION, registry
from .context import current_actor

logger = logging.getLogger(__name__)

# Reentrancy guard. `rebuild` and `export` read from Mongo and would otherwise
# re-archive everything they just read — harmless but slow, and during a
# rebuild actively wrong, because saving a document would rewrite the very
# file the rebuild is reading from.
_suspend_depth = 0


class ArchiveWriteError(RuntimeError):
    """An archive write failed while ``ARCHIVE_REQUIRED`` was in force."""


@dataclass(frozen=True)
class WriteResult:
    """Outcome of one record write."""

    path: str
    sha256: str
    changed: bool


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def archive_root() -> Optional[Path]:
    """Resolve ``ARCHIVE_ROOT``, or ``None`` when archiving is switched off."""
    if not getattr(settings, "ARCHIVE_ENABLED", True):
        return None
    root = getattr(settings, "ARCHIVE_ROOT", None)
    if not root:
        return None
    return Path(root)


def is_enabled() -> bool:
    return archive_root() is not None and not is_suspended()


def is_required() -> bool:
    return bool(getattr(settings, "ARCHIVE_REQUIRED", True))


def _fsync_enabled() -> bool:
    return bool(getattr(settings, "ARCHIVE_FSYNC", True))


def is_suspended() -> bool:
    return _suspend_depth > 0


@contextmanager
def suspended() -> Iterator[None]:
    """Disable archive writes for the duration of the block.

    Used by ``rebuild`` and ``replay``, which populate Mongo *from* the archive
    and must not write back into it, and by ``export``, which reads Mongo and
    writes the archive directly rather than through the hooks.
    """
    global _suspend_depth
    _suspend_depth += 1
    try:
        yield
    finally:
        _suspend_depth -= 1


@contextmanager
def bulk_mode() -> Iterator[None]:
    """Skip per-write fsync for a bulk operation.

    Safe for ``export`` and large imports: those are re-runnable, so trading
    crash-durability of individual records for throughput is a good deal. Not
    safe for request-path writes, which is why it is opt-in.
    """
    previous = getattr(settings, "ARCHIVE_FSYNC", True)
    settings.ARCHIVE_FSYNC = False
    try:
        yield
    finally:
        settings.ARCHIVE_FSYNC = previous


# --------------------------------------------------------------------------
# Low-level IO
# --------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Archive kinds holding credentials or personal data. These stay readable only
# by the service account: the archive is rsynced offsite by an unrelated account
# on a shared host, and django-auth.json contains password hashes. Everything
# else in the archive is catalog data that the backup is supposed to capture.
_PRIVATE_KINDS = frozenset({"auth", "user_affiliation", "user_precursor", "user_protocol"})


def _write_bytes(path: Path, payload: bytes, *, shared: bool = False) -> None:
    if _fsync_enabled():
        atomic_write_bytes(path, payload, shared=shared)
        return
    # Still atomic (temp + rename), just not durably flushed.
    #
    # Default is private. The archive is rsynced offsite by an unrelated account,
    # so catalog records opt into the shared mode, but records/auth/django-auth.json
    # holds password hashes and this host is shared with other lab accounts. The
    # caller decides per kind rather than one mode covering both.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(payload)
    if shared:
        apply_artifact_mode(temp)
    os.replace(temp, path)


def _record_failure(root: Path, detail: dict) -> None:
    """Note a swallowed archive failure so ``verify`` can surface it."""
    state = root / ".state"
    marker = state / "drift.json"
    try:
        existing: dict[str, Any] = {}
        if marker.exists():
            existing = json.loads(marker.read_text(encoding="utf-8"))
        events = list(existing.get("events", []))[-99:]
        events.append({"ts": _utc_now_iso(), **detail})
        _write_bytes(marker, archive_json_bytes({"events": events}))
    except Exception:
        logger.exception("archive: could not record drift marker")


def _guard(root: Path, what: str, key: str):
    """Translate an archive failure into the configured failure mode."""
    if is_required():
        raise ArchiveWriteError(f"Archive write failed for {what} {key!r}")
    logger.error("archive: %s %s failed; continuing (ARCHIVE_REQUIRED=False)", what, key)
    _record_failure(root, {"kind": what, "key": key})


# --------------------------------------------------------------------------
# Journal
# --------------------------------------------------------------------------

def _journal_path(root: Path, when: datetime) -> Path:
    return (
        root / "journal" / f"{when:%Y}" / f"{when:%m}" / f"{when:%Y-%m-%d}.jsonl"
    )


def append_journal(
    *,
    op: str,
    kind_name: str,
    key: str,
    path: str,
    sha256: Optional[str],
    body: Optional[dict],
    prev_sha256: Optional[str] = None,
) -> None:
    """Append one event to the append-only journal.

    The full body is inlined. That makes the journal self-sufficient — it can
    reconstruct any point in time on its own, which ``loop_archive replay``
    exercises — at the cost of storing each record twice. Storage was
    explicitly not a constraint here, and the redundancy is the point: losing
    ``records/`` and losing ``journal/`` are then independent failures.

    Written with ``O_APPEND`` in a single ``write`` call so concurrent gunicorn
    workers interleave whole lines rather than corrupting each other's.
    """
    root = archive_root()
    if root is None:
        return
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.isoformat().replace("+00:00", "Z"),
        "op": op,
        "kind": kind_name,
        "key": key,
        "path": path,
        "sha256": sha256,
        "prev_sha256": prev_sha256,
        "actor": current_actor().actor,
        "source": current_actor().source,
        "body": body,
    }
    line = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    target = _journal_path(root, now)
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(target, flags, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        if _fsync_enabled():
            os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------

def write_payload(kind_name: str, payload: dict) -> Optional[WriteResult]:
    """Write one record payload, skipping the write when nothing changed."""
    root = archive_root()
    if root is None or is_suspended():
        return None
    archive_kind = registry.kind(kind_name)
    try:
        relative = archive_kind.path_of(payload)
        body = archive_json_bytes(payload)
    except Exception:
        logger.exception("archive: could not serialize %s", kind_name)
        _guard(root, kind_name, str(payload.get("_id", "?")))
        return None

    target = root / "records" / relative
    prev_sha: Optional[str] = None
    try:
        if target.exists():
            existing = target.read_bytes()
            if existing == body:
                return WriteResult(path=relative, sha256=sha256_bytes(body), changed=False)
            prev_sha = sha256_bytes(existing)
        _write_bytes(target, body, shared=kind_name not in _PRIVATE_KINDS)
    except Exception:
        logger.exception("archive: could not write %s", target)
        _guard(root, kind_name, archive_kind.key_of(payload))
        return None

    digest = sha256_bytes(body)
    try:
        append_journal(
            op="upsert",
            kind_name=kind_name,
            key=archive_kind.key_of(payload),
            path=relative,
            sha256=digest,
            body=payload,
            prev_sha256=prev_sha,
        )
    except Exception:
        logger.exception("archive: could not journal %s", relative)
        _guard(root, kind_name, archive_kind.key_of(payload))
    return WriteResult(path=relative, sha256=digest, changed=True)


def delete_payload(kind_name: str, payload: dict) -> None:
    """Remove one record file and journal the deletion with its final body."""
    root = archive_root()
    if root is None or is_suspended():
        return
    archive_kind = registry.kind(kind_name)
    relative = archive_kind.path_of(payload)
    target = root / "records" / relative
    prev_sha: Optional[str] = None
    try:
        if target.exists():
            prev_sha = sha256_bytes(target.read_bytes())
            target.unlink()
            _prune_empty_dirs(target.parent, root / "records")
    except Exception:
        logger.exception("archive: could not delete %s", target)
        _guard(root, kind_name, archive_kind.key_of(payload))
        return
    append_journal(
        op="delete",
        kind_name=kind_name,
        key=archive_kind.key_of(payload),
        path=relative,
        sha256=None,
        body=payload,
        prev_sha256=prev_sha,
    )


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Walk up removing now-empty directories, never past ``stop``."""
    current = start
    while current != stop and stop in current.parents:
        try:
            next(current.iterdir())
            return  # not empty
        except StopIteration:
            pass
        except OSError:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


# --------------------------------------------------------------------------
# Recipes: the parent/child reconcile
# --------------------------------------------------------------------------

def write_recipe(recipe) -> None:
    """Archive a Recipe and its embedded trials and literature.

    Trials and literature live in their own files rather than inline, matching
    the ``api_download`` zip layout and keeping any one file small.

    The subtle part is deletion. ``views.delete_trial`` removes a trial by
    filtering ``recipe.trials`` and re-saving the *recipe* — no ``pre_delete``
    fires, because nothing was deleted at the document level. So this function
    reconciles: anything on disk that is absent from the in-memory lists is a
    deletion, and is removed and journaled as one.
    """
    root = archive_root()
    if root is None or is_suspended():
        return

    trials = list(recipe.trials or [])
    literature = list(recipe.literature or [])

    payload = registry.document_payload(recipe, drop=registry.RECIPE.drop_fields)
    payload["trial_ids"] = [t.trial_id for t in trials]
    payload["literature_ids"] = [item.lit_id for item in literature]
    write_payload("recipe", payload)

    recipe_auid = str(recipe.id)
    material_auid = str(recipe.material_auid or "")

    for trial in trials:
        child = registry.embedded_payload(trial)
        # Embedded documents carry no back-reference; add one so each file is
        # independently meaningful and `rebuild` can group without the path.
        child["recipe_auid"] = recipe_auid
        child["material_auid"] = material_auid
        write_payload("trial", child)

    for item in literature:
        child = registry.embedded_payload(item)
        child["recipe_auid"] = recipe_auid
        child["material_auid"] = material_auid
        write_payload("literature", child)

    _reconcile_recipe_children(root, recipe_auid, trials, literature)


def _reconcile_recipe_children(root: Path, recipe_auid: str, trials, literature) -> None:
    """Delete archived trials/literature that are no longer on the recipe."""
    base = root / "records" / registry.recipe_dir(recipe_auid)

    live_trials = {registry.sanitize(t.trial_id) for t in trials}
    trials_dir = base / "trials"
    if trials_dir.is_dir():
        for entry in sorted(trials_dir.iterdir()):
            if not entry.is_dir() or entry.name in live_trials:
                continue
            _delete_orphan(root, entry / "trial.json", "trial")
            shutil.rmtree(entry, ignore_errors=True)

    live_lit = {f"{registry.sanitize(item.lit_id)}.json" for item in literature}
    lit_dir = base / "literature"
    if lit_dir.is_dir():
        for entry in sorted(lit_dir.iterdir()):
            if entry.name in live_lit or entry.suffix != ".json":
                continue
            _delete_orphan(root, entry, "literature")
            entry.unlink(missing_ok=True)


def _delete_orphan(root: Path, path: Path, kind_name: str) -> None:
    """Journal the removal of a child record, reading its final body first."""
    if not path.exists():
        return
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        prev_sha = sha256_bytes(path.read_bytes())
    except Exception:
        body, prev_sha = None, None
    relative = str(path.relative_to(root / "records"))
    key = relative
    if body is not None:
        try:
            key = registry.kind(kind_name).key_of(body)
        except Exception:
            pass
    append_journal(
        op="delete",
        kind_name=kind_name,
        key=key,
        path=relative,
        sha256=None,
        body=body,
        prev_sha256=prev_sha,
    )


def delete_recipe(recipe) -> None:
    """Remove a whole recipe subtree, journaling the recipe and every child."""
    root = archive_root()
    if root is None or is_suspended():
        return
    recipe_auid = str(recipe.id)
    base = root / "records" / registry.recipe_dir(recipe_auid)

    _reconcile_recipe_children(root, recipe_auid, [], [])

    payload = registry.document_payload(recipe, drop=registry.RECIPE.drop_fields)
    payload["trial_ids"] = []
    payload["literature_ids"] = []
    delete_payload("recipe", payload)

    shutil.rmtree(base, ignore_errors=True)
    _prune_empty_dirs(base.parent, root / "records")


# --------------------------------------------------------------------------
# Blobs
# --------------------------------------------------------------------------

def write_blob(sha256: str, source_path: str | Path, *, ext: str = "") -> Optional[str]:
    """Copy an uploaded file into the content-addressed blob store.

    Tries a hard link first: the bytes already exist under ``MEDIA_ROOT``, and
    on the same filesystem a link costs an inode instead of a second copy.
    Falls back to a real copy across filesystems (the common Docker case, where
    ``MEDIA_ROOT`` and ``ARCHIVE_ROOT`` may be different mounts).

    Content addressing means a re-upload of the same file is a no-op, and a
    trial's ``file_hash`` is already the pointer — no extra bookkeeping.
    """
    root = archive_root()
    if root is None or is_suspended() or not sha256:
        return None
    relative = registry.blob_path(sha256, ext)
    target = root / relative
    if target.exists():
        return relative
    source = Path(source_path)
    if not source.is_file():
        logger.warning("archive: blob source missing for %s at %s", sha256, source)
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
    except Exception:
        logger.exception("archive: could not store blob %s", sha256)
        _guard(root, "blob", sha256)
        return None
    append_journal(
        op="blob",
        kind_name="blob",
        key=sha256,
        path=relative,
        sha256=sha256,
        body=None,
    )
    return relative


# --------------------------------------------------------------------------
# Archive metadata
# --------------------------------------------------------------------------

def write_schema_version() -> None:
    root = archive_root()
    if root is None:
        return
    from catalog.auid import AUID_VERSION

    _write_bytes(
        root / "schema_version.json",
        archive_json_bytes({
            "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
            "auid_version": AUID_VERSION,
            "kinds": sorted(registry.KINDS),
        }),
        shared=True,
    )


def read_schema_version() -> Optional[dict]:
    root = archive_root()
    if root is None:
        return None
    path = root / "schema_version.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


__all__ = [
    "ArchiveWriteError",
    "WriteResult",
    "append_journal",
    "archive_root",
    "bulk_mode",
    "delete_payload",
    "delete_recipe",
    "is_enabled",
    "is_required",
    "is_suspended",
    "read_schema_version",
    "suspended",
    "write_blob",
    "write_payload",
    "write_recipe",
    "write_schema_version",
]
