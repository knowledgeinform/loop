import json
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import SimpleTestCase

from catalog.xrd_analysis.pipeline import (
    FUTURE_PIPELINE_STAGES,
    PatternAnalysisStageResult,
    prepare_analysis_input,
    run_xrd_analysis_pipeline,
)
from catalog.xrd_analysis.reporting import dumps_canonical_json, sha256_digest, to_jsonable
from catalog.xrd_analysis.schemas import (
    ALLOWED_PHASE_STATES,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    INPUT_WARNING_CODES,
    PATTERN_WARNING_CODES,
    QUALITY_FAILURE_CODES,
    CandidateGenerationResult,
    InstrumentProfile,
    PatternProvenance,
    PatternQualityControlResult,
    ParsedPatternMetadata,
    PhaseHypothesis,
    QualityControlPlaceholder,
    RawFileReference,
    SinglePhaseRefinementBatchResult,
    StoichiometricAmount,
    SynthesisContext,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
    XRDAnalysisResult,
    XRDAnalysisWarning,
)


def _minimal_input():
    return XRDAnalysisInput(
        material_auid="M:test",
        recipe_auid="M:test:R:test",
        trial_id="T1",
        raw_file_hash="ab" * 32,
        raw_file_reference=RawFileReference(reference_kind="stored_path", locator="/tmp/raw.csv"),
        nominal_composition="CoNiO",
        elements=("Co", "Ni", "O"),
        stoichiometric_amounts=(
            StoichiometricAmount("Co", 1.0),
            StoichiometricAmount("Ni", 1.0),
            StoichiometricAmount("O", 1.0),
        ),
        structure_family="rocksalt",
        expected_space_group="Fm-3m (#225)",
        expected_site_assignments=(),
        radiation_source="cu_ka",
        wavelength_angstrom=1.5406,
        coordinate_type="two_theta",
        coordinate_column="Angle",
        intensity_column="Intensity",
        scan_min=10.0,
        scan_max=80.0,
        step_size=0.02,
        scan_speed=1.0,
        instrument_profile=InstrumentProfile(instrument_label="CuKa lab data"),
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
        warnings=(
            XRDAnalysisWarning(
                code="missing_instrument_profile",
                message="placeholder",
                severity="warning",
                field="instrument_profile",
            ),
        ),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        provenance=XRDAnalysisProvenance(
            source_material={"id": "M:test"},
            source_recipe={"id": "M:test:R:test"},
            source_trial={"trial_id": "T1", "trial_date": datetime(2026, 7, 26, tzinfo=timezone.utc)},
            source_raw_file={"stored_path": "/tmp/raw.csv"},
            measurement_metadata=(),
        ),
    )


