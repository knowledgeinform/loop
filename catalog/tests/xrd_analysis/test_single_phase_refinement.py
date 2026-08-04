import json
import os
import socket
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
from catalog.xrd_analysis.pattern import normalize_table_pattern
from catalog.xrd_analysis.pipeline import run_xrd_analysis_pipeline
from catalog.xrd_analysis.refinement import (
    SinglePhaseRefinementError,
    _build_stage_plan,
    _extract_positive_residual_regions,
    _extract_profile_terms,
    _extract_unsupported_predicted_regions,
    _resolve_instrument_profile,
    _set_histogram_wavelength,
    _validate_snapshot,
    build_single_phase_refinement_request,
    refine_single_phase_candidate,
    refine_top_single_phase_candidates,
)
from catalog.xrd_analysis.reporting import dumps_canonical_json
from catalog.xrd_analysis.schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CandidateGenerationResult,
    CandidateSimulation,
    ExpectedReflectionRecord,
    InstrumentProfile,
    ParsedPatternMetadata,
    PatternProvenance,
    PatternQualityControlResult,
    ProfileParameterBound,
    RankedPhaseCandidate,
    RawFileReference,
    RefinedLatticeParameters,
    ResidualRegion,
    SinglePhaseHypothesisResult,
    SinglePhaseRefinementBatchResult,
    SinglePhaseRefinementSettings,
    StoichiometricAmount,
    SynthesisContext,
    UnsupportedPredictedRegion,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
    XRDAnalysisResult,
    XRDAnalysisWarning,
)


def _gsas_runtime_available():
    try:
        from GSASII import GSASIIscriptable  # type: ignore  # noqa: F401
        from GSASII import defaultIparms  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def _instrument_profile(instprm_path: str | None = None) -> InstrumentProfile:
    return InstrumentProfile(
        instrument_label="CuKa lab data",
        instrument_parameter_path=instprm_path,
        geometry="Bragg-Brentano",
        sample_holder="flat plate",
    )


