# Unified XRD Store + Processed-Plot Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist each trial's processed XRD artifacts (normalized pattern, overlay plot, detected peaks) in a single per-trial store, indexed by a unified `loop_raw` manifest, so detail pages load instantly and the artifacts are retrievable via a JSON API.

**Architecture:** A new `catalog/xrd_store.py` owns a per-trial folder under `MEDIA_ROOT/xrd/<auid>/<trial_id>/` holding the raw upload plus lazily-built derived artifacts (keyed/invalidated by `file_hash`). The `loop_raw`/`RawFile` manifest is extended to index raw + derived files under one hash, and the timestamped upload archive is back-filled with derived files so each archive folder is a self-contained snapshot.

**Tech Stack:** Django 5.2, MongoEngine (`loop_raw` DB), pandas/matplotlib, scipy/GSAS-II peak finding. No new dependencies.

## Global Constraints

- Tests run inside the dev container: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test <path> -v1`. The web container must be up (`docker compose -f docker-compose.dev.yml up -d web`).
- MongoEngine only for materials/raw data — never the Django ORM. Write via `.save()`/`update_one`.
- Side-effects on the archive and `loop_raw` must be **non-fatal**: wrap in try/except, log a warning, never break the page or upload (mirrors `upload_archive.archive_upload`'s existing contract).
- Reuse existing helpers: `catalog.utils.parse_xrd_file`, `catalog.utils.render_xrd_plot`, `catalog.gsas_tools.peak_finder_fast`, `catalog.gsas_tools.peak_finder`.
- Cache key is the trial's `file_hash` (sha256 hex); when absent, hash the resolved raw file.
- Pattern files use the LOOP CSV header exactly: `Angle,Intensity`.
- Spec: `docs/superpowers/specs/2026-06-25-unified-xrd-store-and-plot-cache-design.md`.

---

## File Structure

- **Create** `catalog/xrd_store.py` — per-trial storage layout, `store_raw_file`, `resolve_raw_path`, `get_or_build`, `read_manifest`. Single owner of the unified folder.
- **Modify** `catalog/raw_db.py` — add `derived_files` + `archive_folder` to `RawFile`; add `record_derived_file`; add `archive_folder` param to `record_raw_file`.
- **Modify** `catalog/upload_archive.py` — `archive_upload` returns the folder name; add `add_files`.
- **Modify** `catalog/views.py` — persist writes raw via `xrd_store` + records `archive_folder`; `trial_detail` uses `xrd_store.get_or_build`; new `trial_xrd_cache_api`.
- **Modify** `catalog/urls.py` — add the API route.
- **Create** tests: `catalog/tests/test_raw_db_derived.py`, `catalog/tests/test_upload_archive.py`, `catalog/tests/test_xrd_store.py`, `catalog/tests/test_xrd_cache_api.py`.

---

## Task 1: `RawFile` derived-file manifest

**Files:**
- Modify: `catalog/raw_db.py`
- Test: `catalog/tests/test_raw_db_derived.py`

**Interfaces:**
- Consumes: existing `RawFile`, `record_raw_file`, `_utc_now`, `RAW_DB_ALIAS`.
- Produces:
  - `RawFile.derived_files: ListField(DictField())`, `RawFile.archive_folder: StringField()`
  - `record_derived_file(*, file_hash, kind, variant, stored_path, url, size_bytes, sha256, generated_at) -> None` (idempotent, deduped by `(kind, variant)`)
  - `record_raw_file(..., archive_folder=None)` new optional kwarg

- [ ] **Step 1: Write the failing test**

Create `catalog/tests/test_raw_db_derived.py`:

```python
import os
import unittest

from django.test import SimpleTestCase

from catalog.raw_db import RawFile, record_derived_file, record_raw_file