class SchemaContractTests(SimpleTestCase):
    def test_warning_codes_are_stable(self):
        self.assertEqual(
            INPUT_WARNING_CODES,
            (
                "missing_raw_file_hash",
                "missing_raw_file_reference",
                "missing_wavelength",
                "missing_coordinate_column",
                "missing_intensity_column",
                "missing_instrument_profile",
                "missing_nominal_composition",
                "missing_element_list",
                "missing_scan_range",
                "inconsistent_stoichiometry",
                "missing_recipe_identity",
                "missing_trial_identity",
            ),
        )

    def test_allowed_phase_state_values_are_exact(self):
        self.assertEqual(
            ALLOWED_PHASE_STATES,
            (
                "likely single-phase",
                "likely multiphase",
                "unresolved",
                "insufficient-quality data",
            ),
        )

    def test_candidate_warning_codes_are_stable(self):
        from catalog.xrd_analysis.schemas import CANDIDATE_WARNING_CODES

        self.assertEqual(
            CANDIDATE_WARNING_CODES,
            (
                "no_candidate_sources_available",
                "intended_structure_reference_missing",
                "candidate_cif_unreadable",
                "candidate_contains_unlisted_element",
                "candidate_formula_unparseable",
                "candidate_simulation_failed",
                "duplicate_candidate_removed",
                "no_chemically_compatible_candidates",
                "no_simulatable_candidates",
                "no_rankable_candidates",
                "reference_snapshot_mismatch",
            ),
        )

    def test_pattern_warning_codes_are_stable(self):
        self.assertEqual(
            PATTERN_WARNING_CODES,
            (
                "invalid_rows_removed",
                "missing_wavelength_for_conversion",
                "coordinate_type_conflict",
                "parsed_range_mismatch",
                "parsed_step_size_mismatch",
                "irregular_step_spacing",
                "repeated_coordinates",
                "missing_intervals",
                "coordinates_out_of_bounds",
                "all_non_positive_intensity",
                "high_negative_intensity_fraction",
                "constant_intensity",
                "possible_intensity_clipping",
                "stick_pattern_limited",
                "parser_output_type_conflict",
            ),
        )

    def test_quality_failure_codes_are_stable(self):
        self.assertEqual(
            QUALITY_FAILURE_CODES,
            (
                "no_usable_points",
                "too_few_usable_points",
                "zero_coordinate_range",
                "flat_signal",
                "insufficient_peak_evidence",
                "all_non_positive_intensity",
            ),
        )

    def test_default_config_fixes_maximum_phases_at_two(self):
        self.assertEqual(DEFAULT_XRD_ANALYSIS_CONFIG.maximum_crystalline_phases, 2)

    def test_configuration_serialization_is_deterministic(self):
        config = XRDAnalysisConfig(
            algorithm_version="algo-v1",
            configuration_version="cfg-v1",
            supported_phase_states=ALLOWED_PHASE_STATES,
        )
        self.assertEqual(config.content_hash_input(), config.canonical_json())
        self.assertIn('"maximum_candidates_before_simulation":24', config.content_hash_input())
        self.assertIn('"final_top_k":6', config.content_hash_input())
        self.assertIn('"maximum_allowed_screening_shift_degrees":0.35', config.content_hash_input())

    def test_reporting_helpers_emit_json_safe_output(self):
        payload = {
            "result": XRDAnalysisResult(
                phase_state="unresolved",
                warnings=(),
                algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                best_hypothesis=PhaseHypothesis(hypothesis_id="H1", crystalline_phase_count=1),
                alternative_hypotheses=(),
                evidence_score=0.42,
                provenance=_minimal_input().provenance,
                parsed_pattern=ParsedPatternMetadata(
                    parser_type="dataframe",
                    pattern_type="continuous",
                    original_coordinate_type="two_theta",
                    coordinate_column="Angle",
                    intensity_column="Intensity",
                    original_coordinates=(20.0, 20.2, 20.4),
                    original_intensities=(10.0, 20.0, 10.0),
                    normalized_two_theta=(20.0, 20.2, 20.4),
                    normalized_d_spacing=(4.43, 4.39, 4.35),
                    normalized_q=(1.41, 1.43, 1.45),
                    normalized_intensities=(10.0, 20.0, 10.0),
                    wavelength_angstrom=1.5406,
                    usable_point_count=3,
                    coordinate_order="ascending",
                    median_step_size=0.2,
                    step_size_variation=0.0,
                    provenance=PatternProvenance(parser_type="dataframe", source_label="memory"),
                ),
                quality_control=PatternQualityControlResult(
                    status="pattern accepted for later analysis",
                    pattern_type="continuous",
                    usable_point_count=3,
                    coordinate_min=20.0,
                    coordinate_max=20.4,
                    range_width=0.4,
                    median_step_size=0.2,
                    step_size_variation=0.0,
                    fraction_invalid_rows_removed=0.0,
                    duplicate_count=0,
                    negative_intensity_fraction=0.0,
                    non_positive_intensity_fraction=0.0,
                    approximate_signal_to_noise=3.5,
                    detectable_peak_region_count=1,
                    missing_interval_count=0,
                    clipping_detected=False,
                ),
            )
        }
        text = dumps_canonical_json(payload)
        loaded = json.loads(text)
        self.assertEqual(loaded["result"]["phase_state"], "unresolved")
        self.assertEqual(
            loaded["result"]["provenance"]["source_trial"]["trial_date"],
            "2026-07-26T00:00:00+00:00",
        )
        self.assertEqual(sha256_digest(payload), sha256_digest(payload))
        self.assertEqual(to_jsonable(payload)["result"]["parsed_pattern"]["usable_point_count"], 3)

    def test_prepare_analysis_input_returns_staged_boundary(self):
        prepared = prepare_analysis_input(_minimal_input())
        self.assertEqual(prepared.status, "input_validated")
        self.assertEqual(prepared.next_stage, "pattern_parsing")
        self.assertEqual(prepared.stage_order, FUTURE_PIPELINE_STAGES)

    def test_pipeline_run_returns_final_analysis_result(self):
        with patch("catalog.xrd_analysis.pipeline.parse_and_qc_input_pattern") as runner:
            runner.return_value = (
                ParsedPatternMetadata(
                    parser_type="dataframe",
                    pattern_type="continuous",
                    original_coordinate_type="two_theta",
                    coordinate_column="Angle",
                    intensity_column="Intensity",
                    original_coordinates=(20.0, 20.2),
                    original_intensities=(10.0, 20.0),
                    normalized_two_theta=(20.0, 20.2),
                    normalized_d_spacing=(4.43, 4.39),
                    normalized_q=(1.41, 1.43),
                    normalized_intensities=(10.0, 20.0),
                    wavelength_angstrom=1.5406,
                    usable_point_count=2,
                    coordinate_order="ascending",
                    median_step_size=0.2,
                    step_size_variation=0.0,
                    provenance=PatternProvenance(parser_type="dataframe", source_label="memory"),
                ),
                PatternQualityControlResult(
                    status="pattern accepted for later analysis",
                    pattern_type="continuous",
                    usable_point_count=2,
                    coordinate_min=20.0,
                    coordinate_max=20.2,
                    range_width=0.2,
                    median_step_size=0.2,
                    step_size_variation=0.0,
                    fraction_invalid_rows_removed=0.0,
                    duplicate_count=0,
                    negative_intensity_fraction=0.0,
                    non_positive_intensity_fraction=0.0,
                    approximate_signal_to_noise=2.0,
                    detectable_peak_region_count=1,
                    missing_interval_count=0,
                    clipping_detected=False,
                ),
            )
            with (
                patch("catalog.xrd_analysis.pipeline.build_ranked_phase_candidates") as candidate_builder,
                patch(
                    "catalog.xrd_analysis.pipeline.refine_top_single_phase_candidates",
                    return_value=SinglePhaseRefinementBatchResult(
                        status="single-phase-refinement-ready",
                        warnings=(),
                        successful_hypotheses=(),
                        failed_candidate_results=(),
                        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                    ),
                ),
                patch(
                    "catalog.xrd_analysis.pipeline.run_final_phase_decision",
                    return_value=XRDAnalysisResult(
                        phase_state="unresolved",
                        warnings=(),
                        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                        best_hypothesis=None,
                        alternative_hypotheses=(),
                        evidence_score=0.0,
                        provenance=_minimal_input().provenance,
                    ),
                ) as decision_runner,
            ):
                candidate_builder.return_value = CandidateGenerationResult(
                    status="candidate ranking ready for refinement",
                    failure_reason=None,
                    warnings=(),
                    reference_snapshot=None,
                    candidates=(),
                    algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                    configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                )
                result = run_xrd_analysis_pipeline(_minimal_input())
        self.assertIsInstance(result, XRDAnalysisResult)
        self.assertEqual(result.phase_state, "unresolved")
        decision_runner.assert_called_once()
