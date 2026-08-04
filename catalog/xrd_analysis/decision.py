from __future__ import annotations

import math
from dataclasses import replace
from typing import Callable, Literal, Optional

import numpy as np

from .refinement import (
    _weighted_residual_sum,
    build_single_phase_refinement_request,
    build_two_phase_refinement_request,
    refine_candidate_pair,
    refine_candidate_pairs,
    refine_single_phase_candidate,
)
from .schemas import (
    ALLOWED_PHASE_STATES,
    AnalysisEvidenceComponents,
    CandidateGenerationResult,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    DecisionCriteriaResult,
    ModelComparisonSummary,
    ParsedPatternMetadata,
    PatternQualityControlResult,
    PenalizedModelMetrics,
    PhaseHypothesis,
    PhaseState,
    RankedPhaseCandidate,
    SelectedBestModel,
    SelectedModelKind,
    SinglePhaseHypothesisResult,
    SinglePhaseRefinementBatchResult,
    StabilityAssessmentResult,
    StabilityRunResult,
    TwoPhaseHypothesisResult,
    TwoPhasePairProposal,
    TwoPhaseRefinementBatchResult,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisResult,
    XRDAnalysisWarning,
)
from .reporting import sha256_digest


def run_final_phase_decision(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: ParsedPatternMetadata,
    quality_control: PatternQualityControlResult,
    candidate_generation: CandidateGenerationResult,
    single_phase_refinement: SinglePhaseRefinementBatchResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    progress_callback: Callable[[str, str], None] | None = None,
) -> XRDAnalysisResult:
    if not quality_control.passed:
        return XRDAnalysisResult(
            phase_state="insufficient-quality data",
            warnings=quality_control.warnings,
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            best_hypothesis=None,
            alternative_hypotheses=(),
            evidence_score=0.0,
            provenance=analysis_input.provenance,
            parsed_pattern=parsed_pattern,
            quality_control=quality_control,
            phase_candidates=tuple(candidate_generation.candidates),
            ranked_candidate_shortlist=tuple(candidate_generation.candidates),
            successful_single_phase_hypotheses=tuple(single_phase_refinement.successful_hypotheses),
            failed_single_phase_attempts=tuple(single_phase_refinement.failed_candidate_results),
            failure_codes=("insufficient-quality data",),
            analysis_provenance_notes=("decision:qc_failed",),
        )

    if progress_callback is not None:
        progress_callback(
            "generating_two_phase_pairs",
            "Building two-phase proposals from the strongest residual evidence.",
        )
    proposals, proposal_warnings = build_two_phase_pair_proposals(
        candidate_generation,
        single_phase_refinement,
        configuration=configuration,
    )
    requests = _build_two_phase_requests(
        analysis_input,
        parsed_pattern,
        proposals,
        candidate_generation,
        single_phase_refinement,
    )
    if progress_callback is not None:
        progress_callback("refining_two_phase", "Refining the strongest two-phase proposals.")
    two_phase_refinement = refine_candidate_pairs(
        requests,
        pair_proposals=proposals,
        configuration=configuration,
    )

    if progress_callback is not None:
        progress_callback("comparing_models", "Comparing one-phase and two-phase models.")
    model_comparison = compare_hypothesis_models(
        single_phase_refinement,
        two_phase_refinement,
        configuration=configuration,
    )
    best_single = _best_single_hypothesis(single_phase_refinement, model_comparison)
    best_two = _best_two_hypothesis(two_phase_refinement, model_comparison)
    if progress_callback is not None:
        progress_callback("stability_checks", "Running stability checks on the selected hypotheses.")
    stability = run_stability_checks(
        analysis_input,
        parsed_pattern,
        candidate_generation,
        single_phase_refinement,
        best_single,
        best_two,
        proposals,
        model_comparison,
        configuration=configuration,
    )
    decision_criteria = evaluate_decision_criteria(
        candidate_generation,
        best_single,
        best_two,
        model_comparison,
        stability,
        configuration=configuration,
    )
    phase_state, selected_model, failure_codes, decision_warnings = classify_phase_state(
        best_single,
        best_two,
        model_comparison,
        decision_criteria,
        stability,
        configuration=configuration,
    )
    evidence_components = compute_evidence_components(
        phase_state,
        quality_control,
        candidate_generation,
        single_phase_refinement,
        best_single,
        best_two,
        model_comparison,
        decision_criteria,
        stability,
        configuration=configuration,
    )
    warnings = tuple(
        _dedupe_warnings(
            list(candidate_generation.warnings)
            + list(single_phase_refinement.warnings)
            + list(two_phase_refinement.warnings)
            + list(proposal_warnings)
            + list(model_comparison.warnings)
            + list(decision_criteria.warnings)
            + list(stability.warnings)
            + list(decision_warnings)
        )
    )
    best_hypothesis = None
    if selected_model.model_type == "single_phase" and best_single is not None:
        best_hypothesis = PhaseHypothesis(
            hypothesis_id=best_single.hypothesis_id,
            crystalline_phase_count=1,
            candidate_ids=(best_single.candidate_id,),
            description="best_single_phase_model",
            evidence_score=evidence_components.overall_score,
            provenance={"selected_model": "single_phase"},
        )
    elif selected_model.model_type == "two_phase" and best_two is not None:
        best_hypothesis = PhaseHypothesis(
            hypothesis_id=best_two.hypothesis_id,
            crystalline_phase_count=2,
            candidate_ids=best_two.candidate_ids,
            description="best_two_phase_model",
            evidence_score=evidence_components.overall_score,
            provenance={"selected_model": "two_phase"},
        )
    alternatives: list[PhaseHypothesis] = []
    if best_single is not None:
        alternatives.append(
            PhaseHypothesis(
                hypothesis_id=best_single.hypothesis_id,
                crystalline_phase_count=1,
                candidate_ids=(best_single.candidate_id,),
                description="single_phase_candidate",
                evidence_score=best_single.goodness_of_fit,
            )
        )
    if best_two is not None:
        alternatives.append(
            PhaseHypothesis(
                hypothesis_id=best_two.hypothesis_id,
                crystalline_phase_count=2,
                candidate_ids=best_two.candidate_ids,
                description="two_phase_candidate",
                evidence_score=best_two.goodness_of_fit,
            )
        )
    return XRDAnalysisResult(
        phase_state=phase_state,
        warnings=warnings,
        algorithm_version=analysis_input.algorithm_version,
        configuration_version=analysis_input.configuration_version,
        best_hypothesis=best_hypothesis,
        alternative_hypotheses=tuple(alternatives),
        evidence_score=evidence_components.overall_score,
        provenance=analysis_input.provenance,
        parsed_pattern=parsed_pattern,
        quality_control=quality_control,
        phase_candidates=tuple(candidate_generation.candidates),
        ranked_candidate_shortlist=tuple(candidate_generation.candidates),
        successful_single_phase_hypotheses=tuple(single_phase_refinement.successful_hypotheses),
        failed_single_phase_attempts=tuple(single_phase_refinement.failed_candidate_results),
        successful_two_phase_hypotheses=tuple(two_phase_refinement.successful_hypotheses),
        failed_two_phase_attempts=tuple(two_phase_refinement.failed_hypotheses),
        best_single_phase_hypothesis=best_single,
        best_two_phase_hypothesis=best_two,
        selected_best_model=selected_model,
        model_comparison=model_comparison,
        decision_criteria=decision_criteria,
        stability_results=stability,
        evidence_components=evidence_components,
        failure_codes=failure_codes,
        analysis_provenance_notes=(
            f"two_phase_proposals={len(proposals)}",
            f"two_phase_successful={len(two_phase_refinement.successful_hypotheses)}",
            f"selected_model={selected_model.model_type}",
        ),
    )


