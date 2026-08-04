# API Directory Downloads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three authenticated `GET` endpoints that stream a `.zip` bundle of an entire composition, recipe, or trial "directory" — metadata JSON plus raw and derived data files — usable from the browser and from Python clients.

**Architecture:** A new `catalog/api_download.py` module walks the Material → Recipe → EmbeddedTrial hierarchy, filters every node by affiliation visibility, and assembles an in-memory zip with stdlib `zipfile`. Three thin views in `catalog/views.py` wire it to routes in `catalog/urls.py`. The API console (`api_docs.py` + template + JS) renders a Download link and a Python `requests` snippet for these binary endpoints instead of the JSON try-it panel.

**Tech Stack:** Python 3.12, Django 5.2, MongoEngine, stdlib `zipfile`/`io`, existing `catalog.xrd_store` helpers.

## Global Constraints

- **AUID handling** — parse/validate AUIDs only via `catalog/auid.py` (`is_material_auid`, `is_recipe_id`); never construct or regex them inline.
- **MongoEngine only** for materials data — never the Django ORM.
- **Visibility** — every Material/Recipe/EmbeddedTrial/EmbeddedLiterature/EmbeddedDFT node must pass `views._is_visible_to_user(node.visibility_affiliations, user_affiliations)` before inclusion. Caller affiliations come from `views._user_affiliations(request.user)`.
- **Auth** — no view decorators; `ApiTokenAuthMiddleware` already enforces auth + 401 for everything under `api/`. Views must not add `@login_required`.
- **JSON errors** — error responses are `JsonResponse({"error": ...}, status=...)`, matching the other read APIs.
- **Folder/file name sanitization** — replace `:` with `-` in every zip entry path and in the download filename (colons are illegal on some filesystems).
- **Synchronous** — build the zip in-request, in memory (`io.BytesIO`); no task queue.

---

### Task 1: Zip-building helpers and node serialization (`catalog/api_download.py`)

Create the module with the internal helpers and `build_trial_zip`. Trials are the leaf level and exercise raw-file + derived-artifact logic, so this task stands alone and testable.

**Files:**
- Create: `catalog/api_download.py`
- Test: `catalog/tests/test_api_download.py`

**Interfaces:**
- Consumes:
  - `catalog.documents.get_recipe(recipe_auid) -> Recipe | None`
  - `catalog.documents.find_embedded_trial(recipe, trial_id) -> EmbeddedTrial | None`
  - `catalog.views._is_visible_to_user(item_visibility, user_affiliations) -> bool`
  - `catalog.xrd_store.resolve_raw_path(material_auid, trial_id) -> str | None`
  - `catalog.xrd_store.get_or_build(material_auid, trial_id, file_hash) -> CacheEntry` (fields: `.peaks: list`, `.overlay_png_bytes: bytes`)
  - `catalog.xrd_store.trial_dir(material_auid, trial_id) -> pathlib.Path` (contains `pattern.csv` after a build)
  - `catalog.raw_db.RawFile.objects(id=file_hash).first()` → `.original_filename`
- Produces (used by later tasks):
  - `_sanitize(text: str) -> str` — replaces `:` with `-`.
  - `_embedded_to_dict(doc) -> dict` — `doc.to_mongo().to_dict()` with the Mongo `_cls` key removed.
  - `_add_trial_to_zip(zf, prefix, material_auid, trial, notes) -> None` — writes `<prefix>trial.json` and, when a raw file exists, `<prefix>raw/<original>`, `<prefix>raw/pattern.csv`, `<prefix>raw/overlay.png`, `<prefix>raw/peaks.json`; on artifact-build failure appends a string to `notes` and writes only `trial.json` + the original raw file.
  - `build_trial_zip(recipe_id, trial_id, user_affiliations) -> tuple[bytes, str] | None` — returns `(zip_bytes, filename)` or `None` when the recipe/trial is missing or not visible.

- [ ] **Step 1: Write the failing test**

Add to `catalog/tests/test_api_download.py`:

