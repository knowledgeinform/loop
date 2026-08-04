import json
import math
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase

from catalog.gsas_runtime import cleanup_paths, configure_gsas, new_project, prepare_project_path, resolve_instrument_parameter_file
from catalog.xrd_analysis.candidates import (
    CandidateSimulationError,
    REFERENCE_MANIFEST_PATH,
    REFERENCE_PHASES_DIR,
    ReferenceSnapshotMismatchError,
    _simulate_reflections_from_cif,
    _apply_chemistry_filter,
    _cluster_duplicate_candidates,
    _diffraction_metrics,
    _select_top_candidates,
    _synthesis_context_score,
    _with_soft_scores,
    build_ranked_phase_candidates,
    load_reference_phase_snapshot,
    simulate_candidate_pattern,
    validate_reference_phase_snapshot,
)
from catalog.xrd_analysis.pattern import normalize_table_pattern
from catalog.xrd_analysis.pipeline import run_xrd_analysis_pipeline
from catalog.xrd_analysis.reporting import dumps_canonical_json
from catalog.xrd_analysis.schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CandidateGenerationResult,
    CandidateSimulation,
    LinkedStructureReference,
    PatternQualityControlResult,
    RankedPhaseCandidate,
    ReferencePhaseSnapshot,
    RawFileReference,
    SinglePhaseRefinementBatchResult,
    StoichiometricAmount,
    SynthesisContext,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
    XRDAnalysisResult,
)

POSITION_TOLERANCE_DEGREES = 0.01
D_SPACING_TOLERANCE_ANGSTROM = 1e-4
INTENSITY_TOLERANCE = 1e-6


def _gsas_runtime_available():
    try:
        from GSASII import GSASIIscriptable  # type: ignore  # noqa: F401
        from GSASII import defaultIparms  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def _fake_candidate_simulation(
    candidate,
    analysis_input,
    parsed_pattern,
    *,
    configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
):
    del analysis_input
    del parsed_pattern
    return CandidateSimulation(
        candidate_id=candidate.candidate_id,
        reflection_positions_two_theta=(27.4, 31.7, 45.5, 56.5, 66.2),
        reflection_relative_intensities=(1.0, 0.82, 0.68, 0.45, 0.35),
        measured_coordinate_min=20.0,
        measured_coordinate_max=80.0,
        wavelength_angstrom=1.5406,
        settings_version=configuration.configuration_version,
        warnings=(),
        provenance=("unit_test_simulation",),
    )


def _trusted_gsas_reflections(
    cif_path,
    *,
    wavelength_angstrom,
    two_theta_min,
    two_theta_max,
):
    phase_name = "trusted_phase"
    gpx_path, remove_gpx = prepare_project_path()
    instprm_path, remove_instprm = resolve_instrument_parameter_file()
    with tempfile.NamedTemporaryFile("w", suffix=".xye", delete=False) as handle:
        step = min(0.05, max(0.01, (two_theta_max - two_theta_min) / 3000.0))
        point_count = max(2, int(round((two_theta_max - two_theta_min) / step)) + 1)
        for index in range(point_count):
            angle = two_theta_min + min(index * step, two_theta_max - two_theta_min)
            handle.write(f"{angle:.6f} 100.000000 1.000000\n")
        xye_path = handle.name

    try:
        project = new_project(configure_gsas(), gpx_path)
        histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        instrument = histogram.data["Instrument Parameters"][0]
        if "Lam" in instrument:
            instrument["Lam"][0] = wavelength_angstrom
            instrument["Lam"][1] = wavelength_angstrom
        if "Lam1" in instrument:
            instrument["Lam1"][0] = wavelength_angstrom
            instrument["Lam1"][1] = wavelength_angstrom
        if "Lam2" in instrument:
            instrument["Lam2"][0] = wavelength_angstrom
            instrument["Lam2"][1] = wavelength_angstrom
        if "I(L2)/I(L1)" in instrument:
            instrument["I(L2)/I(L1)"][0] = 0.0
            instrument["I(L2)/I(L1)"][1] = 0.0
        project.add_phase(str(cif_path), phasename=phase_name, histograms=[histogram], fmthint="CIF")
        project.refine(makeBack=True)
        ref_list = histogram.data["Reflection Lists"][phase_name]["RefList"]
        rows = []
        for row in ref_list:
            two_theta = round(float(row[5]), 6)
            if not (two_theta_min <= two_theta <= two_theta_max):
                continue
            rows.append(
                {
                    "h": int(round(float(row[0]))),
                    "k": int(round(float(row[1]))),
                    "l": int(round(float(row[2]))),
                    "multiplicity": int(round(float(row[3]))),
                    "d_spacing": float(row[4]),
                    "two_theta": two_theta,
                    "relative_intensity": float(row[9] * row[11]),
                }
            )
    finally:
        cleanup_paths((gpx_path, remove_gpx), (instprm_path, remove_instprm), (xye_path, True))

    max_intensity = max(item["relative_intensity"] for item in rows)
    for item in rows:
        item["relative_intensity"] /= max_intensity
    rows.sort(key=lambda item: item["two_theta"])
    return rows


