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

### Semantic retrieval

`GET /api/v1/search/?q=quenched+nickel+oxide&elements=Ni,O&structure_family=rocksalt&limit=20`
searches the existing material, recipe, and computation text embeddings. It requires
an approved account and `data:read` scope. `q` is required (1–2000 characters),
`limit` is 1–100, and `elements` / `structure_family` are optional exact filters.
Unrecognised parameters, including `offset`, are rejected.

Each result includes `material_auid`, `similarity_score`, and `matched_records`
identifying the visible material, recipe, or computation that matched. Hidden
source records cannot contribute scores. Similarity is retrieval relevance, not
synthesis probability or proof that two materials are chemically equivalent.

`meta` reports the embedding model, threshold, candidate window, and applied
filters. Semantic retrieval is approximate and bounded, so `complete` is always
false: an empty answer is not proof that the full catalog contains no match.
Missing embedding infrastructure or queryable vector indexes returns **503**;
use `/materials/` for exact chemistry lookup. Administrators can prepare the
existing indexes with `mongo_admin ensure-vector-indexes` and populate vectors
with `backfill_embeddings`.

### Exact list pagination

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

## Python examples

The Python examples use only the standard library — no `pip install`, no
virtual environment — and read credentials from the environment. They are the
files in `catalog/api/samples/`, executed end-to-end against a live server by
`catalog/tests/test_api_python_samples.py`, so what appears here is what runs.

Each example is a single self-contained file: save it, set the two environment
variables, and run it. There is no helper module to fetch.

### Read materials

```python
"""List LOOP materials matching a composition and structure family.

Self-contained: save this one file and run it. Nothing to install — it uses
only the Python standard library.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    python read_materials.py

Needs a key with the `data:read` scope.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")


def call(method, path, params=None, body=None, content_type=None, raw=False):
    """Call the LOOP API and return the decoded JSON body."""
    key = os.environ.get("LOOP_API_KEY")
    if not key:
        raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

    url = f"{BASE_URL}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "X-API-Key": key,
        "Accept": "application/json",
        # urllib sends "Python-urllib/3.x" by default, which bot protection in
        # front of a LOOP deployment rejects before the request reaches the API.
        "User-Agent": "loop-api-client/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        raise SystemExit(f"LOOP returned {error.code}: {_detail(error)}")
    if raw:
        return payload
    return json.loads(payload) if payload else {}


def _detail(error):
    """Pull the human-readable message out of LOOP's problem+json body."""
    body = error.read().decode("utf-8", "replace")
    try:
        return json.loads(body).get("detail", body[:200])
    except ValueError:
        return body[:200]


# `elements` matches materials containing all of the listed symbols. A filter
# LOOP cannot honor is rejected with a 400 rather than silently ignored, so a
# typo never returns an unfiltered population.
response = call(
    "GET",
    "/materials/",
    params={"elements": "Ho,Ti,O", "structure_family": "pyrochlore", "limit": 5},
)

meta = response["meta"]
print(f"Showing {meta['returned']} of {meta['total']} visible materials")
print(f"Filters LOOP applied: {meta['filters_applied']}")

for material in response["data"]:
    # AUIDs are content-derived: the same composition and structure family
    # always resolve to the same material_auid.
    print(material["material_auid"], material["structure_family"])
```

### Post an XRD pattern

`upload_xrd_experiment.py` validates the record, uploads the pattern and the
record in one multipart request, then reads back what LOOP parsed:

