# LOOP API v1

LOOP exposes the same experimental, literature, and computational write paths
used by the website's **Add Data** pages. The API is versioned under
`/api/v1/`; incompatible changes belong in a future major version.

Use the deployed API base URL supplied by your LOOP administrator. An external
client should not assume a host name or path prefix.

For a human-oriented walkthrough of reading materials, recording a detailed
synthesis route, attaching XRD data, validating imports, and operating in
production, use the rendered Integration Guide at `/developers/guide/`.
This Markdown document is the compact reference for automation and agents.

From the documentation site, use the linked OpenAPI JSON schema, API reference,
health endpoint, and integration guide. API routes require an active, approved
account. Those links are generated for the current deployment.

## Authentication

All API operations accept an API key in either header form:

```http
X-API-Key: loop_<prefix>_<secret>
```

```http
Authorization: Bearer loop_<prefix>_<secret>
```

Get a scoped API key from the visible **API Keys** page. Keep it only in an
environment variable or secret manager; do not place it in a chat, source file,
or shared notebook.

Key scopes are:

| Scope | Allows |
|---|---|
| `data:read` | List and retrieve catalog records |
| `data:write` | Create, update, and delete individual records |
| `files:write` | Attach an XRD CSV to an experimental write |
| `imports:write` | Validate or commit batch imports |

## Rate limits

Rate limits apply to both the source IP and authenticated identity. A rejected
request returns `429` and a `Retry-After` header. Treat that header as the
authoritative retry interval.

Do not parallelize requests aggressively. Cache stable read results locally
when appropriate, and retry only after the server-provided interval.

## Response conventions

Successful responses use a `data` envelope and list responses add `meta`:

```json
{
  "data": [],
  "meta": {"limit": 50, "offset": 0, "returned": 0, "total": 0, "has_more": false, "filters_applied": {}}
}
```

`filters_applied` reports the filters that actually narrowed the result set,
with the values the server used. A filter you sent that is absent here had no
effect — an empty value, for example.

`total` is how many rows match your filters before `limit` and `offset` are
applied, counting only records your account is allowed to see. Compare it with
`returned` to tell a complete result set from a truncated one. The two
sub-resource lists that never page — `/materials/{auid}/recipes/` and
`/recipes/{auid}/trials/` — report `total` equal to `returned` and `has_more`
false, and omit `limit` and `offset` because they have no page window. Every
list response carries `filters_applied`, paged or not, so reading it is always
safe.

## Pagination

Every list endpoint pages with `limit` (page size, default 50, clamped to 100)
and `offset` (rows to skip, default 0). `offset` is applied after filtering and
after visibility, so it composes with `structure_family`, `elements`, `doi` and
the `*_auid` filters. Walk a result set by advancing `offset` by `limit` for as
long as `meta.has_more` is `true`:

```
GET /api/v1/materials/?structure_family=pyrochlore&limit=100
GET /api/v1/materials/?structure_family=pyrochlore&limit=100&offset=100
```

A `limit` or `offset` that is not an integer, a `limit` below 1, and any
negative `offset` are rejected with a 400 rather than replaced by a default. A
`limit` above 100 is the one value that is adjusted rather than refused: it is
served at 100 and `meta.limit` says so. `page`, `cursor`, `skip` and `after` are
not implemented and are rejected as unsupported query parameters.

The lists that do not page — `/materials/{auid}/recipes/`, `/recipes/{auid}/trials/`
and `/api-keys/` — reject `limit` and `offset` as unsupported rather than
accepting them and answering with the whole set anyway.

List endpoints reject query parameters they cannot honor instead of ignoring
them, so a mistyped filter never returns a silently unfiltered population:

```json
{
  "type": "about:blank",
  "title": "Bad Request",
  "status": 400,
  "detail": "Unsupported query parameter(s): element_symbols. This endpoint would have ignored them.",
  "instance": "/api/v1/materials/",
  "errors": {
    "code": "unsupported_query_parameters",
    "unsupported": ["element_symbols"],
    "supported": ["elements", "format", "limit", "offset", "structure_family"]
  }
}
```

Errors use `application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "Bad Request",
  "status": 400,
  "detail": "One or more fields could not be accepted.",
  "instance": "/api/v1/experiments/",
  "errors": {"phase_status": ["This field is required."]}
}
```