```python
"""Tests for the API directory-download zip builders and endpoints."""

from __future__ import annotations

import io
import os
import unittest
import uuid
import zipfile
from datetime import datetime, timezone

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, RequestFactory

from catalog import api_download, views
from catalog.documents import (
    EmbeddedDFT,
    EmbeddedLiterature,
    EmbeddedTrial,
    ExpCondition,
    Material,
    Recipe,
)


def _mongo_reachable() -> bool:
    if os.environ.get("SKIP_MONGO_TESTS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        from mongoengine.connection import get_db

        get_db().command("ping")
        return True
    except Exception:
        return False


def _names(zip_bytes):
    return set(zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist())


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class ApiDownloadBuilderTests(SimpleTestCase):
    databases = {}  # Mongo only, not the Django DB

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:testDl{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:r{suffix}"
        Material(
            id=self.material_auid,
            elements={"Za": 1, "Zb": 1},
            structure_family="rocksalt",
            default_visibility_affiliations=["S4E"],
            dft_calculations=[
                EmbeddedDFT(comp_auid=f"{self.material_auid}:C:c{suffix}",
                            dft_bandgap_ev=1.2, visibility_affiliations=["S4E"]),
            ],
        ).save()
        Recipe(
            id=self.recipe_auid,
            material_auid=self.material_auid,
            elements={"Za": 1, "Zb": 1},
            structure_family="rocksalt",
            synthesis_steps=[{"step_type": "grind"}],
            trials=[
                EmbeddedTrial(trial_id="t1",
                              trial_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                              exp_condition=ExpCondition(), phase_status="single_phase",
                              visibility_affiliations=["S4E"]),
            ],
            literature=[
                EmbeddedLiterature(lit_id="L:abc", doi="10.1/x",
                                   exp_condition=ExpCondition(),
                                   visibility_affiliations=["S4E"]),
            ],
            visibility_affiliations=["S4E"],
        ).save()

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()

    def test_build_trial_zip_no_raw_omits_raw_folder(self):
        result = api_download.build_trial_zip(self.recipe_auid, "t1", ["S4E"])
        self.assertIsNotNone(result)
        zip_bytes, filename = result
        names = _names(zip_bytes)
        self.assertIn("manifest.json", names)
        self.assertIn("trial.json", names)
        self.assertFalse(any(n.startswith("raw/") for n in names))
        self.assertTrue(filename.endswith(".zip"))
        self.assertNotIn(":", filename)

    def test_build_trial_zip_missing_trial_returns_none(self):
        self.assertIsNone(api_download.build_trial_zip(self.recipe_auid, "nope", ["S4E"]))

    def test_build_trial_zip_not_visible_returns_none(self):
        self.assertIsNone(api_download.build_trial_zip(self.recipe_auid, "t1", ["APL"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test catalog.tests.test_api_download -v 2`
Expected: FAIL — `ModuleNotFoundError: No module named 'catalog.api_download'` (or `AttributeError: build_trial_zip`). If MongoDB is not running, the tests SKIP instead — start the dev stack (`docker compose -f docker-compose.dev.yml up`) so they actually run.

- [ ] **Step 3: Write minimal implementation**

Create `catalog/api_download.py`:

