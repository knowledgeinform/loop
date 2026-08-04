# XRD Analysis — Deployment Gaps

Status of `feat/dev-environment` as of commit `b6b8394`. Every claim below was verified by
reading the code on this branch and by running the commands quoted in the appendix. Where a
claim could not be verified, it says so.

**All file:line citations refer to committed `HEAD` (`b6b8394`), not the working tree.** Several
files were being modified by concurrent work while this was written; the cited lines were
re-checked against `HEAD` after those edits landed.

## Summary

The pipeline is complete and it runs. What is missing is **not** plumbing — the trigger is fully
wired from a button in the trial page through to the queue. What is missing is the two things the
science needs to produce an answer, plus a one-line settings binding that starts the consumer:

| # | Gap | Blocks | Fix size |
|---|-----|--------|----------|
| 1 | Reference phase library holds 2 CIFs, both NaCl | Any real sample | Data curation (weeks) |
| 2 | GSAS-II not installed | **Candidate screening *and* refinement** | Ops (days) |
| 3 | `settings.XRD_ANALYSIS_WORKER` is never defined, so the queue is never drained | Every submission | One line |
| 4 | Trial metadata lacks wavelength / instrument profile | Refinement, even once 1–3 are fixed | Ingest + curation |

Gaps 1 and 2 are **independent**. Each alone is sufficient to force `phase_state: "unresolved"`.
Fixing the reference library without installing GSAS-II changes nothing.

---

## 1. What must exist before a real S4E sample gets a useful answer

### 1.1 The reference phase library

`catalog/xrd_analysis/candidates.py:37-38` fixes the location:

```python
REFERENCE_PHASES_DIR = Path(__file__).resolve().parent.parent / "reference_phases"
REFERENCE_MANIFEST_PATH = REFERENCE_PHASES_DIR / "manifest.json"
```

**The code does expect a manifest, and the manifest is the source of truth — not the directory
listing.** `load_reference_phase_snapshot()` (`candidates.py:72`) reads `manifest.json` and never
globs the directory. A CIF dropped into `catalog/reference_phases/` without a manifest entry is
invisible to the pipeline.

Required manifest shape (from `load_reference_phase_snapshot`, `candidates.py:76-95`):

```json
{
  "snapshot_version": "loop-reference-phases-v1",
  "notes": ["free-form provenance strings"],
  "entries": [
    {
      "candidate_identifier": "curated_nacl_rocksalt",   // required, unique
      "relative_cif_path": "NaCl_rocksalt.cif",          // required, relative to manifest.json
      "sha256": "172d121d…",                             // required, of the CIF bytes
      "formula": "NaCl",
      "element_set": ["Cl", "Na"],
      "space_group": "F m -3 m",
      "structure_family": "rocksalt",
      "source": "curated_reference",
      "source_identifier": "local:NaCl_rocksalt",
      "notes": ["…"],
      "enabled": true
    }
  ]
}
```

`candidate_identifier`, `relative_cif_path` and `sha256` are read with `raw_entry[...]` — a
missing key raises `KeyError`, not a warning. Everything else is `.get()` with a default.

**Hash validation is enforced.** `validate_reference_phase_snapshot()` (`candidates.py:114-128`)
re-hashes every `enabled` CIF and raises `ReferenceSnapshotMismatchError` on any mismatch or
missing file. `build_ranked_phase_candidates` catches that and returns
`status="candidate generation failed"` for the whole run. So editing a CIF without updating its
`sha256` takes the entire feature offline for every sample, not just that phase. Any curation
tooling must rewrite hashes in the same commit as the CIFs.

The manifest also feeds a `snapshot_hash` (SHA-256 over the canonicalized entry list,
`candidates.py:100-102`) that is stamped into every persisted analysis for provenance. Changing
the library changes the hash, which is the intended behaviour — it means old results are
attributable to the library that produced them.

**Current contents — the actual blocker:**

