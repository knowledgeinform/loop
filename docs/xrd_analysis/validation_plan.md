# XRD Phase-Analysis MVP Validation And Calibration Plan

## Scope

This document defines the validation and calibration plan for the completed LOOP XRD phase-analysis MVP as implemented through Milestones 1 through 9.

The plan is intentionally limited to validation planning. It does not change:

- production scientific code
- thresholds or configuration defaults
- schemas
- APIs
- workers
- persistence contracts
- views, templates, or frontend behavior

Authoritative references:

- Implementation instructions: `<repo-root>/docs/xrd_analysis/implementation_instructions.txt`
- Implementation plan: `<repo-root>/docs/xrd_analysis/implementation_plan.md`

Applicable implementation-instruction sections:

- Step 11: compare one-phase and two-phase models
- Step 12: run stability checks
- Step 13: create a content-addressed analysis ID
- Step 14: store results without creating a new storage system
- Step 15: connect the pipeline to the existing API
- Step 16: display human and automated results separately
- Step 17: add expert review as a separate record
- Step 18: test the pipeline in layers
- MVP completion criteria

Out of scope:

- changing any current algorithm
- changing any current threshold
- implementing evaluation scripts or management commands
- building benchmark manifests or benchmark data in this task
- revising confidence/evidence scoring during this task

## Current MVP Validation Surface

### Persisted outputs already available

The current MVP already persists enough information to support a substantial offline validation program:

- `analysis_id`
- `material_auid`
- `recipe_auid`
- `trial_id`
- `raw_file_hash`
- compact trial-level summary:
  - `analysis_status`
  - `phase_state`
  - `evidence_score`
  - `warning_count`
  - `algorithm_version`
  - `configuration_version`
  - `selected_model_type`
  - `selected_candidate_ids`
  - `failure_code_count`
  - `reference_snapshot_identity`
  - `completion_time`
  - `best_hypothesis_id`
- full result payload:
  - parsed-pattern metadata
  - QC metrics and failure codes
  - candidate shortlist
  - successful and failed single-phase hypotheses
  - successful and failed two-phase hypotheses
  - selected model
  - model comparison summary
  - decision criteria
  - stability results
  - evidence components
  - warnings and failure codes
- reproducibility manifest:
  - configuration serialization and hash
  - reference snapshot version and hash
  - linked-structure snapshots
  - GSAS-II version
  - Python/package versions
  - parsing/simulation/refinement method labels
  - classification-threshold serialization for refinement and decision stages
  - artifact hashes
- persisted artifacts:
  - `result.json`
  - `reproducibility.json`
  - `candidates.json`
  - `single_phase_hypotheses.json`
  - `two_phase_hypotheses.json`
  - `model_comparison.json`
  - `pattern_observed.csv`
  - `pattern_best_model.csv`
  - `reflections.json`
  - `selected_cifs.json`
  - `selected_gsas_project.gpx` when available

### Existing expert-review fields already available

The Milestone 9 review record already supports:

- `analysis_id`
- `material_auid`
- `recipe_auid`
- `trial_id`
- `reviewer_username`
- `reviewer_display_name`
- `reviewer_organization`
- `review_status`
- `reviewed_phase_state`
- `selected_hypothesis_id`
- `added_candidate_identifiers`
- `confidence`
- `notes`
- `supersedes_review_id`
- `is_active`
- `created_at`
- `updated_at`

These fields are sufficient for a benchmark-review workflow, disagreement capture, and label provenance, but they do not yet force all validation-specific benchmark metadata that a final evaluator will need.

### Existing test fixtures that can seed validation

The current test surface already provides deterministic seed material for future benchmark scaffolding:

- `catalog/tests/xrd_analysis/test_pattern.py`
  - clean and messy pattern normalization/QC cases
- `catalog/tests/xrd_analysis/test_candidates.py`
  - curated local reference candidates
  - candidate simulation/ranking cases
- `catalog/tests/xrd_analysis/test_single_phase_refinement.py`
  - deterministic local curated CIF refinement cases
- `catalog/tests/xrd_analysis/test_two_phase_decision.py`
  - controlled one-phase versus two-phase decision cases
- `catalog/tests/xrd_analysis/test_persistence.py`
  - representative fully populated persisted analysis payloads
- curated local reference CIFs in `catalog/reference_phases/`
- preserved parsing/store/GSAS regression tests outside `catalog/tests/xrd_analysis/`

These are useful for:

- evaluator smoke tests
- schema sanity checks
- synthetic benchmark manifest design
- regression comparisons after future calibration changes

They are not sufficient as scientific benchmark ground truth by themselves.

### Current missing data for scientific ground truth

The MVP does not yet guarantee the following information for real experimental validation:

- independently verified phase compositions for most historical trials
- explicit benchmark labels with reviewer provenance
- candidate lists considered by the human ground-truth process
- known secondary-phase fractions for most mixtures
- validated weight-fraction labels tied to the same exact XRD file
- benchmark-ready instrument grouping metadata across all existing trials
- explicit institution/laboratory normalization for all historical data
- benchmark flags for preferred orientation, broad peaks, amorphous background, and solid-solution behavior
- benchmark manifests linking one exact trial/XRD analysis to one exact ground-truth record

## Recommended Validation Phases

### Phase A: Unit and synthetic validation

Entry requirements:

- Milestones 1 through 9 complete
- deterministic local references and integration tests passing

Tasks:

- validate evaluator logic against synthetic and repository-local curated references
- check artifact loading, metric extraction, and manifest generation
- verify no leakage between summary metrics and full-result metrics