```python
"""Build downloadable ``.zip`` archives of a composition, recipe, or trial.

Each archive mirrors the catalog hierarchy as folders — metadata JSON plus the
raw upload and derived XRD artifacts for every visible trial. Visibility is
enforced per node so an archive never exposes more than the caller could see in
the browser. See docs/superpowers/specs/2026-07-14-api-directory-downloads-design.md.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import xrd_store
from .documents import find_embedded_trial, get_recipe
from .raw_db import RawFile


def _sanitize(text: str) -> str:
    """Filesystem-safe token: colons (from AUIDs) become hyphens."""
    return (text or "").replace(":", "-")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _embedded_to_dict(doc) -> dict:
    """Serialize a MongoEngine (embedded) document to a plain JSON-able dict."""
    raw = doc.to_mongo().to_dict()
    raw.pop("_cls", None)
    return raw


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def _add_trial_to_zip(zf, prefix, material_auid, trial, notes) -> None:
    """Write ``<prefix>trial.json`` and, when present, the trial's raw/ folder."""
    zf.writestr(f"{prefix}trial.json", _json_bytes(_embedded_to_dict(trial)))

    raw_path = xrd_store.resolve_raw_path(material_auid, trial.trial_id)
    if not raw_path:
        return

    file_hash = None
    exp = getattr(trial, "exp_condition", None)
    if exp is not None:
        file_hash = (getattr(exp, "additional_params", None) or {}).get("file_hash")

    # Original upload, named as it was uploaded when we know the name.
    original_name = Path(raw_path).name
    if file_hash:
        row = RawFile.objects(id=file_hash).first()
        if row is not None and row.original_filename:
            original_name = _sanitize(row.original_filename)
    try:
        zf.writestr(f"{prefix}raw/{original_name}", Path(raw_path).read_bytes())
    except OSError as exc:
        notes.append(f"trial {trial.trial_id}: could not read raw file ({exc})")
        return

    # Derived artifacts — built on demand, mirroring trial_detail.
    try:
        entry = xrd_store.get_or_build(material_auid, trial.trial_id, file_hash)
        pattern_path = xrd_store.trial_dir(material_auid, trial.trial_id) / "pattern.csv"
        if pattern_path.is_file():
            zf.writestr(f"{prefix}raw/pattern.csv", pattern_path.read_bytes())
        zf.writestr(f"{prefix}raw/overlay.png", entry.overlay_png_bytes)
        zf.writestr(f"{prefix}raw/peaks.json", _json_bytes(entry.peaks))
    except Exception as exc:  # artifact build is best-effort; keep the archive
        notes.append(f"trial {trial.trial_id}: derived artifacts unavailable ({exc})")


def build_trial_zip(recipe_id, trial_id, user_affiliations) -> Optional[tuple[bytes, str]]:
    """Zip a single trial directory. ``None`` if missing or not visible."""
    from .views import _is_visible_to_user

    recipe = get_recipe(recipe_id)
    if recipe is None:
        return None
    trial = find_embedded_trial(recipe, trial_id)
    if trial is None:
        return None
    if not _is_visible_to_user(getattr(trial, "visibility_affiliations", None), user_affiliations):
        return None

    notes: list = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_trial_to_zip(zf, "", recipe.material_auid, trial, notes)
        zf.writestr("manifest.json", _json_bytes({
            "level": "trial",
            "recipe_auid": recipe.id,
            "material_auid": recipe.material_auid,
            "trial_id": trial_id,
            "generated_at": _utc_iso(),
            "notes": notes,
            "schema": "LOOP directory export v1",
        }))

    filename = f"trial_{_sanitize(recipe.id)}-{_sanitize(trial_id)}.zip"
    return buffer.getvalue(), filename
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test catalog.tests.test_api_download -v 2`
Expected: PASS (3 tests in `ApiDownloadBuilderTests`).

- [ ] **Step 5: Commit**

```bash
git add catalog/api_download.py catalog/tests/test_api_download.py
git commit -m "feat(api): trial directory zip builder"
```

---

### Task 2: Recipe and composition zip builders (`catalog/api_download.py`)

Add the two higher levels, reusing `_add_trial_to_zip`. They nest trials (and, for composition, recipes) and enforce visibility at every level.

**Files:**
- Modify: `catalog/api_download.py`
- Test: `catalog/tests/test_api_download.py`

**Interfaces:**
- Consumes: `_add_trial_to_zip`, `_embedded_to_dict`, `_sanitize`, `_json_bytes`, `_utc_iso` (Task 1); `catalog.documents.Material`, `Recipe`, `get_recipe`, `get_recipes_for_material`; `views._is_visible_to_user`.
- Produces:
  - `_recipe_folder_name(recipe_auid: str) -> str` — the `R-<hash>` segment (sanitized `R:` part), or the full sanitized AUID if no `:R:` present.
  - `_add_recipe_subtree(zf, prefix, recipe, user_affiliations, notes) -> bool` — writes `<prefix>recipe.json` (with visible literature only) and `<prefix>trials/<id>/…` for each visible trial; returns `True` if the recipe itself or any visible child was written.
  - `build_recipe_zip(recipe_id, user_affiliations) -> tuple[bytes, str] | None`
  - `build_composition_zip(material_auid, user_affiliations) -> tuple[bytes, str] | None`