def _mongo_reachable() -> bool:
    if os.environ.get("SKIP_MONGO_TESTS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from mongoengine.connection import get_db
        get_db().command("ping")
        return True
    except Exception:
        return False


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class RecordDerivedFileTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        self.h = "deadbeef" * 8
        RawFile.objects(id=self.h).delete()

    def tearDown(self):
        RawFile.objects(id=self.h).delete()

    def test_records_derived_and_dedups_by_kind_variant(self):
        record_raw_file(file_hash=self.h, archive_folder="trial-2026_06_25-00:00:00.000-bob")
        record_derived_file(
            file_hash=self.h, kind="overlay", variant="fast",
            stored_path="xrd/M:x/1/fast.png", url="/media/xrd/M:x/1/fast.png",
            size_bytes=10, sha256="a", generated_at="2026-06-25T00:00:00Z",
        )
        # Re-record same (kind, variant) -> replaces, not appends.
        record_derived_file(
            file_hash=self.h, kind="overlay", variant="fast",
            stored_path="xrd/M:x/1/fast.png", url="/media/xrd/M:x/1/fast.png",
            size_bytes=20, sha256="b", generated_at="2026-06-25T01:00:00Z",
        )
        record_derived_file(
            file_hash=self.h, kind="pattern", variant=None,
            stored_path="xrd/M:x/1/pattern.csv", url="/media/xrd/M:x/1/pattern.csv",
            size_bytes=5, sha256="c", generated_at="2026-06-25T00:00:00Z",
        )
        row = RawFile.objects(id=self.h).first()
        self.assertEqual(row.archive_folder, "trial-2026_06_25-00:00:00.000-bob")
        self.assertEqual(len(row.derived_files), 2)  # one overlay (deduped) + one pattern
        overlay = [d for d in row.derived_files if d["kind"] == "overlay"][0]
        self.assertEqual(overlay["size_bytes"], 20)
        self.assertEqual(overlay["sha256"], "b")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_raw_db_derived -v1`
Expected: FAIL — `ImportError: cannot import name 'record_derived_file'`.

- [ ] **Step 3: Add fields and `record_derived_file`**

In `catalog/raw_db.py`, add two fields to `RawFile` (after `tags = ListField(StringField())`, before `meta = {`):

```python
    # Processed artifacts derived from this raw file (pattern/overlay/peaks).
    derived_files = ListField(DictField())
    # RAW_UPLOADS_ROOT-relative archive folder for this upload, so lazily-built
    # derived artifacts can be back-filled into a self-contained snapshot.
    archive_folder = StringField()
```

Add an `archive_folder` write inside `record_raw_file`, immediately after the `tags` block:

```python
    if tags is not None:
        set_fields["set__tags"] = list(tags)
    if archive_folder is not None:
        set_fields["set__archive_folder"] = archive_folder
```

Add `archive_folder` to the `record_raw_file` signature (after `tags: Optional[list] = None,`):

```python
    tags: Optional[list] = None,
    archive_folder: Optional[str] = None,
```

Append `record_derived_file` at the end of the module (before `__all__`):

```python
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
) -> None:
    """Idempotently record one processed artifact under a raw file's manifest row.

    Deduped by ``(kind, variant)`` so rebuilding a variant replaces its entry.
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
    row = RawFile.objects(id=file_hash).first()
    if row is None:
        RawFile.objects(id=file_hash).update_one(
            set_on_insert__uploaded_at=_utc_now(), upsert=True
        )
        row = RawFile.objects(id=file_hash).first()

    kept = [
        d for d in (row.derived_files or [])
        if not (d.get("kind") == kind and d.get("variant") == variant)
    ]
    kept.append(entry)
    row.derived_files = kept
    row.save()
```

Update `__all__`:

```python
__all__ = ["RAW_DB_ALIAS", "RawFile", "record_raw_file", "record_derived_file"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_raw_db_derived -v1`
Expected: PASS (1 test).

- [ ] **Step 5: Commit**

```bash
git add catalog/raw_db.py catalog/tests/test_raw_db_derived.py
git commit -m "feat(raw_db): index derived XRD artifacts in RawFile manifest"
```

---

## Task 2: Archive returns folder + `add_files`

**Files:**
- Modify: `catalog/upload_archive.py`
- Test: `catalog/tests/test_upload_archive.py`

**Interfaces:**
- Consumes: existing `archive_upload`, `_archive_root`, `_folder_name`.
- Produces:
  - `archive_upload(...) -> Optional[str]` (now returns the created folder name, or `None`)
  - `add_files(archive_folder: str, file_paths: list[str]) -> None` (copies files into `RAW_UPLOADS_ROOT/<archive_folder>`)

- [ ] **Step 1: Write the failing test**

Create `catalog/tests/test_upload_archive.py`:

```python
import tempfile
from datetime import datetime
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from catalog.upload_archive import add_files, archive_upload


class ArchiveUploadTests(SimpleTestCase):
    def test_returns_folder_and_add_files_backfills(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(RAW_UPLOADS_ROOT=root):
                folder = archive_upload(
                    upload_type="trial",
                    username="bob",
                    timestamp=datetime(2026, 6, 25, 9, 30, 0, 123000),
                    metadata={"trial_id": "1"},
                )
                self.assertIsInstance(folder, str)
                self.assertTrue((Path(root) / folder / "metadata.jsonl").is_file())

                src = Path(root) / "overlay.png"
                src.write_bytes(b"PNGDATA")
                add_files(folder, [str(src)])
                self.assertTrue((Path(root) / folder / "overlay.png").is_file())

    def test_archive_disabled_returns_none(self):
        with override_settings(RAW_UPLOADS_ROOT=""):
            self.assertIsNone(
                archive_upload(
                    upload_type="trial", username="bob",
                    timestamp=datetime(2026, 6, 25, 9, 30, 0), metadata={},
                )
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_upload_archive -v1`
Expected: FAIL — `ImportError: cannot import name 'add_files'`.

- [ ] **Step 3: Make `archive_upload` return the folder + add `add_files`**

In `catalog/upload_archive.py`, change the body of `archive_upload` so the success path returns the folder name. Replace the `try:`/`except` block (currently ending at the bare `logger.warning(...)`) with:

```python
    try:
        folder_name = _folder_name(upload_type, username, timestamp)
        folder = root / folder_name
        folder.mkdir(parents=True, exist_ok=True)

        if media_src_path:
            src = Path(media_src_path)
            if src.is_file():
                dest_name = media_dest_filename or src.name
                shutil.copy2(src, folder / dest_name)
            else:
                logger.warning("upload_archive: media file not found at %s", media_src_path)

        meta_line = json.dumps(metadata, default=str) + "\n"
        (folder / "metadata.jsonl").write_text(meta_line, encoding="utf-8")
        return folder_name

    except Exception:
        logger.warning("upload_archive: failed to write archive for %s/%s", upload_type, username, exc_info=True)
        return None
```

Update the `archive_upload` return type annotation from `-> None:` to `-> Optional[str]:`.

Append `add_files` at the end of the module:

```python
def add_files(archive_folder: str, file_paths: list[str]) -> None:
    """Copy already-written files into an existing archive folder.

    Used to back-fill lazily-built derived artifacts into the upload's
    timestamped snapshot. Non-fatal: failures are logged, never raised.
    """
    root = _archive_root()
    if root is None or not archive_folder:
        return
    folder = root / archive_folder
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for path in file_paths:
            src = Path(path)
            if src.is_file():
                shutil.copy2(src, folder / src.name)
    except Exception:
        logger.warning("upload_archive: failed to add files to %s", archive_folder, exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_upload_archive -v1`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add catalog/upload_archive.py catalog/tests/test_upload_archive.py
git commit -m "feat(archive): return folder name and support back-filling derived files"
```

---

## Task 3: `xrd_store` — layout, `store_raw_file`, `resolve_raw_path`

**Files:**
- Create: `catalog/xrd_store.py`
- Test: `catalog/tests/test_xrd_store.py`

**Interfaces:**
- Consumes: `django.conf.settings` (`MEDIA_ROOT`, `MEDIA_URL`).
- Produces:
  - `UNIFIED_SUBDIR = "xrd"`, `LEGACY_SUBDIR = "xrd_data"`
  - `trial_dir(material_auid, trial_id) -> pathlib.Path`
  - `StoredRaw` dataclass: `raw_path: str, ext: str, sha256: str, media_url: str`
  - `store_raw_file(material_auid, trial_id, uploaded_file) -> StoredRaw`
  - `resolve_raw_path(material_auid, trial_id) -> str | None`

- [ ] **Step 1: Write the failing test**

Create `catalog/tests/test_xrd_store.py`:

```python
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from catalog import xrd_store


class StoreRawAndResolveTests(SimpleTestCase):
    def test_store_writes_raw_with_extension_and_hash(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                upload = SimpleUploadedFile("scan.txt", b"9.0 100\n9.1 110\n")
                stored = xrd_store.store_raw_file("M:abc", "1", upload)
                self.assertTrue(stored.raw_path.endswith("xrd/M:abc/1/raw.txt"))
                self.assertEqual(stored.ext, ".txt")
                self.assertEqual(stored.media_url, "/media/xrd/M:abc/1/raw.txt")
                self.assertEqual(len(stored.sha256), 64)
                self.assertEqual(xrd_store.resolve_raw_path("M:abc", "1"), stored.raw_path)

    def test_resolve_falls_back_to_legacy_path(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                import os
                legacy_dir = os.path.join(media, "xrd_data", "M:legacy")
                os.makedirs(legacy_dir)
                legacy = os.path.join(legacy_dir, "7.csv")
                with open(legacy, "w") as fh:
                    fh.write("Angle,Intensity\n10,100\n")
                self.assertEqual(xrd_store.resolve_raw_path("M:legacy", "7"), legacy)

    def test_resolve_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                self.assertIsNone(xrd_store.resolve_raw_path("M:none", "1"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_store -v1`
Expected: FAIL — `ModuleNotFoundError: No module named 'catalog.xrd_store'`.

- [ ] **Step 3: Create `catalog/xrd_store.py`**

```python
"""
Unified per-trial XRD file store.

Owns one folder per trial under ``MEDIA_ROOT/xrd/<material_auid>/<trial_id>/``
holding the original upload (``raw.<ext>``) plus lazily-built derived artifacts
(``pattern.csv``, ``<variant>.png``, ``<variant>.peaks.json``, ``index.json``).
Reads fall back to the legacy ``xrd_data/<auid>/<trial_id>.<ext>`` layout so
trials uploaded before this store keep working.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from django.conf import settings

UNIFIED_SUBDIR = "xrd"
LEGACY_SUBDIR = "xrd_data"
_LEGACY_EXTENSIONS = (".csv", ".txt", ".asc", ".xy", ".raw")


def trial_dir(material_auid: str, trial_id: str) -> Path:
    """Resolve (and create) the per-trial folder under MEDIA_ROOT."""
    path = Path(settings.MEDIA_ROOT) / UNIFIED_SUBDIR / material_auid / trial_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _media_url(rel_path: str) -> str:
    base = settings.MEDIA_URL
    if not base.endswith("/"):
        base += "/"
    return base + rel_path.lstrip("/")


@dataclass
class StoredRaw:
    raw_path: str
    ext: str
    sha256: str
    media_url: str


def store_raw_file(material_auid: str, trial_id: str, uploaded_file) -> StoredRaw:
    """Write the original upload as ``raw.<ext>`` in the per-trial folder."""
    ext = os.path.splitext(getattr(uploaded_file, "name", "") or "")[1].lower() or ".csv"
    folder = trial_dir(material_auid, trial_id)
    raw_path = folder / f"raw{ext}"
    hasher = hashlib.sha256()
    with open(raw_path, "wb+") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)
            hasher.update(chunk)
    try:
        uploaded_file.seek(0)
    except Exception:
        pass
    rel = f"{UNIFIED_SUBDIR}/{material_auid}/{trial_id}/raw{ext}"
    return StoredRaw(str(raw_path), ext, hasher.hexdigest(), _media_url(rel))


