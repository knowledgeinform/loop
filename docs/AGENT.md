# LOOP Agent Client Guide

Use this document as the operating context for an agent integrating with LOOP.
It is intentionally complete enough to paste into an agent instruction or tool
description. Exact field schemas and the current endpoint contract are in the
OpenAPI document linked below.

This is an API client contract: an external agent needs no LOOP repository,
Docker access, or local server process. Read the deployed base URL from
`LOOP_API_BASE_URL` when configured; otherwise use the base URL below.
`localhost` is valid only when the agent and LOOP run on the same machine.

## Authoritative sources

- API base URL: `{{API_BASE_URL}}`
- Human integration guide: `{{INTEGRATION_GUIDE_URL}}`
- OpenAPI schema: `{{OPENAPI_URL}}`
- Interactive API reference: `{{API_REFERENCE_URL}}`
- API health/version: `{{HEALTH_URL}}`

When this guide conflicts with the OpenAPI schema, follow the OpenAPI schema
for fields, types, and required values. Do not invent endpoints or fields.

## Security and operating rules

1. Never ask a user to paste an API key into a chat, prompt, ticket, source
   file, or notebook output.
2. Read the key only from an execution environment variable such as
   `LOOP_API_KEY`, or from the environment's approved secret mechanism. If no
   key is available, stop and ask the user to configure their own credential;
   do not proceed with a write.
3. Send a key in `X-API-Key: $LOOP_API_KEY` (or `Authorization: Bearer
   $LOOP_API_KEY`). Do not log request headers or raw keys.
4. Begin with read-only requests. Before any write, summarize the intended
   record and request confirmation when the user has not already clearly
   authorized that specific write.
5. Validate a representative record before importing, and use batch
   `dry_run=true` before a batch write. Never use an API write merely to test a
   hypothesis.
6. Use the narrowest scope and a separate named key for every integration.
   Stop using an exposed or retired key immediately and have its owner replace
   it through their normal credential process.
7. Respect `429 Retry-After`; retry only after that interval. Treat `401`,
   `403`, and `422` as actionable errors, not as reasons to guess at data.

## Connection bootstrap

On an explore or integration request, first check connectivity and fetch the
JSON contract. Do this automatically; do not search a local filesystem or try
to start a server unless the user explicitly asks for local development work.

```bash
BASE_URL="${LOOP_API_BASE_URL:-{{API_BASE_URL}}}"
BASE_URL="${BASE_URL%/}"
curl --fail-with-body "$BASE_URL/health/"
curl --fail-with-body "$BASE_URL/openapi.json" -o loop-openapi.json
```

For protected reads, add the environment-provided key header. If the service
cannot be reached, report the exact base URL and connection error; do not
modify project files, Docker configuration, or credentials as a workaround.

## Authentication and scopes

An external agent receives a scoped key from its own secret store. Required
scopes for API operations are:

| Scope | Permission |
|---|---|
| `data:read` | List and retrieve visible materials and records. |
| `data:write` | Create, update, and delete individual experimental, literature, and computational records. |
| `files:write` | Attach an XRD CSV to an experimental record. |
| `imports:write` | Validate and commit batch imports. |

Use this base request shape:

```bash
curl --request GET "{{API_BASE_URL}}materials/" \
  --header "X-API-Key: $LOOP_API_KEY"
```

Successful responses use a `data` envelope. Lists also provide `meta`.
Errors use `application/problem+json` with `title`, `status`, `detail`, and
often a field-level `errors` object. This holds for every response under
`/api/v1/`, including a path this API does not serve, so an HTML body from the
API is always a transport or proxy fault and never an API error.

## Capability map

