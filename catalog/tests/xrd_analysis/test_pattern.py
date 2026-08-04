import math
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase, override_settings

from catalog.xrd_analysis.pattern import (
    CoordinateConversionError,
    PatternParseError,
    RawFileReferenceError,
    normalize_table_pattern,
    parse_and_qc_input_pattern,
    parse_pattern_from_input,
    resolve_raw_file_reference_path,
    run_pattern_quality_control,
)
from catalog.xrd_analysis.pipeline import run_xrd_analysis_pipeline
from catalog.xrd_analysis.reporting import dumps_canonical_json
from catalog.xrd_analysis.schemas import (
    CandidateGenerationResult,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    ParsedPatternMetadata,
    RawFileReference,
    SinglePhaseRefinementBatchResult,
    SynthesisContext,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
    XRDAnalysisResult,
)


LOOP_CSV = (
    "[Measurement conditions]\n"
    "K-Alpha1 wavelength,1.5405980\n"
    "Angle,Intensity\n"
    "20.0,50\n"
    "20.2,100\n"
    "20.4,50\n"
)

GENERIC_TXT = (
    "# angle intensity\n"
    "10.0  12\n"
    "10.2  18\n"
    "10.4  15\n"
    "10.6  11\n"
)

RIGAKU_ASC = (
    "*TYPE = Raw\n"
    "*GONIO = RIGAKU\n"
    "*XUNIT = deg.\n"
    "*START = 10.0\n"
    "*STOP = 14.0\n"
    "*STEP = 1.0\n"
    "100,200,300\n"
    "400,500\n"
)

PDF_CARD = (
    "PDF#32-1395: QM=Common(+)\n"
    "Radiation=CuKa1\tLambda=1.5406\n"
    "2-Theta    d(?)   I(f)  ( h k l)\n"
    " 23.143  3.8400   85.0  ( 0 0 2)\n"
    " 23.643  3.7600  100.0  ( 0 2 0)\n"
)


def _analysis_input(path: str, **overrides) -> XRDAnalysisInput:
    payload = {
        "material_auid": "M:test",
        "recipe_auid": "M:test:R:test",
        "trial_id": "T1",
        "raw_file_hash": "ab" * 32,
        "raw_file_reference": RawFileReference(
            reference_kind="stored_path",
            locator=path,
            original_filename=os.path.basename(path),
        ),
        "nominal_composition": "CoNiO",
        "elements": ("Co", "Ni", "O"),
        "stoichiometric_amounts": (),
        "structure_family": "rocksalt",
        "expected_space_group": "Fm-3m (#225)",
        "expected_site_assignments": (),
        "radiation_source": "cu_ka",
        "wavelength_angstrom": 1.5406,
        "coordinate_type": "two_theta",
        "coordinate_column": "Angle",
        "intensity_column": "Intensity",
        "scan_min": 10.0,
        "scan_max": 80.0,
        "step_size": 0.2,
        "scan_speed": 1.0,
        "instrument_profile": None,
        "synthesis_context": SynthesisContext(
            ordered_steps=(),
            precursor_records=(),
            temperatures_c=(),
            ramp_rates_c_min=(),
            hold_times_hours=(),
            atmospheres=(),
            furnace_types=(),
            preparation_notes=(),
        ),
        "warnings": (),
        "algorithm_version": DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        "configuration_version": DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        "provenance": XRDAnalysisProvenance(
            source_material={},
            source_recipe={},
            source_trial={"trial_date": datetime(2026, 7, 27, tzinfo=timezone.utc).isoformat()},
            source_raw_file=None,
            measurement_metadata=(),
        ),
    }
    payload.update(overrides)
    return XRDAnalysisInput(**payload)