def _reflection_identity(reflection):
    return (
        int(reflection["h"]),
        int(reflection["k"]),
        int(reflection["l"]),
    )


def _analysis_input(
    *,
    nominal_composition="NaCl",
    elements=("Cl", "Na"),
    stoichiometric_amounts=(("Cl", 1.0), ("Na", 1.0)),
    structure_family="rocksalt",
    expected_space_group="F m -3 m",
    linked_structure_references=(),
    synthesis_context=None,
):
    if synthesis_context is None:
        synthesis_context = SynthesisContext(
            ordered_steps=(),
            precursor_records=(),
            temperatures_c=(),
            ramp_rates_c_min=(),
            hold_times_hours=(),
            atmospheres=(),
            furnace_types=(),
            preparation_notes=(),
        )
    return XRDAnalysisInput(
        material_auid="M:test",
        recipe_auid="M:test:R:test",
        trial_id="T1",
        raw_file_hash="ab" * 32,
        raw_file_reference=RawFileReference(
            reference_kind="stored_path",
            locator="/tmp/scan.csv",
            original_filename="scan.csv",
        ),
        nominal_composition=nominal_composition,
        elements=elements,
        stoichiometric_amounts=tuple(
            StoichiometricAmount(element, amount) for element, amount in stoichiometric_amounts
        ),
        structure_family=structure_family,
        expected_space_group=expected_space_group,
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
        synthesis_context=synthesis_context,
        warnings=(),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        provenance=XRDAnalysisProvenance(
            source_material={"id": "M:test"},
            source_recipe={"id": "M:test:R:test"},
            source_trial={"trial_id": "T1", "trial_date": datetime(2026, 7, 27, tzinfo=timezone.utc)},
            source_raw_file=None,
            measurement_metadata=(),
        ),
        linked_structure_references=tuple(linked_structure_references),
    )


def _pattern_dataframe():
    points = [
        (26.8, 8.0),
        (27.3, 40.0),
        (27.4, 100.0),
        (27.5, 42.0),
        (31.6, 55.0),
        (31.7, 78.0),
        (31.8, 57.0),
        (45.3, 60.0),
        (45.4, 82.0),
        (45.5, 61.0),
        (56.4, 35.0),
        (56.5, 48.0),
        (56.6, 37.0),
        (66.1, 28.0),
        (66.2, 41.0),
        (66.3, 29.0),
    ]
    return pd.DataFrame(points, columns=["Angle", "Intensity"])


def _parsed_pattern():
    return normalize_table_pattern(_pattern_dataframe(), wavelength_angstrom=1.5406)


def _quality_control():
    return PatternQualityControlResult(
        status="pattern accepted for later analysis",
        pattern_type="continuous",
        usable_point_count=len(_pattern_dataframe()),
        coordinate_min=26.8,
        coordinate_max=66.3,
        range_width=39.5,
        median_step_size=0.1,
        step_size_variation=0.0,
        fraction_invalid_rows_removed=0.0,
        duplicate_count=0,
        negative_intensity_fraction=0.0,
        non_positive_intensity_fraction=0.0,
        approximate_signal_to_noise=6.0,
        detectable_peak_region_count=5,
        missing_interval_count=0,
        clipping_detected=False,
    )


