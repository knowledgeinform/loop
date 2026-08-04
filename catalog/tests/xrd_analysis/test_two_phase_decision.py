import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from catalog.gsas_runtime import (
    cleanup_paths,
    clear_sample_scale_refinement,
    configure_gsas,
    new_project,
    prepare_project_path,
    resolve_instrument_parameter_file,
    set_project_cycles,
    write_temp_xye,
)
from catalog.xrd_analysis.candidates import REFERENCE_PHASES_DIR
from catalog.xrd_analysis.decision import (
    build_two_phase_pair_proposals,
    classify_phase_state,
    compare_hypothesis_models,
    evaluate_decision_criteria,
    run_final_phase_decision,
)
from catalog.xrd_analysis.pattern import normalize_table_pattern
from catalog.xrd_analysis.refinement import (
    _extract_phase_support_regions,
    _set_histogram_wavelength,
    _validate_two_phase_snapshot,
    build_two_phase_refinement_request,
    refine_candidate_pair,
    refine_candidate_pairs,
)
from catalog.xrd_analysis.reporting import dumps_canonical_json
from catalog.xrd_analysis.schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CandidateGenerationResult,
    CandidateSimulation,
    DecisionCriteriaResult,
    ExpectedReflectionRecord,
    InstrumentProfile,
    ParsedPatternMetadata,
    PatternProvenance,
    PatternQualityControlResult,
    PhaseRefinementSummary,
    PhaseSupportRegion,
    RankedPhaseCandidate,
    RawFileReference,
    RefinedLatticeParameters,
    ResidualRegion,
    SinglePhaseHypothesisResult,
    SinglePhaseRefinementBatchResult,
    StabilityAssessmentResult,
    StabilityRunResult,
    StoichiometricAmount,
    SynthesisContext,
    TwoPhaseHypothesisResult,
    TwoPhasePairProposal,
    TwoPhaseRefinementBatchResult,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
)


def _gsas_runtime_available():
    try:
        from GSASII import GSASIIscriptable  # type: ignore  # noqa: F401
        from GSASII import defaultIparms  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def _analysis_input() -> XRDAnalysisInput:
    return XRDAnalysisInput(
        material_auid="M:test",
        recipe_auid="M:test:R:test",
        trial_id="T1",
        raw_file_hash="ab" * 32,
        raw_file_reference=RawFileReference(
            reference_kind="stored_path",
            locator="/tmp/two_phase_scan.csv",
            original_filename="two_phase_scan.csv",
        ),
        nominal_composition="NaCl",
        elements=("Cl", "Na"),
        stoichiometric_amounts=(
            StoichiometricAmount("Cl", 1.0),
            StoichiometricAmount("Na", 1.0),
        ),
        structure_family="rocksalt",
        expected_space_group="F m -3 m",
        expected_site_assignments=(),
        radiation_source="cu_ka",
        wavelength_angstrom=1.5406,
        coordinate_type="two_theta",
        coordinate_column="Angle",
        intensity_column="Intensity",
        scan_min=20.0,
        scan_max=80.0,
        step_size=0.05,
        scan_speed=1.0,
        instrument_profile=None,
        synthesis_context=SynthesisContext(
            ordered_steps=(),
            precursor_records=(),
            temperatures_c=(),
            ramp_rates_c_min=(),
            hold_times_hours=(),
            atmospheres=(),
            furnace_types=(),
            preparation_notes=(),
        ),
        warnings=(),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        provenance=XRDAnalysisProvenance(
            source_material={"id": "M:test"},
            source_recipe={"id": "M:test:R:test"},
            source_trial={"trial_date": datetime(2026, 7, 27, tzinfo=timezone.utc).isoformat()},
            source_raw_file=None,
            measurement_metadata=(),
        ),
    )


def _candidate(
    candidate_id: str,
    *,
    positions: tuple[float, ...],
    intensities: tuple[float, ...],
    intended: bool = False,
    combined_score: float = 0.8,
    cif_name: str = "NaCl_rocksalt.cif",
    cif_hash: str = "hash",
) -> RankedPhaseCandidate:
    return RankedPhaseCandidate(
        candidate_id=candidate_id,
        source="curated_reference",
        source_identifier=f"local:{candidate_id}",
        source_snapshot="loop-reference-phases-v1",
        cif_path=str((REFERENCE_PHASES_DIR / cif_name).resolve()),
        cif_hash=f"{cif_hash}-{candidate_id}",
        formula="NaCl",
        normalized_composition=(
            StoichiometricAmount("Cl", 1.0),
            StoichiometricAmount("Na", 1.0),
        ),
        element_set=("Cl", "Na"),
        space_group="F m -3 m",
        structure_family="rocksalt",
        intended_structure_match=intended,
        chemical_compatibility_score=1.0,
        stoichiometric_similarity_score=1.0,
        synthesis_context_score=0.5,
        diffraction_pre_rank_score=combined_score,
        combined_pre_rank_score=combined_score,
        duplicate_cluster_id=None,
        warnings=(),
        provenance=("unit_test",),
        simulation=CandidateSimulation(
            candidate_id=candidate_id,
            reflection_positions_two_theta=positions,
            reflection_relative_intensities=intensities,
            measured_coordinate_min=20.0,
            measured_coordinate_max=80.0,
            wavelength_angstrom=1.5406,
            settings_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        ),
    )