def _write(tmpdir: str, name: str, content: str) -> str:
    path = os.path.join(tmpdir, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


class RawFileReferenceResolutionTests(SimpleTestCase):
    def test_media_relative_raw_path_resolves_under_media_root(self):
        with tempfile.TemporaryDirectory() as media_root:
            relative_path = "xrd/M:test/R:test/T1/raw.csv"
            expected = _write(media_root, relative_path, "Angle,Intensity\n20,100\n")
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                resolved = resolve_raw_file_reference_path(
                    RawFileReference(reference_kind="raw_db_path", locator=relative_path)
                )
        self.assertEqual(resolved, expected)

    def test_media_relative_raw_data_link_resolves_under_media_root(self):
        with tempfile.TemporaryDirectory() as media_root:
            relative_path = "xrd/M:test/R:test/T1/raw.csv"
            expected = _write(media_root, relative_path, "Angle,Intensity\n20,100\n")
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                resolved = resolve_raw_file_reference_path(
                    RawFileReference(
                        reference_kind="raw_data_link",
                        locator="/media/xrd/M:test/R:test/T1/raw.csv",
                    )
                )
        self.assertEqual(resolved, expected)

    def test_absolute_path_remains_supported_for_internal_callers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "raw.csv", "Angle,Intensity\n20,100\n")
            resolved = resolve_raw_file_reference_path(
                RawFileReference(reference_kind="stored_path", locator=path)
            )
        self.assertEqual(resolved, path)

    def test_nonexistent_media_relative_path_fails(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                with self.assertRaises(RawFileReferenceError):
                    resolve_raw_file_reference_path(
                        RawFileReference(
                            reference_kind="raw_db_path",
                            locator="xrd/M:test/R:test/T1/missing.csv",
                        )
                    )

    def test_relative_path_traversal_outside_media_root_fails(self):
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                with self.assertRaises(RawFileReferenceError):
                    resolve_raw_file_reference_path(
                        RawFileReference(reference_kind="raw_db_path", locator="../escape/raw.csv")
                    )

    def test_pipeline_parser_accepts_repository_media_relative_xrd_locator(self):
        with tempfile.TemporaryDirectory() as media_root:
            relative_path = "xrd/M:6f4b8627e825/R:2d9f67f9a8eb/1/raw.csv"
            _write(
                media_root,
                relative_path,
                "Angle,Intensity\n20.0,50\n20.2,100\n20.4,50\n",
            )
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                parsed = parse_pattern_from_input(
                    _analysis_input(
                        relative_path,
                        raw_file_reference=RawFileReference(
                            reference_kind="raw_db_path",
                            locator=relative_path,
                            original_filename="raw.csv",
                        ),
                        scan_min=20.0,
                        scan_max=20.4,
                    )
                )
        self.assertEqual(parsed.normalized_two_theta, (20.0, 20.2, 20.4))


class UnifiedPatternInterfaceTests(SimpleTestCase):
    def test_csv_through_unified_interface(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "scan.csv", LOOP_CSV)
            parsed = parse_pattern_from_input(_analysis_input(path, scan_min=20.0, scan_max=20.4))
        self.assertEqual(parsed.parser_type, "loop_csv")
        self.assertEqual(parsed.pattern_type, "continuous")
        self.assertEqual(parsed.original_coordinate_type, "two_theta")
        self.assertEqual(parsed.usable_point_count, 3)
        self.assertEqual(parsed.normalized_two_theta, (20.0, 20.2, 20.4))

    def test_generic_txt_through_unified_interface(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "scan.txt", GENERIC_TXT)
            parsed = parse_pattern_from_input(_analysis_input(path, scan_min=10.0, scan_max=10.6))
        self.assertEqual(parsed.parser_type, "generic_txt")
        self.assertEqual(parsed.usable_point_count, 4)
        self.assertEqual(parsed.coordinate_column, "Angle")
        self.assertEqual(parsed.intensity_column, "Intensity")

    def test_rigaku_pathway_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "scan.csv", RIGAKU_ASC)
            parsed = parse_pattern_from_input(_analysis_input(path, scan_min=10.0, scan_max=14.0, step_size=1.0))
        self.assertEqual(parsed.parser_type, "rigaku_ascii")
        self.assertEqual(parsed.usable_point_count, 5)
        self.assertEqual(parsed.normalized_two_theta[0], 10.0)
        self.assertEqual(parsed.normalized_intensities[-1], 500.0)

    def test_reflection_card_stick_pattern_is_supported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "card.txt", PDF_CARD)
            parsed, qc = parse_and_qc_input_pattern(
                _analysis_input(path, scan_min=23.0, scan_max=23.7, step_size=None)
            )
        self.assertEqual(parsed.parser_type, "reflection_card")
        self.assertEqual(parsed.pattern_type, "stick")
        self.assertEqual(qc.status, "pattern accepted for later analysis")
        self.assertTrue(any(w.code == "stick_pattern_limited" for w in qc.warnings))

    def test_binary_raw_adapter_uses_existing_parser_path(self):
        fake_df = pd.DataFrame({"Angle": [20.0, 20.2], "Intensity": [10.0, 30.0]})
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "scan.raw")
            with open(path, "wb") as handle:
                handle.write(b"RAW")
            with patch("catalog.xrd_analysis.pattern.parse_xrd_file", return_value=([], fake_df)) as reader:
                parsed = parse_pattern_from_input(_analysis_input(path))
        reader.assert_called_once_with(path, "scan.raw")
        self.assertEqual(parsed.parser_type, "binary_raw")
        self.assertEqual(parsed.normalized_two_theta, (20.0, 20.2))