```
snapshot_version = loop-reference-phases-v1
entries          = 2
  - curated_nacl_rocksalt:      NaCl,  elements ('Cl','Na'), F m -3 m
  - curated_hypothetical_nacl3: NaCl3, elements ('Cl','Na'), P m -3 m
```

Both are chemistry fixtures for the deterministic candidate tests. `hypothetical_NaCl3_1to3.cif`
is, by its own manifest note, "a synthetic local contrast phase" — it is not a real structure.
There is no production reference library in this repo, and `docs/xrd_analysis/validation_plan.md:966`
already anticipates one ("the curated production CIF directory").

Screening excludes any candidate containing an element absent from the sample
(`candidates.py:474-476`). For the Mg-Mn-Ni-Zn-Cu-O sample this eliminates both entries and the
stage returns `no_chemically_compatible_candidates`, which cascades to `unresolved` /
`evidence 0.143` / `selected model "none"` — exactly the observed run.

**How many phases are needed.** The config gives the working numbers
(`schemas.py:1031-1037`, `1060-1061`):

- `maximum_candidates_before_simulation = 24` — chemistry screening may pass up to 24 forward
- `maximum_simulated_candidates = 12` — at most 12 get a screening simulation
- `final_top_k = 6` — the shortlist
- `maximum_single_phase_candidates_refined = 6` — at most 6 get refined

So the pipeline wants **at least ~24 chemically compatible candidates per sample** to use its
ranking machinery as designed. For the S4E rocksalt-family high-entropy oxide space, that means
the library must cover the binary and ternary oxides of Mg, Mn, Ni, Zn, Cu (plus the rocksalt and
spinel solid-solution prototypes) — realistically **several hundred entries across the S4E
element space**, not a handful. Sourcing options, in descending order of practicality:

- **Crystallography Open Database (COD)** — CIFs are freely redistributable, so they can live in
  the repo under the existing hash-pinned manifest. Best fit for the current design.
- **Materials Project** — good coverage of hypothetical/computed oxides, API-retrievable, but
  entries are DFT-relaxed; lattice parameters will be systematically off by ~1–2%, which matters
  given `maximum_relative_lattice_parameter_change = 0.05`.
- **ICSD** — best experimental coverage, but licensed. CIFs cannot be committed to this repo. If
  ICSD is used, the manifest would have to point at a licensed on-disk path outside the tree and
  the deployment story changes.

This is a curation project with a real scientific owner, not an engineering task. It is the
long pole.

### 1.2 GSAS-II — see section 2. It is required for screening, not just refinement.

### 1.3 Trial metadata

The five warnings observed (`missing_wavelength`, `missing_coordinate_column`,
`missing_intensity_column`, `missing_scan_range`, `missing_instrument_profile`) are emitted during
input assembly at `schemas.py:1334-1400`. Of these, two are hard blockers downstream:

- **`missing_wavelength`** blocks *both* screening simulation
  (`candidates.py:608-609`, raises `CandidateSimulationError`) and refinement
  (`refinement.py:282-291`, returns `missing_wavelength_for_refinement`). Note the pattern parser
  recovered a usable 3751-point, 5–80° pattern regardless — the wavelength gap is a *metadata*
  gap, not a data gap.
- **`missing_instrument_profile`** blocks refinement at `refinement.py:311-320`. There is a
  generic-profile fallback, but `allow_generic_instrument_fallback` defaults to **`False`**
  (`schemas.py:1079`), so today the fallback never fires and the stage fails outright.

`missing_coordinate_column`, `missing_intensity_column` and `missing_scan_range` are advisory —
the parser derives what it needs from the file.

Practically: either the upload path must start capturing radiation source / wavelength and an
instrument profile per trial, or a per-instrument default must be configured and
`allow_generic_instrument_fallback` flipped to `True`. The latter is cheaper but records a
`missing_instrument_profile` warning on every result, which is honest and probably correct for a
research tool.

---

## 2. Is GSAS-II a hard dependency or a degraded-mode optional one?

**Both, and the distinction matters more than the brief suggests.**

