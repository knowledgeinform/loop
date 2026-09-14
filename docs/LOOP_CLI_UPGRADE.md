# LOOP CLI and lab search upgrade

## Implemented locally

The separate installation at `/Volumes/Drive/urdp_materials/loop-cli` now uses
`S4E/loop_mcp/client.py` for shared catalog reads. Both catalog and prediction
tools distinguish live reads from explicitly selected historical snapshots.
Pagination follows `meta.has_more`; legacy pagination, transport failures and
missing configuration fail explicitly. Material reports retrieve visible recipes
and trials from the selected source. Cation fractions remain aligned when sorted,
and hull inputs preserve recorded oxygen stoichiometry. EFA lookup results are
unavailable when the cation-set lookup cannot establish the requested ratios;
ChemScreen's equimolar assumption is separately labelled.

The portable launcher derives server paths, selects the platform binary, and
preconfigures the private Rockfish deployment from `.loop/deployment.json`.
The initial profile retains `https://llm.boctor.dev/v1`, model ID `laguna`, and
the existing Anthropic protocol. `loop doctor` checks local requirements and
service access; `loop doctor --offline` makes no claim about live reachability.
Setup dependencies are pinned. Credential-bearing instructions and private
environment files are excluded from transfer bundles; optional branding plugins
are omitted because the bundled runtime already contains the branding.

The initial Python model probe was rejected by Cloudflare with HTTP 403 / error
1010, while curl using the identical `~/.llmkey` returned HTTP 200. Requests now
identify themselves as `LOOP-CLI/1.0`; the installed model check succeeds and
confirms the served model ID `laguna`. No credential replacement was needed.
Credentials from older bundles still require rotation by their owner.

## Semantic search server change

`GET /api/v1/search/` exposes the existing Atlas vector indexes to approved
accounts with `data:read` scope. It searches material, recipe and computation
descriptions, returning material IDs, relevance scores and matched record IDs.
Exact element/structure filters and source-record visibility apply before a hit
contributes to ranking. Search scores are not synthesis probabilities.

This is approximate retrieval over a bounded candidate window. It does not
provide exhaustive enumeration, exact stoichiometric similarity, PDF full-text
retrieval or XRD-pattern similarity. Missing embedding/index infrastructure
returns 503; exact chemistry search remains available at `/materials/`.

The server code is changed in this checkout and has not been deployed. The
CLI's `loop_semantic_search` tool requires deployment of the endpoint and ready
embeddings/indexes. See `docs/API.md` for the contract and administration commands.

## Verification

- 21 CLI tests: pagination, source isolation, current report reads, cation/oxygen
  ratios, private model defaults, relocation, credential-free packaging, and
  identified model probes using the existing key file.
- 25 LOOP server/search tests: semantic authentication and read scope, exact
  filters, private material/recipe/computation exclusion, missing indexes, and
  existing exact-search helpers.
- Both MCP servers started over stdio; real snapshot catalog search and ranking
  returned nine rows labelled as snapshot. The catalog now exposes 11 tools.
- The actual snapshot report reader retrieved the expected recipe/trial records.

These checks validate integration behavior, not scientific prediction accuracy.
The separate CLI update preserved originals in a protected backup directory
under `/private/tmp/loop-cli-backup-rv0rtdm3`.

## Highest-value next lab workflows

1. **Comparable experiments:** retrieve successful, failed and unresolved trials;
   compare chemistry, structure, atmosphere, temperature and dwell time. Show
   confounders and source records, rather than implying a causal effect.
2. **Recipe comparison and preparation:** side-by-side ordered steps, measured
   quantities and units, with a reviewable batch worksheet. Integrate available
   precursors only after matching the lab's inventory and purity information.
3. **XRD evidence review:** check actual pattern availability, submit the existing
   analysis job, and link fitted results and artifacts to the exact trial.
   Separate recorded phase calls from independently supported conclusions.
4. **Prospective shortlists:** freeze compositions, model versions, expected
   outcomes and selection reasons before lab work. Join later measurements to
   that immutable record to assess whether recommendations helped.
5. **Literature retrieval with citations:** index permission-appropriate article
   passages with DOI, page and section identifiers, then return quotations and
   links alongside extracted conditions. Validate retrieval on lab questions
   before treating generated summaries as evidence.

Start with comparable experiments and XRD evidence review: both use data and
server operations LOOP already has. Evaluate with real lab tasks, measuring
retrieval coverage, factual accuracy, traceability and time saved.