class ReferenceSnapshotTests(SimpleTestCase):
    def test_loading_reference_phase_manifest_is_deterministic(self):
        snapshot = load_reference_phase_snapshot()
        self.assertEqual(snapshot.snapshot_version, "loop-reference-phases-v1")
        self.assertEqual(len(snapshot.entries), 2)
        self.assertEqual(snapshot, load_reference_phase_snapshot())
        payload = json.loads(dumps_canonical_json(snapshot))
        self.assertEqual(payload["entries"][0]["candidate_identifier"], "curated_nacl_rocksalt")

    def test_validating_reference_phase_hashes_detects_mismatch(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "manifest.json")
            bad_payload = json.loads(REFERENCE_MANIFEST_PATH.read_text(encoding="utf-8"))
            bad_payload["entries"][0]["sha256"] = "0" * 64
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(bad_payload, handle)
            snapshot = load_reference_phase_snapshot(manifest_path, validate_hashes=False)
            with self.assertRaises(ReferenceSnapshotMismatchError):
                validate_reference_phase_snapshot(snapshot)


class SimulatorValidationTests(SimpleTestCase):
    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_local_simulator_matches_gsas_reflection_set_for_nacl_reference(self):
        entry = next(
            item for item in load_reference_phase_snapshot().entries
            if item.candidate_identifier == "curated_nacl_rocksalt"
        )
        cif_path = REFERENCE_PHASES_DIR / entry.relative_cif_path
        local = _simulate_reflections_from_cif(
            cif_path,
            wavelength_angstrom=1.5406,
            two_theta_min=20.0,
            two_theta_max=80.0,
        )
        trusted = _trusted_gsas_reflections(
            cif_path.resolve(),
            wavelength_angstrom=1.5406,
            two_theta_min=20.0,
            two_theta_max=80.0,
        )
        self.assertEqual([_reflection_identity(item) for item in local], [_reflection_identity(item) for item in trusted])
        for local_row, trusted_row in zip(local, trusted):
            self.assertEqual(local_row["multiplicity"], trusted_row["multiplicity"])
            self.assertAlmostEqual(local_row["two_theta"], trusted_row["two_theta"], delta=POSITION_TOLERANCE_DEGREES)
            self.assertAlmostEqual(local_row["d_spacing"], trusted_row["d_spacing"], delta=D_SPACING_TOLERANCE_ANGSTROM)
        strongest_local = sorted(local, key=lambda item: item["relative_intensity"], reverse=True)
        strongest_trusted = sorted(trusted, key=lambda item: item["relative_intensity"], reverse=True)
        self.assertEqual(
            [_reflection_identity(item) for item in strongest_local[:5]],
            [_reflection_identity(item) for item in strongest_trusted[:5]],
        )
        for local_row, trusted_row in zip(
            sorted(local, key=lambda item: item["two_theta"]),
            sorted(trusted, key=lambda item: item["two_theta"]),
        ):
            self.assertAlmostEqual(local_row["relative_intensity"], trusted_row["relative_intensity"], delta=INTENSITY_TOLERANCE)

    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_all_enabled_reference_cifs_validate_against_gsas(self):
        snapshot = load_reference_phase_snapshot()
        for entry in snapshot.entries:
            if not entry.enabled:
                continue
            cif_path = (REFERENCE_PHASES_DIR / entry.relative_cif_path).resolve()
            local = _simulate_reflections_from_cif(
                cif_path,
                wavelength_angstrom=1.5406,
                two_theta_min=20.0,
                two_theta_max=80.0,
            )
            trusted = _trusted_gsas_reflections(
                cif_path,
                wavelength_angstrom=1.5406,
                two_theta_min=20.0,
                two_theta_max=80.0,
            )
            self.assertEqual(
                [_reflection_identity(item) for item in local],
                [_reflection_identity(item) for item in trusted],
                entry.candidate_identifier,
            )
            self.assertEqual(
                [item["multiplicity"] for item in local],
                [item["multiplicity"] for item in trusted],
                entry.candidate_identifier,
            )
            for local_row, trusted_row in zip(local, trusted):
                self.assertAlmostEqual(local_row["two_theta"], trusted_row["two_theta"], delta=POSITION_TOLERANCE_DEGREES)
                self.assertAlmostEqual(local_row["d_spacing"], trusted_row["d_spacing"], delta=D_SPACING_TOLERANCE_ANGSTROM)
                self.assertAlmostEqual(local_row["relative_intensity"], trusted_row["relative_intensity"], delta=INTENSITY_TOLERANCE)

    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_systematic_absences_and_centering_extinctions_match_trusted_nacl_reference(self):
        entry = next(
            item for item in load_reference_phase_snapshot().entries
            if item.candidate_identifier == "curated_nacl_rocksalt"
        )
        reflections = _simulate_reflections_from_cif(
            (REFERENCE_PHASES_DIR / entry.relative_cif_path).resolve(),
            wavelength_angstrom=1.5406,
            two_theta_min=20.0,
            two_theta_max=80.0,
        )
        hkl_set = {_reflection_identity(item) for item in reflections}
        self.assertNotIn((1, 1, 0), hkl_set)
        self.assertNotIn((2, 1, 0), hkl_set)
        self.assertNotIn((2, 1, 1), hkl_set)
        self.assertIn((1, 1, 1), hkl_set)
        self.assertIn((2, 0, 0), hkl_set)

    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_duplicate_handling_range_filtering_and_wavelength_are_deterministic(self):
        entry = next(
            item for item in load_reference_phase_snapshot().entries
            if item.candidate_identifier == "curated_nacl_rocksalt"
        )
        cif_path = (REFERENCE_PHASES_DIR / entry.relative_cif_path).resolve()
        first = _simulate_reflections_from_cif(cif_path, wavelength_angstrom=1.5406, two_theta_min=20.0, two_theta_max=80.0)
        second = _simulate_reflections_from_cif(cif_path, wavelength_angstrom=1.5406, two_theta_min=20.0, two_theta_max=80.0)
        self.assertEqual(first, second)
        self.assertEqual(len(first), len({_reflection_identity(item) for item in first}))
        self.assertEqual(len(first), len({round(float(item["d_spacing"]), 6) for item in first}))
        self.assertTrue(all(20.0 <= float(item["two_theta"]) <= 80.0 for item in first))
        self.assertTrue(all(float(item["wavelength_angstrom"]) == 1.5406 for item in first))

    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_screening_simulation_does_not_call_refinement_or_write_reference_files(self):
        entry = next(
            item for item in load_reference_phase_snapshot().entries
            if item.candidate_identifier == "curated_nacl_rocksalt"
        )
        cif_path = (REFERENCE_PHASES_DIR / entry.relative_cif_path).resolve()
        before = sorted(path.name for path in REFERENCE_PHASES_DIR.iterdir())
        G2sc = configure_gsas()
        with patch.object(G2sc.G2Project, "refine", side_effect=AssertionError("screening should not refine")):
            reflections = _simulate_reflections_from_cif(
                cif_path,
                wavelength_angstrom=1.5406,
                two_theta_min=20.0,
                two_theta_max=80.0,
            )
        after = sorted(path.name for path in REFERENCE_PHASES_DIR.iterdir())
        self.assertTrue(reflections)
        self.assertEqual(before, after)