def _single_hypothesis(
    candidate: RankedPhaseCandidate,
    *,
    residual_regions: tuple[ResidualRegion, ...] = (),
    expected_reflections: tuple[ExpectedReflectionRecord, ...] = (),
    observed_intensities: tuple[float, ...] = (100.0, 80.0, 60.0, 40.0),
    calculated_total: tuple[float, ...] = (98.0, 79.0, 59.0, 39.0),
    rwp: float = 8.0,
    refined_parameter_count: int = 4,
) -> SinglePhaseHypothesisResult:
    observed_two_theta = tuple(20.0 + (0.1 * idx) for idx in range(len(observed_intensities)))
    return SinglePhaseHypothesisResult(
        hypothesis_id=f"single-{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        candidate_source=candidate.source,
        candidate_source_identifier=candidate.source_identifier,
        candidate_cif_hash=candidate.cif_hash,
        reference_snapshot=candidate.source_snapshot,
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        gsasii_version="test",
        instrument_profile_serialization=None,
        screening_pre_rank_score=candidate.combined_pre_rank_score,
        refinement_status="completed",
        convergence_status="converged",
        completed_stages=(),
        failed_stage=None,
        runtime_seconds=0.01,
        warnings=(),
        failure_codes=(),
        exception_summary=None,
        rwp=rwp,
        rp=rwp - 1.0,
        goodness_of_fit=1.0,
        chi_squared=1.0,
        weighted_residual=0.5,
        observation_count=len(observed_intensities),
        refined_parameter_count=refined_parameter_count,
        degrees_of_freedom=max(len(observed_intensities) - refined_parameter_count, 1),
        phase_scale_factor=1.0,
        refined_lattice_parameters=RefinedLatticeParameters(length_a=5.64, length_b=5.64, length_c=5.64),
        zero_shift=0.0,
        sample_displacement=None,
        refined_profile_terms={},
        observed_two_theta=observed_two_theta,
        observed_intensities=observed_intensities,
        calculated_total_pattern=calculated_total,
        calculated_background=tuple(5.0 for _ in observed_intensities),
        difference_pattern=tuple(obs - calc for obs, calc in zip(observed_intensities, calculated_total)),
        expected_reflections=expected_reflections,
        significant_positive_residual_regions=residual_regions,
        unsupported_strong_predicted_regions=(),
        raw_residual_metrics={},
        provenance=("unit_test",),
    )


def _two_phase_hypothesis(
    primary: RankedPhaseCandidate,
    secondary: RankedPhaseCandidate,
    *,
    observed_intensities: tuple[float, ...] = (100.0, 80.0, 60.0, 40.0),
    calculated_total: tuple[float, ...] = (99.5, 79.7, 60.2, 39.8),
    second_phase_scale: float = 0.18,
    shared_regions: tuple[PhaseSupportRegion, ...] = (),
    primary_regions: tuple[PhaseSupportRegion, ...] = (),
    refined_parameter_count: int = 7,
) -> TwoPhaseHypothesisResult:
    observed_two_theta = tuple(20.0 + (0.1 * idx) for idx in range(len(observed_intensities)))
    primary_phase = PhaseRefinementSummary(
        candidate_id=primary.candidate_id,
        candidate_source=primary.source,
        candidate_source_identifier=primary.source_identifier,
        candidate_cif_hash=primary.cif_hash,
        scale_factor=1.0,
        refined_lattice_parameters=RefinedLatticeParameters(length_a=5.64, length_b=5.64, length_c=5.64),
        expected_reflections=(
            ExpectedReflectionRecord(h=1, k=1, l=1, multiplicity=8, d_spacing=3.2, two_theta=27.4, predicted_intensity=1.0),
        ),
    )
    secondary_phase = PhaseRefinementSummary(
        candidate_id=secondary.candidate_id,
        candidate_source=secondary.source,
        candidate_source_identifier=secondary.source_identifier,
        candidate_cif_hash=secondary.cif_hash,
        scale_factor=second_phase_scale,
        refined_lattice_parameters=RefinedLatticeParameters(length_a=4.0, length_b=4.0, length_c=4.0),
        expected_reflections=(
            ExpectedReflectionRecord(h=1, k=0, l=0, multiplicity=6, d_spacing=2.3, two_theta=39.0, predicted_intensity=0.8),
        ),
    )
    return TwoPhaseHypothesisResult(
        hypothesis_id=f"two-{primary.candidate_id}-{secondary.candidate_id}",
        candidate_ids=(primary.candidate_id, secondary.candidate_id),
        candidate_sources=(primary.source, secondary.source),
        candidate_source_identifiers=(primary.source_identifier, secondary.source_identifier),
        candidate_cif_hashes=(primary.cif_hash, secondary.cif_hash),
        reference_snapshots=(primary.source_snapshot, secondary.source_snapshot),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        gsasii_version="test",
        instrument_profile_serialization=None,
        proposal_provenance=("unit_test_pair",),
        source_single_phase_hypothesis_id=f"single-{primary.candidate_id}",
        screening_pre_rank_score=primary.combined_pre_rank_score + secondary.combined_pre_rank_score,
        refinement_status="completed",
        convergence_status="converged",
        completed_stages=(),
        failed_stage=None,
        runtime_seconds=0.02,
        warnings=(),
        failure_codes=(),
        exception_summary=None,
        rwp=6.0,
        rp=5.0,
        goodness_of_fit=0.8,
        chi_squared=0.7,
        weighted_residual=0.3,
        observation_count=len(observed_intensities),
        refined_parameter_count=refined_parameter_count,
        degrees_of_freedom=max(len(observed_intensities) - refined_parameter_count, 1),
        phase_results=(primary_phase, secondary_phase),
        zero_shift=0.0,
        sample_displacement=None,
        refined_profile_terms={},
        observed_two_theta=observed_two_theta,
        observed_intensities=observed_intensities,
        calculated_total_pattern=calculated_total,
        calculated_background=tuple(5.0 for _ in observed_intensities),
        difference_pattern=tuple(obs - calc for obs, calc in zip(observed_intensities, calculated_total)),
        significant_positive_residual_regions=(),
        unsupported_strong_predicted_regions=(),
        regions_supported_by_both_phases=shared_regions,
        regions_primarily_supported_by_one_phase=primary_regions,
        raw_residual_metrics={},
        provenance=("unit_test",),
    )