Outputs:

- evaluator smoke report
- synthetic confusion matrices
- first threshold sensitivity scan

Exit criteria:

- metric extraction from persisted results is correct
- evaluator is deterministic on repeated runs
- all planned validation metrics can be computed from stored artifacts or are explicitly marked missing

Risks:

- synthetic success may overstate real-world performance
- good CIF/refinement behavior may not transfer to messy experimental scans

### Phase B: Curated experimental benchmark

Entry requirements:

- Phase A evaluator stable
- benchmark schema and review protocol defined
- initial ground-truth curation complete

Tasks:

- assemble multi-instrument curated benchmark
- perform blinded expert review
- compute classification, candidate, refinement, calibration, and robustness metrics

Outputs:

- benchmark manifest
- expert-reviewed labels with provenance
- first threshold-calibration recommendations

Exit criteria:

- minimum target counts reached across key categories
- inter-reviewer workflow functioning
- instrument-grouped holdout report completed

Risks:

- historical labels may conflict with expert adjudication
- institutions may use incompatible metadata conventions

### Phase C: Prospective silent deployment

Entry requirements:

- Phase B benchmark complete
- provisional thresholds frozen for silent study

Tasks:

- run the automated pipeline on newly arriving trials
- hide automated classification from routine users
- collect expert review asynchronously

Outputs:

- prospective blind-comparison set
- error taxonomy frequencies on fresh data
- runtime/reliability statistics on live traffic

Exit criteria:

- silent deployment demonstrates stable runtime and failure handling
- expert disagreement patterns are characterized

Risks:

- live data may differ materially from the curated benchmark
- institutional drift may surface unanticipated candidate gaps

### Phase D: Limited expert-visible deployment

Entry requirements:

- silent prospective study acceptable
- expert-review workflow operational

Tasks:

- expose automated result with warnings to expert users only
- continue collecting review records and adjudications
- track whether expert-visible outputs improve or bias review quality

Outputs:

- expert-visible usage report
- adjudication trends
- calibration update proposals

Exit criteria:

- no unacceptable false single-phase or false multiphase patterns in expert-visible use
- unresolved behavior is acceptable to experts

Risks:

- user overtrust in evidence score
- experts anchoring on the automated answer

### Phase E: Calibrated production use

Entry requirements:

- prior phases complete
- calibration recommendations accepted
- deployment gating criteria met

Tasks:

- lock validated thresholds/configuration
- publish benchmark summary and limitations
- define monitoring cadence for post-release drift

Outputs:

- release recommendation
- production validation report
- post-release monitoring checklist

Exit criteria:

- MVP judged suitable for its intended deployment tier

Risks:

- calibration may not remain stable as new candidate sources or instruments are added

## Benchmark Dataset Design

The benchmark must include all categories below. Counts are minimum starting targets for the first serious validation cycle and may be increased if class imbalance or instrument imbalance remains high.