- [ ] **Step 1: Write the failing test**

Append to `catalog/tests/test_api_download.py` inside `ApiDownloadBuilderTests`:

```python
    def test_build_recipe_zip_has_recipe_and_trials(self):
        result = api_download.build_recipe_zip(self.recipe_auid, ["S4E"])
        self.assertIsNotNone(result)
        zip_bytes, filename = result
        names = _names(zip_bytes)
        self.assertIn("manifest.json", names)
        self.assertIn("recipe.json", names)
        self.assertTrue(any(n.startswith("trials/t1/") for n in names))
        self.assertIn("trials/t1/trial.json", names)
        self.assertTrue(filename.startswith("recipe_"))

    def test_build_composition_zip_nests_recipes(self):
        result = api_download.build_composition_zip(self.material_auid, ["S4E"])
        self.assertIsNotNone(result)
        zip_bytes, filename = result
        names = _names(zip_bytes)
        self.assertIn("manifest.json", names)
        self.assertIn("material.json", names)
        self.assertTrue(any(n.startswith("recipes/") and n.endswith("/recipe.json") for n in names))
        self.assertTrue(any("/trials/t1/trial.json" in n for n in names))
        self.assertTrue(filename.startswith("composition_"))

    def test_composition_hidden_when_no_visible_children(self):
        self.assertIsNone(api_download.build_composition_zip(self.material_auid, ["APL"]))

    def test_missing_material_returns_none(self):
        self.assertIsNone(api_download.build_composition_zip("M:doesnotexist", ["S4E"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test catalog.tests.test_api_download -v 2`
Expected: FAIL — `AttributeError: module 'catalog.api_download' has no attribute 'build_recipe_zip'`.

- [ ] **Step 3: Write minimal implementation**

Add to `catalog/api_download.py` (imports: extend the documents import to include `Material`, `Recipe`, `get_recipes_for_material`):

```python
def _recipe_folder_name(recipe_auid: str) -> str:
    """``M:...:R:7f8e`` -> ``R-7f8e``; fall back to the whole sanitized AUID."""
    marker = ":R:"
    idx = recipe_auid.find(marker)
    if idx == -1:
        return _sanitize(recipe_auid)
    return "R-" + _sanitize(recipe_auid[idx + len(marker):])


def _add_recipe_subtree(zf, prefix, recipe, user_affiliations, notes) -> bool:
    """Write a recipe's json + visible trials under ``prefix``. Returns whether
    anything visible was written."""
    from .views import _is_visible_to_user

    visible_trials = [
        t for t in (recipe.trials or [])
        if _is_visible_to_user(getattr(t, "visibility_affiliations", None), user_affiliations)
    ]
    visible_lits = [
        lit for lit in (recipe.literature or [])
        if _is_visible_to_user(getattr(lit, "visibility_affiliations", None), user_affiliations)
    ]
    recipe_visible = _is_visible_to_user(
        getattr(recipe, "visibility_affiliations", None), user_affiliations
    )
    if not (recipe_visible or visible_trials or visible_lits):
        return False

    recipe_doc = _embedded_to_dict(recipe)
    recipe_doc["literature"] = [_embedded_to_dict(lit) for lit in visible_lits]
    recipe_doc["trials"] = [t.trial_id for t in visible_trials]  # bodies live in trials/
    zf.writestr(f"{prefix}recipe.json", _json_bytes(recipe_doc))

    for trial in visible_trials:
        _add_trial_to_zip(
            zf, f"{prefix}trials/{_sanitize(trial.trial_id)}/",
            recipe.material_auid, trial, notes,
        )
    return True


def build_recipe_zip(recipe_id, user_affiliations) -> Optional[tuple[bytes, str]]:
    """Zip a recipe directory. ``None`` if missing or nothing visible."""
    recipe = get_recipe(recipe_id)
    if recipe is None:
        return None

    notes: list = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        wrote = _add_recipe_subtree(zf, "", recipe, user_affiliations, notes)
        if not wrote:
            return None
        zf.writestr("manifest.json", _json_bytes({
            "level": "recipe",
            "recipe_auid": recipe.id,
            "material_auid": recipe.material_auid,
            "generated_at": _utc_iso(),
            "notes": notes,
            "schema": "LOOP directory export v1",
        }))

    return buffer.getvalue(), f"recipe_{_sanitize(recipe.id)}.zip"


def build_composition_zip(material_auid, user_affiliations) -> Optional[tuple[bytes, str]]:
    """Zip a whole composition directory. ``None`` if missing or nothing visible."""
    from .views import _is_visible_to_user

    material = Material.objects(id=material_auid).first()
    if material is None:
        return None

    material_visible = _is_visible_to_user(
        material.default_visibility_affiliations, user_affiliations
    )

    notes: list = []
    buffer = io.BytesIO()
    wrote_any = False
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for recipe in get_recipes_for_material(material_auid):
            folder = f"recipes/{_recipe_folder_name(recipe.id)}/"
            if _add_recipe_subtree(zf, folder, recipe, user_affiliations, notes):
                wrote_any = True

        if not (wrote_any or material_visible):
            return None

        material_doc = _embedded_to_dict(material)
        material_doc["dft_calculations"] = [
            _embedded_to_dict(d) for d in (material.dft_calculations or [])
            if _is_visible_to_user(getattr(d, "visibility_affiliations", None), user_affiliations)
        ]
        zf.writestr("material.json", _json_bytes(material_doc))
        zf.writestr("manifest.json", _json_bytes({
            "level": "composition",
            "material_auid": material.id,
            "generated_at": _utc_iso(),
            "notes": notes,
            "schema": "LOOP directory export v1",
        }))

    return buffer.getvalue(), f"composition_{_sanitize(material.id)}.zip"
```