**At the job level it is optional and degrades cleanly.** Every GSAS-II entry point is wrapped.
`refinement.py:322-337`:

```python
G2sc = None
try:
    G2sc = configure_gsas()
    from GSASII import GSASIIpath
except Exception as exc:
    warning = _refinement_warning(
        "gsas_project_creation_failed", "gsas_runtime",
        f"GSAS-II could not be configured: {exc}",
    )
    return _failure_result(..., failure_codes=("gsas_project_creation_failed",), ...)
```

It **returns a failure record; it does not raise.** `run_xrd_analysis_pipeline`
(`pipeline.py`) runs every stage unconditionally and hands whatever it has to
`run_final_phase_decision`, which classifies the empty-evidence case as `"unresolved"` with
`SelectedBestModel("none", …, "no_valid_hypotheses")` (`decision.py:696-698`). The job therefore
reports `succeeded`, persists artifacts, and renders a detail page. That is why the end-to-end run
came back `{'succeeded': 1, 'failed': 0}` — success there means *the pipeline completed*, not
*the science produced an answer*. This is a deliberate and good design choice, but it means job
status is not a health signal.

**At the science level it is hard, and its reach is wider than refinement.**
`_simulate_reflections_from_cif` — the *candidate screening* simulator — also calls
`configure_gsas()` (`candidates.py:961`) and there is **no non-GSAS fallback simulator**:

```python
except Exception as exc:  # pragma: no cover - exercised only without GSAS-II
    raise CandidateSimulationError("GSAS-II runtime is unavailable for candidate screening") from exc
```

Verified directly. With a sample whose chemistry *does* match the library (NaCl), so that nothing
is excluded, candidate generation still fails:

```
=== A. sample chemistry MATCHES the library (NaCl) ===
  status          = candidate generation failed
  candidates kept = 2
    warning: candidate_simulation_failed :: curated_nacl_rocksalt could not be simulated: GSAS-II runtime is unavailable for candidate screening
    warning: candidate_simulation_failed :: curated_hypothetical_nacl3 could not be simulated: GSAS-II runtime is unavailable for candidate screening
    warning: no_simulatable_candidates :: No chemically compatible candidates could be simulated over the measured range.
```

**This corrects the brief.** The report's root-cause framing attributes the `unresolved` result
solely to the reference library. It is one of two independent causes. Expanding the library to
1000 CIFs on a host without GSAS-II moves the failure from `no_chemically_compatible_candidates`
to `no_simulatable_candidates` and still yields `unresolved`.

There is a third, quieter coupling: the generic instrument-profile fallback resolves through
`write_default_instprm()`, which imports GSAS-II (`gsas_runtime.py:88-90`). Setting
`GSAS2_INSTPRM_PATH` short-circuits that (`gsas_runtime.py:118-120`), but otherwise even the
"fallback" path needs GSAS-II present.

**The test suite confirms the dependency is real and untested without it.** All 8 skipped tests in
`catalog.tests.xrd_analysis` are gated on `_gsas_runtime_available()`, and they are precisely the
tests that exercise real science:

```
skipped 'GSAS-II runtime not available':
  test_real_gsas_single_phase_refinement_with_curated_nacl
  test_real_two_phase_gsas_refinement_with_curated_local_cifs
  test_local_simulator_matches_gsas_reflection_set_for_nacl_reference
  test_systematic_absences_and_centering_extinctions_match_trusted_nacl_reference
  test_all_enabled_reference_cifs_validate_against_gsas
  test_simulation_of_simple_local_reference_phase
  test_screening_simulation_does_not_call_refinement_or_write_reference_files
  test_duplicate_handling_range_filtering_and_wavelength_are_deterministic
```

**No CI or dev environment currently runs these.** The 128 tests that do pass cover schemas,
persistence, jobs, UI and mocked refinement. The numerical correctness of the simulator and the
refinement is, on this branch, unexercised in every environment we have.