| Category | Purpose | Minimum sample count | Ground-truth source | Expected failure modes | Metrics | Provisional acceptance criteria |
|---|---|---:|---|---|---|---|
| Clean known single-phase experimental patterns | Validate best-case single-phase behavior | 40 | Certified reference material, expert quantitative Rietveld, or independently verified phase composition | false multiphase, wrong primary candidate, unstable lattice refinement | single-phase precision/recall, top-1 recall, refinement convergence, Rwp distribution | false multiphase rate low enough for internal research use; candidate recall acceptable on clean scans |
| Known binary mixtures | Validate core one-phase versus two-phase decision | 40 | Synthetic mixture with known construction or expert quantitative Rietveld | false single-phase, wrong second candidate, unresolved from overlap | multiphase recall, candidate-set recall, two-phase selection accuracy | multiphase recall acceptable for expert-assisted use |
| Mixtures at multiple secondary-phase fractions | Measure second-phase sensitivity across concentration | 70 | Synthetic mixtures or expert quantitative Rietveld | missed low-fraction phase, unstable second scale, ambiguous model comparison | recall by fraction band, second-candidate rank, unresolved rate | monotonic sensitivity improvement with phase fraction |
| Trace secondary phases near detection limits | Calibrate unresolved versus multiphase near the edge | 50 | Synthetic mixtures and controlled experimental blends | false multiphase at 0%, missed trace phase, overconfident calls | detection curves, false-positive rate, unresolved rate | uncertainty should rise rather than forcing incorrect labels |
| Strongly overlapping phase pairs | Stress overlap-aware logic | 25 | Expert quantitative Rietveld or synthetic mixtures | false certainty, no distinctive-reflection support, wrong pair | unresolved rate, multiphase recall, evidence-rule pass rates | unresolved allowed when overlap is irreducible |
| Preferred-orientation cases | Test robustness to distorted relative intensities | 25 | Expert review with supporting fit files | wrong ranking from intensity distortion, false missing-candidate conclusions | candidate recall, classification accuracy, confidence calibration | performance drop should be bounded and explained by warnings |
| Broad or poorly crystalline patterns | Validate QC and unresolved behavior on broadened peaks | 25 | Expert review and supporting pattern notes | insufficient-quality under-calling or overcalling, false multiphase | insufficient-quality rate, unresolved rate, QC failure reason distribution | QC should reject only clearly unusable scans |
| Amorphous-plus-crystalline samples | Measure confusion with non-modeled amorphous background | 25 | Expert review with supporting files | false multiphase, false unresolved, inflated residual evidence | unresolved rate, false multiphase rate, residual-region burden | algorithm should not overstate certainty |
| Solid solutions with shifted lattice parameters | Check tolerance to lattice shifts and prototype mismatch | 30 | Expert quantitative refinement or published assignment with supporting fit | wrong candidate family, lattice bound violations, false missing-candidate warning | candidate recall, lattice-parameter error, bound-violation rate | correct family should remain competitive despite shifts |
| Samples with correct candidates present | Establish best-case candidate recall | 40 | Any benchmark sample where benchmark manifest confirms presence | later-stage failure despite correct shortlist | top-1/top-3/top-6 recall, candidate-set recall | candidate generation should not be the limiting failure stage on this subset |
| Samples with the correct candidate deliberately removed | Measure unresolved and incomplete-candidate behavior | 30 | Derived from benchmark cases by ablation | false certainty with wrong structure, missing `candidate_set_may_be_incomplete` warning | unresolved rate, false single-phase rate, incomplete-candidate warning rate | unresolved or warning should dominate over incorrect certainty |
| Samples with incomplete element lists | Test soft chemistry filtering robustness | 25 | Curated benchmark with deliberately masked metadata | wrong exclusion of true phase, unstable shortlist | candidate-set recall, warning rates, unresolved rate | true candidate should remain reachable when chemistry is incomplete |
| Samples with incorrect element lists | Measure robustness to bad metadata | 25 | Curated benchmark with deliberately incorrect metadata | candidate elimination, false unresolved, wrong confident answer | incomplete-candidate warning rate, candidate recall, false certainty | pipeline should warn and degrade rather than hallucinate certainty |
| Multiple X-ray wavelengths | Check wavelength-dependent parsing and simulation robustness | 30 | Expert-reviewed or synthetic, split across Cu/Co/Mo or other supported sources | conversion failure, wrong peak alignment, candidate misranking | classification accuracy by wavelength, position error, candidate recall | no wavelength-specific catastrophic failure |
| Multiple instrument profiles | Test instrument-profile sensitivity and fallback behavior | 40 | Benchmark scans with documented instrument metadata | invalid profile adaptation, reduced convergence, runtime inflation | refinement convergence, instrument-stratified accuracy, runtime | held-out instrument performance must remain acceptable for target tier |
| Multiple universities or laboratories | Measure lab-specific export and convention robustness | 40 | Cross-lab curated set with normalized provenance | metadata mismatch, parser quirks, candidate ranking drift | institution-stratified accuracy, unresolved rate, warning distribution | no single institution should dominate calibration conclusions |
| Different scan ranges and step sizes | Test range/spacing robustness and metadata mismatch warnings | 35 | Curated benchmark spanning common acquisition settings | QC misclassification, missed candidate from limited range | performance by range/step band, QC warning rates | graceful degradation with shorter ranges rather than silent misclassification |
| High-background or fluorescence-heavy patterns | Measure robustness to noisy backgrounds | 25 | Expert-reviewed real scans | false residual evidence, false secondary phase, poor convergence | false multiphase rate, unresolved rate, refinement failure rate | confidence should drop when background dominates |
| Patterns with metadata mismatches | Test warning coverage and resilience | 25 | Curated cases with known file/metadata disagreement | wrong coordinate interpretation, range mismatch, instrument mismatch | warning precision, failure modes, classification drift | major metadata conflicts should be warned or rejected, not silently ignored |
| Patterns previously labeled unresolved by experts | Validate that unresolved remains a legitimate output | 25 | Independent expert review or adjudication | overconfident single- or two-phase call | unresolved precision, disagreement rate, confidence calibration | unresolved should remain available and common where ambiguity is real |

## Ground-Truth Hierarchy

Ground truth must be ranked by reliability and stored with provenance. Use the highest available level per benchmark case.

1. Certified reference material
2. Independently verified phase composition
3. Quantitative Rietveld analysis by an expert
4. Search-match plus expert review
5. Published phase assignment
6. Existing LOOP human label
7. Synthetic mixture with known construction
8. Simulated pattern

### Ground-truth rules

- The current LOOP `phase_status` field must not be treated as unquestioned ground truth.
- Synthetic mixtures and simulated patterns are valuable for controlled studies, but they must be reported separately from real experimental benchmark performance.
- Published phase assignments without raw supporting files must be downweighted relative to expert-reviewed local files.
- Every benchmark label must store:
  - who assigned it
  - method used
  - candidate phases considered
  - confidence
  - date
  - supporting files
  - whether it was independently reviewed

### Required ground-truth provenance record

For every benchmark case, future benchmark manifests should capture:

- benchmark case ID
- linked `trial_id`
- linked `analysis_id` if already run
- raw-file hash
- ground-truth reliability tier
- primary and secondary phases
- estimated fractions if known
- label assigner
- review status
- supporting artifact references
- notes on uncertainty or ambiguity

## Leakage-Resistant Data Splitting

Random row-level splitting is not acceptable.

Validation/calibration/test splits must group by:

- material family
- phase family
- instrument
- institution
- source publication
- related synthesis series

Rules:

- do not split near-duplicate measurements across calibration and test
- do not split repeated scans of the same specimen across calibration and test
- do not leak nearly identical synthetic mixtures of the same phase pair into both calibration and final test unless the held-out variable is explicitly concentration
- for institution/instrument holdouts, entire groups must be absent from calibration

Recommended split strategy:

- core benchmark test set:
  - held-out instruments or institutions
- calibration set:
  - remaining instruments with broad chemistry/range coverage
- shadow prospective set:
  - newest incoming trials collected after calibration freeze

## Metric Plan

### Classification metrics