Also update the top-of-file import line:

```python
from .documents import (
    Material,
    Recipe,
    find_embedded_trial,
    get_recipe,
    get_recipes_for_material,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test catalog.tests.test_api_download -v 2`
Expected: PASS (7 tests in `ApiDownloadBuilderTests`).

- [ ] **Step 5: Commit**

```bash
git add catalog/api_download.py catalog/tests/test_api_download.py
git commit -m "feat(api): recipe and composition directory zip builders"
```

---

### Task 3: Download views and routes (`catalog/views.py`, `catalog/urls.py`)

Wire the builders to three `GET` routes returning `application/zip` with a filename, and assert the response contract a Python client relies on.

**Files:**
- Modify: `catalog/views.py` (add three views near the other read APIs, ~line 3106; add `import` for `api_download` and `HttpResponse` if not present)
- Modify: `catalog/urls.py` (add three routes in the `# API` block)
- Test: `catalog/tests/test_api_download.py`

**Interfaces:**
- Consumes: `api_download.build_composition_zip/build_recipe_zip/build_trial_zip`; `views._user_affiliations`; `auid_mod.is_material_auid`, `auid_mod.is_recipe_id`; `django.http.HttpResponse`.
- Produces (url names): `composition_download`, `recipe_download`, `trial_download`.

- [ ] **Step 1: Write the failing test**

Append a new class to `catalog/tests/test_api_download.py`:

```python
@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class ApiDownloadViewTests(SimpleTestCase):
    databases = {}

    def setUp(self):
        self.factory = RequestFactory()
        suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:testDlv{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:r{suffix}"
        Material(id=self.material_auid, elements={"Za": 1, "Zb": 1},
                 structure_family="rocksalt",
                 default_visibility_affiliations=["S4E"]).save()
        Recipe(id=self.recipe_auid, material_auid=self.material_auid,
               elements={"Za": 1, "Zb": 1}, structure_family="rocksalt",
               trials=[EmbeddedTrial(trial_id="t1",
                                     trial_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                                     exp_condition=ExpCondition(),
                                     phase_status="single_phase",
                                     visibility_affiliations=["S4E"])],
               literature=[], visibility_affiliations=["S4E"]).save()

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()

    def _get(self, view, **kwargs):
        request = self.factory.get("/api/")
        request.user = AnonymousUser()
        return view(request, **kwargs)

    def test_composition_download_headers_and_zip(self):
        resp = self._get(views.composition_download, material_auid=self.material_auid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(".zip", resp["Content-Disposition"])
        self.assertIn("material.json", _names(resp.content))

    def test_trial_download_ok(self):
        resp = self._get(views.trial_download,
                         recipe_id=self.recipe_auid, trial_id="t1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("trial.json", _names(resp.content))

    def test_recipe_download_ok(self):
        resp = self._get(views.recipe_download, recipe_id=self.recipe_auid)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("recipe.json", _names(resp.content))

    def test_download_malformed_auid_404(self):
        resp = self._get(views.composition_download, material_auid="not-an-auid")
        self.assertEqual(resp.status_code, 404)

    def test_download_missing_material_404(self):
        resp = self._get(views.composition_download, material_auid="M:missing123456")
        self.assertEqual(resp.status_code, 404)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test catalog.tests.test_api_download.ApiDownloadViewTests -v 2`
Expected: FAIL — `AttributeError: module 'catalog.views' has no attribute 'composition_download'`.

- [ ] **Step 3: Write minimal implementation**

In `catalog/views.py`, confirm `from django.http import ... HttpResponse` is imported (it is used elsewhere; add `HttpResponse` to that import if missing) and add near the top-level imports:

```python
from . import api_download
```

Add these three views immediately after `recipe_trials_api` (around line 3106):

```python
def composition_download(request, material_auid):
    """Download a whole composition directory as a ``.zip`` (metadata + data files)."""
    if not auid_mod.is_material_auid(material_auid):
        return JsonResponse({"error": "not found"}, status=404)
    result = api_download.build_composition_zip(
        material_auid, _user_affiliations(request.user)
    )
    return _zip_response_or_404(result)


def recipe_download(request, recipe_id):
    """Download a whole recipe directory as a ``.zip`` (metadata + trial data files)."""
    if not auid_mod.is_recipe_id(recipe_id):
        return JsonResponse({"error": "not found"}, status=404)
    result = api_download.build_recipe_zip(
        recipe_id, _user_affiliations(request.user)
    )
    return _zip_response_or_404(result)


def trial_download(request, recipe_id, trial_id):
    """Download a single trial directory as a ``.zip`` (metadata + raw/derived files)."""
    if not auid_mod.is_recipe_id(recipe_id):
        return JsonResponse({"error": "not found"}, status=404)
    result = api_download.build_trial_zip(
        recipe_id, trial_id, _user_affiliations(request.user)
    )
    return _zip_response_or_404(result)


def _zip_response_or_404(result):
    if result is None:
        return JsonResponse({"error": "not found"}, status=404)
    zip_bytes, filename = result
    response = HttpResponse(zip_bytes, content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
```

In `catalog/urls.py`, add inside the `# API` block (after the `api/recipe/.../xrd-cache/` route near line 112):

```python
    # Directory downloads (zip of metadata + data files)
    path("api/composition/<path:material_auid>/download", views.composition_download, name="composition_download"),
    path("api/recipe/<path:recipe_id>/trial/<str:trial_id>/download", views.trial_download, name="trial_download"),
    path("api/recipe/<path:recipe_id>/download", views.recipe_download, name="recipe_download"),
```

Note: the trial route is listed before the recipe route so the more specific `/trial/<id>/download` pattern is matched first.

- [ ] **Step 4: Run test to verify it passes**

Run: `python manage.py test catalog.tests.test_api_download -v 2`
Expected: PASS (all builder + view tests).

- [ ] **Step 5: Commit**

```bash
git add catalog/views.py catalog/urls.py catalog/tests/test_api_download.py
git commit -m "feat(api): composition/recipe/trial download endpoints"
```

---

### Task 4: API console — binary endpoint rendering + Python snippet (`catalog/api_docs.py`, template, JS)

Make the auto-derived console treat the download endpoints as binary: not JSON-executable, shown with a Download link and a Python `requests` snippet.