GSAS-II is **not in `requirements.txt`** (which pins `numpy==2.3.1`, `scipy==1.16.1` and no
GSAS-II). It is configured out-of-band via `GSAS2_PATH` / `GSAS2_INSTPRM_PATH`, documented in
`.env.example:17-20`, and `settings.py` does not read either variable — `gsas_runtime.py` reads
them straight from `os.getenv`. Confirmed absent on the dev server's pinned venv as well as
locally.

---

## 3. The smallest change that would let a run be triggered

**The trigger is already wired end-to-end.** This contradicts the brief on both counts; the
brief asked that both claims be verified before repeating, and neither survives.

The full chain exists at `HEAD`:

1. `catalog/templates/catalog/trial_detail.html:87-88,172-176` renders an XRD-analysis card with a
   `data-xrd-submit-button` and a `data-submit-url`, enabled when the user is approved and no job
   is already queued/running (`catalog/views.py:2628,2641-2642`).
2. `catalog/static/js/xrd_analysis.js:94-99` POSTs to that URL on click and then polls status.
3. `catalog/api/urls.py:97-99` routes `recipes/<recipe_auid>/trials/<trial_id>/xrd-analyses/` to
   `views.experiment_xrd_analysis_submit`.
4. `catalog/api/views.py:1711` (at `HEAD`) calls `submit_xrd_analysis_job(...)` and returns 202.

So submissions already reach the queue. **What is missing is the consumer.**

`catalog/apps.py:82-99` already contains a complete background worker, and `ready()` already tries
to start it (`apps.py:246-251`):

```python
if getattr(settings, "XRD_ANALYSIS_WORKER", False) and _is_web_process():
    threading.Thread(target=_run_xrd_analysis_worker, name="xrd-analysis-worker", daemon=True).start()
```

**`XRD_ANALYSIS_WORKER` is never defined in `loop/settings.py`.** The `getattr` default of `False`
therefore wins in every environment, and the thread has never started anywhere. Verified:

```
hasattr(settings, 'XRD_ANALYSIS_WORKER')      = False
hasattr(settings, 'XRD_ANALYSIS_POLL_SECONDS') = False
```

Compare `SYNTHESIS_LLM_WORKER`, whose identical pattern *is* bound at `settings.py:354`.

### Smallest change: one line

```python
# loop/settings.py, next to SYNTHESIS_LLM_WORKER (line 354)
XRD_ANALYSIS_WORKER = os.environ.get("XRD_ANALYSIS_WORKER", "0") != "0"
```

Ships dark by default, matching the synthesis worker's convention, and is enabled per-environment
via `.env`. `.env.example` should gain the key with a comment. No new code, no new command.

### Recommended addition: a management command

The in-process thread is right for dev but wrong for prod, because GSAS-II refinement is
minutes-long CPU work that would compete with request handling inside gunicorn workers, and
because per-worker threads make concurrency depend on worker count. A one-file command lets the
work run as its own process:

```python
# catalog/management/commands/run_xrd_analysis_worker.py
"""Drain the XRD analysis queue in a dedicated process."""

from django.core.management.base import BaseCommand

from catalog.xrd_analysis.worker import (
    process_pending_xrd_analysis_jobs,
    run_xrd_analysis_worker_loop,
)


class Command(BaseCommand):
    help = "Run the XRD phase-analysis worker."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Drain the queue once and exit instead of polling forever.",
        )

    def handle(self, *args, **options):
        if options["once"]:
            counts = process_pending_xrd_analysis_jobs()
            self.stdout.write(f"Processed {counts}.")
            return
        run_xrd_analysis_worker_loop()
```

Both target functions already exist and are exported (`worker.py:620,643`; `__init__.py:77-78`).
The command name should also be added to `_NON_WEB_COMMANDS` in `catalog/apps.py:17-37` so it does
not pay for the embedding-model preload, and so it does not start a *second* worker as an
in-process thread on top of its own loop.

`--once` is what makes this operable: it gives ops and the PI a way to drain the queue by hand
without a long-lived process, and it is the natural hook for cron.