| Goal | Endpoint | Scope | Agent behavior |
|---|---|---|---|
| Check service availability | `GET /health/`, `GET /version/` | key/session | Authenticate before checking status. |
| Identify current credential | `GET /me/` | key/session | Use only to diagnose authorization. |
| Find materials | `GET /materials/` | `data:read` | Start here; filter before retrieving details. |
| Retrieve one material | `GET /materials/{material_auid}/` | `data:read` | Use a known AUID; never fabricate one. |
| Normalize a composition | `POST /compositions/normalize/` | API permission | Normalize before proposing a material identity. |
| Validate one record | `POST /records/validate/` | write/import permission | No persistence; use before writing. |
| Import records | `POST /imports/` | `imports:write` | Always dry-run first. |
| Create experimental data | `POST /experiments/` | `data:write` | Preserve ordered synthesis steps. |
| Create literature data | `POST /literature/` | `data:write` | Keep reported source metadata and routes separate from inference. |
| Create computational data | `POST /computational/` | `data:write` | Store DFT properties and labelled model output. |
| Manage precursors | `/precursors/` | relevant write permission | Reuse saved precursor data where available. |
| Manage protocols | `/protocols/` | relevant write permission | Use reusable protocol records for repeatable routes. |
| Resolve DOI/CAS context | `/doi/`, `/doi/metadata/`, `/precursors/cas-lookup/` | read/write as documented | Treat Crossref and PubChem failures as external `502` errors. |

All paths above are relative to `{{API_BASE_URL}}`.

## Record lifecycle routes

Use these only after the write checklist below is satisfied. A change or delete
must be explicitly authorized by the user and must identify an existing server
record; never infer a record ID from composition alone.

| Record type | Create/list | Read, update, or delete |
|---|---|---|
| Experimental trial | `GET`, `POST /experiments/` | `GET`, `PATCH`, `DELETE /recipes/{recipe_auid}/trials/{trial_id}/` |
| Literature record | `GET`, `POST /literature/` | `GET`, `PATCH`, `DELETE /recipes/{recipe_auid}/literature/{lit_id}/` |
| Computational record | `GET`, `POST /computational/` | `GET`, `PATCH`, `DELETE /materials/{material_auid}/computations/{comp_auid}/` |
| Precursor | `GET`, `POST /precursors/` | `GET`, `PATCH`, `DELETE /precursors/{id}/` |
| Reusable protocol | `GET`, `POST /protocols/` | `GET`, `PATCH`, `DELETE /protocols/{id}/` |

Use the identifiers returned by LOOP. For every `PATCH`, send only the fields
the user intends to change, retain source provenance, and validate any changed
synthesis data first. For every `DELETE`, state the target identifier and wait
for explicit confirmation at the action point.

## Read workflow

1. Check `GET /health/` if connectivity is uncertain.
2. Use `GET /materials/` with known filters such as `elements=Ti,Zr,O` and
   `structure_family=fluorite`.
3. Inspect the returned AUIDs and select an actual result.
4. Use `GET /materials/{material_auid}/` for the complete visible record.
5. Report data provenance accurately: experimental, literature, and
   computational records are distinct sources and should not be presented as
   equivalent evidence.

Example:

```bash
curl --get "{{API_BASE_URL}}materials/" \
  --data-urlencode "elements=Ti,Zr,O" \
  --data-urlencode "structure_family=fluorite" \
  --header "X-API-Key: $LOOP_API_KEY"
```

## Composition and synthesis-route workflow

Before creating a material-associated record, normalize the composition with
`POST /compositions/normalize/` when the composition is not already canonical.
Use the returned canonical elements and prospective material AUID; do not
derive AUIDs locally.

An experimental synthesis route is an ordered `synthesis_steps` array. Keep
the sequence faithful to the experiment. Supported step types include:

`weighing`, `ball_milling`, `mixing`, `pelletizing`, `heat_treatment`,
`annealing`, `arc_melting`, `quenching`, `cooling`, `grinding`,
`xrd_measurement`, `other`, `unknown`, and `na`.

For detailed ball milling, record fields supported by the schema such as
`milling_time_hours`, `milling_rpm`, `ball_powder_ratio`, and `atmosphere`.
For thermal operations, use fields such as `max_temp_c`, `ramp_rate_c_min`,
`hold_time_hours`, and `atmosphere` when known. Do not silently fill unknown
conditions with assumed values.