**Files:**
- Modify: `catalog/api_docs.py` (add `ANNOTATIONS` entries with `binary`; set `executable=False` and attach `binary`/`python_snippet` in `build_catalog`; add a snippet generator)
- Modify: `catalog/templates/catalog/api_docs.html` (render binary endpoints)
- Test: `catalog/tests/test_api_download.py`

**Interfaces:**
- Consumes: `catalog.api_docs.build_catalog()` (existing).
- Produces: each endpoint dict may carry `binary: bool` and `python_snippet: str`; binary endpoints have `executable=False`.

- [ ] **Step 1: Write the failing test**

Append a new class to `catalog/tests/test_api_download.py` (does not need MongoDB):

```python
from catalog.api_docs import build_catalog


class ApiDownloadCatalogTests(SimpleTestCase):
    databases = {}

    def _endpoint(self, path):
        for section in build_catalog():
            for ep in section["endpoints"]:
                if ep["path"] == path:
                    return ep
        return None

    def test_download_endpoints_are_binary_not_executable(self):
        ep = self._endpoint("/api/composition/<material_auid>/download")
        self.assertIsNotNone(ep)
        self.assertEqual(ep["method"], "GET")
        self.assertTrue(ep["binary"])
        self.assertFalse(ep["executable"])

    def test_download_endpoint_has_python_snippet(self):
        ep = self._endpoint("/api/recipe/<recipe_id>/download")
        self.assertIsNotNone(ep)
        self.assertIn("requests.get", ep["python_snippet"])
        self.assertIn("Authorization", ep["python_snippet"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python manage.py test catalog.tests.test_api_download.ApiDownloadCatalogTests -v 2`
Expected: FAIL — `KeyError: 'binary'` (the endpoint dict has no `binary` key yet).

- [ ] **Step 3: Write minimal implementation**

In `catalog/api_docs.py`:

Add a `"download"` group so the endpoints land in their own section — insert into `_GROUPS` (before `"recipe"`, since paths contain "recipe"):

```python
_GROUPS = [
    ("download", "Downloads"),
    ("doi", "DOI"),
    ("recipe", "Recipes"),
    ...
]
```

Add `"Downloads"` to `_SECTION_ORDER` (e.g. right after `"DOI"`).

Add three `ANNOTATIONS` entries (keyed by url name):

```python
    "composition_download": {
        "summary": "Download an entire composition directory as a .zip: material.json, "
                   "every visible recipe, and each trial's raw + derived data files.",
        "binary": True,
    },
    "recipe_download": {
        "summary": "Download an entire recipe directory as a .zip: recipe.json plus each "
                   "visible trial's raw + derived data files.",
        "binary": True,
    },
    "trial_download": {
        "summary": "Download a single trial directory as a .zip: trial.json plus the raw "
                   "upload and derived XRD artifacts.",
        "binary": True,
    },
```

Add a snippet generator and wire it into `build_catalog`. Add this helper above `build_catalog`:

```python
def _python_snippet(path: str) -> str:
    """A copy-pasteable requests example for a binary download endpoint.

    The path keeps its ``<param>`` placeholders so the reader substitutes real
    AUIDs; host is left as a placeholder too.
    """
    return (
        "import requests, zipfile, io\n\n"
        "r = requests.get(\n"
        f'    "https://<host>{path}",\n'
        '    headers={"Authorization": "Bearer <your-key>"},\n'
        "    stream=True,\n"
        ")\n"
        "r.raise_for_status()\n"
        'zipfile.ZipFile(io.BytesIO(r.content)).extractall("download")'
    )
```

In `build_catalog`, inside the loop where each `endpoints.append({...})` dict is built, compute `binary` and override `executable`/attach the snippet. Change the append block to:

```python
        binary = bool(annotation.get("binary"))
        endpoints.append({
            "method": method,
            "path": "/" + pattern,
            "name": name,
            "section": _section_for(name),
            "description": annotation.get("summary") or _describe(entry.callback),
            "mutating": mutating,
            "binary": binary,
            "executable": (method == "GET") and not mutating and not binary,
            "python_snippet": _python_snippet("/" + pattern) if binary else "",
            "params": path_params + extra_params,
            "example_response": example,
            "example_response_pretty": json.dumps(example, indent=2) if example is not None else "",
        })
```