Every 404 under `/api/v1/` is problem+json too, including a path this API does
not route at all — no request under `/api/v1/` answers with an HTML error page,
so a JSON client never has to distinguish "no such endpoint" from a parse
failure. This holds whatever `Accept` header you send: the browsable HTML
renderer serves successful responses only, and errors are always JSON. (The one
exception is a server running with `DEBUG=True`, where Django renders its own
technical 404 before this API sees it. Deployments do not.) Where another
endpoint serves what you asked for, the error names it:

```json
{
  "type": "about:blank",
  "title": "Not Found",
  "status": 404,
  "detail": "No API endpoint at /api/v1/trials/. Trial records are served by /api/v1/experiments/; a single trial is /api/v1/recipes/{recipe_auid}/trials/{trial_id}/.",
  "instance": "/api/v1/trials/",
  "errors": {"code": "unknown_endpoint", "endpoint": "/api/v1/experiments/"}
}
```

`errors.endpoint` is present only when one endpoint replaces the one you asked
for. Otherwise `detail` points at `/api/v1/openapi.json`, which is the full list
of routed paths.

## Endpoint map

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health/`, `/version/` | Authenticated service status |
| `GET` | `/me/` | Current API identity |
| `POST` | `/records/validate/` | Normalize one record without writing |
| `POST` | `/imports/` | Validate or import up to 100 records |
| `POST` | `/compositions/normalize/` | Normalize composition and calculate material AUID |
| `GET` | `/doi/`, `/doi/metadata/` | Query LOOP DOI mappings or Crossref metadata |
| `GET`, `POST` | `/precursors/` | List or create saved precursor entries |
| `GET`, `PATCH`, `DELETE` | `/precursors/{id}/` | Precursor lifecycle |
| `GET` | `/precursors/cas-lookup/` | Resolve CAS metadata through PubChem |
| `GET`, `POST` | `/protocols/` | List or create reusable synthesis protocols |
| `GET`, `PATCH`, `DELETE` | `/protocols/{id}/` | Protocol lifecycle |
| `GET`, `POST` | `/experiments/` | List or create experimental trials |
| `GET`, `PATCH`, `DELETE` | `/recipes/{recipe_auid}/trials/{trial_id}/` | Experimental lifecycle |
| `GET`, `POST` | `/literature/` | List or create literature records |
| `GET`, `PATCH`, `DELETE` | `/recipes/{recipe_auid}/literature/{lit_id}/` | Literature lifecycle |
| `GET`, `POST` | `/computational/` | List or create computational records |
| `GET`, `PATCH`, `DELETE` | `/materials/{material_auid}/computations/{comp_auid}/` | Computational lifecycle |
| `GET` | `/materials/` | Filter and list visible materials |
| `GET` | `/materials/{material_auid}/` | Retrieve one material |
| `GET` | `/materials/{material_auid}/recipes/` | List visible recipes for a material |
| `GET` | `/recipes/{recipe_auid}/`, `/recipes/{recipe_auid}/trials/` | Retrieve a recipe or list its trials |
| `GET` | `/materials/{material_auid}/download/`, `/recipes/{recipe_auid}/download/` | Export visible material or recipe data as JSON |
| `GET` | `/recipes/{recipe_auid}/trials/{trial_id}/download/` | Export one visible trial as JSON |
| `GET` | `/recipes/{recipe_auid}/trials/{trial_id}/xrd/` | Download the original XRD CSV when attached |
| `GET` | `/recipes/{recipe_auid}/trials/{trial_id}/xrd/metadata/`, `/xrd/preview/` | Read parsed XRD metadata or generate a preview |

Paths in this table are relative to `/api/v1`.

There is no `/trials/` and no `/recipes/` collection. Trials are `/experiments/`
— one endpoint, one name. Recipes are reachable only per material, through
`/materials/{material_auid}/recipes/`; a cross-material recipe list does not
exist yet. Both paths 404 with a body naming the alternative.

## Add Data helpers

The API also covers the support operations used by the Add Data forms:

- composition normalization returns the canonical elements and prospective
  `material_auid`, plus whether that material is visible to the caller;
- DOI lookup checks LOOP's local DOI mapping, while DOI metadata fetch uses the
  same Crossref integration as the literature form;
- CAS lookup uses the same PubChem integration as the precursor form;
- precursor and protocol CRUD preserves the website's affiliation visibility
  and uploader-only update/delete rules.

Crossref and PubChem availability is external to LOOP. Upstream failures are
returned as problem JSON with a `502` status rather than written to the catalog.

## Experimental data

The synthesis route is an ordered `synthesis_steps` array. Supported step types
include `weighing`, `ball_milling`, `mixing`, `pelletizing`, `heat_treatment`,
`annealing`, `arc_melting`, `quenching`, `cooling`, `grinding`,
`xrd_measurement`, `other`, `unknown`, and `na`. Swagger shows the complete
request contract; unknown fields within a step are discarded by normalization.

```bash
curl -H "X-API-Key: $LOOP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "elements": {"Ti": 1, "Zr": 1, "O": 4},
    "structure_family": "fluorite",
    "phase_status": "single_phase",
    "synthesis_steps": [
      {
        "step_type": "weighing",
        "precursors_list": [
          {"name": "TiO2", "purity": "99.99%", "supplier": "Example"}
        ]
      },
      {
        "step_type": "ball_milling",
        "milling_time_hours": 12,
        "milling_rpm": 300,
        "ball_powder_ratio": "10:1",
        "atmosphere": "air"
      },
      {
        "step_type": "heat_treatment",
        "max_temp_c": 1400,
        "ramp_rate_c_min": 5,
        "hold_time_hours": 6,
        "atmosphere": "air"
      }
    ]
  }' \
  "$LOOP_API_BASE_URL/experiments/"