- accuracy
- balanced accuracy
- precision for `likely multiphase`
- recall for `likely multiphase`
- precision for `likely single-phase`
- recall for `likely single-phase`
- unresolved rate
- insufficient-quality rate
- false secondary-phase rate
- false single-phase rate

Required stratifications:

- by benchmark category
- by instrument
- by institution
- by wavelength
- by phase-fraction band
- by data-quality band
- by candidate completeness

### Candidate-generation metrics

- top-1 candidate recall
- top-3 candidate recall
- top-6 candidate recall
- candidate-set recall
- intended-structure recall
- secondary-phase candidate recall

Report separately for:

- primary phase
- secondary phase
- cases where the correct candidate was deliberately removed
- cases with duplicate or near-duplicate CIF clusters

### Refinement metrics

- convergence rate
- valid-result rate
- lattice-parameter error
- zero-shift error
- Rwp distribution
- refinement-bound violation rate
- candidate failure isolation rate

Additional recommended summaries:

- per-stage failure distribution
- percentage of hypotheses ending in `completed`, `partial`, and `failed`
- warnings-per-hypothesis distribution

### Model-comparison metrics

- AIC margin distribution
- AICc margin distribution
- BIC margin distribution
- one-phase versus two-phase selection accuracy
- ambiguity-rate calibration
- second-phase evidence-rule pass rates

Also report:

- proportion of two-phase wins driven by one dominant residual region
- proportion of selected two-phase models lacking distinctive-reflection support

### Confidence/evidence-score metrics

- reliability diagram
- expected calibration error
- confidence versus empirical accuracy
- confidence stratified by data quality
- confidence stratified by instrument
- confidence stratified by candidate completeness

Rules:

- do not describe the current evidence score as a calibrated probability
- calibration plots must explicitly state the score is provisional and monotonic only until calibrated

### Robustness metrics

- performance by instrument
- performance by institution
- performance by wavelength
- performance by scan range
- performance by step size
- performance by signal-to-noise band
- performance by phase-fraction band

### Runtime metrics

- median runtime
- 90th percentile runtime
- 95th percentile runtime
- candidate-generation runtime
- single-phase refinement runtime
- two-phase refinement runtime
- failure-recovery runtime

## Controlled Secondary-Phase Detection-Limit Study

This study is required before treating `likely multiphase` as reliable at low fractions.

### Study design

For selected phase pairs:

- prepare or collect mixtures at approximately:
  - `0%`
  - `0.5%`
  - `1%`
  - `2%`
  - `5%`
  - `10%`
  - `20%`
- include at least two instruments or realistic instrument profiles
- include both overlapping and non-overlapping reflection pairs
- include at least one pair with the correct secondary candidate available
- include at least one ablated condition with the correct secondary candidate absent

Recommended starting phase-pair panel:

- one easily distinguishable pair
- one strongly overlapping pair
- one pair with the same chemistry but different structure families
- one solid-solution-like pair with small lattice shifts

### Measurements

For each fraction and instrument condition, record:

- probability of `likely multiphase` classification
- probability of `unresolved`
- secondary-candidate rank
- refined second-phase scale behavior
- distinctive-reflection support
- evidence score
- false-positive rate at `0%`

### Interpretation rules

- do not interpret the refined GSAS phase scale directly as weight fraction unless separately validated
- detection-limit claims must be instrument-specific unless cross-instrument consistency is demonstrated
- trace-phase sensitivity should be reported as a curve, not a single cutoff claim

## Instrument And Institution Holdouts

Validation must explicitly test generalization outside the instruments used for calibration.

Required holdout studies:

- hold out one instrument family at a time
- hold out one university/laboratory at a time
- hold out one radiation-source group at a time where sample counts permit

For each holdout:

- calibrate thresholds on the remaining instruments
- evaluate on the held-out group
- compare:
  - classification accuracy
  - unresolved rate
  - candidate recall
  - refinement convergence
  - confidence calibration
  - runtime

Primary purpose:

- detect overfitting to instrument profiles
- detect overfitting to export conventions or metadata habits
- detect whether instrument-specific calibration will eventually be required

## Candidate-Set Ablation Plan

The following ablations are required:

- correct primary candidate present
- correct secondary candidate present
- correct secondary candidate absent
- correct primary candidate absent
- near-duplicate CIFs present
- only a related structural prototype present
- linked DFT candidates added
- only curated local references available

For each ablation, measure how often the pipeline returns:

- `likely single-phase`
- `likely multiphase`
- `unresolved`
- `candidate_set_may_be_incomplete` warning

Primary objectives:

- quantify candidate-source recall ceilings
- measure how often incorrect certainty appears when the correct candidate is unavailable
- determine whether intended-structure priors are too strong or too weak

## Threshold-Calibration Inventory

All values below are current defaults only. They are provisional and must be calibrated, not assumed correct.

### Quality control thresholds

