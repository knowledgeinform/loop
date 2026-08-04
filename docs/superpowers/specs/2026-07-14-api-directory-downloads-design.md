# API Directory Downloads — Design

**Date:** 2026-07-14
**Status:** Approved design, pending implementation plan
**Branch:** `ui-overhaul` (current)

## Summary

Add three authenticated `GET` endpoints that stream a `.zip` bundle of an entire
"directory" in the catalog hierarchy — a composition, a recipe, or a single
trial — containing metadata JSON plus the raw and derived data files. Downloads
respect affiliation visibility exactly like the existing read APIs, and are
first-class for Python clients (API-key header auth, `application/zip` response
with a filename), with a copy-pasteable Python snippet in the API console.

## Motivation

The existing `/api/` read endpoints return JSON *metadata and counts* only
(`composition_recipes_api`, `recipe_trials_api`, etc.). A consortium researcher
who wants everything under a composition — every recipe, every trial, and the
actual XRD data files — has no single-call way to get it. These endpoints give
them a portable, self-describing archive they can re-import or analyze offline,
callable from a browser or a Python script/notebook.

## Routes

Path-based, added to `catalog/urls.py` beside the existing `/api/` routes. The
`<path:...>` converter preserves the colons in AUIDs. `ApiTokenAuthMiddleware`
already covers everything under `api/`, giving these endpoints key-or-session
auth and a clean JSON 401 on unauthenticated access for free.

```
GET /api/composition/<material_auid>/download          → composition_download
GET /api/recipe/<recipe_id>/download                   → recipe_download
GET /api/recipe/<recipe_id>/trial/<trial_id>/download  → trial_download
```

## Archive Layout

Composition download (the full tree):

```
composition_M-abc123.zip
├── manifest.json          # level, AUID, generated_at, generated_by, counts, schema note
├── material.json          # elements, structure_family, embedded DFT calculations
└── recipes/
    └── R-7f8e/
        ├── recipe.json     # synthesis_steps, literature[], visibility
        └── trials/
            └── 1/
                ├── trial.json
                └── raw/
                    ├── <original-filename>.csv   # as uploaded
                    ├── pattern.csv                # normalized angle/intensity
                    ├── overlay.png                # rendered plot
                    └── peaks.json                 # detected peaks
```

- **Recipe download**: the `recipes/R-…/` subtree hoisted to the archive root —
  `recipe.json` + `trials/`.
- **Trial download**: the `trials/<id>/` subtree at the root — `trial.json` +
  `raw/`.
- Folder names sanitize `:` → `-`. The zip filename is likewise sanitized:
  `composition_M-abc123.zip`, `recipe_M-abc123-R-7f8e.zip`,
  `trial_M-abc123-R-7f8e-1.zip`.
- Every level includes a `manifest.json` describing what the archive is.

## Component: `catalog/api_download.py` (new module)

Keeps `views.py` thin (it is already ~3000 lines). Public functions:

```python
build_composition_zip(material_auid, user_affiliations) -> tuple[bytes, str]  # (zip_bytes, filename)
build_recipe_zip(recipe_id, user_affiliations)          -> tuple[bytes, str]
build_trial_zip(recipe_id, trial_id, user_affiliations) -> tuple[bytes, str]
```

Each returns `None` when the requested object does not exist or has no
visible content, so the view can answer 404. (No exceptions for the
not-found case — `None` is the single sentinel.)

Internals:

- Walk `Material → Recipe → EmbeddedTrial / EmbeddedLiterature / EmbeddedDFT`,
  filtering **every node** through `_is_visible_to_user(node.visibility_affiliations,
  user_affiliations)`. A recipe/trial/literature the caller cannot see is
  omitted. If the requested top-level object has no visible content remaining,
  the build returns "not found" and the view responds 404 — mirroring
  `composition_recipes_api`.
- Serialize embedded documents to plain dicts via their MongoEngine
  `to_mongo().to_dict()` (already used elsewhere in `views.py`), stripping
  internal-only keys.
