# Unified XRD Store + Processed-Plot Cache — Design

**Date:** 2026-06-25
**Status:** Approved (pending spec review)
**Branch context:** `batch-experimental-upload`

## Context & Problem

The trial detail page (`trial_detail`, `catalog/views.py`) regenerates everything on **every** page load: it re-reads the raw XRD file (up to ~4000 points), re-runs peak detection (`peak_finder_fast`, scipy; or `peak_finder` full GSAS-II via `?refine_gsas=1`, which takes minutes), and re-renders the matplotlib overlay. Nothing is cached, so repeat views pay the full cost each time, and there is no server-side place to retrieve the processed outputs for later use.

We want to (a) **persist the processed artifacts** so pages load instantly after the first view, and (b) expose them for **programmatic download/reuse**.

A second concern surfaced during design: production already has **three** separate file stores, and naively adding a cache directory would create a fourth. The processed artifacts should be folded into the existing systems so there is a single source of truth.

### Existing storage topology (production)

| Store | Path / location | Role |
|-------|-----------------|------|
| Live served files | `MEDIA_ROOT/xrd_data/<auid>/<trial_id>.<ext>` (for example `<data-root>/media`) | The raw upload, URL-addressable via `raw_data_link`, re-read by the detail view. |
| Archive | `RAW_UPLOADS_ROOT` (for example `<data-root>/raw-uploads`) | `archive_upload()` writes one timestamped folder per upload (`{type}-{date}-{time}-{user}/{trial_id}.<ext>` + `metadata.jsonl`). Append-only disaster-recovery backup of originals. |
| Manifest | `loop_raw` Mongo DB, `RawFile` collection (`catalog/raw_db.py`) | One content-addressed row per file, keyed by `file_hash` (sha256). Records `stored_path`, provenance backlinks, size. Portable index. |

## Decisions (confirmed with user)

1. **Persist three artifacts** per trial: the rendered overlay **PNG**, the detected **peaks**, and the parsed **2θ/intensity pattern**.
2. **Lazy generation** on first detail-view load, keyed by `file_hash`.
3. **JSON API endpoint** for programmatic access (no UI download buttons).
4. **Unify at both levels** — a single per-trial folder on disk (physical) **and** a `loop_raw` manifest indexing raw + derived (manifest).
5. **Bundle derived artifacts into the `RAW_UPLOADS_ROOT` archive** folder so each upload's archive folder becomes a self-contained snapshot.

### The lazy-vs-archive tension and its resolution

The archive folder is written at **upload time** by `archive_upload()`, but derived artifacts are built **lazily** on first view — after that folder already exists. Resolution: `archive_upload()` records the archive folder it created (persisted in the `loop_raw` manifest and in `index.json`); when the cache is built later, `xrd_store` copies the derived files into that recorded folder. This keeps generation lazy, avoids any folder scanning, and still yields self-contained snapshots.

## Architecture

### 1. Unified per-trial storage — new module `catalog/xrd_store.py`

One folder per trial under `MEDIA_ROOT`, owning raw **and** derived files:

```
MEDIA_ROOT/xrd/<material_auid>/<trial_id>/
    raw.<ext>                    # the original upload (was xrd_data/<auid>/<trial_id>.<ext>)
    pattern.csv                  # normalized "Angle,Intensity" (LOOP CSV) — variant-independent
    fast.png   / fast.peaks.json   # overlay + peaks, scipy variant
    gsas.png   / gsas.peaks.json   # overlay + peaks, full GSAS-II variant (only if requested)
    stick.png  / stick.peaks.json  # reflection-list/PDF cards (no peak detection)
    index.json                   # manifest: {file_hash, generated_at, variants{}, n_points, archive_folder}
```

`xrd_store` is the **single module** owning this layout and is testable without HTTP. Public interface:

- `trial_dir(material_auid, trial_id) -> Path` — resolve (and create) the per-trial folder.
- `store_raw_file(material_auid, trial_id, uploaded_file) -> StoredRaw` — write the original upload as `raw.<ext>`; returns `{raw_path, ext, sha256, media_url}`. Used by persist.
- `resolve_raw_path(material_auid, trial_id) -> Path | None` — return the unified `raw.<ext>` if present, else fall back to the **legacy** `xrd_data/<auid>/<trial_id>.<ext>` so existing trials keep working.
- `get_or_build(material_auid, trial_id, file_hash, variant="fast") -> CacheEntry` — on miss/stale: parse via `parse_xrd_file`, write `pattern.csv`, run the finder for the variant, render the overlay, write `<variant>.png` + `<variant>.peaks.json`, update `index.json`, register in `loop_raw`, back-fill the archive folder. Returns `{peaks, overlay_png_bytes, overlay_url, pattern_url, from_cache, plot_style}`.
- `read_manifest(material_auid, trial_id) -> dict | None` — combined view for the API (reads `index.json` + the `RawFile` row).