Example experimental payload:

```json
{
  "elements": {"Ti": 1, "Zr": 1, "O": 4},
  "structure_family": "fluorite",
  "phase_status": "single_phase",
  "synthesis_steps": [
    {"step_type": "weighing"},
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
}
```

Validate this through `POST /records/validate/` first. Only after successful
validation and user authorization, submit it to `POST /experiments/`.

For an XRD upload, submit multipart form data to `POST /experiments/` with a
`record` JSON part and a `csv_file` part. Require both `data:write` and
`files:write`. Never claim a CSV was attached unless the server confirms it.

## Literature, computational, and model-output rules

- Literature records belong in `POST /literature/`. Preserve DOI, citation
  metadata, reported synthesis outcome, and reported synthesis steps. Use DOI
  helpers to look up context, but keep the source attribution.
- Computational records belong in `POST /computational/`. Record DFT source,
  method, functional, and physical properties with their supplied units.
- `ml_predictions` and `extended_data` can retain structured model output.
  Model output must be labelled as a prediction, including transition
  temperature or a proposed synthesis route. Do not represent it as an
  experimental outcome, literature fact, or validated production model.
- LOOP currently stores model output; it does not make an unvalidated model
  into a public prediction service. Do not imply benchmarked accuracy,
  synthesizability, or causality unless supplied by validated data and the
  user’s approved model workflow.

## Batch import workflow

`POST /imports/` accepts a JSON body with `record_type`, `records`, and
optional `dry_run`, or multipart upload with `record_type` and a `.json`,
`.jsonl`, or `.ndjson` file.

1. Check the OpenAPI schema for the selected `record_type`.
2. Run `dry_run=true`.
3. Report each row error to the user; correct only supported fields.
4. Obtain clear approval to commit the corrected batch.
5. Repeat with `dry_run=false`.

A mixed batch can return `207 Multi-Status`; an entirely invalid batch returns
`422`. Do not retry failed rows unchanged.

## Error and rate-limit handling

| Status | Meaning | Correct response |
|---|---|---|
| `400` | Request cannot be accepted | Correct syntax or unsupported input. |
| `401` | Missing, invalid, expired, or revoked credential | Stop; obtain a valid environment-provided key. |
| `403` | Missing approval, ownership, or scope | Stop; request the minimum necessary access. |
| `404` | No such record, or no such endpoint | Read `detail`. With `errors.code == "unknown_endpoint"` the path is not served: follow `errors.endpoint` if present, otherwise read `/api/v1/openapi.json`. Never retry the same path. |
| `422` | Record validation failed | Read `errors`, correct source data, validate again. |
| `429` | Rate limit reached | Wait for `Retry-After`, then retry with backoff. |
| `502` | External DOI/CAS provider failed | Explain upstream failure; do not fabricate metadata. |

Default limits are enforced by both source IP and authenticated identity. Do
not parallelize requests aggressively, and cache stable read results locally
when appropriate. Current defaults are 60 anonymous requests/minute per IP,
1,200 authenticated requests/minute per IP, 600 requests/minute per API key,
and 600 requests/minute per session user. `Retry-After` is authoritative at
runtime.

## Boundaries

- Work only with documented public API responses and identifiers returned by
  LOOP. Do not infer internal storage, administration, or deployment details.
- Existing material AUIDs remain valid. Do not create substitute identifiers
  client-side.
- A public API version change is the point to reassess client behavior. Do not
  rely on undocumented response fields or normalization side effects.

## Write checklist

Before a state-changing request, verify all of the following:

- The user explicitly authorized this record or import.
- The agent has validated the payload or dry-run batch.
- Composition, units, provenance, and synthesis-step order reflect supplied
  data; unknowns remain unknown.
- The requested key has the least-required scope.
- The agent can describe exactly what will be written and where.

After a write, return the server’s identifiers and status. If a request fails,
return the server-provided field errors without exposing credentials.