class TableNormalizationTests(SimpleTestCase):
    def test_explicit_column_selection(self):
        df = pd.DataFrame({"d_spacing": [2.0, 1.5], "counts": [10.0, 15.0]})
        parsed = normalize_table_pattern(
            df,
            coordinate_column="d_spacing",
            intensity_column="counts",
            coordinate_type="d_spacing",
            wavelength_angstrom=1.5406,
        )
        self.assertEqual(parsed.coordinate_column, "d_spacing")
        self.assertEqual(parsed.intensity_column, "counts")
        self.assertIsNotNone(parsed.normalized_two_theta)
        self.assertAlmostEqual(parsed.normalized_q[0], (2.0 * 3.141592653589793) / 2.0)

    def test_automatic_column_detection(self):
        df = pd.DataFrame({"Q_value": [1.0, 1.2, 1.4], "counts": [5.0, 8.0, 7.0]})
        parsed = normalize_table_pattern(
            df,
            coordinate_type="q",
            wavelength_angstrom=1.5406,
        )
        self.assertEqual(parsed.coordinate_column, "Q_value")
        self.assertEqual(parsed.intensity_column, "counts")
        self.assertEqual(parsed.original_coordinate_type, "q")

    def test_invalid_row_removal_and_deterministic_sorting(self):
        df = pd.DataFrame(
            {
                "Angle": [20.4, "bad", 20.0, 20.2],
                "Intensity": [40.0, 10.0, 20.0, None],
            }
        )
        parsed = normalize_table_pattern(df)
        self.assertEqual(parsed.original_coordinates, (20.4, None, 20.0, 20.2))
        self.assertEqual(parsed.coordinate_order, "unsorted")
        self.assertEqual(parsed.normalized_two_theta, (20.0, 20.4))
        self.assertTrue(any(w.code == "invalid_rows_removed" for w in parsed.warnings))

    def test_deterministic_duplicate_handling(self):
        df = pd.DataFrame({"Angle": [20.0, 20.0, 20.0000004, 20.2], "Intensity": [1.0, 2.0, 3.0, 4.0]})
        parsed = normalize_table_pattern(df)
        self.assertEqual(parsed.normalized_two_theta, (20.0, 20.2))
        self.assertEqual(parsed.normalized_intensities, (6.0, 4.0))

    def test_two_theta_to_d_spacing_and_q_conversion(self):
        df = pd.DataFrame({"Angle": [20.0, 40.0], "Intensity": [10.0, 5.0]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        expected_d = 1.5406 / (2.0 * math.sin(math.radians(20.0 / 2.0)))
        expected_q = (4.0 * math.pi * math.sin(math.radians(40.0 / 2.0))) / 1.5406
        self.assertAlmostEqual(parsed.normalized_d_spacing[0], expected_d, places=6)
        self.assertAlmostEqual(parsed.normalized_q[1], expected_q, places=6)

    def test_d_spacing_to_two_theta_conversion(self):
        df = pd.DataFrame({"d": [2.0, 1.5], "Intensity": [10.0, 20.0]})
        parsed = normalize_table_pattern(
            df,
            coordinate_column="d",
            intensity_column="Intensity",
            coordinate_type="d_spacing",
            wavelength_angstrom=1.5406,
        )
        self.assertAlmostEqual(parsed.normalized_two_theta[0], 45.30610552092664)
        self.assertAlmostEqual(parsed.normalized_two_theta[1], 61.79894248438266)

    def test_q_to_two_theta_conversion(self):
        df = pd.DataFrame({"Q": [1.0, 2.0], "Intensity": [5.0, 10.0]})
        parsed = normalize_table_pattern(
            df,
            coordinate_type="q",
            wavelength_angstrom=1.5406,
        )
        expected_two_theta = math.degrees(
            2.0 * math.asin((1.0 * 1.5406) / (4.0 * math.pi))
        )
        self.assertAlmostEqual(parsed.normalized_two_theta[0], expected_two_theta, places=6)
        self.assertAlmostEqual(parsed.normalized_d_spacing[1], math.pi, places=6)

    def test_missing_wavelength_preserves_original_representation(self):
        df = pd.DataFrame({"Angle": [20.0, 20.2], "Intensity": [10.0, 15.0]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=None)
        self.assertEqual(parsed.normalized_two_theta, (20.0, 20.2))
        self.assertIsNone(parsed.normalized_d_spacing)
        self.assertTrue(any(w.code == "missing_wavelength_for_conversion" for w in parsed.warnings))

    def test_impossible_coordinate_conversion_raises_typed_error(self):
        df = pd.DataFrame({"d": [0.1], "Intensity": [10.0]})
        with self.assertRaises(CoordinateConversionError):
            normalize_table_pattern(
                df,
                coordinate_column="d",
                intensity_column="Intensity",
                coordinate_type="d_spacing",
                wavelength_angstrom=1.5406,
            )


class QualityControlTests(SimpleTestCase):
    def test_scan_range_mismatch_warning(self):
        df = pd.DataFrame({"Angle": [20.0, 20.2, 20.4], "Intensity": [10.0, 20.0, 10.0]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=10.0, scan_max=80.0))
        self.assertTrue(any(w.code == "parsed_range_mismatch" for w in qc.warnings))

    def test_step_size_mismatch_warning(self):
        df = pd.DataFrame({"Angle": [20.0, 20.5, 21.0], "Intensity": [10.0, 20.0, 10.0]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=21.0, step_size=0.1))
        self.assertTrue(any(w.code == "parsed_step_size_mismatch" for w in qc.warnings))

    def test_irregular_grid_warning_and_missing_interval_detection(self):
        df = pd.DataFrame({"Angle": [20.0, 20.1, 20.7, 20.8], "Intensity": [10.0, 15.0, 14.0, 11.0]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=20.8, step_size=0.1))
        self.assertGreater(qc.missing_interval_count, 0)
        self.assertTrue(any(w.code == "irregular_step_spacing" for w in qc.warnings))
        self.assertTrue(any(w.code == "missing_intervals" for w in qc.warnings))

    def test_flat_pattern_rejection(self):
        df = pd.DataFrame({"Angle": [20.0 + (0.1 * idx) for idx in range(30)], "Intensity": [100.0] * 30})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=22.9, step_size=0.1))
        self.assertEqual(qc.status, "insufficient-quality data")
        self.assertIn("flat_signal", qc.failure_codes)

    def test_empty_pattern_rejection(self):
        df = pd.DataFrame({"Angle": ["bad"], "Intensity": ["nope"]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv"))
        self.assertEqual(qc.status, "insufficient-quality data")
        self.assertIn("no_usable_points", qc.failure_codes)

    def test_low_information_pattern_rejection(self):
        df = pd.DataFrame({"Angle": [20.0, 20.1, 20.2, 20.3, 20.4], "Intensity": [10, 10.2, 10.1, 10.3, 10.2]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=20.4, step_size=0.1))
        self.assertEqual(qc.status, "insufficient-quality data")
        self.assertIn("too_few_usable_points", qc.failure_codes)

    def test_noisy_but_usable_pattern_acceptance(self):
        theta = [20.0 + (0.1 * idx) for idx in range(60)]
        intensity = [
            20.0 + (idx % 3) + (80.0 if 25 <= idx <= 28 else 0.0) + (35.0 if 42 <= idx <= 45 else 0.0)
            for idx in range(60)
        ]
        df = pd.DataFrame({"Angle": theta, "Intensity": intensity})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=25.9, step_size=0.1))
        self.assertEqual(qc.status, "pattern accepted for later analysis")
        self.assertGreaterEqual(qc.detectable_peak_region_count, 1)

    def test_clean_pattern_acceptance(self):
        theta = [20.0 + (0.1 * idx) for idx in range(40)]
        intensity = [50.0 + (180.0 if idx == 20 else 0.0) for idx in range(40)]
        df = pd.DataFrame({"Angle": theta, "Intensity": intensity})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=23.9, step_size=0.1))
        self.assertEqual(qc.status, "pattern accepted for later analysis")

    def test_negative_intensity_metrics(self):
        df = pd.DataFrame({"Angle": [20.0 + (0.1 * idx) for idx in range(30)], "Intensity": [(-1.0 if idx < 5 else 10.0) for idx in range(30)]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=22.9, step_size=0.1))
        self.assertGreater(qc.negative_intensity_fraction, 0.0)
        self.assertTrue(any(w.code == "high_negative_intensity_fraction" for w in qc.warnings))

    def test_all_non_positive_intensity_rejection(self):
        df = pd.DataFrame({"Angle": [20.0 + (0.1 * idx) for idx in range(30)], "Intensity": [0.0] * 30})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=22.9, step_size=0.1))
        self.assertIn("all_non_positive_intensity", qc.failure_codes)

    def test_json_safe_deterministic_serialization(self):
        df = pd.DataFrame({"Angle": [20.0, 20.2, 20.4], "Intensity": [10.0, 20.0, 10.0]})
        parsed = normalize_table_pattern(df, wavelength_angstrom=1.5406)
        qc = run_pattern_quality_control(parsed, _analysis_input("/tmp/fake.csv", scan_min=20.0, scan_max=20.4, step_size=0.2))
        payload = {"parsed": parsed, "qc": qc}
        self.assertEqual(dumps_canonical_json(payload), dumps_canonical_json(payload))