def build_two_phase_pair_proposals(
    candidate_generation: CandidateGenerationResult,
    single_phase_refinement: SinglePhaseRefinementBatchResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> tuple[tuple[TwoPhasePairProposal, ...], tuple[XRDAnalysisWarning, ...]]:
    warnings: list[XRDAnalysisWarning] = []
    successful_single = [hyp for hyp in single_phase_refinement.successful_hypotheses if hyp.refinement_status in {"completed", "partial"}]
    if not successful_single:
        warnings.append(_decision_warning("no_valid_single_phase_hypothesis", "single_phase_refinement", "No valid single-phase hypothesis is available for pair expansion."))
        return (), tuple(warnings)

    hypotheses = sorted(
        successful_single,
        key=lambda hypothesis: (
            hypothesis.rwp if hypothesis.rwp is not None else float("inf"),
            -hypothesis.screening_pre_rank_score,
            hypothesis.hypothesis_id,
        ),
    )
    base_hypotheses = hypotheses[: configuration.decision.maximum_base_single_phase_hypotheses]
    if configuration.decision.preserve_intended_candidate_when_available:
        intended = next(
            (
                hypothesis
                for hypothesis in hypotheses
                if any(candidate.candidate_id == hypothesis.candidate_id and candidate.intended_structure_match for candidate in candidate_generation.candidates)
            ),
            None,
        )
        if intended is not None and all(item.hypothesis_id != intended.hypothesis_id for item in base_hypotheses):
            base_hypotheses.append(intended)

    pair_map: dict[tuple[str, str], TwoPhasePairProposal] = {}
    candidate_lookup = {candidate.candidate_id: candidate for candidate in candidate_generation.candidates}
    for base_hypothesis in base_hypotheses:
        base_candidate = candidate_lookup.get(base_hypothesis.candidate_id)
        if base_candidate is None:
            continue
        ranked = []
        for candidate in candidate_generation.candidates:
            if candidate.candidate_id == base_candidate.candidate_id:
                continue
            score = _rank_secondary_candidate(base_hypothesis, candidate, configuration=configuration)
            if score is None:
                continue
            ranked.append((score, candidate))
        ranked.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], item[1].candidate_id))
        for (proposal_score, supported_regions, distinctive_regions), candidate in ranked[: configuration.decision.maximum_secondary_candidates_per_base]:
            normalized_ids = tuple(sorted((base_candidate.candidate_id, candidate.candidate_id)))
            proposal = TwoPhasePairProposal(
                proposal_id=sha256_digest(
                    {
                        "base_hypothesis_id": base_hypothesis.hypothesis_id,
                        "candidate_ids": normalized_ids,
                    }
                ),
                base_single_phase_hypothesis_id=base_hypothesis.hypothesis_id,
                primary_candidate_id=base_candidate.candidate_id,
                secondary_candidate_id=candidate.candidate_id,
                normalized_candidate_ids=(normalized_ids[0], normalized_ids[1]),
                proposal_score=proposal_score,
                supported_residual_region_count=supported_regions,
                partially_distinctive_region_count=distinctive_regions,
                includes_intended_structure=bool(base_candidate.intended_structure_match or candidate.intended_structure_match),
                provenance=(
                    f"base_candidate={base_candidate.candidate_id}",
                    f"secondary_candidate={candidate.candidate_id}",
                    f"supported_residual_regions={supported_regions}",
                    f"distinctive_regions={distinctive_regions}",
                ),
            )
            existing = pair_map.get(normalized_ids)
            if existing is not None:
                warnings.append(_decision_warning("duplicate_two_phase_pair_removed", "pair_generation", f"Removed duplicate candidate pair {normalized_ids[0]} + {normalized_ids[1]} after order normalization."))
                if existing.proposal_score >= proposal.proposal_score:
                    continue
            pair_map[normalized_ids] = proposal

    proposals = tuple(
        sorted(
            pair_map.values(),
            key=lambda proposal: (-proposal.proposal_score, -proposal.supported_residual_region_count, proposal.normalized_candidate_ids),
        )[: configuration.decision.maximum_total_two_phase_refinements]
    )
    if not proposals:
        warnings.append(_decision_warning("two_phase_pair_generation_failed", "pair_generation", "No valid two-phase proposals could be generated from the retained candidate set."))
    return proposals, tuple(_dedupe_warnings(warnings))