In `catalog/templates/catalog/api_docs.html`, replace the `{% if ep.executable %}…{% else %}…{% endif %}` block (lines ~70–93) with a three-way branch that handles binary endpoints:

```html
        {% if ep.executable %}
        <form class="api-try" data-api-try data-path="{{ ep.path }}" data-method="{{ ep.method }}">
          <div class="api-try-fields">
            {% for p in ep.params %}
            <label class="api-try-field">
              <span class="api-try-field-name">{{ p.name }}{% if p.required %} <span class="req">*</span>{% endif %}</span>
              <input type="text" class="api-input mono" name="{{ p.name }}"
                     data-in="{{ p.in }}"
                     placeholder="{{ p.type }}"{% if p.required %} data-required="1"{% endif %}>
            </label>
            {% endfor %}
          </div>
          <div class="api-try-actions">
            <button type="submit" class="btn btn-primary api-execute">Execute</button>
            <span class="api-try-status" data-status aria-live="polite"></span>
          </div>
          <pre class="api-response mono" data-response hidden></pre>
        </form>
        {% elif ep.binary %}
        <p class="api-readonly-note">
          Returns a <code class="mono">.zip</code> download. Substitute real IDs for the
          <code class="mono">&lt;…&gt;</code> path parameters, then open the URL in your browser
          (signed in) or call it from Python with an API key:
        </p>
        <div class="api-example">
          <span class="api-example-label">Python</span>
          <pre class="api-response mono">{{ ep.python_snippet }}</pre>
        </div>
        {% else %}
        <p class="api-readonly-note">
          Read-only in the console — this endpoint mutates data. See the parameters above
          to call it from your own client.
        </p>
        {% endif %}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python manage.py test catalog.tests.test_api_download.ApiDownloadCatalogTests catalog.tests.test_api_docs -v 2`
Expected: PASS — new catalog tests pass and the existing `test_api_docs` suite still passes (notably `test_only_get_endpoints_are_executable`, which now sees binary GETs as non-executable).

- [ ] **Step 5: Commit**

```bash
git add catalog/api_docs.py catalog/templates/catalog/api_docs.html catalog/tests/test_api_download.py
git commit -m "feat(api): document directory downloads in the console with a Python snippet"
```

---

### Task 5: Full-suite verification

Confirm nothing regressed across the app's test suite and the new endpoints resolve.

**Files:** none (verification only).

- [ ] **Step 1: Run the download suite**

Run: `python manage.py test catalog.tests.test_api_download -v 2`
Expected: PASS (all classes; Mongo-backed classes run because the dev stack is up).

- [ ] **Step 2: Run the API-adjacent suites**

Run: `python manage.py test catalog.tests.test_api_docs catalog.tests.test_api_read_endpoints catalog.tests.test_xrd_cache_api -v 2`
Expected: PASS, no failures or errors.

- [ ] **Step 3: Sanity-check URL resolution**

Run:
```bash
python manage.py shell -c "from django.urls import reverse; print(reverse('composition_download', args=['M:abc123'])); print(reverse('recipe_download', args=['M:a:R:b'])); print(reverse('trial_download', args=['M:a:R:b','1']))"
```
Expected output (paths, colons intact):
```
/api/composition/M:abc123/download
/api/recipe/M:a:R:b/download
/api/recipe/M:a:R:b/trial/1/download
```

- [ ] **Step 4: Commit (if any lint/cleanup was needed; otherwise skip)**

```bash
git commit -am "chore(api): download endpoints verification cleanup"
```

---

## Notes for the implementer

- **MongoDB required:** The builder/view tests skip without a live Mongo. Start `docker compose -f docker-compose.dev.yml up` first, or they will silently SKIP and give false confidence.
- **Circular import:** `api_download` imports `_is_visible_to_user` from `catalog.views` *inside* functions (not at module top) because `views` imports `api_download` at module top. Keep those imports function-local.
- **Why `trials` becomes a list of IDs in `recipe.json`:** the full trial bodies live under `trials/<id>/trial.json`; duplicating them inside `recipe.json` would bloat the archive and drift. The manifest and folder layout are the source of truth.