```

To attach XRD data, send multipart form data. `record` contains the same JSON
object and `csv_file` contains the file. The key needs both `data:write` and
`files:write`.

```bash
curl -H "X-API-Key: $LOOP_API_KEY" \
  -F 'record={"elements":{"Ti":1,"Zr":1,"O":4},"structure_family":"fluorite","phase_status":"single_phase"}' \
  -F 'csv_file=@pattern.csv;type=text/csv' \
  "$LOOP_API_BASE_URL/experiments/"
```

## Literature data

```bash
curl -H "X-API-Key: $LOOP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "doi": "10.1000/example",
    "synthesis_successful": true,
    "elements": {"Er": 2, "Zr": 2, "O": 7},
    "structure_family": "pyrochlore",
    "title": "Example synthesis",
    "authors": ["A. Researcher"],
    "year": 2026,
    "synthesis_steps": [
      {"step_type": "heat_treatment", "max_temp_c": 1450, "hold_time_hours": 8}
    ]
  }' \
  "$LOOP_API_BASE_URL/literature/"
```

## Computational and ML data

`ml_predictions` and `extended_data` accept JSON objects, allowing model output
to be retained alongside DFT properties without pretending an unvalidated model
is already a production prediction endpoint.

```bash
curl -H "X-API-Key: $LOOP_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "elements": {"Dy": 2, "Ti": 2, "O": 7},
    "structure_family": "pyrochlore",
    "dft_source": "S4E",
    "calculation_method": "DFT",
    "functional": "PBE",
    "formation_energy_ev": -2.4,
    "hull_distance_ev": 0.03,
    "bandgap_ev": 1.7,
    "ml_predictions": {"transition_temperature_c": 1280}
  }' \
  "$LOOP_API_BASE_URL/computational/"
```

## Validation and batch imports

Validate a single record without writing:

```json
{
  "record_type": "experiment",
  "record": {
    "elements": {"La": 1, "Mn": 1, "O": 3},
    "structure_family": "perovskite",
    "phase_status": "single_phase"
  }
}
```

`POST /imports/` accepts either a JSON body with `record_type`, `records`, and
optional `dry_run`, or multipart form data with `record_type` and a `.json`,
`.jsonl`, or `.ndjson` file. Each row is independent. A mixed import returns
`207 Multi-Status`; an entirely invalid import returns `422`.

```bash
curl -H "X-API-Key: $LOOP_API_KEY" \
  -F 'record_type=experiment' \
  -F 'dry_run=false' \
  -F 'file=@experiments.jsonl;type=application/x-ndjson' \
  "$LOOP_API_BASE_URL/imports/"
```

## Python example

```python
import os
import requests

base_url = "https://loop.example.edu/api/v1"
headers = {"X-API-Key": os.environ["LOOP_API_KEY"]}

response = requests.get(
    f"{base_url}/materials/",
    headers=headers,
    params={"elements": "Ti,Zr,O", "structure_family": "fluorite"},
    timeout=30,
)
response.raise_for_status()
materials = response.json()["data"]
```

Never hard-code a key into a notebook that will be shared. Store it in an
environment variable or a secret manager and revoke it when the notebook is
retired.

## Client use

Client integrations should rely only on the published versioned API, OpenAPI
schema, and documented responses. Verify the health endpoint through the
provided API base URL and begin with a read-only request before attempting a
write.