class PipelineStageTests(SimpleTestCase):
    def test_pipeline_stops_on_failed_qc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "flat.csv", "Angle,Intensity\n20.0,100\n20.1,100\n20.2,100\n20.3,100\n20.4,100\n")
            result = run_xrd_analysis_pipeline(
                _analysis_input(path, scan_min=20.0, scan_max=20.4, step_size=0.1)
            )
        self.assertEqual(result.phase_state, "insufficient-quality data")

    def test_pipeline_continues_to_final_decision_when_qc_passes(self):
        theta = "\n".join(f"{20.0 + (0.1 * idx):.1f},{50 + (200 if idx == 20 else 0)}" for idx in range(40))
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _write(tmpdir, "clean.csv", "Angle,Intensity\n" + theta + "\n")
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
                        provenance=_analysis_input(path).provenance,
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
                result = run_xrd_analysis_pipeline(
                    _analysis_input(path, scan_min=20.0, scan_max=23.9, step_size=0.1)
                )
        self.assertEqual(result.phase_state, "unresolved")
        decision_runner.assert_called_once()

    def test_missing_raw_reference_raises_typed_error(self):
        with self.assertRaises(RawFileReferenceError):
            parse_pattern_from_input(_analysis_input("/tmp/does-not-exist.csv"))

    def test_invalid_explicit_columns_raise_parse_error(self):
        df = pd.DataFrame({"Angle": [20.0], "Intensity": [10.0]})
        with self.assertRaises(PatternParseError):
            normalize_table_pattern(df, coordinate_column="TwoTheta")