def compare_hypothesis_models(
    single_phase_refinement: SinglePhaseRefinementBatchResult,
    two_phase_refinement: TwoPhaseRefinementBatchResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> ModelComparisonSummary:
    single_metrics = [
        _penalized_metrics_for_single(hypothesis)
        for hypothesis in single_phase_refinement.successful_hypotheses
        if hypothesis.refinement_status in {"completed", "partial"}
    ]
    two_metrics = [
        _penalized_metrics_for_two(hypothesis)
        for hypothesis in two_phase_refinement.successful_hypotheses
        if hypothesis.refinement_status in {"completed", "partial"}
    ]
    warnings: list[XRDAnalysisWarning] = []
    best_single = _select_best_metrics(single_metrics)
    best_two = _select_best_metrics(two_metrics)
    if best_single is None and best_two is None:
        warnings.append(_decision_warning("model_comparison_unavailable", "model_comparison", "No valid hypothesis exposed comparable fit metrics."))
        return ModelComparisonSummary(
            best_single_phase_metrics=None,
            best_two_phase_metrics=None,
            preferred_model="none",
            warnings=tuple(warnings),
        )
    if best_single is None:
        warnings.append(_decision_warning("no_valid_single_phase_hypothesis", "single_phase_refinement", "No valid single-phase hypothesis was available for penalized comparison."))
    if best_two is None:
        warnings.append(_decision_warning("no_valid_two_phase_hypothesis", "two_phase_refinement", "No valid two-phase hypothesis was available for penalized comparison."))
    deltas = _comparison_deltas(best_single, best_two)
    preferred_model: SelectedModelKind = "none"
    threshold = configuration.decision.minimum_penalized_improvement
    if best_single is not None and best_two is None:
        preferred_model = "single_phase"
    elif best_two is not None and best_single is None:
        preferred_model = "two_phase"
    else:
        two_phase_support = _metric_supports_model(
            deltas,
            aic_threshold=configuration.decision.aic_threshold,
            aicc_threshold=configuration.decision.aicc_threshold,
            bic_threshold=configuration.decision.bic_threshold,
            direction="two_phase",
        )
        single_phase_support = _metric_supports_model(
            deltas,
            aic_threshold=configuration.decision.aic_threshold,
            aicc_threshold=configuration.decision.aicc_threshold,
            bic_threshold=configuration.decision.bic_threshold,
            direction="single_phase",
        )
        comparable = [value for value in (deltas["aic"], deltas["aicc"], deltas["bic"]) if value is not None]
        max_margin = max((abs(value) for value in comparable), default=0.0)
        if two_phase_support and all(value >= threshold for value in comparable):
            preferred_model = "two_phase"
        elif single_phase_support and all(value <= -threshold for value in comparable):
            preferred_model = "single_phase"
        elif comparable and max_margin >= configuration.decision.ambiguity_margin:
            warnings.append(_decision_warning("model_comparison_ambiguous", "model_comparison", "One-phase and two-phase penalized model support remained too close to separate confidently."))
        elif comparable:
            warnings.append(_decision_warning("model_comparison_ambiguous", "model_comparison", "One-phase and two-phase penalized model support remained within the configured ambiguity range."))
    return ModelComparisonSummary(
        best_single_phase_metrics=best_single,
        best_two_phase_metrics=best_two,
        preferred_model=preferred_model,
        delta_aic=deltas["aic"],
        delta_aicc=deltas["aicc"],
        delta_bic=deltas["bic"],
        minimum_penalized_improvement=threshold,
        warnings=tuple(_dedupe_warnings(warnings)),
    )


def evaluate_decision_criteria(
    candidate_generation: CandidateGenerationResult,
    best_single: Optional[SinglePhaseHypothesisResult],
    best_two: Optional[TwoPhaseHypothesisResult],
    model_comparison: ModelComparisonSummary,
    stability: StabilityAssessmentResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> DecisionCriteriaResult:
    warnings: list[XRDAnalysisWarning] = []
    failure_codes: list[str] = []
    single_phase_adequate = bool(
        best_single is not None
        and len(best_single.significant_positive_residual_regions) <= configuration.decision.adequate_single_phase_max_residual_regions
        and len(best_single.unsupported_strong_predicted_regions) <= configuration.decision.adequate_single_phase_max_unsupported_regions
    )
    if best_two is None:
        second_phase_positive_scale = False
        second_phase_scale_above_threshold = False
        second_phase_reflection_support_sufficient = False
        second_phase_partially_distinctive = False
        second_phase_not_dominated_by_one_region = False
        two_phase_penalized_improvement = False
    else:
        second_scale = best_two.phase_results[1].scale_factor or 0.0
        second_phase_positive_scale = second_scale > 0
        second_phase_scale_above_threshold = second_scale >= configuration.decision.minimum_second_phase_scale_factor
        support_regions = len(best_two.regions_supported_by_both_phases) + len(best_two.regions_primarily_supported_by_one_phase)
        second_phase_reflection_support_sufficient = support_regions >= configuration.decision.minimum_supported_reflection_regions
        second_phase_partially_distinctive = len(best_two.regions_primarily_supported_by_one_phase) >= configuration.decision.minimum_partially_distinctive_regions
        second_phase_not_dominated_by_one_region = _not_dominated_by_one_region(best_two, configuration=configuration)
        two_phase_penalized_improvement = model_comparison.preferred_model == "two_phase"
        if second_phase_positive_scale and not second_phase_partially_distinctive:
            warnings.append(_decision_warning("second_phase_evidence_fully_overlapped", "two_phase_support", "The candidate second phase is supported only by shared or fully overlapping regions."))
            failure_codes.append("second_phase_evidence_fully_overlapped")
    classification_stable = stability.classification_agreement >= configuration.decision.minimum_classification_agreement
    score_stable = (stability.score_stability or 0.0) >= configuration.decision.minimum_score_stability
    candidate_set_may_be_incomplete = len(candidate_generation.candidates) < configuration.candidate_ranking.final_top_k or best_two is None
    if candidate_set_may_be_incomplete:
        warnings.append(_decision_warning("candidate_set_may_be_incomplete", "candidate_generation", "The retained Milestone 4 candidate shortlist may not contain the true second phase."))
    if not classification_stable:
        warnings.append(_decision_warning("unstable_phase_assignment", "stability", "The provisional phase-state assignment changed materially under deterministic perturbations."))
        failure_codes.append("unstable_phase_assignment")
    if not score_stable:
        warnings.append(_decision_warning("unstable_phase_assignment", "stability", "The penalized model preference did not remain sufficiently stable under deterministic perturbations."))
        failure_codes.append("unstable_phase_assignment")
    if best_two is not None and stability.scale_stability is not None and stability.scale_stability < configuration.decision.minimum_scale_stability:
        warnings.append(_decision_warning("unstable_second_phase_scale", "stability", "The second-phase scale factor varied too strongly under deterministic perturbations."))
        failure_codes.append("unstable_second_phase_scale")
    if best_single is None and best_two is None:
        failure_codes.append("final_decision_unresolved")
    return DecisionCriteriaResult(
        single_phase_adequate=single_phase_adequate,
        two_phase_penalized_improvement=two_phase_penalized_improvement,
        second_phase_positive_scale=second_phase_positive_scale,
        second_phase_scale_above_threshold=second_phase_scale_above_threshold,
        second_phase_reflection_support_sufficient=second_phase_reflection_support_sufficient,
        second_phase_partially_distinctive=second_phase_partially_distinctive,
        second_phase_not_dominated_by_one_region=second_phase_not_dominated_by_one_region,
        classification_stable=classification_stable,
        score_stable=score_stable,
        candidate_set_may_be_incomplete=candidate_set_may_be_incomplete,
        warnings=tuple(_dedupe_warnings(warnings)),
        failure_codes=tuple(dict.fromkeys(failure_codes)),
    )


def run_stability_checks(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: ParsedPatternMetadata,
    candidate_generation: CandidateGenerationResult,
    single_phase_refinement: SinglePhaseRefinementBatchResult,
    best_single: Optional[SinglePhaseHypothesisResult],
    best_two: Optional[TwoPhaseHypothesisResult],
    proposals: tuple[TwoPhasePairProposal, ...],
    model_comparison: ModelComparisonSummary,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> StabilityAssessmentResult:
    runs: list[StabilityRunResult] = []
    warnings: list[XRDAnalysisWarning] = []
    candidate_stability_flags: list[bool] = []
    for multiplier in configuration.decision.residual_threshold_multipliers:
        perturbed = replace(
            configuration,
            decision=replace(
                configuration.decision,
                minimum_residual_region_strength_fraction=configuration.decision.minimum_residual_region_strength_fraction * multiplier,
            ),
        )
        perturbed_proposals, _ = build_two_phase_pair_proposals(
            candidate_generation,
            single_phase_refinement,
            configuration=perturbed,
        )
        candidate_stability_flags.append(
            bool(proposals)
            and bool(perturbed_proposals)
            and perturbed_proposals[0].normalized_candidate_ids == proposals[0].normalized_candidate_ids
        )
    if best_single is not None:
        for delta in (-configuration.decision.zero_shift_seed_delta, configuration.decision.zero_shift_seed_delta):
            request = build_single_phase_refinement_request(
                analysis_input,
                parsed_pattern,
                next(candidate for candidate in candidate_generation.candidates if candidate.candidate_id == best_single.candidate_id),
            )
            perturbed_request = replace(
                request,
                initial_phase_scale_factor=best_single.phase_scale_factor,
                initial_zero_shift=(best_single.zero_shift or 0.0) + delta,
            )
            result = refine_single_phase_candidate(
                perturbed_request,
                configuration=replace(
                    configuration,
                    single_phase_refinement=replace(
                        configuration.single_phase_refinement,
                        background_coefficient_count=max(2, configuration.single_phase_refinement.background_coefficient_count + configuration.decision.background_coefficient_perturbation),
                    ),
                ),
            )
            runs.append(
                StabilityRunResult(
                    perturbation_label=f"single_zero_shift_{delta:+.3f}",
                    model_type="single_phase",
                    candidate_ids=(best_single.candidate_id,),
                    selected_hypothesis_id=result.hypothesis_id,
                    phase_state="likely single-phase" if result.refinement_status in {"completed", "partial"} else "unresolved",
                    background_coefficient_count=max(2, configuration.single_phase_refinement.background_coefficient_count + configuration.decision.background_coefficient_perturbation),
                    residual_threshold_multiplier=1.0,
                    scale_seed_multipliers=(1.0,),
                    zero_shift_seed=(best_single.zero_shift or 0.0) + delta,
                    dominant_region_downweighted=False,
                    penalized_score_delta=None,
                    second_phase_scale_factor=None,
                    provenance=(f"refinement_status={result.refinement_status}",),
                )
            )
            if len(runs) >= configuration.decision.maximum_stability_runs:
                break
    if best_two is not None and len(runs) < configuration.decision.maximum_stability_runs:
        candidate_lookup = {candidate.candidate_id: candidate for candidate in candidate_generation.candidates}
        for multiplier in configuration.decision.scale_seed_multipliers:
            if len(runs) >= configuration.decision.maximum_stability_runs:
                break
            request = build_two_phase_refinement_request(
                analysis_input,
                parsed_pattern,
                candidate_lookup[best_two.candidate_ids[0]],
                candidate_lookup[best_two.candidate_ids[1]],
                source_single_phase_hypothesis_id=best_two.source_single_phase_hypothesis_id,
                proposal_provenance=best_two.proposal_provenance,
                initial_phase_scale_factors=(
                    (best_two.phase_results[0].scale_factor or 1.0) * multiplier,
                    (best_two.phase_results[1].scale_factor or configuration.decision.minimum_second_phase_scale_factor) * multiplier,
                ),
                initial_zero_shift=best_two.zero_shift,
            )
            result = refine_candidate_pair(request, configuration=configuration)
            second_scale = result.phase_results[1].scale_factor
            runs.append(
                StabilityRunResult(
                    perturbation_label=f"two_phase_scale_seed_{multiplier:.2f}",
                    model_type="two_phase",
                    candidate_ids=result.candidate_ids,
                    selected_hypothesis_id=result.hypothesis_id,
                    phase_state="likely multiphase" if (result.refinement_status in {"completed", "partial"} and (second_scale or 0.0) >= configuration.decision.minimum_second_phase_scale_factor) else "unresolved",
                    background_coefficient_count=configuration.single_phase_refinement.background_coefficient_count,
                    residual_threshold_multiplier=1.0,
                    scale_seed_multipliers=(multiplier,),
                    zero_shift_seed=best_two.zero_shift,
                    dominant_region_downweighted=False,
                    penalized_score_delta=None,
                    second_phase_scale_factor=second_scale,
                    provenance=(f"refinement_status={result.refinement_status}",),
                )
            )
    if not runs:
        warnings.append(_decision_warning("model_comparison_unavailable", "stability", "No stability perturbation run could be executed for the best available hypotheses."))
        return StabilityAssessmentResult(
            runs=(),
            classification_agreement=0.0,
            candidate_agreement=0.0,
            second_phase_support_retained=False,
            scale_stability=None,
            score_stability=None,
            warnings=tuple(warnings),
        )
    phase_states = [run.phase_state for run in runs]
    most_common_state = max(set(phase_states), key=phase_states.count)
    classification_agreement = phase_states.count(most_common_state) / len(phase_states)
    candidate_agreement = sum(1 for item in candidate_stability_flags if item) / len(candidate_stability_flags) if candidate_stability_flags else 1.0
    second_phase_scales = [run.second_phase_scale_factor for run in runs if run.second_phase_scale_factor is not None]
    scale_stability = None
    if second_phase_scales:
        base = max(abs(second_phase_scales[0]), configuration.decision.minimum_second_phase_scale_factor)
        scale_stability = max(0.0, 1.0 - (float(np.std(second_phase_scales)) / base))
    score_stability = classification_agreement
    second_phase_support_retained = any(run.phase_state == "likely multiphase" for run in runs)
    return StabilityAssessmentResult(
        runs=tuple(runs),
        classification_agreement=classification_agreement,
        candidate_agreement=candidate_agreement,
        second_phase_support_retained=second_phase_support_retained,
        scale_stability=scale_stability,
        score_stability=score_stability,
        warnings=tuple(_dedupe_warnings(warnings)),
    )


def compute_evidence_components(
    phase_state: PhaseState,
    quality_control: PatternQualityControlResult,
    candidate_generation: CandidateGenerationResult,
    single_phase_refinement: SinglePhaseRefinementBatchResult,
    best_single: Optional[SinglePhaseHypothesisResult],
    best_two: Optional[TwoPhaseHypothesisResult],
    model_comparison: ModelComparisonSummary,
    decision_criteria: DecisionCriteriaResult,
    stability: StabilityAssessmentResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> AnalysisEvidenceComponents:
    margin = max(
        0.0,
        min(
            configuration.decision.evidence_score_clip,
            max(abs(model_comparison.delta_bic or 0.0), abs(model_comparison.delta_aic or 0.0)) / 10.0,
        ),
    )
    if phase_state == "likely single-phase":
        residual_evidence = 1.0 - min(
            1.0,
            (len(best_single.significant_positive_residual_regions) if best_single is not None else 4)
            / max(configuration.decision.adequate_single_phase_max_residual_regions + 1, 1),
        )
    else:
        residual_evidence = min(
            1.0,
            (
                len(best_two.regions_supported_by_both_phases) + len(best_two.regions_primarily_supported_by_one_phase)
                if best_two is not None
                else 0
            )
            / max(configuration.decision.minimum_supported_reflection_regions + 1, 1),
        )
    second_phase_evidence = 0.0
    if best_two is not None:
        second_phase_evidence = sum(
            [
                1.0 if decision_criteria.second_phase_positive_scale else 0.0,
                1.0 if decision_criteria.second_phase_scale_above_threshold else 0.0,
                1.0 if decision_criteria.second_phase_reflection_support_sufficient else 0.0,
                1.0 if decision_criteria.second_phase_partially_distinctive else 0.0,
                1.0 if decision_criteria.second_phase_not_dominated_by_one_region else 0.0,
            ]
        ) / 5.0
    stability_score = min(1.0, max(0.0, stability.classification_agreement))
    data_quality = 1.0 if quality_control.passed else 0.0
    candidate_coverage = min(1.0, len(candidate_generation.candidates) / max(configuration.candidate_ranking.final_top_k, 1))
    convergence = min(
        1.0,
        (
            len(single_phase_refinement.successful_hypotheses)
            + (1 if best_two is not None else 0)
        )
        / max(configuration.single_phase_refinement.maximum_single_phase_candidates_refined + 1, 1),
    )
    overall = float(np.mean([margin, residual_evidence, second_phase_evidence, stability_score, data_quality, candidate_coverage, convergence]))
    return AnalysisEvidenceComponents(
        model_comparison_margin=margin,
        residual_evidence=residual_evidence,
        second_phase_evidence=second_phase_evidence,
        stability=stability_score,
        data_quality=data_quality,
        candidate_coverage=candidate_coverage,
        refinement_convergence=convergence,
        overall_score=overall,
    )


def classify_phase_state(
    best_single: Optional[SinglePhaseHypothesisResult],
    best_two: Optional[TwoPhaseHypothesisResult],
    model_comparison: ModelComparisonSummary,
    decision_criteria: DecisionCriteriaResult,
    stability: StabilityAssessmentResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> tuple[PhaseState, SelectedBestModel, tuple[str, ...], tuple[XRDAnalysisWarning, ...]]:
    warnings: list[XRDAnalysisWarning] = []
    failure_codes: list[str] = []
    if best_single is None and best_two is None:
        failure_codes.extend(("no_valid_single_phase_hypothesis", "no_valid_two_phase_hypothesis", "final_decision_unresolved"))
        warnings.append(_decision_warning("final_decision_unresolved", "decision", "All plausible one-phase and two-phase hypotheses failed before a defensible phase-state decision could be made."))
        return "unresolved", SelectedBestModel("none", None, (), "no_valid_hypotheses"), tuple(dict.fromkeys(failure_codes)), tuple(warnings)
    if (
        best_two is not None
        and model_comparison.preferred_model == "two_phase"
        and decision_criteria.two_phase_penalized_improvement
        and decision_criteria.second_phase_positive_scale
        and decision_criteria.second_phase_scale_above_threshold
        and decision_criteria.second_phase_reflection_support_sufficient
        and decision_criteria.second_phase_partially_distinctive
        and decision_criteria.second_phase_not_dominated_by_one_region
        and decision_criteria.classification_stable
        and decision_criteria.score_stable
        and (stability.scale_stability is None or stability.scale_stability >= configuration.decision.minimum_scale_stability)
    ):
        return "likely multiphase", SelectedBestModel("two_phase", best_two.hypothesis_id, best_two.candidate_ids, "penalized_two_phase_advantage"), (), ()
    if (
        best_single is not None
        and decision_criteria.single_phase_adequate
        and (best_two is None or not decision_criteria.two_phase_penalized_improvement)
        and decision_criteria.classification_stable
        and decision_criteria.score_stable
    ):
        return "likely single-phase", SelectedBestModel("single_phase", best_single.hypothesis_id, (best_single.candidate_id,), "adequate_single_phase_model"), (), ()
    failure_codes.append("final_decision_unresolved")
    if not decision_criteria.classification_stable:
        failure_codes.append("unstable_phase_assignment")
    if best_two is None:
        failure_codes.append("no_valid_two_phase_hypothesis")
    warnings.append(_decision_warning("final_decision_unresolved", "decision", "The available one-phase and two-phase evidence remained ambiguous after penalized comparison and deterministic stability checks."))
    return "unresolved", SelectedBestModel("none", None, (), "ambiguous_or_unstable"), tuple(dict.fromkeys(failure_codes)), tuple(_dedupe_warnings(warnings))


def _build_two_phase_requests(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: ParsedPatternMetadata,
    proposals: tuple[TwoPhasePairProposal, ...],
    candidate_generation: CandidateGenerationResult,
    single_phase_refinement: SinglePhaseRefinementBatchResult,
) -> tuple:
    candidate_lookup = {candidate.candidate_id: candidate for candidate in candidate_generation.candidates}
    single_lookup = {hypothesis.hypothesis_id: hypothesis for hypothesis in single_phase_refinement.successful_hypotheses}
    requests = []
    for proposal in proposals:
        base_hypothesis = single_lookup.get(proposal.base_single_phase_hypothesis_id)
        primary_candidate = candidate_lookup.get(proposal.primary_candidate_id)
        secondary_candidate = candidate_lookup.get(proposal.secondary_candidate_id)
        if base_hypothesis is None or primary_candidate is None or secondary_candidate is None:
            continue
        base_scale = base_hypothesis.phase_scale_factor or 1.0
        secondary_scale = max(0.25 * base_scale, 0.05)
        requests.append(
            build_two_phase_refinement_request(
                analysis_input,
                parsed_pattern,
                primary_candidate,
                secondary_candidate,
                source_single_phase_hypothesis_id=base_hypothesis.hypothesis_id,
                proposal_provenance=proposal.provenance,
                initial_phase_scale_factors=(base_scale, secondary_scale),
                initial_zero_shift=base_hypothesis.zero_shift,
            )
        )
    return tuple(requests)


def _rank_secondary_candidate(
    base_hypothesis: SinglePhaseHypothesisResult,
    candidate: RankedPhaseCandidate,
    *,
    configuration: XRDAnalysisConfig,
) -> Optional[tuple[float, int, int]]:
    simulation = candidate.simulation
    if simulation is None:
        return None
    residual_regions = list(base_hypothesis.significant_positive_residual_regions)
    if not residual_regions:
        return None
    max_strength = max(region.integrated_positive_residual for region in residual_regions) or 1.0
    effective_regions = [
        region
        for region in residual_regions
        if (region.integrated_positive_residual / max_strength) >= configuration.decision.minimum_residual_region_strength_fraction
    ]
    if len(effective_regions) < configuration.decision.minimum_residual_region_count:
        return None
    base_positions = [reflection.two_theta for reflection in base_hypothesis.expected_reflections]
    max_candidate_intensity = max(simulation.reflection_relative_intensities) if simulation.reflection_relative_intensities else 1.0
    score = 0.0
    supported_regions = 0
    distinctive_regions = 0
    for region in effective_regions:
        supporting = []
        for two_theta, rel_intensity in zip(simulation.reflection_positions_two_theta, simulation.reflection_relative_intensities):
            if region.start_two_theta - configuration.candidate_ranking.peak_position_tolerance_degrees <= two_theta <= region.end_two_theta + configuration.candidate_ranking.peak_position_tolerance_degrees:
                supporting.append((two_theta, rel_intensity))
        if not supporting:
            continue
        supported_regions += 1
        local_strength = max(intensity for _, intensity in supporting) / max(max_candidate_intensity, 1e-12)
        score += region.integrated_positive_residual * local_strength
        if any(all(abs(two_theta - base_position) > configuration.candidate_ranking.peak_position_tolerance_degrees for base_position in base_positions) for two_theta, _ in supporting):
            distinctive_regions += 1
            score += 0.5 * region.maximum_residual
    if supported_regions < configuration.decision.minimum_residual_region_count:
        return None
    return score, supported_regions, distinctive_regions


def _penalized_metrics_for_single(hypothesis: SinglePhaseHypothesisResult) -> PenalizedModelMetrics:
    wrss = _weighted_residual_sum(hypothesis.observed_intensities, hypothesis.calculated_total_pattern)
    return _penalized_metrics(
        hypothesis_id=hypothesis.hypothesis_id,
        model_type="single_phase",
        candidate_ids=(hypothesis.candidate_id,),
        weighted_residual_sum=wrss,
        observation_count=hypothesis.observation_count,
        refined_parameter_count=max(hypothesis.refined_parameter_count, 1),
    )


def _penalized_metrics_for_two(hypothesis: TwoPhaseHypothesisResult) -> PenalizedModelMetrics:
    wrss = _weighted_residual_sum(hypothesis.observed_intensities, hypothesis.calculated_total_pattern)
    return _penalized_metrics(
        hypothesis_id=hypothesis.hypothesis_id,
        model_type="two_phase",
        candidate_ids=hypothesis.candidate_ids,
        weighted_residual_sum=wrss,
        observation_count=hypothesis.observation_count,
        refined_parameter_count=max(hypothesis.refined_parameter_count, 1),
    )


def _penalized_metrics(
    *,
    hypothesis_id: str,
    model_type: SelectedModelKind,
    candidate_ids: tuple[str, ...],
    weighted_residual_sum: float,
    observation_count: int,
    refined_parameter_count: int,
) -> PenalizedModelMetrics:
    n = max(observation_count, 1)
    k = max(refined_parameter_count, 1)
    wrss_per_obs = max(weighted_residual_sum / n, 1e-12)
    aic = (n * math.log(wrss_per_obs)) + (2 * k)
    aicc = None
    if n > k + 1:
        aicc = aic + ((2 * k * (k + 1)) / (n - k - 1))
    bic = (n * math.log(wrss_per_obs)) + (k * math.log(n))
    return PenalizedModelMetrics(
        hypothesis_id=hypothesis_id,
        model_type=model_type,
        candidate_ids=candidate_ids,
        weighted_residual_sum=weighted_residual_sum,
        observation_count=n,
        refined_parameter_count=k,
        aic=aic,
        aicc=aicc,
        bic=bic,
    )


def _select_best_metrics(metrics: list[PenalizedModelMetrics]) -> Optional[PenalizedModelMetrics]:
    if not metrics:
        return None
    return min(
        metrics,
        key=lambda item: (
            item.bic if item.bic is not None else float("inf"),
            item.aicc if item.aicc is not None else float("inf"),
            item.aic if item.aic is not None else float("inf"),
            item.hypothesis_id,
        ),
    )


def _comparison_deltas(
    best_single: Optional[PenalizedModelMetrics],
    best_two: Optional[PenalizedModelMetrics],
) -> dict[str, Optional[float]]:
    if best_single is None or best_two is None:
        return {"aic": None, "aicc": None, "bic": None}
    return {
        "aic": (best_single.aic - best_two.aic) if best_single.aic is not None and best_two.aic is not None else None,
        "aicc": (best_single.aicc - best_two.aicc) if best_single.aicc is not None and best_two.aicc is not None else None,
        "bic": (best_single.bic - best_two.bic) if best_single.bic is not None and best_two.bic is not None else None,
    }


def _metric_supports_model(
    deltas: dict[str, Optional[float]],
    *,
    aic_threshold: float,
    aicc_threshold: float,
    bic_threshold: float,
    direction: Literal["single_phase", "two_phase"],
) -> bool:
    metric_thresholds = {
        "aic": aic_threshold,
        "aicc": aicc_threshold,
        "bic": bic_threshold,
    }
    comparable = 0
    for metric_name, threshold in metric_thresholds.items():
        value = deltas.get(metric_name)
        if value is None:
            continue
        comparable += 1
        if direction == "two_phase":
            if value < threshold:
                return False
        else:
            if value > (-threshold):
                return False
    return comparable > 0


def _best_single_hypothesis(
    single_phase_refinement: SinglePhaseRefinementBatchResult,
    model_comparison: ModelComparisonSummary,
) -> Optional[SinglePhaseHypothesisResult]:
    if model_comparison.best_single_phase_metrics is None:
        return None
    return next(
        (hypothesis for hypothesis in single_phase_refinement.successful_hypotheses if hypothesis.hypothesis_id == model_comparison.best_single_phase_metrics.hypothesis_id),
        None,
    )


def _best_two_hypothesis(
    two_phase_refinement: TwoPhaseRefinementBatchResult,
    model_comparison: ModelComparisonSummary,
) -> Optional[TwoPhaseHypothesisResult]:
    if model_comparison.best_two_phase_metrics is None:
        return None
    return next(
        (hypothesis for hypothesis in two_phase_refinement.successful_hypotheses if hypothesis.hypothesis_id == model_comparison.best_two_phase_metrics.hypothesis_id),
        None,
    )


def _not_dominated_by_one_region(
    best_two: TwoPhaseHypothesisResult,
    *,
    configuration: XRDAnalysisConfig,
) -> bool:
    regions = list(best_two.regions_primarily_supported_by_one_phase) + list(best_two.regions_supported_by_both_phases)
    if not regions:
        return False
    weights = [max(region.nearby_observed_peak_intensity or 0.0, 0.0) for region in regions]
    total = sum(weights)
    if total <= 0.0:
        return False
    return (max(weights) / total) <= configuration.decision.maximum_single_region_evidence_fraction


def _decision_warning(code: str, field: str, message: str) -> XRDAnalysisWarning:
    return XRDAnalysisWarning(
        code=code,
        message=message,
        severity="warning",
        field=field,
        stage="decision",
    )


def _dedupe_warnings(warnings: list[XRDAnalysisWarning]) -> list[XRDAnalysisWarning]:
    seen = set()
    unique = []
    for warning in warnings:
        key = (warning.code, warning.message, warning.severity, warning.field, warning.stage)
        if key in seen:
            continue
        seen.add(key)
        unique.append(warning)
    return unique


__all__ = [
    "build_two_phase_pair_proposals",
    "classify_phase_state",
    "compare_hypothesis_models",
    "compute_evidence_components",
    "evaluate_decision_criteria",
    "run_final_phase_decision",
    "run_stability_checks",
]