def resolve_raw_path(material_auid: str, trial_id: str) -> Optional[str]:
    """Return the raw file path, preferring the unified folder, else legacy."""
    unified = Path(settings.MEDIA_ROOT) / UNIFIED_SUBDIR / material_auid / trial_id
    if unified.is_dir():
        for candidate in sorted(unified.glob("raw.*")):
            return str(candidate)
    legacy_dir = Path(settings.MEDIA_ROOT) / LEGACY_SUBDIR / material_auid
    for ext in _LEGACY_EXTENSIONS:
        candidate = legacy_dir / f"{trial_id}{ext}"
        if candidate.is_file():
            return str(candidate)
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_store -v1`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add catalog/xrd_store.py catalog/tests/test_xrd_store.py
git commit -m "feat(xrd_store): per-trial raw storage with legacy read fallback"
```

---

## Task 4: `xrd_store.get_or_build` — lazy artifact build + invalidation

**Files:**
- Modify: `catalog/xrd_store.py`
- Test: `catalog/tests/test_xrd_store.py`

**Interfaces:**
- Consumes: `parse_xrd_file`, `render_xrd_plot` (`catalog.utils`); `peak_finder_fast`, `peak_finder` (`catalog.gsas_tools`); `store_raw_file`, `resolve_raw_path`, `trial_dir` (Task 3).
- Produces:
  - `CacheEntry` dataclass: `peaks: list, overlay_png_bytes: bytes, overlay_url: str, pattern_url: str, from_cache: bool, plot_style: Optional[str], variant: str`
  - `get_or_build(material_auid, trial_id, file_hash, variant="fast", *, source_path=None) -> CacheEntry`