---

## 4. Recommendation

**Merge, with the settings flag added and left off. Do not enable it for users.**

Reasoning:

**Why merge.** The code is finished, structurally sound, and non-invasive. It sits behind an
approval check, it has 128 passing tests, and — critically — its failure mode is *honest*. The
observed run did not produce a wrong answer; it produced `unresolved`, an evidence score of 0.143,
19 stage-tagged warnings naming every missing input, and full provenance. A pipeline that says
"I could not determine this, and here is precisely why" is safe to have in the tree. Holding a
10,000-line branch out of `main` while a months-long CIF curation effort runs invites a painful
rebase against the rest of `dev-environment` and helps no one.

**Why not enable it.** Today, enabling the worker would let an approved user click a button and
receive a rendered 79 KB analysis page for every sample, always saying `unresolved`. That is worse
than no button. It trains users to distrust the feature before it has ever worked, and the
detail page's polish — data-quality metrics, provenance, 10 SHA-256'd artifacts — makes an empty
result look substantive. The trial page's "no result yet" state is more truthful than a confident
rendering of nothing.

**The correction to the brief that changes the plan.** The brief treats the missing trigger as the
gating engineering task. It is not — the trigger is one settings line, and the button already
exists in the UI. The real gating items are the reference library and GSAS-II, both of which are
outside this branch's scope. Framing this as "merge as-is with the trigger unwired" understates
how close the plumbing is and overstates how close the science is.

**Suggested gate for turning it on**, in order:

1. `XRD_ANALYSIS_WORKER` bound in `settings.py`, defaulting off, plus the management command.
2. GSAS-II installed in the dev venv and `GSAS2_PATH` / `GSAS2_INSTPRM_PATH` set — **then re-run
   the suite and confirm the 8 skipped tests pass.** Until those tests run green somewhere, the
   simulator and refinement are numerically unvalidated. This is the cheapest high-value step and
   should happen regardless of the merge decision.
3. A production reference library covering the S4E element space, with the manifest and hashes
   generated by tooling rather than by hand.
4. Wavelength and instrument profile captured at upload, or a per-instrument default configured.
5. Enable for a small group on real trials, and use the Milestone 9 review records as the audit
   trail before opening it up.

Steps 1 and 2 are days. Step 3 is the schedule.

---

## Appendix — verification

Environment: local checkout of `feat/dev-environment` at `b6b8394`, scratchpad venv, local mongod
on `127.0.0.1:27099`. Full env exports omitted for brevity; they are the standard test exports.

### Reference library, settings gates, GSAS-II availability, element screening

```
$ python scratchpad/verify_gaps.py
=== 1. settings gates ===
  hasattr(settings, 'XRD_ANALYSIS_WORKER') = False  getattr(...,False) = False
  hasattr(settings, 'SYNTHESIS_LLM_WORKER') = True  getattr(...,False) = False
  hasattr(settings, 'XRD_ANALYSIS_POLL_SECONDS') = False  getattr(...,False) = False
  hasattr(settings, 'GSAS2_PATH') = False  getattr(...,False) = False
=== 2. reference snapshot ===
  REFERENCE_MANIFEST_PATH = …/catalog/reference_phases/manifest.json
  snapshot_version = loop-reference-phases-v1
  snapshot_hash    = c87acb51b3d9c6fa4cf4ebc188ae611817e6df20585af9e3c8fea2a7bfc645d9
  entries          = 2
    - curated_nacl_rocksalt: formula=NaCl elements=('Cl', 'Na') sg=F m -3 m enabled=True
    - curated_hypothetical_nacl3: formula=NaCl3 elements=('Cl', 'Na') sg=P m -3 m enabled=True
=== 3. GSAS-II availability ===
  import GSASIIscriptable: ModuleNotFoundError: No module named 'GSASIIscriptable'
  configure_gsas() raised GSASRuntimeError: GSAS-II could not be imported. Install GSAS-II into
    the active Python environment or set GSAS2_PATH to the GSAS-II checkout root.
=== 4. element screening against a real S4E sample ===
  sample element set = ['Cu', 'Mg', 'Mn', 'Ni', 'O', 'Zn']
    curated_nacl_rocksalt:      unlisted=['Cl', 'Na'] -> EXCLUDED
    curated_hypothetical_nacl3: unlisted=['Cl', 'Na'] -> EXCLUDED
```