```python
"""Post an XRD pattern to LOOP as a new experimental trial.

Self-contained: save this one file and run it. Nothing to install — it uses
only the Python standard library.

    export LOOP_API_KEY='loop_...'
    export LOOP_API_BASE_URL='https://loop.example.edu/api/v1'
    export LOOP_XRD_CSV=pattern.csv
    python upload_xrd_experiment.py

Needs a key with both `data:write` and `files:write`.

The CSV is two columns — 2-theta angle and intensity — with a header row:

    Angle,Intensity
    10.00,120
    10.02,131
"""

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE_URL = os.environ.get(
    "LOOP_API_BASE_URL", "https://loop.example.edu/api/v1"
).rstrip("/")

# The trial LOOP will create. `raw_data_type` labels the attached file;
# `phase_status` is the outcome you observed, not a prediction.
RECORD = {
    "elements": {"Gd": 2, "Zr": 2, "O": 7},
    "structure_family": "pyrochlore",
    "phase_status": "single_phase",
    "raw_data_type": "xrd",
    "comments": "Uploaded through the documented LOOP API v1 Python example.",
    "synthesis_steps": [
        {
            "step_type": "ball_milling",
            "milling_time_hours": 8,
            "milling_rpm": 250,
            "ball_powder_ratio": "10:1",
            "atmosphere": "air",
        },
        {
            "step_type": "heat_treatment",
            "max_temp_c": 1500,
            "hold_time_hours": 6,
            "atmosphere": "air",
        },
        {"step_type": "xrd_measurement"},
    ],
}


def call(method, path, params=None, body=None, content_type=None, raw=False):
    """Call the LOOP API and return the decoded JSON body."""
    key = os.environ.get("LOOP_API_KEY")
    if not key:
        raise SystemExit("Set LOOP_API_KEY to a scoped LOOP API key.")

    url = f"{BASE_URL}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    headers = {
        "X-API-Key": key,
        "Accept": "application/json",
        # urllib sends "Python-urllib/3.x" by default, which bot protection in
        # front of a LOOP deployment rejects before the request reaches the API.
        "User-Agent": "loop-api-client/1.0",
    }
    if content_type:
        headers["Content-Type"] = content_type

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        detail = _detail(error)
        # LOOP deduplicates raw data by SHA-256, so re-running this script with
        # the same pattern is refused rather than quietly creating a twin.
        if error.code == 409:
            raise SystemExit(f"Already in LOOP: {detail}")
        raise SystemExit(f"LOOP returned {error.code}: {detail}")
    if raw:
        return payload
    return json.loads(payload) if payload else {}


def _detail(error):
    """Pull the human-readable message out of LOOP's problem+json body."""
    body = error.read().decode("utf-8", "replace")
    try:
        problem = json.loads(body)
    except ValueError:
        return body[:200]
    # A 422 names the fields that failed; show them rather than the summary.
    if problem.get("errors"):
        return json.dumps(problem["errors"])
    return problem.get("detail", body[:200])


def encode_multipart(fields, files):
    """Encode form fields and file parts as multipart/form-data.

    The standard library has no multipart encoder, so uploading a pattern
    without third-party packages means building the body yourself. Every line
    break in the envelope must be CRLF.
    """
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += b"--%s\r\n" % boundary.encode()
        body += b'Content-Disposition: form-data; name="%s"\r\n\r\n' % name.encode()
        body += b"%s\r\n" % str(value).encode("utf-8")
    for name, (filename, content) in files.items():
        guessed = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        body += b"--%s\r\n" % boundary.encode()
        body += (
            b'Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
            % (name.encode(), filename.encode())
        )
        body += b"Content-Type: %s\r\n\r\n" % guessed.encode()
        body += content + b"\r\n"
    body += b"--%s--\r\n" % boundary.encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


csv_path = Path(os.environ.get("LOOP_XRD_CSV", "pattern.csv"))
if not csv_path.is_file():
    raise SystemExit(f"No XRD CSV at {csv_path}. Set LOOP_XRD_CSV.")

# 1. Validate first. Nothing is written, so a malformed record costs one cheap
#    request instead of a half-finished upload.
check = call(
    "POST",
    "/records/validate/",
    body=json.dumps({"record_type": "experiment", "record": RECORD}).encode(),
    content_type="application/json",
)
if not check["data"]["accepted"]:
    print(json.dumps(check["data"]["errors"], indent=2))
    raise SystemExit("Fix the record before uploading the pattern.")

# 2. Upload. A multipart request carries the record as a JSON string in the
#    `record` field and the pattern in `csv_file`. This is the one write that
#    needs `files:write` on top of `data:write`.
body, content_type = encode_multipart(
    {"record": json.dumps(RECORD)},
    {"csv_file": (csv_path.name, csv_path.read_bytes())},
)
identity = call("POST", "/experiments/", body=body, content_type=content_type)["data"]
print("Stored trial:")
print(json.dumps(identity, indent=2))

# 3. Read the pattern back. LOOP parses the CSV on upload, so this confirms it
#    understood the file rather than merely accepting it.
metadata = call(
    "GET",
    f"/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/xrd/metadata/",
)
print("Parsed XRD metadata:")
print(json.dumps(metadata["data"]["metadata"], indent=2, default=str))
```

Run it with:

```bash
export LOOP_API_KEY='loop_...'
export LOOP_API_BASE_URL="$LOOP_API_BASE_URL"
export LOOP_XRD_CSV=pattern.csv
python upload_xrd_experiment.py
```

Raw data is deduplicated by SHA-256: re-sending a pattern that already exists
on another trial is refused with a `409`, as is an identical trial on the same
recipe. Treat a `409` as "already recorded" rather than as a failure to retry.

### Other samples

`catalog/api/samples/` also holds `paginate_materials.py`,
`validate_record.py`, `create_experiment.py`, `upload_xrd_minimal.py`,
`download_xrd.py`, and `batch_import.py`. Each is downloadable at
`/developers/samples/<name>.py`. The rendered integration guide shows
each one with its curl equivalent beside it.

Never hard-code a key into a notebook that will be shared. Store it in an
environment variable or a secret manager and revoke it when the notebook is
retired.

## Client use

Client integrations should rely only on the published versioned API, OpenAPI
schema, and documented responses. Verify the health endpoint through the
provided API base URL and begin with a read-only request before attempting a
write.