class CandidateConstructionTests(SimpleTestCase):
    def test_intended_structure_candidate_is_included_and_preserved(self):
        with (
            patch("catalog.xrd_analysis.candidates.configure_gsas", side_effect=AssertionError("unit test should not initialize GSAS-II")),
            patch("catalog.xrd_analysis.candidates.simulate_candidate_pattern", side_effect=_fake_candidate_simulation),
        ):
            result = build_ranked_phase_candidates(_analysis_input(), _parsed_pattern(), _quality_control())
        self.assertEqual(result.status, "candidate ranking ready for refinement")
        self.assertTrue(any(candidate.intended_structure_match for candidate in result.candidates))
        self.assertEqual(result.candidates[0].candidate_id, "curated_nacl_rocksalt")

    def test_missing_intended_structure_reference_emits_warning(self):
        result = build_ranked_phase_candidates(
            _analysis_input(
                nominal_composition="Co0.5Ni0.5O",
                elements=("Co", "Ni", "O"),
                stoichiometric_amounts=(("Co", 0.5), ("Ni", 0.5), ("O", 1.0)),
                structure_family="rocksalt",
                expected_space_group="Fm-3m (#225)",
            ),
            _parsed_pattern(),
            _quality_control(),
        )
        self.assertTrue(any(warning.code == "intended_structure_reference_missing" for warning in result.warnings))

    def test_human_candidate_filter_allows_subset_elements_and_excludes_unlisted_elements(self):
        good = RankedPhaseCandidate(
            candidate_id="subset",
            source="material_linked_structure",
            source_identifier="subset",
            source_snapshot=None,
            cif_path=str(REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"),
            cif_hash=None,
            formula="Na",
            normalized_composition=(StoichiometricAmount("Na", 1.0),),
            element_set=("Na",),
            space_group="P 1",
            structure_family="metal",
            intended_structure_match=False,
            chemical_compatibility_score=0.0,
            stoichiometric_similarity_score=0.5,
            synthesis_context_score=0.5,
            diffraction_pre_rank_score=0.0,
            combined_pre_rank_score=0.0,
            duplicate_cluster_id=None,
        )
        bad = replace(
            good,
            candidate_id="extra",
            formula="NaBr",
            normalized_composition=(StoichiometricAmount("Br", 1.0), StoichiometricAmount("Na", 1.0)),
            element_set=("Br", "Na"),
        )
        kept, warnings = _apply_chemistry_filter([good, bad], _analysis_input(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        self.assertEqual([candidate.candidate_id for candidate in kept], ["subset"])
        self.assertTrue(any(warning.code == "candidate_contains_unlisted_element" for warning in warnings))

    def test_stoichiometric_ranking_prefers_matching_formula_and_missing_stoich_is_neutral(self):
        snapshot = load_reference_phase_snapshot()
        result = build_ranked_phase_candidates(_analysis_input(), _parsed_pattern(), _quality_control(), reference_snapshot=snapshot)
        score_by_id = {candidate.candidate_id: candidate.stoichiometric_similarity_score for candidate in result.candidates}
        self.assertGreater(score_by_id["curated_nacl_rocksalt"], score_by_id["curated_hypothetical_nacl3"])

        neutral_input = _analysis_input(stoichiometric_amounts=())
        neutral_result = build_ranked_phase_candidates(neutral_input, _parsed_pattern(), _quality_control(), reference_snapshot=snapshot)
        neutral_scores = {candidate.candidate_id: candidate.stoichiometric_similarity_score for candidate in neutral_result.candidates}
        self.assertEqual(neutral_scores["curated_nacl_rocksalt"], 0.5)
        self.assertEqual(neutral_scores["curated_hypothetical_nacl3"], 0.5)

    def test_synthesis_context_scoring_is_deterministic_and_missing_context_is_neutral(self):
        candidate = RankedPhaseCandidate(
            candidate_id="test",
            source="curated_reference",
            source_identifier="test",
            source_snapshot="snapshot",
            cif_path=str(REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"),
            cif_hash=None,
            formula="NaCl",
            normalized_composition=(StoichiometricAmount("Cl", 1.0), StoichiometricAmount("Na", 1.0)),
            element_set=("Cl", "Na"),
            space_group="F m -3 m",
            structure_family="rocksalt",
            intended_structure_match=True,
            chemical_compatibility_score=0.0,
            stoichiometric_similarity_score=0.0,
            synthesis_context_score=0.0,
            diffraction_pre_rank_score=0.0,
            combined_pre_rank_score=0.0,
            duplicate_cluster_id=None,
        )
        neutral = _synthesis_context_score(candidate, _analysis_input(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        self.assertEqual(neutral, 0.5)
        self.assertEqual(
            neutral,
            _synthesis_context_score(candidate, _analysis_input(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG),
        )


class DuplicateAndRankingTests(SimpleTestCase):
    def test_exact_duplicate_and_formula_space_group_duplicates_cluster_deterministically(self):
        base = RankedPhaseCandidate(
            candidate_id="a",
            source="curated_reference",
            source_identifier="a",
            source_snapshot="snapshot",
            cif_path=str(REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"),
            cif_hash="same",
            formula="NaCl",
            normalized_composition=(StoichiometricAmount("Cl", 1.0), StoichiometricAmount("Na", 1.0)),
            element_set=("Cl", "Na"),
            space_group="F m -3 m",
            structure_family="rocksalt",
            intended_structure_match=False,
            chemical_compatibility_score=1.0,
            stoichiometric_similarity_score=1.0,
            synthesis_context_score=0.5,
            diffraction_pre_rank_score=0.5,
            combined_pre_rank_score=0.8,
            duplicate_cluster_id=None,
        )
        duplicate_hash = replace(base, candidate_id="b", combined_pre_rank_score=0.6)
        duplicate_formula = replace(base, candidate_id="c", cif_hash=None)
        clustered, warnings = _cluster_duplicate_candidates(
            [base, duplicate_hash, duplicate_formula],
            configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
        )
        self.assertEqual(len(clustered), 1)
        self.assertEqual(clustered[0].candidate_id, "a")
        self.assertEqual(clustered[0].alternate_sources, ("b", "c"))
        self.assertTrue(all(warning.code == "duplicate_candidate_removed" for warning in warnings))

    def test_duplicate_diversity_and_intended_preservation_in_top_k(self):
        candidates = []
        for index in range(8):
            candidates.append(
                RankedPhaseCandidate(
                    candidate_id=f"c{index}",
                    source="curated_reference",
                    source_identifier=f"c{index}",
                    source_snapshot="snapshot",
                    cif_path=str(REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"),
                    cif_hash=f"h{index}",
                    formula="NaCl",
                    normalized_composition=(StoichiometricAmount("Cl", 1.0), StoichiometricAmount("Na", 1.0)),
                    element_set=("Cl", "Na"),
                    space_group="F m -3 m",
                    structure_family="rocksalt",
                    intended_structure_match=index == 7,
                    chemical_compatibility_score=1.0,
                    stoichiometric_similarity_score=1.0,
                    synthesis_context_score=0.5,
                    diffraction_pre_rank_score=0.2 + index * 0.01,
                    combined_pre_rank_score=0.2 + index * 0.01,
                    duplicate_cluster_id=f"g{index}",
                )
            )
        top = _select_top_candidates(candidates, configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        self.assertLessEqual(len(top), DEFAULT_XRD_ANALYSIS_CONFIG.candidate_ranking.final_top_k)
        self.assertTrue(any(candidate.intended_structure_match for candidate in top))

    def test_combined_ranking_is_deterministic(self):
        candidate = RankedPhaseCandidate(
            candidate_id="soft",
            source="curated_reference",
            source_identifier="soft",
            source_snapshot="snapshot",
            cif_path=str(REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif"),
            cif_hash="h",
            formula="NaCl",
            normalized_composition=(StoichiometricAmount("Cl", 1.0), StoichiometricAmount("Na", 1.0)),
            element_set=("Cl", "Na"),
            space_group="F m -3 m",
            structure_family="rocksalt",
            intended_structure_match=True,
            chemical_compatibility_score=0.0,
            stoichiometric_similarity_score=0.0,
            synthesis_context_score=0.0,
            diffraction_pre_rank_score=0.0,
            combined_pre_rank_score=0.0,
            duplicate_cluster_id=None,
        )
        scored_one = _with_soft_scores(candidate, _analysis_input(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        scored_two = _with_soft_scores(candidate, _analysis_input(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        self.assertEqual(scored_one.combined_pre_rank_score, scored_two.combined_pre_rank_score)


class SimulationAndMetricsTests(SimpleTestCase):
    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_simulation_of_simple_local_reference_phase(self):
        snapshot = load_reference_phase_snapshot()
        entry = snapshot.entries[0]
        candidate = RankedPhaseCandidate(
            candidate_id=entry.candidate_identifier,
            source="curated_reference",
            source_identifier=entry.source_identifier or entry.candidate_identifier,
            source_snapshot=snapshot.snapshot_hash,
            cif_path=str((REFERENCE_PHASES_DIR / entry.relative_cif_path).resolve()),
            cif_hash=entry.sha256,
            formula=entry.formula,
            normalized_composition=(StoichiometricAmount("Cl", 1.0), StoichiometricAmount("Na", 1.0)),
            element_set=entry.element_set,
            space_group=entry.space_group,
            structure_family=entry.structure_family,
            intended_structure_match=True,
            chemical_compatibility_score=1.0,
            stoichiometric_similarity_score=1.0,
            synthesis_context_score=0.5,
            diffraction_pre_rank_score=0.0,
            combined_pre_rank_score=0.0,
            duplicate_cluster_id=None,
        )
        simulation = simulate_candidate_pattern(candidate, _analysis_input(), _parsed_pattern())
        self.assertTrue(simulation.reflection_positions_two_theta)
        self.assertAlmostEqual(simulation.reflection_positions_two_theta[0], 27.364401, delta=POSITION_TOLERANCE_DEGREES)

    def test_simulation_failure_is_isolated_from_other_candidates(self):
        bad_link = LinkedStructureReference(
            reference_id="bad",
            source_kind="material",
            cif_path="/tmp/does-not-exist.cif",
            formula="NaCl",
        )
        def _simulation_side_effect(candidate, analysis_input, parsed_pattern, *, configuration=DEFAULT_XRD_ANALYSIS_CONFIG):
            if candidate.candidate_id == "material:bad":
                raise CandidateSimulationError("candidate CIF is unreadable")
            return _fake_candidate_simulation(
                candidate,
                analysis_input,
                parsed_pattern,
                configuration=configuration,
            )

        with (
            patch("catalog.xrd_analysis.candidates.configure_gsas", side_effect=AssertionError("unit test should not initialize GSAS-II")),
            patch("catalog.xrd_analysis.candidates.simulate_candidate_pattern", side_effect=_simulation_side_effect),
        ):
            result = build_ranked_phase_candidates(
                _analysis_input(linked_structure_references=(bad_link,)),
                _parsed_pattern(),
                _quality_control(),
            )
        self.assertEqual(result.status, "candidate ranking ready for refinement")
        self.assertTrue(any(warning.code == "candidate_cif_unreadable" for warning in result.warnings))

    def test_diffraction_metrics_reward_coverage_and_penalize_absent_and_unexplained_regions(self):
        simulation = CandidateSimulation(
            candidate_id="test",
            reflection_positions_two_theta=(27.4, 31.7, 45.4, 56.5, 66.2),
            reflection_relative_intensities=(1.0, 0.8, 0.7, 0.5, 0.4),
            measured_coordinate_min=26.8,
            measured_coordinate_max=66.3,
            wavelength_angstrom=1.5406,
            settings_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        metrics = _diffraction_metrics(_parsed_pattern(), simulation, _quality_control(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        self.assertGreater(metrics["coverage"], 0.7)
        self.assertLess(metrics["absent_strong_peak_penalty"], 0.5)
        self.assertLess(metrics["unexplained_region_penalty"], 0.4)

    def test_limited_shift_handling_is_deterministic(self):
        shifted = CandidateSimulation(
            candidate_id="shifted",
            reflection_positions_two_theta=(27.62, 31.92, 45.62, 56.72, 66.42),
            reflection_relative_intensities=(1.0, 0.8, 0.7, 0.5, 0.4),
            measured_coordinate_min=26.8,
            measured_coordinate_max=66.3,
            wavelength_angstrom=1.5406,
            settings_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        )
        metrics = _diffraction_metrics(_parsed_pattern(), shifted, _quality_control(), configuration=DEFAULT_XRD_ANALYSIS_CONFIG)
        self.assertLessEqual(abs(metrics["best_shift"]), DEFAULT_XRD_ANALYSIS_CONFIG.candidate_ranking.maximum_allowed_screening_shift_degrees)
        self.assertGreater(metrics["coverage"], 0.5)


class CandidateStageFailureTests(SimpleTestCase):
    def test_no_candidate_stage_failure(self):
        empty_snapshot = ReferencePhaseSnapshot(
            snapshot_version="empty",
            snapshot_hash="empty",
            manifest_path=str(REFERENCE_MANIFEST_PATH),
            entries=(),
        )
        result = build_ranked_phase_candidates(_analysis_input(), _parsed_pattern(), _quality_control(), reference_snapshot=empty_snapshot)
        self.assertEqual(result.status, "candidate generation failed")
        self.assertEqual(result.failure_reason, "no_candidate_sources_available")

    def test_no_simulatable_candidate_stage_failure(self):
        bad_link = LinkedStructureReference(
            reference_id="bad",
            source_kind="material",
            cif_path="/tmp/missing.cif",
            formula="NaCl",
        )
        empty_snapshot = ReferencePhaseSnapshot(
            snapshot_version="empty",
            snapshot_hash="empty",
            manifest_path=str(REFERENCE_MANIFEST_PATH),
            entries=(),
        )
        result = build_ranked_phase_candidates(
            _analysis_input(linked_structure_references=(bad_link,)),
            _parsed_pattern(),
            _quality_control(),
            reference_snapshot=empty_snapshot,
        )
        self.assertEqual(result.status, "candidate generation failed")
        self.assertEqual(result.failure_reason, "no_simulatable_candidates")

    def test_candidate_generation_result_is_json_safe(self):
        with (
            patch("catalog.xrd_analysis.candidates.configure_gsas", side_effect=AssertionError("unit test should not initialize GSAS-II")),
            patch("catalog.xrd_analysis.candidates.simulate_candidate_pattern", side_effect=_fake_candidate_simulation),
        ):
            result = build_ranked_phase_candidates(_analysis_input(), _parsed_pattern(), _quality_control())
        loaded = json.loads(dumps_canonical_json(result))
        self.assertEqual(loaded["status"], "candidate ranking ready for refinement")
        self.assertTrue(loaded["candidates"])

    def test_disabled_manifest_entry_is_ignored_by_hash_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = os.path.join(tmpdir, "manifest.json")
            payload = {
                "snapshot_version": "disabled-test",
                "entries": [
                    {
                        "candidate_identifier": "disabled_bad",
                        "relative_cif_path": "missing.cif",
                        "sha256": "0" * 64,
                        "formula": "NaCl",
                        "element_set": ["Cl", "Na"],
                        "space_group": "F m -3 m",
                        "structure_family": "rocksalt",
                        "source": "curated_reference",
                        "source_identifier": "disabled_bad",
                        "enabled": False,
                    }
                ],
            }
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            snapshot = load_reference_phase_snapshot(manifest_path, validate_hashes=False)
            validate_reference_phase_snapshot(snapshot)


class PipelineIntegrationTests(SimpleTestCase):
    def test_pipeline_continues_to_final_decision_without_triggering_cod_helpers(self):
        parsed = _parsed_pattern()
        qc = _quality_control()
        with (
            patch("catalog.xrd_analysis.pipeline.parse_and_qc_input_pattern", return_value=(parsed, qc)),
            patch(
                "catalog.xrd_analysis.pipeline.build_ranked_phase_candidates",
                return_value=CandidateGenerationResult(
                    status="candidate ranking ready for refinement",
                    failure_reason=None,
                    warnings=(),
                    reference_snapshot=None,
                    candidates=(),
                    algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
                    configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                ),
            ),
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
                    provenance=_analysis_input().provenance,
                ),
            ) as decision_runner,
            patch("catalog.rietveld_refinement.derive_structures_from_xrd") as derive_mock,
            patch("catalog.rietveld_refinement.search_cod_by_indexing_or_refine") as cod_mock,
        ):
            result = run_xrd_analysis_pipeline(_analysis_input())
        self.assertIsInstance(result, XRDAnalysisResult)
        self.assertEqual(result.phase_state, "unresolved")
        decision_runner.assert_called_once()
        derive_mock.assert_not_called()
        cod_mock.assert_not_called()
