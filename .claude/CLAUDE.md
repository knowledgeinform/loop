# LOOP — Claude Code Context

## Project Overview

LOOP is a Django + MongoDB web platform for the **Entropy for Energy Laboratory** at Johns Hopkins University (Department of Materials Science and Engineering). It stores, browses, and analyzes experimental, literature, and computational records on **high-entropy materials**. Records are addressable by content-derived **AUIDs** (Aurora UIDs), enabling deterministic deduplication across data sources. Users from multiple affiliated research groups can upload data, run Rietveld refinements on XRD patterns, and search the catalog semantically.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.12 |
| Web framework | Django 5.2 |
| Database (primary) | MongoDB via MongoEngine 0.29 (`loop` database) |
| Database (backup) | MongoDB via MongoEngine (`loop_raw` database) |
| Django auth DB | SQLite (`db.sqlite3`) |
| Production server | Gunicorn |
| Containerisation | Docker + Docker Compose |
| Semantic search | sentence-transformers 3.0 + PyTorch 2.6 (CPU) |
| XRD analysis | GSAS-II (external install) + SciPy + NumPy |
| Visualisation | Matplotlib + Pandas |
| Static files | WhiteNoise + libsass (SCSS compilation) |
| Frontend | Bootstrap 5.3.3 (CDN), vanilla JS (no SPA framework) |

---

## Running the Project

```bash
# Start dev stack (Django + MongoDB)
docker compose -f docker-compose.dev.yml up --build

# Open a shell inside the web container
docker compose -f docker-compose.dev.yml exec web bash

# Run tests
python manage.py test

# MongoDB health / reindex / backup
python manage.py mongo_admin health
python manage.py mongo_admin reindex
```

App is available at `http://localhost:8000`. Migrations run automatically on container startup via `/docker/entrypoint.sh`.

---

## Architecture Overview

- **Single Django app**: all business logic lives in `catalog/`.
- **Server-side rendering**: Django templates, no REST framework — JSON endpoints use plain `JsonResponse`.
- **No task queue**: processing (including Rietveld refinement) is synchronous. Long-running scientific jobs run in-request.
- **Dual MongoDB connections**: `loop` (primary) and `loop_raw` (flat raw-file backup). Django auth uses SQLite.
- **Affiliation-gated access**: `ApprovedGateMiddleware` blocks unapproved users; all documents carry `visibility_affiliations` lists.
- **JSON archive is the source of truth**: every catalog write lands as plain JSON under `ARCHIVE_ROOT` *before* it reaches MongoDB, which is treated as a rebuildable index. Enforced by MongoEngine `pre_save_post_validation` / `pre_delete` hooks in `catalog/archive/hooks.py`, so ordinary `.save()` call sites need no changes. `manage.py loop_archive rebuild` restores the database from disk. See `docs/ARCHIVE.md`.

---

## Data Model

Documents live in MongoDB; MongoEngine is the ODM. The core topology is:

```
Material (material_auid)
  └── dft_calculations[]      EmbeddedDFT
  └── ml_embeddings[]         (vector search)

Recipe (recipe_auid = M:<hash>:R:<hash>)
  ├── material_auid            → back-reference to Material
  ├── synthesis_steps[]        free-form dict array
  ├── trials[]                 EmbeddedTrial
  └── literature[]             EmbeddedLiterature

RawFile (loop_raw DB, _id = SHA256 hex)
```

Key embedded documents:

| Document | Key fields |
|----------|-----------|
| `EmbeddedTrial` | `trial_id`, `phase_status` (single/multi/not_confirmed), `exp_condition`, `raw_data_type`, `file_hash` |
| `EmbeddedLiterature` | `lit_id` (L:<hash-of-doi>), `doi`, `exp_condition`, `synthesis_successful` |
| `EmbeddedDFT` | `comp_auid`, `dft_formation_energy_ev`, `dft_hull_distance_ev`, `dft_bandgap_ev` |
| `ExpCondition` | `milling_time_hours`, `milling_rpm`, `temp_profile[]`, `precursors[]`, `atmosphere`, `cooling_method`, `additional_params` |

User library (Mongo):

- `UserAffiliation` — maps Django user → affiliation strings
- `UserPrecursor` — saved reagent entries per user
- `UserProtocol` — saved multi-step synthesis protocol templates

Structure families: `Rocksalt`, `Pyrochlore`, `Spinel`, `Perovskite`, `Fluorite`, `Other`.

---

## AUID Scheme

All IDs are content-addressable: SHA256 of canonical content, truncated to 12 hex chars.

| ID type | Format | Source |
|---------|--------|--------|
| `material_auid` | `M:<12-hex>` | hash of (elements, structure_family) |
| `recipe_auid` | `M:<hash>:R:<hash>` | composite; prefix matches parent |
| `comp_auid` | `M:<hash>:C:<hash>` | DFT input set |
| `lit_id` | `L:<12-hex>` | hash of DOI string |
| `trial_id` | sequential string (`"1"`, `"2"`, …) | per-recipe counter |
| raw file `_id` | full SHA256 | SHA256 of uploaded bytes |

**Always call functions in `catalog/auid.py`** to generate or parse AUIDs. Canonical form: lowercase keys, sorted, 4 sig-fig floats, no empty values.

---

## Key Files