**Variant selection:** `gsas` when `?refine_gsas=1` / `LOOP_GSAS_FULL_SYNC`; `stick` when the parsed DataFrame is tagged `plot_style="stick"` (reflection cards); otherwise `fast`.

**Cache key / invalidation:** the trial's `file_hash`. If `index.json`'s stored hash differs (re-upload, replaced file), the entry is rebuilt. When `file_hash` is absent (legacy trials), `xrd_store` hashes the resolved raw file to derive the key.

**Migration safety:** new uploads write to `MEDIA/xrd/<auid>/<trial_id>/`; reads fall back to the legacy `xrd_data/...` path. An optional `migrate_xrd_storage` management command (relocate legacy files + update `raw_data_link`) is a **follow-up**, not required for launch.

### 2. Manifest merge — extend `catalog/raw_db.py`

Add to `RawFile`:

```python
derived_files = ListField(DictField())   # entries describing processed artifacts
archive_folder = StringField()           # RAW_UPLOADS_ROOT-relative folder for this upload
```

Add `record_derived_file(*, file_hash, kind, variant, stored_path, url, size_bytes, sha256, generated_at)` — idempotent upsert into `derived_files`, deduped by `(kind, variant)`. `kind ∈ {"pattern", "overlay", "peaks"}`. Every artifact (raw + derived) is now indexed under one `file_hash` row, making `loop_raw` the single queryable source of truth.

### 3. Wiring into existing flows

- **`persist_experimental_trial`** (`catalog/views.py`): write the raw file via `xrd_store.store_raw_file` into the unified folder; set `raw_data_link` to the unified media URL; capture the archive folder returned by `archive_upload()` and pass it to `record_raw_file` (new `archive_folder` field). The existing `parse_xrd_file` preview render stays for the immediate post-upload display.
- **`archive_upload`** (`catalog/upload_archive.py`): return the folder path it wrote (currently returns `None`). Add an `add_files(archive_folder, files)` helper so `xrd_store` can copy derived artifacts into that folder during a lazy build.
- **`trial_detail`** (`catalog/views.py:~2131`): replace the inline parse/detect/render block with a single `xrd_store.get_or_build(...)` call. Behavior is identical on a cold cache, instant on a warm one; the stick-pattern and full-GSAS branches are folded into the `variant` argument.

### 4. JSON API endpoint

`GET /api/recipe/<recipe_id>/trial/<trial_id>/xrd-cache/` → `views.trial_xrd_cache_api`, reusing `trial_detail`'s recipe lookup and affiliation/visibility checks. Builds the cache on demand if absent, then returns:

```json
{
  "trial_id": "06_25_2026_2",
  "file_hash": "…",
  "generated_at": "2026-06-25T…Z",
  "raw_url": "/media/xrd/<auid>/<trial>/raw.csv",
  "pattern_url": "/media/xrd/<auid>/<trial>/pattern.csv",
  "variants": {
    "fast": {"overlay_url": "…/fast.png", "peaks_url": "…/fast.peaks.json", "n_peaks": 23}
  },
  "peaks": [ {"two_theta": 23.1, "intensity": 100.0}, … ]
}
```

Errors (unparseable file, GSAS unavailable) return a clear JSON error and HTTP 4xx/5xx; access failures return 404 to match `trial_detail`.

## Error Handling

- Build failures degrade exactly as today: a warning is recorded and a best-effort plain plot is rendered; the cache simply isn't written.
- Archive back-fill and `loop_raw` writes are **non-fatal** (mirroring `archive_upload`'s existing contract) — a failure logs a warning and the page/endpoint still succeeds.
- Stale `index.json` (hash mismatch) triggers a transparent rebuild.

## Testing

- **`xrd_store` unit tests:** cold build writes `pattern.csv` + `<variant>.png` + `<variant>.peaks.json` + `index.json`; warm hit returns `from_cache=True` without re-rendering (assert via a render spy/mock); stale `file_hash` rebuilds; legacy `xrd_data/...` path is read when the unified file is absent; reflection card → `stick` variant with no peaks.
- **`raw_db` tests:** `record_derived_file` upserts and dedups by `(kind, variant)`; `archive_folder` is stored.
- **Archive tests:** `archive_upload` returns its folder; `add_files` copies derived artifacts into the recorded folder.
- **Endpoint test:** access control (visibility), JSON shape, on-demand build.
- Existing suite (currently 145 tests) stays green.

## Scope Guard (YAGNI)

- No eager caching at upload.
- `migrate_xrd_storage` command is optional/follow-up.
- No `.gpx` storage for the full-GSAS variant.
- No UI download buttons (JSON API only).

## Risks

- **Moving the raw file path** is the main risk vs. a cache-only design. Mitigated by the legacy-read fallback in `xrd_store.resolve_raw_path` and by leaving `raw_data_link` resolution working for old trials. The migration command is deferred until the new path is proven.