- [ ] **Step 1: Write the failing test**

Append to `catalog/tests/test_xrd_store.py`:

```python
import json
import os
from unittest.mock import patch


LOOP_CSV = "Angle,Intensity\n20.0,100\n20.1,150\n20.2,90\n20.3,400\n20.4,95\n"
PDF_CARD = (
    "2-Theta    d(?)   I(f)  ( h k l)\n"
    " 23.143  3.8400   85.0  ( 0 0 2)\n"
    " 23.643  3.7600  100.0  ( 0 2 0)\n"
)


class GetOrBuildTests(SimpleTestCase):
    def _media(self):
        return override_settings(MEDIA_ROOT=self._dir.name, MEDIA_URL="/media/")

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _seed_raw(self, content, name="scan.csv", auid="M:abc", trial="1"):
        with self._media():
            upload = SimpleUploadedFile(name, content.encode())
            return xrd_store.store_raw_file(auid, trial, upload)

    def test_cold_build_writes_all_artifacts(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertFalse(entry.from_cache)
            self.assertTrue(entry.peaks)
            self.assertTrue(entry.overlay_png_bytes.startswith(b"\x89PNG"))
            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            self.assertTrue(os.path.isfile(os.path.join(folder, "pattern.csv")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "fast.png")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "fast.peaks.json")))
            index = json.load(open(os.path.join(folder, "index.json")))
            self.assertEqual(index["file_hash"], stored.sha256)
            self.assertIn("fast", index["variants"])

    def test_warm_hit_skips_render(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            with patch("catalog.xrd_store.peak_finder_fast") as spy:
                entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            spy.assert_not_called()
            self.assertTrue(entry.from_cache)
            self.assertTrue(entry.peaks)

    def test_stale_hash_rebuilds(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            entry = xrd_store.get_or_build("M:abc", "1", "different-hash", variant="fast")
            self.assertFalse(entry.from_cache)

    def test_reflection_card_builds_stick_variant_without_peaks(self):
        stored = self._seed_raw(PDF_CARD, name="card.txt")
        with self._media():
            entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertEqual(entry.variant, "stick")
            self.assertEqual(entry.plot_style, "stick")
            self.assertEqual(entry.peaks, [])
            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            self.assertTrue(os.path.isfile(os.path.join(folder, "stick.png")))
            # Repeat view serves the stick cache rather than rebuilding as 'fast'.
            entry2 = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertTrue(entry2.from_cache)
            self.assertEqual(entry2.variant, "stick")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_store.GetOrBuildTests -v1`
Expected: FAIL — `AttributeError: module 'catalog.xrd_store' has no attribute 'get_or_build'`.

- [ ] **Step 3: Implement `get_or_build`**

Add imports at the top of `catalog/xrd_store.py` (after the existing imports):

```python
import base64
import json
from datetime import datetime, timezone

from catalog.utils import parse_xrd_file, render_xrd_plot
from catalog.gsas_tools import peak_finder, peak_finder_fast
```

Add the dataclass next to `StoredRaw`:

```python
@dataclass
class CacheEntry:
    peaks: list
    overlay_png_bytes: bytes
    overlay_url: str
    pattern_url: str
    from_cache: bool
    plot_style: Optional[str]
    variant: str
```

Add these helpers and `get_or_build` to the module:

```python
def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _hash_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _rel_under_media(path: Path) -> str:
    return str(path.relative_to(Path(settings.MEDIA_ROOT))).replace(os.sep, "/")


def get_or_build(
    material_auid: str,
    trial_id: str,
    file_hash: Optional[str],
    variant: str = "fast",
    *,
    source_path: Optional[str] = None,
) -> CacheEntry:
    """Return cached processed artifacts for a trial, building them on a miss.

    ``variant`` is ``"fast"`` (scipy) or ``"gsas"`` (full refinement). Reflection
    cards are always stored/served as the ``"stick"`` variant regardless of the
    requested one. Invalidation keys on ``file_hash`` (or a hash of the raw file
    when ``file_hash`` is falsy).
    """
    folder = trial_dir(material_auid, trial_id)
    index_path = folder / "index.json"
    index = _read_json(index_path) or {}

    src = source_path or resolve_raw_path(material_auid, trial_id)
    if not src:
        raise FileNotFoundError(f"No raw XRD file for {material_auid}/{trial_id}")

    key = file_hash or _hash_file(src)

    # Reflection cards are cached as 'stick'; serve that on repeat views even
    # though the caller requests the default 'fast' variant.
    effective_variant = "stick" if index.get("plot_style") == "stick" else variant
    overlay_path = folder / f"{effective_variant}.png"
    peaks_path = folder / f"{effective_variant}.peaks.json"
    pattern_path = folder / "pattern.csv"

    cache_valid = (
        index.get("file_hash") == key
        and effective_variant in (index.get("variants") or {})
        and overlay_path.is_file()
        and peaks_path.is_file()
    )
    if cache_valid:
        return CacheEntry(
            peaks=_read_json(peaks_path) or [],
            overlay_png_bytes=overlay_path.read_bytes(),
            overlay_url=_media_url(_rel_under_media(overlay_path)),
            pattern_url=_media_url(_rel_under_media(pattern_path)),
            from_cache=True,
            plot_style=index.get("plot_style"),
            variant=effective_variant,
        )

    # --- build ---
    _, df = parse_xrd_file(src, src)
    plot_style = df.attrs.get("plot_style")
    df[["Angle", "Intensity"]].to_csv(pattern_path, index=False)

    if plot_style == "stick":
        built_variant = "stick"
        peaks: list = []
        overlay_uri = render_xrd_plot(df, encode_base64=True)
    elif variant == "gsas":
        built_variant = "gsas"
        peaks, _, overlay_uri = peak_finder(df, use_gsas=True)
    else:
        built_variant = "fast"
        peaks, _, overlay_uri = peak_finder_fast(df)

    overlay_path = folder / f"{built_variant}.png"
    peaks_path = folder / f"{built_variant}.peaks.json"
    png_bytes = base64.b64decode(overlay_uri.split(",", 1)[1])
    overlay_path.write_bytes(png_bytes)
    _write_json(peaks_path, peaks)

    index["file_hash"] = key
    index["generated_at"] = _utc_iso()
    index["n_points"] = int(len(df))
    index["plot_style"] = plot_style
    index.setdefault("variants", {})[built_variant] = {"n_peaks": len(peaks)}
    _write_json(index_path, index)

    _register_and_archive(
        file_hash=file_hash,
        material_auid=material_auid,
        trial_id=trial_id,
        built_variant=built_variant,
        pattern_path=pattern_path,
        overlay_path=overlay_path,
        peaks_path=peaks_path,
    )

    return CacheEntry(
        peaks=peaks,
        overlay_png_bytes=png_bytes,
        overlay_url=_media_url(_rel_under_media(overlay_path)),
        pattern_url=_media_url(_rel_under_media(pattern_path)),
        from_cache=False,
        plot_style=plot_style,
        variant=built_variant,
    )


def _register_and_archive(**kwargs) -> None:
    """Placeholder; implemented in Task 5."""
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_store.GetOrBuildTests -v1`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add catalog/xrd_store.py catalog/tests/test_xrd_store.py
git commit -m "feat(xrd_store): lazy build + cache of pattern, overlay, peaks"
```

---

## Task 5: Build side-effects — manifest registration + archive back-fill

**Files:**
- Modify: `catalog/xrd_store.py`
- Test: `catalog/tests/test_xrd_store.py`

**Interfaces:**
- Consumes: `record_derived_file` (Task 1), `add_files` (Task 2), `RawFile` (`catalog.raw_db`); replaces the `_register_and_archive` placeholder from Task 4.
- Produces: `_register_and_archive(...)` registers `pattern`/`overlay`/`peaks` in `loop_raw` and copies them into the recorded `archive_folder` (non-fatal).

- [ ] **Step 1: Write the failing test**

Append to `catalog/tests/test_xrd_store.py`:

```python
import unittest