def _quality_control() -> PatternQualityControlResult:
    return PatternQualityControlResult(
        status="pattern accepted for later analysis",
        pattern_type="continuous",
        usable_point_count=80,
        coordinate_min=20.0,
        coordinate_max=80.0,
        range_width=60.0,
        median_step_size=0.05,
        step_size_variation=0.0,
        fraction_invalid_rows_removed=0.0,
        duplicate_count=0,
        negative_intensity_fraction=0.0,
        non_positive_intensity_fraction=0.0,
        approximate_signal_to_noise=5.0,
        detectable_peak_region_count=3,
        missing_interval_count=0,
        clipping_detected=False,
    )


def _parsed_pattern() -> ParsedPatternMetadata:
    return ParsedPatternMetadata(
        parser_type="dataframe",
        pattern_type="continuous",
        original_coordinate_type="two_theta",
        coordinate_column="Angle",
        intensity_column="Intensity",
        original_coordinates=(20.0, 20.1, 20.2, 20.3),
        original_intensities=(100.0, 80.0, 60.0, 40.0),
        normalized_two_theta=(20.0, 20.1, 20.2, 20.3),
        normalized_d_spacing=(4.4, 4.3, 4.2, 4.1),
        normalized_q=(1.4, 1.45, 1.5, 1.55),
        normalized_intensities=(100.0, 80.0, 60.0, 40.0),
        wavelength_angstrom=1.5406,
        usable_point_count=4,
        coordinate_order="ascending",
        median_step_size=0.1,
        step_size_variation=0.0,
        provenance=PatternProvenance(parser_type="dataframe", source_label="unit_test"),
    )


def _stability_result(*, phase_state: str, scale_stability: float = 1.0, score_stability: float = 1.0) -> StabilityAssessmentResult:
    return StabilityAssessmentResult(
        runs=(
            StabilityRunResult(
                perturbation_label="baseline",
                model_type="single_phase" if phase_state == "likely single-phase" else "two_phase",
                candidate_ids=("base",),
                selected_hypothesis_id="H",
                phase_state=phase_state,  # type: ignore[arg-type]
                background_coefficient_count=6,
                residual_threshold_multiplier=1.0,
                scale_seed_multipliers=(1.0,),
                zero_shift_seed=0.0,
                dominant_region_downweighted=False,
                penalized_score_delta=0.0,
                second_phase_scale_factor=0.2 if phase_state == "likely multiphase" else None,
            ),
        ),
        classification_agreement=1.0,
        candidate_agreement=1.0,
        second_phase_support_retained=phase_state == "likely multiphase",
        scale_stability=scale_stability,
        score_stability=score_stability,
        warnings=(),
    )


def _decision_criteria(**overrides) -> DecisionCriteriaResult:
    payload = {
        "single_phase_adequate": True,
        "two_phase_penalized_improvement": False,
        "second_phase_positive_scale": False,
        "second_phase_scale_above_threshold": False,
        "second_phase_reflection_support_sufficient": False,
        "second_phase_partially_distinctive": False,
        "second_phase_not_dominated_by_one_region": False,
        "classification_stable": True,
        "score_stable": True,
        "candidate_set_may_be_incomplete": False,
        "warnings": (),
        "failure_codes": (),
    }
    payload.update(overrides)
    return DecisionCriteriaResult(**payload)