| Threshold | Current default | Scientific purpose | Data needed for calibration | Optimization objective | Acceptable range to explore | Risk if too low | Risk if too high |
|---|---:|---|---|---|---|---|---|
| `min_usable_points_continuous` | `20` | reject nearly empty scans | broad real benchmark including short scans | reduce false acceptance of unusable patterns | `10-100` | garbage passes | good short scans rejected |
| `min_range_width_continuous` | `1.0` | reject negligible angular span | short-range experimental scans | avoid meaningless fits | `0.5-10.0` | useless range accepted | limited-range but useful scans rejected |
| `min_signal_to_noise` | `1.5` | detect patterns too noisy for interpretation | noisy real scans with expert QC labels | balance acceptance versus noise failure | `1.0-5.0` | noise misclassified as evidence | too many usable scans rejected |
| `min_detectable_peak_regions_continuous` | `1` | ensure some crystalline structure exists | broad and weakly crystalline patterns | avoid flat/no-feature acceptance | `1-5` | flat scans accepted | weak but valid patterns rejected |
| `flat_signal_relative_std_threshold` | `0.01` | reject effectively flat signal | flat and weak-signal controls | catch truly featureless data | `0.005-0.05` | flat scans pass | broad weak peaks rejected |
| `irregular_step_variation_warning_ratio` | `0.2` | flag irregular grids | multi-instrument export set | warn on severe spacing irregularity | `0.05-0.5` | spacing issues missed | benign exports over-warned |
| `negative_intensity_fraction_warning` | `0.05` | warn on suspicious negativity | fluorescence-heavy/noisy scans | identify problematic intensity baselines | `0.01-0.2` | poor scans under-warned | benign baseline noise over-warned |
| `negative_intensity_fraction_failure` | `0.5` | fail clearly unusable intensity profiles | pathological scans | avoid nonsensical refinement input | `0.2-0.8` | unusable scans pass | salvageable scans rejected |
| `clipping_fraction_threshold` | `0.02` | detect saturation/clipping | clipped detector examples | warn on saturation-driven distortion | `0.005-0.1` | clipping missed | strong real peaks misflagged |
| `saturation_relative_level` | `0.99` | define saturation neighborhood | clipped scans | support clipping detection | `0.95-1.0` | misses clipping | overflags high peaks |
| `range_mismatch_tolerance` | `0.5` | compare file range to metadata | scans with trusted metadata | flag significant metadata disagreement | `0.1-2.0` | mismatches missed | harmless metadata drift over-warned |
| `step_size_relative_tolerance` | `0.25` | compare measured spacing to metadata | scans with trusted metadata | flag meaningful mismatch | `0.05-0.5` | mismatches missed | benign variation over-warned |
| `missing_interval_multiplier` | `5.0` | detect severe gaps | interrupted or stitched scans | identify corrupted acquisition grids | `2.0-10.0` | broken scans pass | sparse but valid scans overflagged |

### Candidate-ranking thresholds and weights

| Threshold | Current default | Scientific purpose | Data needed for calibration | Optimization objective | Acceptable range to explore | Risk if too low | Risk if too high |
|---|---:|---|---|---|---|---|---|
| `maximum_candidates_before_simulation` | `24` | cap raw candidate pool | chemistry-diverse benchmark | preserve recall with bounded runtime | `12-64` | correct candidates dropped too early | runtime explosion |
| `maximum_simulated_candidates` | `12` | cap simulated candidates | same as above | maintain recall before refinement | `6-32` | recall loss | unnecessary runtime |
| `final_top_k` | `6` | cap refinement shortlist | candidate-recall benchmark | preserve true phases in shortlist | `3-12` | missed true candidates | slow refinement |
| `maximum_representatives_per_duplicate_cluster` | `1` | avoid duplicate domination | duplicate-rich candidate subsets | diversify shortlist | `1-3` | shortlist redundancy | potentially lose useful variants |
| `maximum_allowed_screening_shift_degrees` | `0.35` | permit limited position mismatch | solid solutions and miscalibrated scans | tolerate realistic shifts only | `0.1-1.0` | true candidates under-ranked | wrong candidates overfit by shift |
| `peak_position_tolerance_degrees` | `0.20` | match predicted and observed peaks | multi-instrument benchmark | maximize recall with limited false matches | `0.05-0.5` | true matches missed | false matches inflated |
| `strong_peak_fraction_threshold` | `0.35` | define “strong predicted peak” | candidate screening benchmark | penalize truly missing major peaks | `0.2-0.6` | major misses ignored | weak peaks over-penalized |
| `stick_pattern_peak_position_tolerance_degrees` | `0.18` | match stick patterns | reflection-card subset | support non-scan inputs | `0.05-0.4` | valid stick matches missed | false stick matches inflated |
| `coverage_weight` | `0.30` | reward observed-region coverage | ranked-candidate benchmark | maximize shortlist recall | `0.1-0.5` | coverage underweighted | noisy coverage dominates |
| `position_agreement_weight` | `0.25` | reward positional agreement | same | improve physically meaningful ranking | `0.1-0.5` | bad alignment tolerated | minor shifts over-penalized |
| `whole_pattern_similarity_weight` | `0.15` | use broad scan similarity | same | improve ranking stability | `0.0-0.3` | whole-pattern info ignored | intensity distortion overweights |
| `position_only_similarity_weight` | `0.10` | use intensity-light comparison | same | robustness to orientation effects | `0.0-0.3` | robust cue lost | overfavors sparse peak coincidence |
| `matched_region_count_weight` | `0.10` | reward number of matched regions | same | prioritize broad support | `0.0-0.3` | narrow matches look too good | weak many-region matches dominate |
| `absent_strong_peak_penalty_weight` | `0.15` | penalize missing major predictions | ablation benchmark | suppress wrong candidates | `0.05-0.4` | false positives rise | true candidates over-penalized |
| `unexplained_region_penalty_weight` | `0.10` | penalize unexplained observed signal | same | identify incomplete models | `0.05-0.3` | poor candidates survive | noisy scans over-penalized |
| `shift_penalty_weight` | `0.05` | discourage excessive screening shifts | shifted-lattice benchmark | keep shifts physically plausible | `0.0-0.2` | shift abuse | true shifted phases under-ranked |
| `chemical_score_weight` | `0.12` | incorporate chemistry compatibility | chemistry-ablation benchmark | filter implausible chemistry softly | `0.0-0.3` | chemistry ignored | diffraction evidence suppressed |
| `stoichiometric_score_weight` | `0.08` | use nominal stoichiometry softly | stoichiometry-ablation benchmark | improve ranking without overfiltering | `0.0-0.2` | stoichiometry ignored | true nonstoichiometric phases under-ranked |
| `synthesis_context_score_weight` | `0.05` | use synthesis context softly | context-rich benchmark | exploit useful synthesis clues | `0.0-0.15` | useful context ignored | context overrides diffraction |
| `neutral_context_score` | `0.5` | default missing-context prior | sparse-metadata benchmark | ensure missing context is neutral | `0.3-0.7` | hidden bias against missing data | hidden bias in favor of missing data |
| `subset_element_bonus` | `0.05` | preserve subset phases in multiphase samples | multiphase benchmark | keep legitimate subset phases competitive | `0.0-0.15` | subset phases under-ranked | unrelated subset phases over-promoted |
| `intended_structure_bonus` | `0.05` | preserve intended structure as prior | intended-structure ablations | help but not force intended structure | `0.0-0.15` | intended structure unfairly ignored | intended structure overdominates |

