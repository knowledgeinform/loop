"""Deterministic JSON serialization and atomic file writes.

This module is the one place LOOP decides how a Python value becomes bytes on
disk. It has no Django or MongoEngine imports on purpose: everything here must
be usable from a management command, a signal handler, or a plain unit test
without a configured app registry.

Two serializations live here, and the difference matters:

``dumps_canonical_json``
    Compact (``separators=(",", ":")``), sorted keys. **These exact bytes feed
    content hashes that are already persisted** — ``analysis_id`` and
    ``result_hash`` in :mod:`catalog.xrd_analysis.persistence` are derived from
    them. Changing this function's output invalidates every stored XRD
    analysis, so treat it as frozen.

``dumps_archive_json``
    Indented, sorted keys. Used for the on-disk archive
    (:mod:`catalog.archive`), where a human reading the file is a design goal
    and no pre-existing hash depends on the layout. Still fully deterministic,
    so archive round-trips are byte-comparable.

Both run over the same :func:`to_jsonable` normalization, so the two forms
always agree about *content* and differ only in whitespace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import tempfile
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "to_jsonable",
    "dumps_canonical_json",
    "canonical_json_bytes",
    "dumps_archive_json",
    "archive_json_bytes",
    "sha256_bytes",
    "sha256_digest",
    "sha256_file",
    "atomic_write_bytes",
    "artifact_file_mode",
    "apply_artifact_mode",
    "artifact_dir_mode",
    "apply_artifact_mode_tree",
]

logger = logging.getLogger(__name__)


def to_jsonable(value: Any) -> Any:
    """Normalize an arbitrary Python value into JSON-safe primitives.

    Dict keys are stringified and sorted, datetimes become ISO-8601 strings,
    ``Path`` becomes ``str``, and non-finite floats become ``None`` (JSON has no
    NaN/Infinity, and emitting the JavaScript-only literals would make the
    archive unreadable by conforming parsers).
    """
    if is_dataclass(value):
        result: dict[str, Any] = {}
        for item in fields(value):
            result[item.name] = to_jsonable(getattr(value, item.name))
        return result
    if isinstance(value, dict):
        return {
            str(key): to_jsonable(val)
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def dumps_canonical_json(value: Any) -> str:
    """Compact deterministic JSON. Frozen — persisted hashes depend on it."""
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"))


def canonical_json_bytes(value: Any) -> bytes:
    """UTF-8 bytes of the compact canonical form."""
    return dumps_canonical_json(value).encode("utf-8")


def dumps_archive_json(value: Any) -> str:
    """Indented deterministic JSON for the on-disk archive.

    ``ensure_ascii=False`` keeps element symbols, author names, and journal
    titles legible as UTF-8 rather than ``\\uXXXX`` escapes. A trailing newline
    makes the files behave under ``diff``, ``git``, and shell tooling.
    """
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    ) + "\n"


def archive_json_bytes(value: Any) -> bytes:
    """UTF-8 bytes of the indented archive form."""
    return dumps_archive_json(value).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    """SHA-256 hex digest of already-serialized bytes."""
    return hashlib.sha256(payload).hexdigest()


def sha256_digest(value: Any) -> str:
    """SHA-256 hex digest of a value's compact canonical JSON."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: str | Path) -> str:
    """SHA-256 hex digest of a file's contents, read in chunks."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_bytes(path: Path, payload: bytes, *, shared: bool = False) -> None:
    """Write ``payload`` to ``path`` atomically and durably.

    Writes to a temp file in the same directory, fsyncs it, then ``os.replace``
    (atomic within a filesystem). A reader therefore never observes a partial
    record: it sees either the old file or the complete new one.

    The parent directory itself is fsynced too, so the rename survives a crash.
    Without that, the file contents are durable but the directory entry naming
    them may not be.

    ``shared`` opts the file into the readable-by-everyone artifact mode. It is
    off by default on purpose. This helper writes both published XRD artifacts,
    which an unrelated account rsyncs offsite and so must be able to read, and
    the JSON archive, which contains ``records/auth/django-auth.json`` with
    password hashes on a host shared with other lab accounts. Defaulting to
    permissive would quietly publish the second to fix the first, so the choice
    belongs to the caller.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    if shared:
        apply_artifact_mode(temp_name)
    os.replace(temp_name, path)
    _fsync_dir(path.parent)


def artifact_file_mode() -> int:
    """Permission bits for files published into the shared data volume.

    ``tempfile.NamedTemporaryFile`` deliberately creates with 0600, and
    ``os.replace`` preserves the mode of the source. Every artifact written via
    the atomic-write path therefore lands unreadable to anyone but the service
    user, unlike files saved through Django storage which honour
    ``FILE_UPLOAD_PERMISSIONS``. That broke the nightly rsync of
    ``/mnt/data/common/LOOP`` to bellatrix with EACCES on every analysis file.

    Mirrors ``FILE_UPLOAD_PERMISSIONS`` so storage-written and atomically
    written files are indistinguishable on disk.
    """
    try:
        from django.conf import settings

        mode = getattr(settings, "FILE_UPLOAD_PERMISSIONS", None)
    except Exception:  # pragma: no cover - canonical.py is usable without Django
        mode = None
    return 0o644 if mode is None else mode


def apply_artifact_mode(target) -> None:
    """Best-effort chmod of a freshly written artifact to the shared mode.

    Failure is non-fatal: on a filesystem that rejects chmod, an unreadable
    artifact is still preferable to a lost one.
    """
    try:
        os.chmod(target, artifact_file_mode())
    except OSError:  # pragma: no cover - depends on filesystem
        logger.warning("Could not set permissions on artifact %s", target)


def artifact_dir_mode() -> int:
    """Permission bits for directories holding published artifacts.

    Mirrors ``FILE_UPLOAD_DIRECTORY_PERMISSIONS``. Directories need the execute
    bit to be traversable, so a readable file inside an unsearchable directory
    is still unreachable.
    """
    try:
        from django.conf import settings

        mode = getattr(settings, "FILE_UPLOAD_DIRECTORY_PERMISSIONS", None)
    except Exception:  # pragma: no cover - canonical.py is usable without Django
        mode = None
    return 0o755 if mode is None else mode


def apply_artifact_mode_tree(root) -> int:
    """Normalize permissions across an entire published artifact tree.

    Chmodding at each write site only protects the paths we know about. A tree
    sweep is the backstop: it covers artifacts written by any code path, files
    copied in with their source mode preserved (``shutil.copy2`` does that), and
    anything restored from a quarantine directory with its old bits intact.

    Returns the number of paths changed. Never raises: a permissions problem
    must not fail an analysis that otherwise succeeded.
    """
    file_mode = artifact_file_mode()
    dir_mode = artifact_dir_mode()
    changed = 0
    try:
        root = Path(root)
        if not root.exists():
            return 0
        targets = [(root, dir_mode)] if root.is_dir() else [(root, file_mode)]
        if root.is_dir():
            for path in root.rglob("*"):
                targets.append((path, dir_mode if path.is_dir() else file_mode))
        for path, mode in targets:
            try:
                if stat.S_IMODE(path.stat().st_mode) != mode:
                    os.chmod(path, mode)
                    changed += 1
            except OSError:  # pragma: no cover - individual path may vanish
                logger.warning("Could not set permissions on %s", path)
    except Exception:  # pragma: no cover - defensive, never fail the caller
        logger.warning("Permission sweep failed for %s", root, exc_info=True)
    return changed


def _fsync_dir(directory: Path) -> None:
    """Best-effort directory fsync; not all platforms permit opening a dir."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