### GSAS-II is required for screening, not only refinement

```
$ python scratchpad/verify_screening.py
=== A. sample chemistry MATCHES the library (NaCl) ===
  status          = candidate generation failed
  candidates kept = 2
    warning: candidate_simulation_failed :: curated_nacl_rocksalt could not be simulated: GSAS-II runtime is unavailable for candidate screening
    warning: candidate_simulation_failed :: curated_hypothetical_nacl3 could not be simulated: GSAS-II runtime is unavailable for candidate screening
    warning: no_simulatable_candidates :: No chemically compatible candidates could be simulated over the measured range.

=== B. real S4E sample chemistry (Mg-Mn-Ni-Zn-Cu-O) ===
  status          = candidate generation failed
  candidates kept = 0
    warning: intended_structure_reference_missing :: No validated intended-structure reference CIF was found …
    warning: candidate_contains_unlisted_element :: Excluded curated_hypothetical_nacl3 because it contains unlisted elements: Cl, Na.
    warning: candidate_contains_unlisted_element :: Excluded curated_nacl_rocksalt because it contains unlisted elements: Cl, Na.
    warning: no_chemically_compatible_candidates :: All available candidates were excluded by the sample element set.

=== C. does screening simulation need GSAS-II? (direct call) ===
  CandidateSimulationError: GSAS-II runtime is unavailable for candidate screening
  __cause__: GSASRuntimeError: GSAS-II could not be imported…
```

### Test suite

```
$ python manage.py test catalog.tests.xrd_analysis
Ran 140 tests in 1.827s
FAILED (failures=4, skipped=8)
```

The 4 failures are all in `test_pattern.py` and are the known environmental ones (macOS resolves
`/var` → `/private/var`); they are unrelated to this report. All 8 skips are
`'GSAS-II runtime not available'`.

### Dev server (read-only)

```
$ ssh mintaka '~/loop-dev/.venv/bin/python -c "…"'
GSASIIscriptable spec: None
GSASII import: ModuleNotFoundError No module named 'GSASII'
numpy 2.3.1 scipy 1.16.1
--- env GSAS2 ---
(no gsas/xrd keys in ~/loop-dev/.env)
--- reference_phases on dev ---
hypothetical_NaCl3_1to3.cif
manifest.json
NaCl_rocksalt.cif
```

The dev server runs the pinned dependency set and shows the same two gaps.

### Trigger wiring

Read from committed `HEAD` (`git show HEAD:…`) rather than the working tree, because
`catalog/api/views.py` and `catalog/api/urls.py` were being edited concurrently by another task.

```
$ git show HEAD:catalog/api/views.py | grep -n submit_xrd_analysis_job
45:    submit_xrd_analysis_job,
1711:        submission = submit_xrd_analysis_job(

$ git show HEAD:catalog/api/urls.py | grep -n xrd-analyses -A2
97:        "recipes/<str:recipe_auid>/trials/<str:trial_id>/xrd-analyses/",
98:        views.experiment_xrd_analysis_submit,
99:        name="api-v1-experiment-xrd-analysis-submit",
```

### Not verified

- Whether GSAS-II, once installed, actually converges on real S4E patterns. Nothing in this
  branch has ever run a real refinement in an environment we control.
- Production (`mintaka-www-s4e`) was not inspected, per the constraints on this task. All
  server-side statements above refer to `~/loop-dev` only.
- The concurrent edit to `catalog/api/views.py` (+49/−2 lines, unreviewed here) may change the
  submit endpoint. Line numbers cited for `views.py` and `urls.py` are from `HEAD`, not the
  working tree.