### Single-phase refinement thresholds

| Threshold | Current default | Scientific purpose | Data needed for calibration | Optimization objective | Acceptable range to explore | Risk if too low | Risk if too high |
|---|---:|---|---|---|---|---|---|
| `maximum_single_phase_candidates_refined` | `6` | cap expensive refinement count | runtime + recall benchmark | preserve recall with practical runtime | `3-12` | true phase omitted | runtime grows sharply |
| `background_coefficient_count` | `6` | control background flexibility | mixed background-quality benchmark | improve fit without absorbing peaks | `3-12` | structured background underfit | overfit background hides phase evidence |
| `maximum_absolute_zero_shift_degrees` | `0.5` | bound geometric shift | instrument-holdout benchmark | allow realistic alignment only | `0.05-1.0` | valid scans fail bounds | wrong candidates gain spurious freedom |
| `maximum_absolute_sample_displacement` | `5.0` | bound displacement when enabled | displacement-labeled subset | allow realistic correction only | `0.5-10.0` | valid corrections blocked | unphysical fits accepted |
| `maximum_relative_lattice_parameter_change` | `0.05` | bound lattice movement | solid-solution benchmark | allow realistic strain/solution shifts | `0.01-0.10` | valid shifted phases fail | wrong candidates distort lattice to fit |
| `initial_stage_cycles` | `3` | stabilize early refinement | convergence benchmark | improve success without wasted runtime | `1-6` | unstable early refinement | unnecessary runtime |
| `maximum_iterations` | `6` | cap refinement schedule | runtime and convergence benchmark | enough stages for stable results | `3-10` | incomplete convergence | slow, unstable refinements |
| `convergence_tolerance` | `1e-4` | define stopping sensitivity | integration benchmark | stable stopping without over-iteration | `1e-5-1e-3` | noisy over-iteration | premature stopping |
| `residual_region_threshold_fraction_of_max` | `0.10` | flag meaningful positive residuals | residual-analysis benchmark | identify real unexplained structure | `0.05-0.3` | weak noise overflagged | real unexplained peaks missed |
| `residual_region_min_points` | `3` | require minimal region extent | same | avoid one-point noise artifacts | `2-8` | noise regions survive | narrow but real residuals missed |
| `residual_expected_reflection_window_degrees` | `0.25` | associate residuals to nearby reflections | same | interpret residuals consistently | `0.1-0.5` | associations missed | unrelated reflections linked |
| `unsupported_predicted_region_strong_fraction` | `0.35` | define strong unsupported prediction | single-phase failure benchmark | flag real unsupported predictions | `0.2-0.6` | unsupported peaks underflagged | too many benign peaks flagged |
| `unsupported_predicted_region_support_ratio_threshold` | `0.25` | define insufficient observed support | same | separate absent versus weakly supported | `0.1-0.5` | poor support ignored | valid peaks falsely unsupported |
| `unsupported_predicted_region_window_degrees` | `0.20` | local support window | same | compare local support consistently | `0.05-0.4` | support mismatched | unrelated local intensity counted |

### Two-phase decision thresholds