- **Raw file** for a trial: `xrd_store.resolve_raw_path(material_auid, trial_id)`.
  Original filename comes from the `loop_raw` `RawFile` row (looked up by
  `file_hash` from the trial's `additional_params`), falling back to the resolved
  path's basename.
- **Derived artifacts**: `xrd_store.get_or_build(material_auid, trial_id,
  file_hash)` — built on demand if uncached, exactly as `trial_detail` does. It
  yields `pattern.csv` (on disk), `overlay.png` bytes, and `peaks` JSON.
- A trial with no raw file gets `trial.json` only, no `raw/` folder. Artifact
  build failures are caught per-trial and noted in `manifest.json` rather than
  failing the whole archive.
- Archive is assembled in memory with stdlib `zipfile` + `io.BytesIO`. This is
  synchronous/in-request, consistent with the documented architecture ("no task
  queue; long-running scientific jobs run in-request"). Acceptable at expected
  catalog sizes; streaming is a possible future optimization, explicitly out of
  scope here.

## Component: view functions (`catalog/views.py`)

Three thin views, one per route. Each:

1. Validates the AUID shape via `catalog.auid` helpers (e.g. `is_recipe_id`),
   404 on malformed.
2. Resolves `user_affiliations = _user_affiliations(request.user)`.
3. Calls the matching `build_*_zip(...)`.
4. On `None` → `JsonResponse({"error": "not found"}, status=404)`.
5. On success → `HttpResponse(zip_bytes, content_type="application/zip")` with
   `Content-Disposition: attachment; filename="<name>.zip"`.

No `@login_required` decorator needed — the middleware enforces auth for
`/api/`, matching the other read-API views.

## Component: API console integration

`catalog/api_docs.py` auto-discovers `api/` routes and infers `GET` (the word
"download" trips no mutating hint), so these would render as executable JSON
try-it panels — wrong for a binary response. Changes:

- Add `ANNOTATIONS` entries for the three endpoints with a `"binary": True`
  flag, a `summary`, and path-param descriptions.
- In `build_catalog`, when `binary` is set: `executable=False`, and attach a
  `download_href` (the concrete path with params) and a generated host-relative
  **Python snippet** string.
- `api_docs.html` / `api_console.js`: for a binary endpoint, render a
  **Download** link (in-browser, works as the signed-in user) and a
  **Python snippet** code block, instead of the JSON try-it form or the generic
  "read-only, it mutates" note. (Reuse existing `.mono` / `.api-*` styles.)

Python snippet shape (host filled from the request/settings):

```python
import requests, zipfile, io

r = requests.get(
    "https://<host>/api/composition/M:abc123/download",
    headers={"Authorization": "Bearer <your-key>"},
    stream=True,
)
r.raise_for_status()
zipfile.ZipFile(io.BytesIO(r.content)).extractall("M-abc123")
```

## Access Control

- Reuses `_user_affiliations` and `_is_visible_to_user` — the same primitives as
  every other read endpoint. A key "acts as" its owning user with that user's
  affiliation visibility (per `ApiTokenAuthMiddleware`), so downloads never
  expose more than the browser would.
- Superusers see everything, consistent with existing behavior.

## Testing — `catalog/tests/test_api_download.py`

For each of the three levels:

- **Happy path**: 200; response is a valid zip; expected entries present
  (`manifest.json`, level-appropriate `*.json`, `raw/` files for trials that
  have a raw file).
- **Response headers** (Python-client contract): `Content-Type: application/zip`
  and a `Content-Disposition: attachment; filename="…zip"` with the sanitized
  name.
- **Visibility filtering**: a recipe/trial/literature owned by another
  affiliation is absent from the archive; a composition with no visible children
  → 404.
- **Not found / malformed**: unknown AUID → 404; malformed AUID → 404.
- **Trial without a raw file**: archive omits the `raw/` folder, still includes
  `trial.json`.

## Scope / YAGNI

- **DFT is embedded** in `material.json`, not exposed as its own download level —
  only the three named levels (composition, recipe, trial) get endpoints.
- **In-memory zip**, not streaming — fits the app's synchronous model and
  expected sizes.
- No new auth mechanism, serializer framework, or task queue — reuses what
  exists.