def _mongo_reachable() -> bool:
    if os.environ.get("SKIP_MONGO_TESTS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from mongoengine.connection import get_db
        get_db().command("ping")
        return True
    except Exception:
        return False


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class BuildSideEffectsTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._arch = tempfile.TemporaryDirectory()
        self.addCleanup(self._arch.cleanup)
        from catalog.raw_db import RawFile
        self.RawFile = RawFile

    def test_build_registers_derived_and_backfills_archive(self):
        from catalog.raw_db import record_raw_file
        h = "feed" * 16  # 64 hex chars
        self.RawFile.objects(id=h).delete()
        self.addCleanup(lambda: self.RawFile.objects(id=h).delete())
        with override_settings(MEDIA_ROOT=self._dir.name, MEDIA_URL="/media/",
                               RAW_UPLOADS_ROOT=self._arch.name):
            from catalog.upload_archive import archive_upload
            from datetime import datetime
            folder = archive_upload(upload_type="trial", username="bob",
                                    timestamp=datetime(2026, 6, 25, 9, 0, 0, 1000),
                                    metadata={"trial_id": "1"})
            record_raw_file(file_hash=h, archive_folder=folder)

            upload = SimpleUploadedFile("scan.csv",
                                        b"Angle,Intensity\n20,100\n20.1,400\n20.2,90\n")
            xrd_store.store_raw_file("M:abc", "1", upload)
            xrd_store.get_or_build("M:abc", "1", h, variant="fast")

            row = self.RawFile.objects(id=h).first()
            kinds = sorted(d["kind"] for d in row.derived_files)
            self.assertEqual(kinds, ["overlay", "pattern", "peaks"])
            self.assertTrue(os.path.isfile(os.path.join(self._arch.name, folder, "pattern.csv")))
            self.assertTrue(os.path.isfile(os.path.join(self._arch.name, folder, "fast.png")))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_store.BuildSideEffectsTests -v1`
Expected: FAIL — derived_files empty / archive files missing (placeholder is a no-op).

- [ ] **Step 3: Implement `_register_and_archive`**

In `catalog/xrd_store.py`, add imports (after the existing `from catalog.gsas_tools ...` line):

```python
import logging

from catalog.raw_db import RawFile, record_derived_file
from catalog.upload_archive import add_files

logger = logging.getLogger(__name__)
```

Replace the placeholder `_register_and_archive` with:

```python
def _register_and_archive(
    *,
    file_hash: Optional[str],
    material_auid: str,
    trial_id: str,
    built_variant: str,
    pattern_path: Path,
    overlay_path: Path,
    peaks_path: Path,
) -> None:
    """Index derived artifacts in loop_raw and back-fill the archive snapshot.

    Non-fatal: any failure is logged and swallowed so the page/upload succeeds.
    """
    if not file_hash:
        return

    artifacts = [
        ("pattern", None, pattern_path),
        ("overlay", built_variant, overlay_path),
        ("peaks", built_variant, peaks_path),
    ]
    try:
        for kind, variant, path in artifacts:
            rel = _rel_under_media(path)
            record_derived_file(
                file_hash=file_hash,
                kind=kind,
                variant=variant,
                stored_path=rel,
                url=_media_url(rel),
                size_bytes=path.stat().st_size,
                sha256=_hash_file(str(path)),
                generated_at=_utc_iso(),
            )
    except Exception:
        logger.warning("xrd_store: failed to register derived files for %s", file_hash, exc_info=True)

    try:
        row = RawFile.objects(id=file_hash).first()
        archive_folder = getattr(row, "archive_folder", None) if row else None
        if archive_folder:
            add_files(archive_folder, [str(p) for _, _, p in artifacts])
    except Exception:
        logger.warning("xrd_store: failed to back-fill archive for %s", file_hash, exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_store -v1`
Expected: PASS (all `test_xrd_store` tests).

- [ ] **Step 5: Commit**

```bash
git add catalog/xrd_store.py catalog/tests/test_xrd_store.py
git commit -m "feat(xrd_store): register derived files in manifest and archive snapshot"
```

---

## Task 6: Wire persist to the unified store

**Files:**
- Modify: `catalog/views.py` (`persist_experimental_trial`, ~lines 868-1012)
- Test: existing `catalog/tests/test_batch_experiment_upload.py` integration coverage (re-run).

**Interfaces:**
- Consumes: `xrd_store.store_raw_file` (Task 3), `record_raw_file(..., archive_folder=...)` (Task 1), `archive_upload(...) -> folder` (Task 2).
- Produces: raw files written to `MEDIA/xrd/<auid>/<trial_id>/raw.<ext>`; `raw_data_link` points there; `RawFile.archive_folder` populated.

- [ ] **Step 1: Add the import**

In `catalog/views.py`, near the other `from .` imports, add:

```python
from catalog import xrd_store
```

- [ ] **Step 2: Replace the raw-write block**

Replace the `if has_csv:` storage block (currently lines ~868-885, beginning `additional_params["file_hash"] = csv_sha256` and ending at the `warnings.append(...)` for the parse failure) with:

```python
    if has_csv:
        additional_params["file_hash"] = csv_sha256
        stored = xrd_store.store_raw_file(material_auid, trial_id, csv_file)
        csv_path = stored.raw_path
        raw_data_link = request.build_absolute_uri(stored.media_url)
        csv_file.seek(0)
        try:
            # Binary formats (.raw) parse from the written path; text uses the upload.
            xrd_source = csv_path if stored.ext in (".raw",) else csv_file
            metadata, df = parse_xrd_file(xrd_source, csv_file.name)
            plot_image = render_xrd_plot(df, encode_base64=True)
            additional_params["xrd_metadata"] = metadata
        except Exception as exc:
            warnings.append(f"Could not parse XRD file for plotting: {exc}")
```

(`csv_filename` is no longer used by this block; the stored path comes from `stored.raw_path`.)

- [ ] **Step 3: Update the raw-DB record + archive ordering**

Replace the existing `# Idempotent Raw DB entry...` block and the following `archive_upload(...)` call (lines ~972-1012) so the archive runs first and its folder is recorded. New code:

```python
    archive_folder = archive_upload(
        upload_type="trial",
        username=user.username if user.is_authenticated else "anonymous",
        timestamp=timezone.localtime(trial_date),
        metadata={
            "type": "trial",
            "trial_id": trial_id,
            "material_auid": material_auid,
            "recipe_auid": recipe_auid,
            "experimenter": user.username if user.is_authenticated else "",
            "trial_date": trial_date.isoformat(),
            "phase_status": phase_status_raw,
            "raw_data_type": raw_data_type,
            "elements": raw_elements,
            "structure_family": structure_family,
            "synthesis_steps": synthesis_steps,
            "notes": notes,
            "file_hash": additional_params.get("file_hash"),
            "has_csv": has_csv,
            "source_batch_id": source_batch_id,
        },
        media_src_path=csv_path if has_csv else None,
        media_dest_filename=os.path.basename(csv_path) if has_csv else None,
    )

    # Idempotent Raw DB entry for the uploaded file.
    if additional_params.get("file_hash"):
        stored_path = _rel_media_path(csv_path) if has_csv else None
        record_raw_file(
            file_hash=additional_params["file_hash"],
            material_auid=material_auid,
            recipe_auid=recipe_auid,
            trial_id=trial_id,
            original_filename=getattr(csv_file, "name", None) if has_csv else None,
            stored_path=stored_path,
            content_type=getattr(csv_file, "content_type", None) if has_csv else None,
            size_bytes=getattr(csv_file, "size", None) if has_csv else None,
            uploaded_by=user.username if user.is_authenticated else None,
            elements=raw_elements,
            structure_family=structure_family,
            archive_folder=archive_folder,
        )
```

Add this small helper near the top of `persist_experimental_trial`'s module (e.g. just above the function), to express the stored path relative to MEDIA_ROOT:

```python
def _rel_media_path(abs_path: str) -> str:
    return os.path.relpath(abs_path, settings.MEDIA_ROOT).replace(os.sep, "/")
```

- [ ] **Step 4: Run the persist integration tests**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_batch_experiment_upload -v1`
Expected: PASS. The `.txt`-extension test now finds the raw at `xrd/<auid>/<trial>/raw.txt`; update that test's stored-path assertion to:

```python
stored = os.path.join(media_root, "xrd", material_auid, trial.trial_id, "raw.txt")
```

- [ ] **Step 5: Commit**

```bash
git add catalog/views.py catalog/tests/test_batch_experiment_upload.py
git commit -m "feat(upload): store raw via xrd_store and record archive folder"
```

---

## Task 7: Wire `trial_detail` to `get_or_build`

**Files:**
- Modify: `catalog/views.py` (`trial_detail`, ~lines 2099-2166)
- Test: `catalog/tests/test_http_smoke.py` (re-run) + manual.

**Interfaces:**
- Consumes: `xrd_store.get_or_build`, `CacheEntry` (Tasks 4-5).
- Produces: identical `context` keys (`plot_b64`, `detected_peaks`, `plot_notice`), now cache-backed. Metadata rows come from the stored `xrd_metadata` fallback already present below the block.

- [ ] **Step 1: Replace the parse/detect/render block**

In `trial_detail`, replace the block from `raw_link = getattr(record, "raw_data_link", "") or ""` through the `except Exception:` that resets `metadata_rows`/`plot_b64` (currently lines ~2099-2166) with:

```python
    plot_b64 = None
    detected_peaks = []
    plot_notice = None
    full_gsas_applied = False

    use_full_gsas = request.GET.get("refine_gsas", "").strip().lower() in ("1", "true", "yes") or \
        os.environ.get("LOOP_GSAS_FULL_SYNC", "").strip().lower() in ("1", "true", "yes")

    file_hash = additional.get("file_hash")
    if xrd_store.resolve_raw_path(recipe.material_auid, trial_id):
        try:
            entry = xrd_store.get_or_build(
                recipe.material_auid, trial_id, file_hash,
                variant="gsas" if use_full_gsas else "fast",
            )
            detected_peaks = entry.peaks
            plot_b64 = base64.b64encode(entry.overlay_png_bytes).decode("ascii")
            full_gsas_applied = entry.variant == "gsas"
            if entry.plot_style == "stick":
                plot_notice = "Reference reflection list (calculated stick pattern)."
            elif not detected_peaks:
                plot_notice = "No peaks were detected."
        except Exception:
            plot_b64 = None
            detected_peaks = []
            plot_notice = "Plot is unavailable for this trial."
```

Confirm `import base64` and `from catalog import xrd_store` are present at module top (add if missing). The metadata table is unchanged: the `if not metadata_rows:` block below already populates `metadata_rows` from the stored `xrd_metadata`.

- [ ] **Step 2: Run smoke tests**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_http_smoke -v1`
Expected: PASS.

- [ ] **Step 3: Manual cache check**

Run:
```bash
docker compose -f docker-compose.dev.yml exec -T web python - <<'PY'
import os, django; os.environ.setdefault("DJANGO_SETTINGS_MODULE","loop.settings"); django.setup()
from catalog import xrd_store
# pick any existing trial folder under media/xrd_data or media/xrd
print("resolve:", xrd_store.resolve_raw_path("M:e3191dffa318", "06_25_2026_2"))
PY
```
Expected: prints a path (legacy fallback works) or `None` if that trial isn't present locally.

- [ ] **Step 4: Commit**

```bash
git add catalog/views.py
git commit -m "feat(trial_detail): serve cached XRD plot via xrd_store"
```

---

## Task 8: JSON API endpoint + `read_manifest`

**Files:**
- Modify: `catalog/xrd_store.py` (add `read_manifest`)
- Modify: `catalog/views.py` (add `trial_xrd_cache_api`)
- Modify: `catalog/urls.py`
- Test: `catalog/tests/test_xrd_cache_api.py`

**Interfaces:**
- Consumes: `get_or_build`, `resolve_raw_path` (Tasks 3-4); `find_embedded_trial`, `get_recipe`, `_user_affiliations`, `_is_visible_to_user` (existing in `views.py`); `RawFile` (`catalog.raw_db`).
- Produces:
  - `xrd_store.read_manifest(material_auid, trial_id, file_hash) -> dict`
  - `views.trial_xrd_cache_api(request, recipe_id, trial_id) -> JsonResponse`
  - URL name `trial_xrd_cache`

- [ ] **Step 1: Write the failing test**

Create `catalog/tests/test_xrd_cache_api.py`:

```python
import os
import unittest
import uuid

from django.test import Client, TestCase, override_settings
from django.contrib.auth import get_user_model


def _mongo_reachable() -> bool:
    if os.environ.get("SKIP_MONGO_TESTS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from mongoengine.connection import get_db
        get_db().command("ping")
        return True
    except Exception:
        return False


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
@override_settings(EMBEDDINGS_ON_WRITE=False)
class XrdCacheApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "apiuser", "api@example.com", "pass", is_staff=True, is_superuser=True
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_returns_404_for_unknown_recipe(self):
        resp = self.client.get("/api/recipe/M:0000:R:0000/trial/x/xrd-cache/")
        self.assertEqual(resp.status_code, 404)
```

(Full end-to-end is covered by `test_xrd_store`; this guards routing + access control + JSON.)

- [ ] **Step 2: Run test to verify it fails**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_cache_api -v1`
Expected: FAIL — 404 view not wired yet (URL resolves to nothing → Django 404 page, but the named route/view doesn't exist).

- [ ] **Step 3: Add `read_manifest` to `xrd_store.py`**

```python
def read_manifest(material_auid: str, trial_id: str, file_hash: Optional[str]) -> dict:
    """Build (if needed) and return a JSON-serialisable manifest for a trial.

    Includes the raw URL, the pattern URL, per-variant overlay/peaks info, and
    the default-variant peaks inline.
    """
    entry = get_or_build(material_auid, trial_id, file_hash)
    folder = trial_dir(material_auid, trial_id)
    index = _read_json(folder / "index.json") or {}

    raw_path = resolve_raw_path(material_auid, trial_id)
    raw_url = _media_url(_rel_under_media(Path(raw_path))) if raw_path and Path(raw_path).is_relative_to(Path(settings.MEDIA_ROOT)) else None

    variants = {}
    for name, info in (index.get("variants") or {}).items():
        variants[name] = {
            "overlay_url": _media_url(f"{UNIFIED_SUBDIR}/{material_auid}/{trial_id}/{name}.png"),
            "peaks_url": _media_url(f"{UNIFIED_SUBDIR}/{material_auid}/{trial_id}/{name}.peaks.json"),
            "n_peaks": info.get("n_peaks", 0),
        }

    return {
        "trial_id": trial_id,
        "file_hash": index.get("file_hash"),
        "generated_at": index.get("generated_at"),
        "n_points": index.get("n_points"),
        "raw_url": raw_url,
        "pattern_url": entry.pattern_url,
        "variants": variants,
        "peaks": entry.peaks,
    }
```

- [ ] **Step 4: Add the view**

In `catalog/views.py`, add (near `trial_detail`):

```python
def trial_xrd_cache_api(request, recipe_id, trial_id):
    """JSON manifest of a trial's raw + processed XRD artifacts (built on demand)."""
    if not auid_mod.is_recipe_id(recipe_id):
        return JsonResponse({"error": "not found"}, status=404)
    recipe = get_recipe(recipe_id)
    if recipe is None:
        return JsonResponse({"error": "not found"}, status=404)
    record = find_embedded_trial(recipe, trial_id)
    if record is None:
        return JsonResponse({"error": "not found"}, status=404)
    if not _is_visible_to_user(record.visibility_affiliations, _user_affiliations(request.user)):
        return JsonResponse({"error": "not found"}, status=404)

    additional = getattr(getattr(record, "exp_condition", None), "additional_params", {}) or {}
    if not xrd_store.resolve_raw_path(recipe.material_auid, trial_id):
        return JsonResponse({"error": "no raw XRD file for this trial"}, status=404)
    try:
        manifest = xrd_store.read_manifest(
            recipe.material_auid, trial_id, additional.get("file_hash")
        )
    except Exception as exc:
        return JsonResponse({"error": f"could not build cache: {exc}"}, status=500)
    return JsonResponse(manifest)
```

- [ ] **Step 5: Add the URL**

In `catalog/urls.py`, in the JSON API group (after the `api/protocols/...` lines), add:

```python
    path(
        "api/recipe/<path:recipe_id>/trial/<str:trial_id>/xrd-cache/",
        views.trial_xrd_cache_api,
        name="trial_xrd_cache",
    ),
```

- [ ] **Step 6: Run test to verify it passes**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog.tests.test_xrd_cache_api -v1`
Expected: PASS (1 test).

- [ ] **Step 7: Run the full suite**

Run: `docker compose -f docker-compose.dev.yml exec -T web python manage.py test catalog -v1`
Expected: PASS (existing 145 + new tests).

- [ ] **Step 8: Commit**

```bash
git add catalog/xrd_store.py catalog/views.py catalog/urls.py catalog/tests/test_xrd_cache_api.py
git commit -m "feat(api): JSON endpoint for a trial's XRD cache manifest"
```

---

## Self-Review Notes

- **Spec coverage:** unified per-trial folder (Task 3-4), three artifacts pattern/overlay/peaks (Task 4), lazy + file_hash invalidation (Task 4), manifest merge with `derived_files`/`archive_folder` (Task 1, 5), archive back-fill resolving the lazy/archive tension via recorded `archive_folder` (Task 2, 5, 6), JSON endpoint with access control (Task 8), persist + detail wiring (Task 6, 7), legacy read fallback (Task 3). Migration command intentionally deferred (spec scope guard).
- **Variants:** `fast`/`gsas`/`stick` used consistently across `get_or_build`, `read_manifest`, and the detail view.
- **Non-fatal side-effects:** `_register_and_archive`, `add_files`, and the archive call all swallow/log errors.
- **Type consistency:** `CacheEntry` fields (`peaks`, `overlay_png_bytes`, `overlay_url`, `pattern_url`, `from_cache`, `plot_style`, `variant`) are produced in Task 4 and consumed unchanged in Tasks 5, 7, 8.