| Threshold | Current default | Scientific purpose | Data needed for calibration | Optimization objective | Acceptable range to explore | Risk if too low | Risk if too high |
|---|---:|---|---|---|---|---|---|
| `maximum_base_single_phase_hypotheses` | `3` | cap seed single-phase models | two-phase benchmark | preserve good base diversity | `1-6` | miss valid second-phase routes | runtime growth |
| `maximum_secondary_candidates_per_base` | `3` | cap residual-driven second candidates | same | preserve recall with bounded branching | `1-6` | miss valid pairs | runtime growth |
| `maximum_total_two_phase_refinements` | `6` | cap expensive two-phase fits | same | control runtime | `2-12` | missed true pairs | runtime growth |
| `minimum_residual_region_strength_fraction` | `0.20` | define meaningful residual region | residual-driven pair benchmark | ignore trivial residuals | `0.05-0.4` | noisy second-phase proposals | real weak second phase missed |
| `minimum_residual_region_count` | `1` | require at least some residual evidence | same | avoid gratuitous two-phase search | `1-3` | two-phase search too eager | subtle second phase missed |
| `minimum_second_phase_scale_factor` | `0.02` | reject negligible second phases | detection-limit study | suppress fake tiny second phases | `0.005-0.1` | false multiphase rises | low-fraction true phases missed |
| `minimum_supported_reflection_regions` | `2` | require multiple support regions | two-phase benchmark | improve robustness | `1-4` | one-region artifacts pass | real overlapping phases unresolved |
| `minimum_partially_distinctive_regions` | `1` | require some distinctiveness when possible | overlap benchmark | reduce purely overlapped claims | `0-3` | overlap-driven false positives | true overlapping pairs undercalled |
| `maximum_single_region_evidence_fraction` | `0.70` | prevent one region dominating evidence | same | avoid overreliance on one peak cluster | `0.4-0.9` | true pairs undercalled | one-region artifacts accepted |
| `aic_threshold` | `2.0` | penalized win threshold | benchmark with adjudicated labels | improve model-choice reliability | `0.5-10.0` | too many two-phase wins | true multiphase undercalled |
| `aicc_threshold` | `2.0` | finite-sample penalized win threshold | same | same as above | `0.5-10.0` | overfit accepted | true wins missed |
| `bic_threshold` | `2.0` | stronger complexity penalty | same | same as above | `0.5-10.0` | overfit accepted | true wins missed |
| `minimum_penalized_improvement` | `2.0` | global required improvement | same | avoid marginal complexity gains | `0.5-10.0` | false multiphase | false unresolved/false single-phase |
| `ambiguity_margin` | `2.0` | unresolved band around decision boundary | same | route borderline cases to unresolved | `0.5-10.0` | forced wrong labels | too many unresolved |
| `background_coefficient_perturbation` | `1` | stability-perturbation size | stability benchmark | meaningful but small perturbation | `1-3` | stability check too weak | stability check too disruptive |
| `residual_threshold_multipliers` | `(0.8, 1.2)` | perturb residual sensitivity | stability benchmark | assess robustness | nearby values around `1.0` | stability overestimated | stability under-estimated |
| `scale_seed_multipliers` | `(0.8, 1.2)` | perturb phase-scale initialization | same | assess sensitivity to seeds | nearby values around `1.0` | fragile fits missed | perturbations unrealistic |
| `zero_shift_seed_delta` | `0.02` | perturb zero-shift seed | same | assess shift sensitivity | `0.005-0.1` | instability hidden | perturbation unrealistic |
| `dominant_region_half_window_degrees` | `0.20` | define dominant region for downweighting | same | test dominance sensitivity | `0.05-0.5` | dominance not detected | unrelated region merged |
| `minimum_classification_agreement` | `0.67` | require stability across reruns | stability benchmark | suppress unstable certainty | `0.5-0.9` | unstable labels pass | too many unresolved |
| `minimum_scale_stability` | `0.60` | require stable second-phase scale | same | suppress fragile second-phase claims | `0.3-0.9` | unstable scales accepted | true weak phases unresolved |
| `minimum_score_stability` | `0.60` | require stable evidence score | same | suppress unstable certainty | `0.3-0.9` | unstable scores accepted | too many unresolved |
| `adequate_single_phase_max_residual_regions` | `1` | define acceptable leftover residual burden | single-phase benchmark | determine adequacy of one-phase model | `0-4` | poor single-phase fits accepted | too many false multiphase/unresolved |
| `adequate_single_phase_max_unsupported_regions` | `2` | define acceptable unsupported-prediction burden | same | same | `0-5` | poor one-phase fits accepted | too many false multiphase/unresolved |
| `evidence_score_clip` | `1.0` | cap provisional score | calibration benchmark | stable score range for reporting | fixed around `1.0` | score scale drifts | score compression masks differences |

## Expert-Review Protocol

The benchmark review process must use the Milestone 9 review records as the audit trail, but benchmark curation will eventually need a benchmark-manifest layer above them.

### Review questions

Reviewers must assess:

- data quality
- primary candidate plausibility
- secondary candidate plausibility
- selected model
- unexplained peaks
- missing candidates
- classification
- confidence
- whether more data are needed

### Blind-review protocol

1. Reviewer A sees the observed pattern, metadata, supporting files, and current candidate/hypothesis evidence.
2. Reviewer A does not initially see the automated final classification or evidence score.
3. Reviewer A records:
   - independent classification
   - likely phases considered
   - confidence
   - notes on data quality and missing candidates
4. Only after the independent judgment is saved may Reviewer A compare with the automated output.

### Disagreement handling

- any disagreement on class or key candidate identity triggers a second independent review
- if Reviewers A and B disagree, an adjudicator reviews:
  - raw pattern
  - supporting files
  - both review records
  - automated outputs
- adjudication outcome becomes the benchmark label, with both original reviews preserved

### Inter-reviewer agreement metrics

- raw agreement
- Cohen’s kappa or weighted equivalent where appropriate
- agreement by class
- agreement by benchmark category
- agreement stratified by instrument

### How corrected labels become benchmark data

- do not overwrite historical automated outputs
- do not overwrite the embedded human `phase_status`
- preserve all review history
- benchmark manifests should point to:
  - original `analysis_id`
  - active adjudicated review ID
  - reliability tier
  - provenance record

## Structured Error Analysis

Every failed or suspicious benchmark case must be categorized into at least one structured error class:

- parser failure
- metadata failure
- instrument-profile failure
- candidate missing
- wrong candidate ranking
- refinement nonconvergence
- parameter-bound failure
- false secondary phase
- missed secondary phase
- overlap ambiguity
- preferred-orientation distortion
- amorphous-content confusion
- solid-solution mismatch
- insufficient angular range
- high background
- incorrect element list
- incorrect human ground truth

For every failed case, the evaluation record must preserve:

- `analysis_id`
- input metadata
- selected hypotheses
- warnings
- residual plot
- expert notes
- suspected root cause
- proposed remediation category

Recommended remediation categories:

- dataset/ground-truth correction
- metadata normalization
- candidate-source expansion
- candidate-ranking recalibration
- refinement-configuration recalibration
- decision-threshold recalibration
- UI/expert-guidance change
- out-of-scope limitation

## Provisional Acceptance Criteria By Deployment Tier

All numeric targets below are provisional and must be treated as planning anchors only.

### 1. Internal research use

Suggested provisional criteria:

- multiphase recall: at least `0.75`
- false single-phase rate: at most `0.10`
- candidate-set recall: at least `0.90`
- unresolved rate: at most `0.30`
- refinement failure rate: at most `0.20`
- held-out instrument balanced accuracy: at least `0.70`

Rationale:

- acceptable for exploratory internal analysis if accompanied by artifact inspection and expert review

### 2. Expert-assisted website use

Suggested provisional criteria:

- multiphase recall: at least `0.85`
- false multiphase rate: at most `0.10`
- false single-phase rate: at most `0.05`
- top-6 candidate recall: at least `0.95`
- unresolved rate: at most `0.25`
- refinement failure rate: at most `0.15`
- acceptable evidence-score calibration in at least coarse bins

Rationale:

- experts can inspect evidence, but the automated result must not frequently mislead them

### 3. Automated screening use

Suggested provisional criteria:

- balanced accuracy: at least `0.85`
- false single-phase rate: at most `0.03`
- false multiphase rate: at most `0.08`
- candidate-set recall: at least `0.97`
- held-out instrument performance degradation: limited relative to in-distribution benchmark
- confidence calibration error: low enough to support thresholding by score band
- 95th percentile runtime within operational budget

Rationale:

- automated triage requires stronger conservatism and robust generalization

### 4. Future experiment-selection use

Suggested provisional criteria:

- multiphase recall: at least `0.90`
- false single-phase rate: at most `0.02`
- unresolved behavior preferred over uncertain forced labels
- confidence calibration validated prospectively
- cross-institution holdout results stable
- secondary-phase detection curves characterized for relevant chemistries

Rationale:

- experiment-selection decisions require the strictest trust boundary and should not use the MVP until calibration is substantially stronger than expert-assisted display

## Validation Report Structure

Every formal validation cycle should publish a report with these sections:

1. Dataset summary
2. Ground-truth quality
3. Classification metrics
4. Candidate metrics
5. Refinement metrics
6. Confidence calibration
7. Detection-limit results
8. Instrument holdouts
9. Candidate-set ablations
10. Runtime
11. Failure analysis
12. Threshold recommendations
13. Unresolved scientific limitations
14. Release recommendation

Each report should separate:

- synthetic/unit benchmark results
- curated retrospective experimental results
- prospective silent-deployment results

## Repository Support And Future Minimal Organization

### What can already be computed directly

Directly computable from persisted analyses:

- class labels
- unresolved and insufficient-quality rates
- warnings and failure-code frequencies
- top-ranked candidate availability
- candidate shortlist recall, if benchmark truth is available externally
- refinement convergence and failure rates
- Rwp/Rp/goodness-of-fit distributions
- lattice-parameter and zero-shift error, if external truth exists
- model-comparison margin distributions
- runtime distributions
- stability-check outputs

### What is currently missing

Not directly computable without future benchmark data or evaluation code:

- empirical accuracy against trusted ground truth
- calibrated evidence-score reliability
- inter-reviewer agreement
- candidate completeness against authoritative true phase sets
- detection-limit curves
- institution-normalized holdout comparisons

### Whether evaluation tooling will eventually be needed

Yes. A separate offline evaluator is strongly recommended, but not implemented in this task.

Recommended future path:

- `catalog/xrd_analysis/evaluation/`

Recommended future contents:

- benchmark manifest schema
- artifact-loading helpers
- metric calculators
- split builders
- report generators
- review/adjudication import helpers

### Future benchmark-data placement

Recommended minimal organization:

- `docs/xrd_analysis/validation_plan.md`
- `catalog/xrd_analysis/evaluation/`
- `catalog/tests/xrd_analysis/fixtures/`
- benchmark manifest outside production source data

Recommended benchmark data principle:

- keep benchmark manifests and supporting evaluation metadata outside production scientific reference data and outside the curated production CIF directory

### How benchmark data should reference existing LOOP records

Every benchmark case should reference:

- `trial_id`
- `recipe_auid`
- `material_auid`
- `analysis_id` when the pipeline has already run
- `raw_file_hash`
- ground-truth record ID
- expert-review record IDs used for adjudication

This will allow re-evaluation after future calibration changes without changing the historical production analysis.

## Release Recommendation Logic

The validation program should end each formal cycle with one of:

- not ready for scientific use
- ready for internal research use only
- ready for expert-assisted website use
- ready for limited automated screening

The recommendation must explicitly consider:

- classification accuracy
- unresolved behavior
- confidence calibration
- held-out instrument robustness
- failure taxonomy
- runtime
- benchmark ground-truth quality

## Immediate Next Steps After This Plan

1. Define a benchmark manifest schema outside production source data.
2. Curate the first Phase B benchmark with explicit ground-truth provenance.
3. Add an offline evaluation package under `catalog/xrd_analysis/evaluation/`.
4. Extract persisted analyses and review history into benchmark-ready tables.
5. Run the first threshold-sensitivity study without changing defaults.
6. Produce the first formal validation report before any scientific threshold recalibration.