def _analysis_input(*, instrument_profile: InstrumentProfile | None = None) -> XRDAnalysisInput:
    return XRDAnalysisInput(
        material_auid="M:test",
        recipe_auid="M:test:R:test",
        trial_id="T1",
        raw_file_hash="ab" * 32,
        raw_file_reference=RawFileReference(
            reference_kind="stored_path",
            locator="/tmp/synthetic_scan.csv",
            original_filename="synthetic_scan.csv",
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
        instrument_profile=instrument_profile,
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


def _reference_candidate() -> RankedPhaseCandidate:
    cif_path = REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"
    return RankedPhaseCandidate(
        candidate_id="curated_nacl_rocksalt",
        source="curated_reference",
        source_identifier="local:NaCl_rocksalt",
        source_snapshot="loop-reference-phases-v1",
        cif_path=str(cif_path),
        cif_hash="172d121d5c420b9ceb4e18edd2f83c730230cfd0a158fd6761836bfe57746b16",
        formula="NaCl",
        normalized_composition=(
            StoichiometricAmount("Cl", 1.0),
            StoichiometricAmount("Na", 1.0),
        ),
        element_set=("Cl", "Na"),
        space_group="F m -3 m",
        structure_family="rocksalt",
        intended_structure_match=True,
        chemical_compatibility_score=1.0,
        stoichiometric_similarity_score=1.0,
        synthesis_context_score=0.5,
        diffraction_pre_rank_score=0.9,
        combined_pre_rank_score=0.95,
        duplicate_cluster_id=None,
        warnings=(),
        provenance=("candidate_source=curated_reference",),
        simulation=CandidateSimulation(
            candidate_id="curated_nacl_rocksalt",
            reflection_positions_two_theta=(27.4, 31.7, 45.5),
            reflection_relative_intensities=(1.0, 0.85, 0.75),
            measured_coordinate_min=20.0,
            measured_coordinate_max=80.0,
            wavelength_angstrom=1.5406,
            settings_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        ),
    )


def _parsed_pattern() -> ParsedPatternMetadata:
    df = pd.DataFrame(
        {
            "Angle": [27.2, 27.4, 27.6, 31.5, 31.7, 31.9, 45.3, 45.5, 45.7],
            "Intensity": [20.0, 120.0, 25.0, 18.0, 95.0, 19.0, 16.0, 82.0, 17.0],
        }
    )
    return normalize_table_pattern(df, wavelength_angstrom=1.5406)


def _quality_control() -> PatternQualityControlResult:
    return PatternQualityControlResult(
        status="pattern accepted for later analysis",
        pattern_type="continuous",
        usable_point_count=9,
        coordinate_min=27.2,
        coordinate_max=45.7,
        range_width=18.5,
        median_step_size=0.2,
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


def _candidate_generation(*candidates: RankedPhaseCandidate) -> CandidateGenerationResult:
    return CandidateGenerationResult(
        status="candidate ranking ready for refinement",
        failure_reason=None,
        warnings=(),
        reference_snapshot=None,
        candidates=candidates,
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
    )


def _hypothesis(
    candidate: RankedPhaseCandidate,
    *,
    refinement_status: str,
    convergence_status: str,
    failure_codes: tuple[str, ...] = (),
) -> SinglePhaseHypothesisResult:
    return SinglePhaseHypothesisResult(
        hypothesis_id=f"h-{candidate.candidate_id}-{refinement_status}",
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
        refinement_status=refinement_status,  # type: ignore[arg-type]
        convergence_status=convergence_status,  # type: ignore[arg-type]
        completed_stages=(),
        failed_stage=None,
        runtime_seconds=0.01,
        warnings=(),
        failure_codes=failure_codes,
        exception_summary=None,
        rwp=8.5 if refinement_status == "completed" else None,
        rp=6.0 if refinement_status == "completed" else None,
        goodness_of_fit=1.1 if refinement_status == "completed" else None,
        chi_squared=1.2 if refinement_status == "completed" else None,
        weighted_residual=0.5 if refinement_status == "completed" else None,
        observation_count=9,
        refined_parameter_count=3 if refinement_status == "completed" else 0,
        degrees_of_freedom=6 if refinement_status == "completed" else None,
        phase_scale_factor=1.0 if refinement_status == "completed" else None,
        refined_lattice_parameters=RefinedLatticeParameters(length_a=5.64, length_b=5.64, length_c=5.64),
        zero_shift=0.0 if refinement_status == "completed" else None,
        sample_displacement=None,
        refined_profile_terms={},
        observed_two_theta=(27.2, 27.4, 27.6),
        observed_intensities=(20.0, 120.0, 25.0),
        calculated_total_pattern=(19.0, 118.0, 24.0),
        calculated_background=(5.0, 5.0, 5.0),
        difference_pattern=(1.0, 2.0, 1.0),
        expected_reflections=(),
        significant_positive_residual_regions=(),
        unsupported_strong_predicted_regions=(),
        raw_residual_metrics={},
        provenance=("unit_test",),
    )


def _gsas_calculated_pattern_dataframe(cif_path: Path, instprm_path: str) -> pd.DataFrame:
    x = np.arange(20.0, 80.0 + 0.05, 0.05)
    y = np.full_like(x, 100.0)
    sigma = np.ones_like(x)
    xye_path = write_temp_xye(x, y, sigma)
    gpx_path, remove_gpx = prepare_project_path()
    try:
        project = new_project(configure_gsas(), gpx_path)
        histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        _set_histogram_wavelength(histogram, 1.5406)
        phase = project.add_phase(str(cif_path.resolve()), phasename="nacl_reference", histograms=[histogram], fmthint="CIF")
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
        set_project_cycles(project, 3)
        project.refine(makeBack=True)
        calculated = np.asarray(histogram.getdata("Ycalc"), dtype=float) + 5.0
        return pd.DataFrame({"Angle": x, "Intensity": calculated})
    finally:
        cleanup_paths((xye_path, True), (gpx_path, remove_gpx))


class SinglePhaseRefinementUnitTests(SimpleTestCase):
    def test_refinement_settings_are_deterministic_and_fixed_flags_cannot_be_enabled(self):
        payload = dumps_canonical_json(DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement)
        self.assertEqual(payload, dumps_canonical_json(DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement))
        loaded = json.loads(payload)
        self.assertEqual(loaded["maximum_single_phase_candidates_refined"], 6)
        self.assertTrue(loaded["refine_zero_shift"])
        self.assertFalse(loaded["refine_sample_displacement"])
        self.assertFalse(loaded["refine_crystallite_size"])
        self.assertFalse(loaded["refine_microstrain"])
        self.assertEqual(loaded["allowed_profile_terms"], [])
        with self.assertRaises(ValueError):
            SinglePhaseRefinementSettings(refine_atomic_coordinates=True)
        with self.assertRaises(ValueError):
            SinglePhaseRefinementSettings(refine_preferred_orientation=True)

    def test_request_builder_uses_cleaned_pattern_arrays_and_plain_data(self):
        request = build_single_phase_refinement_request(
            _analysis_input(instrument_profile=_instrument_profile("/tmp/test.instprm")),
            _parsed_pattern(),
            _reference_candidate(),
        )
        parsed = _parsed_pattern()
        self.assertEqual(request.observed_two_theta, parsed.normalized_two_theta)
        self.assertEqual(request.observed_intensities, parsed.normalized_intensities)
        self.assertEqual(request.original_coordinate_type, "two_theta")
        self.assertIsInstance(request.instrument_profile, InstrumentProfile)
        self.assertFalse(hasattr(request.instrument_profile, "objects"))

    def test_instrument_profile_adapter_prefers_explicit_path(self):
        with tempfile.NamedTemporaryFile("w", suffix=".instprm", delete=False) as handle:
            handle.write("Instrument Parameters\n")
            instprm_path = handle.name
        try:
            resolved = _resolve_instrument_profile(
                _instrument_profile(instprm_path),
                DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
            )
        finally:
            os.remove(instprm_path)
        self.assertTrue(resolved.instrument_parameter_path)
        self.assertFalse(resolved.remove_after_use)
        self.assertIn("instrument_profile_source=explicit_path", resolved.provenance)
        self.assertEqual(resolved.warnings, ())

    def test_incomplete_instrument_profile_warns_without_silent_fallback(self):
        resolved = _resolve_instrument_profile(
            InstrumentProfile(instrument_label="CuKa lab data"),
            DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
        )
        self.assertIsNone(resolved.instrument_parameter_path)
        self.assertEqual(resolved.warnings[0].code, "invalid_instrument_profile")

    def test_stage_plan_respects_optional_refinement_flags(self):
        stages = _build_stage_plan(DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement, _instrument_profile())
        self.assertEqual(stages, ("background_scale", "zero_shift", "lattice"))
        configured = replace(
            DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
            refine_zero_shift=False,
            refine_sample_displacement=True,
            allowed_profile_terms=("U", "V"),
            refine_crystallite_size=True,
        )
        self.assertEqual(
            _build_stage_plan(configured, _instrument_profile()),
            (
                "background_scale",
                "sample_displacement",
                "lattice",
                "profile_terms",
                "crystallite_size",
            ),
        )

    def test_missing_pattern_wavelength_and_cif_fail_with_typed_codes(self):
        base_request = build_single_phase_refinement_request(
            _analysis_input(instrument_profile=_instrument_profile("/tmp/test.instprm")),
            _parsed_pattern(),
            _reference_candidate(),
        )
        missing_pattern = refine_single_phase_candidate(
            replace(base_request, observed_two_theta=()),
        )
        self.assertEqual(missing_pattern.failure_codes, ("missing_two_theta_pattern",))

        missing_wavelength = refine_single_phase_candidate(
            replace(base_request, wavelength_angstrom=None),
        )
        self.assertEqual(missing_wavelength.failure_codes, ("missing_wavelength_for_refinement",))

        unreadable_cif = refine_single_phase_candidate(
            replace(base_request, candidate=replace(_reference_candidate(), cif_path="/tmp/does-not-exist.cif")),
        )
        self.assertEqual(unreadable_cif.failure_codes, ("candidate_cif_load_failed",))

    def test_missing_or_incomplete_instrument_profile_returns_typed_failure(self):
        candidate = _reference_candidate()
        parsed = _parsed_pattern()
        missing_request = build_single_phase_refinement_request(
            _analysis_input(instrument_profile=None),
            parsed,
            candidate,
        )
        missing_result = refine_single_phase_candidate(missing_request)
        self.assertEqual(missing_result.failure_codes, ("missing_instrument_profile",))

        incomplete_request = build_single_phase_refinement_request(
            _analysis_input(instrument_profile=InstrumentProfile(instrument_label="CuKa lab data")),
            parsed,
            candidate,
        )
        incomplete_result = refine_single_phase_candidate(incomplete_request)
        self.assertEqual(incomplete_result.failure_codes, ("invalid_instrument_profile",))

    def test_validate_snapshot_enforces_zero_shift_and_lattice_bounds(self):
        candidate = _reference_candidate()
        snapshot = _hypothesis(candidate, refinement_status="completed", convergence_status="converged")
        initial = RefinedLatticeParameters(length_a=5.64, length_b=5.64, length_c=5.64)
        with self.assertRaises(SinglePhaseRefinementError):
            _validate_snapshot(
                replace(snapshot, zero_shift=0.8),
                initial,
                DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
            )
        with self.assertRaises(SinglePhaseRefinementError):
            _validate_snapshot(
                replace(
                    snapshot,
                    refined_lattice_parameters=RefinedLatticeParameters(length_a=6.2, length_b=5.64, length_c=5.64),
                ),
                initial,
                DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
            )

    def test_profile_term_and_residual_evidence_helpers_are_deterministic(self):
        histogram = type(
            "Histogram",
            (),
            {"data": {"Instrument Parameters": [{"U": [0.0, 0.12], "V": [0.0, -0.02], "W": [0.0, 0.03]}]}},
        )()
        self.assertEqual(_extract_profile_terms(histogram, ("U", "W")), {"U": 0.12, "W": 0.03})

        reflections = (
            ExpectedReflectionRecord(1, 0, 0, 4, 3.2, 25.0, 80.0),
            ExpectedReflectionRecord(2, 0, 0, 6, 2.3, 30.0, 30.0),
        )
        positive_regions = _extract_positive_residual_regions(
            observed_two_theta=(24.8, 24.9, 25.0, 25.1, 25.2, 30.0),
            residuals=(0.0, 5.0, 9.0, 4.0, 0.0, -1.0),
            observed_intensities=(10.0, 30.0, 60.0, 25.0, 10.0, 5.0),
            expected_reflections=reflections,
            settings=DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
        )
        self.assertEqual(len(positive_regions), 1)
        self.assertIsInstance(positive_regions[0], ResidualRegion)
        self.assertAlmostEqual(positive_regions[0].nearby_observed_peak_two_theta, 25.0)

        unsupported = _extract_unsupported_predicted_regions(
            observed_two_theta=(24.8, 25.0, 30.0),
            observed_intensities=(0.5, 1.0, 6.0),
            expected_reflections=reflections,
            settings=DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
        )
        self.assertEqual(len(unsupported), 1)
        self.assertIsInstance(unsupported[0], UnsupportedPredictedRegion)
        self.assertAlmostEqual(unsupported[0].predicted_two_theta, 25.0)

    def test_batch_refinement_keeps_running_after_one_candidate_failure(self):
        candidate_one = replace(_reference_candidate(), candidate_id="candidate-one")
        candidate_two = replace(_reference_candidate(), candidate_id="candidate-two")
        with patch(
            "catalog.xrd_analysis.refinement.refine_single_phase_candidate",
            side_effect=[
                _hypothesis(candidate_one, refinement_status="failed", convergence_status="failed", failure_codes=("candidate_refinement_failed",)),
                _hypothesis(candidate_two, refinement_status="completed", convergence_status="converged"),
            ],
        ) as refine_mock:
            result = refine_top_single_phase_candidates(
                _analysis_input(),
                _parsed_pattern(),
                _candidate_generation(candidate_one, candidate_two),
            )
        self.assertEqual(refine_mock.call_count, 2)
        self.assertEqual(result.status, "single-phase-refinement-ready")
        self.assertEqual(len(result.successful_hypotheses), 1)
        self.assertEqual(len(result.failed_candidate_results), 1)

    def test_batch_refinement_stops_at_configured_candidate_limit_and_reports_all_failed(self):
        candidates = tuple(
            replace(_reference_candidate(), candidate_id=f"candidate-{index}")
            for index in range(8)
        )
        limited_config = replace(
            DEFAULT_XRD_ANALYSIS_CONFIG,
            single_phase_refinement=replace(
                DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
                maximum_single_phase_candidates_refined=3,
            ),
        )
        with patch(
            "catalog.xrd_analysis.refinement.refine_single_phase_candidate",
            side_effect=[
                _hypothesis(candidate, refinement_status="failed", convergence_status="failed", failure_codes=("candidate_refinement_failed",))
                for candidate in candidates[:3]
            ],
        ) as refine_mock:
            result = refine_top_single_phase_candidates(
                _analysis_input(),
                _parsed_pattern(),
                _candidate_generation(*candidates),
                configuration=limited_config,
            )
        self.assertEqual(refine_mock.call_count, 3)
        self.assertEqual(result.status, "single-phase refinement failed")
        self.assertEqual(result.warnings[-1].code, "all_single_phase_refinements_failed")

    def test_pipeline_continues_to_final_decision_boundary_without_classification_shortcuts(self):
        candidate_generation = _candidate_generation(_reference_candidate())
        refinement_batch = SinglePhaseRefinementBatchResult(
            status="single-phase-refinement-ready",
            warnings=(),
            successful_hypotheses=(_hypothesis(_reference_candidate(), refinement_status="completed", convergence_status="converged"),),
            failed_candidate_results=(),
            algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
            configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        with (
            patch("catalog.xrd_analysis.pipeline.parse_and_qc_input_pattern", return_value=(_parsed_pattern(), _quality_control())),
            patch("catalog.xrd_analysis.pipeline.build_ranked_phase_candidates", return_value=candidate_generation),
            patch("catalog.xrd_analysis.pipeline.refine_top_single_phase_candidates", return_value=refinement_batch),
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
                    provenance=_analysis_input().provenance,
                ),
            ) as decision_runner,
        ):
            result = run_xrd_analysis_pipeline(_analysis_input())
        self.assertIsInstance(result, XRDAnalysisResult)
        self.assertEqual(result.phase_state, "unresolved")
        decision_runner.assert_called_once()

    def test_hypothesis_serialization_is_json_safe_and_deterministic(self):
        hypothesis = _hypothesis(_reference_candidate(), refinement_status="completed", convergence_status="converged")
        payload = dumps_canonical_json(hypothesis)
        self.assertEqual(payload, dumps_canonical_json(hypothesis))
        loaded = json.loads(payload)
        self.assertEqual(loaded["candidate_id"], "curated_nacl_rocksalt")
        self.assertEqual(loaded["refinement_status"], "completed")


class SinglePhaseRefinementIntegrationTests(SimpleTestCase):
    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_real_gsas_single_phase_refinement_with_curated_nacl(self):
        candidate = _reference_candidate()
        files_before = sorted(path.name for path in REFERENCE_PHASES_DIR.iterdir())
        instprm_path, remove_instprm = resolve_instrument_parameter_file()
        try:
            analysis_input = _analysis_input(instrument_profile=_instrument_profile(instprm_path))
            dataframe = _gsas_calculated_pattern_dataframe(Path(candidate.cif_path), instprm_path)
            parsed_pattern = normalize_table_pattern(dataframe, wavelength_angstrom=1.5406)
            request = build_single_phase_refinement_request(analysis_input, parsed_pattern, candidate)
            with patch.object(socket, "create_connection", side_effect=AssertionError("network access is forbidden")):
                result = refine_single_phase_candidate(request)
        finally:
            cleanup_paths((instprm_path, remove_instprm))
        self.assertEqual(result.refinement_status, "completed")
        self.assertEqual(result.convergence_status, "converged")
        self.assertEqual(
            tuple(stage.stage_name for stage in result.completed_stages),
            ("background_scale", "zero_shift", "lattice"),
        )
        self.assertIsNotNone(result.gsasii_version)
        self.assertIsNotNone(result.rwp)
        self.assertIsNotNone(result.rp)
        self.assertIsNotNone(result.goodness_of_fit)
        self.assertGreater(result.observation_count, 100)
        self.assertGreater(len(result.expected_reflections), 3)
        self.assertEqual(len(result.observed_two_theta), len(result.calculated_total_pattern))
        self.assertEqual(len(result.observed_two_theta), len(result.calculated_background))
        self.assertEqual(len(result.observed_two_theta), len(result.difference_pattern))
        self.assertNotIn("sample_displacement", tuple(stage.stage_name for stage in result.completed_stages))
        self.assertEqual(files_before, sorted(path.name for path in REFERENCE_PHASES_DIR.iterdir()))