class PairProposalTests(SimpleTestCase):
    def test_residual_guided_secondary_candidate_ranking_prefers_supported_candidate(self):
        base = _candidate("base", positions=(27.4,), intensities=(1.0,), intended=True)
        strong = _candidate("strong", positions=(35.0, 41.0), intensities=(1.0, 0.8), combined_score=0.9)
        weak = _candidate("weak", positions=(41.1,), intensities=(0.3,), combined_score=0.7)
        hypothesis = _single_hypothesis(
            base,
            residual_regions=(
                ResidualRegion(34.8, 35.2, 20.0, 10.0, nearby_observed_peak_two_theta=35.0, nearby_observed_peak_intensity=90.0),
                ResidualRegion(40.8, 41.2, 16.0, 8.0, nearby_observed_peak_two_theta=41.0, nearby_observed_peak_intensity=75.0),
            ),
            expected_reflections=(
                ExpectedReflectionRecord(h=1, k=1, l=1, multiplicity=8, d_spacing=3.2, two_theta=27.4, predicted_intensity=1.0),
            ),
        )
        candidate_generation = CandidateGenerationResult(
            status="candidate ranking ready for refinement",
            failure_reason=None,
            warnings=(),
            reference_snapshot=None,
            candidates=(base, strong, weak),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        single_batch = SinglePhaseRefinementBatchResult(
            status="single-phase-refinement-ready",
            warnings=(),
            successful_hypotheses=(hypothesis,),
            failed_candidate_results=(),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )

        proposals, warnings = build_two_phase_pair_proposals(candidate_generation, single_batch)

        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].secondary_candidate_id, "strong")
        self.assertNotEqual(proposals[0].primary_candidate_id, proposals[0].secondary_candidate_id)
        self.assertFalse(warnings)

    def test_duplicate_pair_removal_normalizes_candidate_order(self):
        candidate_a = _candidate("alpha", positions=(35.0,), intensities=(1.0,))
        candidate_b = _candidate("beta", positions=(41.0,), intensities=(1.0,))
        hypothesis_a = _single_hypothesis(
            candidate_a,
            residual_regions=(ResidualRegion(40.8, 41.2, 16.0, 8.0, nearby_observed_peak_two_theta=41.0, nearby_observed_peak_intensity=60.0),),
        )
        hypothesis_b = _single_hypothesis(
            candidate_b,
            residual_regions=(ResidualRegion(34.8, 35.2, 20.0, 10.0, nearby_observed_peak_two_theta=35.0, nearby_observed_peak_intensity=70.0),),
        )
        generation = CandidateGenerationResult(
            status="candidate ranking ready for refinement",
            failure_reason=None,
            warnings=(),
            reference_snapshot=None,
            candidates=(candidate_a, candidate_b),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        batch = SinglePhaseRefinementBatchResult(
            status="single-phase-refinement-ready",
            warnings=(),
            successful_hypotheses=(hypothesis_a, hypothesis_b),
            failed_candidate_results=(),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )

        proposals, warnings = build_two_phase_pair_proposals(generation, batch)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].normalized_candidate_ids, ("alpha", "beta"))
        self.assertTrue(any(w.code == "duplicate_two_phase_pair_removed" for w in warnings))

    def test_pair_generation_limits_and_intended_candidate_not_forced(self):
        base = _candidate("base", positions=(27.4,), intensities=(1.0,), intended=True)
        best = _candidate("best", positions=(35.0, 41.0), intensities=(1.0, 0.9), combined_score=0.95)
        intended = _candidate("intended_alt", positions=(41.05,), intensities=(0.2,), intended=True, combined_score=0.6)
        other = _candidate("other", positions=(52.0,), intensities=(0.8,), combined_score=0.7)
        hypothesis = _single_hypothesis(
            base,
            residual_regions=(ResidualRegion(34.8, 35.2, 12.0, 8.0, nearby_observed_peak_two_theta=35.0, nearby_observed_peak_intensity=80.0),),
        )
        config = replace(
            DEFAULT_XRD_ANALYSIS_CONFIG,
            decision=replace(
                DEFAULT_XRD_ANALYSIS_CONFIG.decision,
                maximum_base_single_phase_hypotheses=1,
                maximum_secondary_candidates_per_base=1,
                maximum_total_two_phase_refinements=1,
            ),
        )
        generation = CandidateGenerationResult(
            status="candidate ranking ready for refinement",
            failure_reason=None,
            warnings=(),
            reference_snapshot=None,
            candidates=(base, best, intended, other),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        batch = SinglePhaseRefinementBatchResult(
            status="single-phase-refinement-ready",
            warnings=(),
            successful_hypotheses=(hypothesis,),
            failed_candidate_results=(),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )

        proposals, _ = build_two_phase_pair_proposals(generation, batch, configuration=config)

        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].secondary_candidate_id, "best")
        self.assertFalse(proposals[0].secondary_candidate_id == "intended_alt")