| File | Description |
|------|-------------|
| `catalog/documents.py` | MongoEngine document schemas (Material, Recipe, embedded types) |
| `catalog/views.py` | All view functions (~3 000 lines) |
| `catalog/auid.py` | AUID generation and canonicalization |
| `catalog/aggregation.py` | MongoDB aggregation pipelines for cross-collection reads |
| `catalog/rietveld_refinement.py` | GSAS-II Rietveld refinement helpers (~1 800 lines) |
| `catalog/gsas_tools.py` | GSAS-II XRD peak-finding and overlay rendering |
| `catalog/gsas_runtime.py` | Shared GSAS-II runtime helpers |
| `catalog/embeddings.py` | Sentence-transformer embedding generation |
| `catalog/vector_search.py` | Atlas Vector Search similarity queries |
| `catalog/raw_db.py` | Raw file backup DB model and helpers |
| `catalog/data_management_views.py` | Superuser MongoDB collection browser |
| `catalog/search.py` | Catalog search helpers |
| `catalog/utils.py` | XRD pattern plotting and CSV parsing |
| `catalog/signals.py` | Django signals for embedding refresh on write |
| `catalog/upload_archive.py` | Timestamped per-upload snapshot folders (`RAW_UPLOADS_ROOT`) |
| `catalog/archive/` | JSON archive: source of truth. `registry.py` (what/where), `writer.py` (the write choke point), `hooks.py` (signal wiring), `rebuild.py` (export/verify/rebuild/replay) |
| `catalog/canonical.py` | Deterministic JSON + atomic writes, shared by the archive and XRD analysis |
| `loop/settings.py` | Django settings (env-driven via `.env`) |

---

## URL Map

| Group | Routes |
|-------|--------|
| Public browse | `/`, `/browse/`, `/composition/<material_auid>/`, `/recipe/<recipe_id>/`, `/recipe/<id>/trial/<trial_id>/`, `/recipe/<id>/literature/<lit_id>/`, `/material/<auid>/dft/<comp_auid>/` |
| Data entry (auth) | `/add/`, `/add/experiment/`, `/add/literature/`, `/add/computational/` |
| User library | `/account/`, `/account/precursors/`, `/account/protocols/` |
| JSON API | `/api/search-doi/`, `/api/fetch-doi/`, `/api/normalize-composition/`, `/api/precursors/`, `/api/protocols/` |
| Superuser admin | `/data-management/`, `/data-management/<collection>/`, `/data-management/<collection>/<object_id>/` |
| Auth | `/accounts/login/`, `/accounts/signup/`, `/accounts/activate/<uid>/<token>/` |

---

## Access Control

- Every document (Material, Recipe, EmbeddedTrial, EmbeddedLiterature, EmbeddedDFT) has a `visibility_affiliations` list (e.g. `["S4E", "APL"]`).
- `UserAffiliation` (MongoDB) maps each Django user to their allowed affiliations.
- `ApprovedGateMiddleware` (`loop/middleware.py`) redirects unapproved users to `/accounts/awaiting-approval/`.
- Superusers listed in `APPROVED_BYPASS_SUPERUSERS` (settings) skip the affiliation check.

---

## Scientific Computing Notes

- **GSAS-II** must be installed at the path set by the `GSAS2_PATH` environment variable. Entry in `.env.example`. The Dockerfile installs it automatically.
- XRD peak-finding uses `scipy.signal.find_peaks`, `peak_prominences`, `peak_widths`, and `savgol_filter`.
- Rietveld refinement (`rietveld_refinement.py`) is long-running and synchronous — treat it as a slow in-request job.
- CIF (Crystallographic Information File) format is supported for phase definitions.
- Instrument parameters default to Cu Kα lab geometry.

---

## Frontend Conventions

- Bootstrap 5.3.3 loaded from CDN — no npm/build step for CSS framework.
- SCSS compiled server-side via `libsass`; source in `catalog/static/`.
- Vanilla JS, no React/Vue/Angular. `periodic.js` uses ES modules (`export function`).
- Key JS files: `periodic.js` (interactive element selector), `search_filters.js`, `upload_form.js`, `precursors.js`, `protocols.js`.

---

## Coding Conventions

- **Use MongoEngine** documents everywhere — never Django ORM for materials data.
- Write to MongoDB via `.save()` on document instances.
- For complex reads joining materials + recipes + trials, use the aggregation pipelines in `catalog/aggregation.py` rather than multiple separate queries.
- AUID generation and parsing must go through `catalog/auid.py` — never construct them inline.
- JSON API views return `JsonResponse` directly; no serializer framework.
- Affiliation lists on new documents should default to the submitting user's affiliations (`request.user.useraffiliation.affiliations`).

---

## Design Context

UI/design work is governed by two root-level files — read them before building or changing any interface:

- **`PRODUCT.md`** — strategic. Register is **product** (design serves the research work, not a marketing site). Users are consortium materials researchers; the platform is an authoritative, content-addressable catalog.
- **`DESIGN.md`** — visual system (Stitch format + `.impeccable/design.json` sidecar). North Star: **"The Institutional Archive."**

Load-bearing design rules (full detail in `DESIGN.md`):

- **The One Navy Rule** — Hopkins Navy `#002d72` marks authority and action only (headings, primary buttons, active states, AUIDs, S4E affiliation); never a background wash.
- **The Encoding Rule** — affiliation and structure-family hues are a *data encoding*. Never reuse them for buttons, backgrounds, or ornament, and always pair a color with its text label (colorblind-safe).
- **The Call-Number Rule** — every AUID/hash/exact identifier is set in monospace; prose never is.
- **The Line-Not-Shadow Rule** — surfaces are flat, separated by 1px hairlines and tonal washes; shadows only on focus/hover/raised layers.
- Anti-references: no generic SaaS dashboard, no cluttered-legacy-academic look, nothing consumer/playful.

Design tooling is the **impeccable** plugin skill; run `/impeccable` (e.g. `craft`, `critique`, `polish`) for UI work so it reads these files first.