class ModelComparisonAndClassificationTests(SimpleTestCase):
    def test_aic_aicc_and_bic_are_computed_from_weighted_residual_sum(self):
        single = _single_hypothesis(
            _candidate("single", positions=(27.4,), intensities=(1.0,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0),
            calculated_total=(99.0, 63.0, 35.0, 15.0),
            refined_parameter_count=3,
        )
        two = _two_phase_hypothesis(
            _candidate("p1", positions=(27.4,), intensities=(1.0,)),
            _candidate("p2", positions=(39.0,), intensities=(0.8,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0),
            calculated_total=(100.0, 64.0, 36.0, 16.0),
            refined_parameter_count=5,
        )
        summary = compare_hypothesis_models(
            SinglePhaseRefinementBatchResult(
                status="single-phase-refinement-ready",
                warnings=(),
                successful_hypotheses=(single,),
                failed_candidate_results=(),
                algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
            ),
            TwoPhaseRefinementBatchResult(
                status="two-phase-hypotheses-ready",
                warnings=(),
                pair_proposals=(),
                successful_hypotheses=(two,),
                failed_hypotheses=(),
                algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
            ),
        )
        best_single = summary.best_single_phase_metrics
        self.assertIsNotNone(best_single)
        wrss = (1.0 / 10.0) ** 2 + (1.0 / 8.0) ** 2 + (1.0 / 6.0) ** 2 + (1.0 / 4.0) ** 2
        expected_aic = (4 * math.log(wrss / 4.0)) + (2 * 3)
        expected_bic = (4 * math.log(wrss / 4.0)) + (3 * math.log(4))
        self.assertAlmostEqual(best_single.aic, expected_aic, places=6)
        self.assertAlmostEqual(best_single.bic, expected_bic, places=6)
        self.assertIsNone(best_single.aicc)

    def test_complexity_penalty_can_favor_simpler_model(self):
        single = _single_hypothesis(
            _candidate("single", positions=(27.4,), intensities=(1.0,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            calculated_total=(100.0, 63.95, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            refined_parameter_count=3,
        )
        two = _two_phase_hypothesis(
            _candidate("p1", positions=(27.4,), intensities=(1.0,)),
            _candidate("p2", positions=(39.0,), intensities=(0.8,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            calculated_total=(100.0, 63.95, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            refined_parameter_count=7,
        )
        summary = compare_hypothesis_models(
            SinglePhaseRefinementBatchResult("single-phase-refinement-ready", (), (single,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
            TwoPhaseRefinementBatchResult("two-phase-hypotheses-ready", (), (), (two,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
        )
        self.assertEqual(summary.preferred_model, "single_phase")

    def test_complexity_penalty_can_favor_clearly_superior_two_phase_model(self):
        single = _single_hypothesis(
            _candidate("single", positions=(27.4,), intensities=(1.0,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0, 9.0, 4.0),
            calculated_total=(90.0, 54.0, 26.0, 6.0, 2.0, 1.0),
            refined_parameter_count=3,
        )
        two = _two_phase_hypothesis(
            _candidate("p1", positions=(27.4,), intensities=(1.0,)),
            _candidate("p2", positions=(39.0,), intensities=(0.8,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0, 9.0, 4.0),
            calculated_total=(99.0, 64.0, 35.5, 16.2, 8.8, 4.0),
            refined_parameter_count=7,
            primary_regions=(
                PhaseSupportRegion(38.8, 39.2, "primary", ("p2",), primarily_supported_candidate_id="p2", nearby_observed_peak_two_theta=39.0, nearby_observed_peak_intensity=75.0, predicted_reflection_positions=(39.0,)),
            ),
        )
        summary = compare_hypothesis_models(
            SinglePhaseRefinementBatchResult("single-phase-refinement-ready", (), (single,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
            TwoPhaseRefinementBatchResult("two-phase-hypotheses-ready", (), (), (two,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
        )
        self.assertEqual(summary.preferred_model, "two_phase")

    def test_ambiguous_model_comparison_emits_warning(self):
        single = _single_hypothesis(
            _candidate("single", positions=(27.4,), intensities=(1.0,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            calculated_total=(100.0, 63.85, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            refined_parameter_count=3,
        )
        two = _two_phase_hypothesis(
            _candidate("p1", positions=(27.4,), intensities=(1.0,)),
            _candidate("p2", positions=(39.0,), intensities=(0.8,)),
            observed_intensities=(100.0, 64.0, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            calculated_total=(100.0, 63.9, 36.0, 16.0, 9.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0),
            refined_parameter_count=7,
        )
        summary = compare_hypothesis_models(
            SinglePhaseRefinementBatchResult("single-phase-refinement-ready", (), (single,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
            TwoPhaseRefinementBatchResult("two-phase-hypotheses-ready", (), (), (two,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
        )
        self.assertEqual(summary.preferred_model, "none")
        self.assertTrue(any(w.code == "model_comparison_ambiguous" for w in summary.warnings))

    def test_classification_returns_likely_single_phase_for_stable_adequate_single_model(self):
        single = _single_hypothesis(_candidate("single", positions=(27.4,), intensities=(1.0,)))
        phase_state, selected_model, failure_codes, warnings = classify_phase_state(
            single,
            None,
            compare_hypothesis_models(
                SinglePhaseRefinementBatchResult("single-phase-refinement-ready", (), (single,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
                TwoPhaseRefinementBatchResult("two-phase refinement failed", (), (), (), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
            ),
            _decision_criteria(),
            _stability_result(phase_state="likely single-phase"),
        )
        self.assertEqual(phase_state, "likely single-phase")
        self.assertEqual(selected_model.model_type, "single_phase")
        self.assertFalse(failure_codes)
        self.assertFalse(warnings)

    def test_classification_returns_likely_multiphase_when_two_phase_evidence_is_stable(self):
        single = _single_hypothesis(_candidate("single", positions=(27.4,), intensities=(1.0,)))
        two = _two_phase_hypothesis(
            _candidate("p1", positions=(27.4,), intensities=(1.0,)),
            _candidate("p2", positions=(39.0,), intensities=(0.8,)),
            primary_regions=(
                PhaseSupportRegion(38.8, 39.2, "primary", ("p2",), primarily_supported_candidate_id="p2", nearby_observed_peak_two_theta=39.0, nearby_observed_peak_intensity=75.0, predicted_reflection_positions=(39.0,)),
            ),
        )
        phase_state, selected_model, _, _ = classify_phase_state(
            single,
            two,
            compare_hypothesis_models(
                SinglePhaseRefinementBatchResult("single-phase-refinement-ready", (), (single,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
                TwoPhaseRefinementBatchResult("two-phase-hypotheses-ready", (), (), (two,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
            ),
            _decision_criteria(
                single_phase_adequate=False,
                two_phase_penalized_improvement=True,
                second_phase_positive_scale=True,
                second_phase_scale_above_threshold=True,
                second_phase_reflection_support_sufficient=True,
                second_phase_partially_distinctive=True,
                second_phase_not_dominated_by_one_region=True,
            ),
            _stability_result(phase_state="likely multiphase", scale_stability=0.95, score_stability=0.95),
        )
        self.assertEqual(phase_state, "likely multiphase")
        self.assertEqual(selected_model.model_type, "two_phase")

    def test_classification_returns_unresolved_for_overlapped_or_unstable_second_phase(self):
        single = _single_hypothesis(_candidate("single", positions=(27.4,), intensities=(1.0,)))
        two = _two_phase_hypothesis(
            _candidate("p1", positions=(27.4,), intensities=(1.0,)),
            _candidate("p2", positions=(39.0,), intensities=(0.8,)),
            shared_regions=(
                PhaseSupportRegion(38.8, 39.2, "shared", ("p1", "p2"), nearby_observed_peak_two_theta=39.0, nearby_observed_peak_intensity=75.0, predicted_reflection_positions=(39.0, 39.05)),
            ),
        )
        phase_state, _, failure_codes, warnings = classify_phase_state(
            single,
            two,
            compare_hypothesis_models(
                SinglePhaseRefinementBatchResult("single-phase-refinement-ready", (), (single,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
                TwoPhaseRefinementBatchResult("two-phase-hypotheses-ready", (), (), (two,), (), DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version),
            ),
            _decision_criteria(
                single_phase_adequate=False,
                two_phase_penalized_improvement=True,
                second_phase_positive_scale=True,
                second_phase_scale_above_threshold=True,
                second_phase_reflection_support_sufficient=True,
                second_phase_partially_distinctive=False,
                second_phase_not_dominated_by_one_region=False,
                classification_stable=False,
                score_stable=False,
            ),
            _stability_result(phase_state="unresolved", scale_stability=0.4, score_stability=0.4),
        )
        self.assertEqual(phase_state, "unresolved")
        self.assertIn("final_decision_unresolved", failure_codes)
        self.assertTrue(any(w.code == "final_decision_unresolved" for w in warnings))


class TwoPhaseRefinementUnitTests(SimpleTestCase):
    def test_pair_failure_does_not_stop_other_pairs(self):
        primary = _candidate("primary", positions=(27.4,), intensities=(1.0,))
        secondary = _candidate("secondary", positions=(39.0,), intensities=(0.8,))
        tertiary = _candidate("tertiary", positions=(52.0,), intensities=(0.7,))
        request_one = build_two_phase_refinement_request(
            _analysis_input(),
            _parsed_pattern(),
            primary,
            secondary,
            source_single_phase_hypothesis_id="single-primary",
            proposal_provenance=("pair_one",),
        )
        request_two = build_two_phase_refinement_request(
            _analysis_input(),
            _parsed_pattern(),
            primary,
            tertiary,
            source_single_phase_hypothesis_id="single-primary",
            proposal_provenance=("pair_two",),
        )
        failed = replace(
            _two_phase_hypothesis(primary, secondary, second_phase_scale=0.0),
            refinement_status="failed",
            convergence_status="failed",
            failure_codes=("second_phase_scale_below_threshold",),
        )
        succeeded = _two_phase_hypothesis(primary, tertiary)
        with patch(
            "catalog.xrd_analysis.refinement.refine_candidate_pair",
            side_effect=[failed, succeeded],
        ):
            batch = refine_candidate_pairs(
                (request_one, request_two),
                pair_proposals=(
                    TwoPhasePairProposal("p1", "single-primary", "primary", "secondary", ("primary", "secondary"), 1.0, 1, 1, False),
                    TwoPhasePairProposal("p2", "single-primary", "primary", "tertiary", ("primary", "tertiary"), 0.8, 1, 1, False),
                ),
            )
        self.assertEqual(batch.status, "two-phase-hypotheses-ready")
        self.assertEqual(len(batch.failed_hypotheses), 1)
        self.assertEqual(len(batch.successful_hypotheses), 1)

    def test_two_phase_snapshot_validation_rejects_small_second_phase_and_bound_violations(self):
        primary = _candidate("primary", positions=(27.4,), intensities=(1.0,))
        secondary = _candidate("secondary", positions=(39.0,), intensities=(0.8,))
        snapshot = _two_phase_hypothesis(primary, secondary, second_phase_scale=0.01)
        with self.assertRaisesRegex(Exception, "second phase scale below threshold"):
            _validate_two_phase_snapshot(
                snapshot,
                (
                    RefinedLatticeParameters(length_a=5.64, length_b=5.64, length_c=5.64),
                    RefinedLatticeParameters(length_a=4.0, length_b=4.0, length_c=4.0),
                ),
                DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
                DEFAULT_XRD_ANALYSIS_CONFIG.decision,
            )
        shifted = replace(
            snapshot,
            phase_results=(
                replace(snapshot.phase_results[0], scale_factor=1.0),
                replace(snapshot.phase_results[1], scale_factor=0.1),
            ),
            zero_shift=1.0,
        )
        with self.assertRaisesRegex(Exception, "zero shift exceeded bound"):
            _validate_two_phase_snapshot(
                shifted,
                (
                    RefinedLatticeParameters(length_a=5.64, length_b=5.64, length_c=5.64),
                    RefinedLatticeParameters(length_a=4.0, length_b=4.0, length_c=4.0),
                ),
                DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
                DEFAULT_XRD_ANALYSIS_CONFIG.decision,
            )

    def test_phase_support_regions_capture_shared_and_partially_distinctive_evidence(self):
        observed_two_theta = tuple(20.0 + (0.1 * idx) for idx in range(400))
        observed_intensities = tuple(20.0 + (80.0 if 74 <= idx <= 76 else 0.0) + (60.0 if 189 <= idx <= 191 else 0.0) + (55.0 if 349 <= idx <= 351 else 0.0) for idx in range(400))
        primary_phase = PhaseRefinementSummary(
            candidate_id="p1",
            candidate_source="curated_reference",
            candidate_source_identifier="local:p1",
            candidate_cif_hash="hash-p1",
            scale_factor=1.0,
            refined_lattice_parameters=RefinedLatticeParameters(length_a=5.64),
            expected_reflections=(
                ExpectedReflectionRecord(1, 1, 1, 8, 3.2, 27.5, 1.0),
                ExpectedReflectionRecord(2, 0, 0, 6, 2.2, 39.0, 0.7),
            ),
        )
        secondary_phase = PhaseRefinementSummary(
            candidate_id="p2",
            candidate_source="curated_reference",
            candidate_source_identifier="local:p2",
            candidate_cif_hash="hash-p2",
            scale_factor=0.15,
            refined_lattice_parameters=RefinedLatticeParameters(length_a=4.0),
            expected_reflections=(
                ExpectedReflectionRecord(1, 0, 0, 6, 2.3, 39.05, 0.9),
                ExpectedReflectionRecord(1, 1, 0, 12, 1.7, 55.0, 0.85),
            ),
        )
        shared, primary = _extract_phase_support_regions(
            observed_two_theta,
            observed_intensities,
            (primary_phase, secondary_phase),
            settings=DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
        )
        self.assertTrue(any(region.support_kind == "shared" for region in shared))
        self.assertTrue(any(region.primarily_supported_candidate_id == "p2" for region in primary))

    def test_final_decision_result_is_json_safe_and_deterministic(self):
        candidate = _candidate("single", positions=(27.4,), intensities=(1.0,))
        single = _single_hypothesis(candidate)
        single_batch = SinglePhaseRefinementBatchResult(
            status="single-phase-refinement-ready",
            warnings=(),
            successful_hypotheses=(single,),
            failed_candidate_results=(),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        generation = CandidateGenerationResult(
            status="candidate ranking ready for refinement",
            failure_reason=None,
            warnings=(),
            reference_snapshot=None,
            candidates=(candidate,),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        with (
            patch(
                "catalog.xrd_analysis.decision.refine_candidate_pairs",
                return_value=TwoPhaseRefinementBatchResult(
                    status="two-phase refinement failed",
                    warnings=(),
                    pair_proposals=(),
                    successful_hypotheses=(),
                    failed_hypotheses=(),
                    algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                    configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                ),
            ),
            patch(
                "catalog.xrd_analysis.decision.run_stability_checks",
                return_value=_stability_result(phase_state="likely single-phase"),
            ),
        ):
            result = run_final_phase_decision(
                _analysis_input(),
                _parsed_pattern(),
                _quality_control(),
                generation,
                single_batch,
            )
        payload = dumps_canonical_json(result)
        self.assertEqual(payload, dumps_canonical_json(result))
        loaded = json.loads(payload)
        self.assertEqual(loaded["phase_state"], "likely single-phase")
        self.assertIn("evidence_components", loaded)


def _gsas_calculated_pattern_dataframe(cif_path: Path, instprm_path: str, *, phase_name: str) -> pd.DataFrame:
    x = np.arange(20.0, 80.0 + 0.05, 0.05)
    y = np.full_like(x, 100.0)
    sigma = np.ones_like(x)
    xye_path = write_temp_xye(x, y, sigma)
    gpx_path, remove_gpx = prepare_project_path()
    try:
        project = new_project(configure_gsas(), gpx_path)
        histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        _set_histogram_wavelength(histogram, 1.5406)
        phase = project.add_phase(str(cif_path.resolve()), phasename=phase_name, histograms=[histogram], fmthint="CIF")
        histogram.set_refinements(
            {
                "Limits": [20.0, 80.0],
                "Background": {
                    "type": "chebyschev-1",
                    "no. coeffs": 6,
                    "refine": True,
                },
            }
        )
        clear_sample_scale_refinement(histogram)
        phase.set_HAP_refinements({"Scale": True}, [histogram])
        set_project_cycles(project, 2)
        project.refine(makeBack=True)
        calculated = np.asarray(histogram.getdata("Ycalc"), dtype=float) + 5.0
        return pd.DataFrame({"Angle": x, "Intensity": calculated})
    finally:
        cleanup_paths((xye_path, True), (gpx_path, remove_gpx))


class TwoPhaseRefinementIntegrationTests(SimpleTestCase):
    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_real_two_phase_gsas_refinement_with_curated_local_cifs(self):
        primary_cif = REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"
        secondary_cif = REFERENCE_PHASES_DIR / "hypothetical_NaCl3_1to3.cif"
        files_before = sorted(path.name for path in REFERENCE_PHASES_DIR.iterdir())
        instprm_path, remove_instprm = resolve_instrument_parameter_file()
        try:
            primary_df = _gsas_calculated_pattern_dataframe(primary_cif, instprm_path, phase_name="primary_ref")
            secondary_df = _gsas_calculated_pattern_dataframe(secondary_cif, instprm_path, phase_name="secondary_ref")
            mixture = primary_df.copy()
            mixture["Intensity"] = (0.85 * primary_df["Intensity"]) + (0.35 * secondary_df["Intensity"])
            parsed_pattern = normalize_table_pattern(mixture, wavelength_angstrom=1.5406)
            analysis_input = replace(
                _analysis_input(),
                instrument_profile=InstrumentProfile(
                    instrument_label="CuKa lab data",
                    instrument_parameter_path=instprm_path,
                    geometry="Bragg-Brentano",
                    sample_holder="flat plate",
                ),
            )
            primary = _candidate("curated_nacl_rocksalt", positions=(27.4, 31.7, 45.5), intensities=(1.0, 0.8, 0.75), intended=True, cif_name="NaCl_rocksalt.cif", cif_hash="nacl")
            secondary = _candidate("curated_hypothetical_nacl3", positions=(22.4, 39.0, 55.0), intensities=(0.9, 1.0, 0.7), cif_name="hypothetical_NaCl3_1to3.cif", cif_hash="nacl3")
            request = build_two_phase_refinement_request(
                analysis_input,
                parsed_pattern,
                primary,
                secondary,
                source_single_phase_hypothesis_id="single-curated_nacl_rocksalt",
                proposal_provenance=("integration_test",),
                initial_phase_scale_factors=(1.0, 0.25),
            )
            configuration = replace(
                DEFAULT_XRD_ANALYSIS_CONFIG,
                single_phase_refinement=replace(
                    DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
                    refine_zero_shift=False,
                    initial_stage_cycles=2,
                    maximum_iterations=3,
                ),
                decision=replace(
                    DEFAULT_XRD_ANALYSIS_CONFIG.decision,
                    minimum_second_phase_scale_factor=1e-12,
                ),
            )
            result = refine_candidate_pair(request, configuration=configuration)
        finally:
            cleanup_paths((instprm_path, remove_instprm))
        files_after = sorted(path.name for path in REFERENCE_PHASES_DIR.iterdir())
        self.assertEqual(files_before, files_after)
        self.assertEqual(result.refinement_status, "completed")
        self.assertEqual(len(result.phase_results), 2)
        self.assertIsNotNone(result.phase_results[0].scale_factor)
        self.assertIsNotNone(result.phase_results[1].scale_factor)
        self.assertTrue(result.calculated_total_pattern)
