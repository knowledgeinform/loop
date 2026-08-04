from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from catalog.xrd_analysis.candidates import REFERENCE_PHASES_DIR
from catalog.xrd_analysis.persistence import (
    build_analysis_identity_payload,
    compute_analysis_identity,
    persist_xrd_analysis_result,
    run_and_persist_xrd_analysis,
    validate_persisted_xrd_analysis,
    XRDAnalysisPersistenceError,
)
from catalog.xrd_analysis.reporting import sha256_digest, sha256_file
from catalog.xrd_analysis.schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CandidateSimulation,
    ExpectedReflectionRecord,
    InstrumentProfile,
    MetadataItem,
    ParsedPatternMetadata,
    PatternProvenance,
    PatternQualityControlResult,
    PhaseHypothesis,
    RankedPhaseCandidate,
    RawFileReference,
    RefinedLatticeParameters,
    ReferencePhaseSnapshot,
    SelectedBestModel,
    SinglePhaseHypothesisResult,
    StoichiometricAmount,
    SynthesisContext,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
    XRDAnalysisResult,
)


def _reference_snapshot() -> ReferencePhaseSnapshot:
    return ReferencePhaseSnapshot(
        snapshot_version="reference-phases-v1",
        snapshot_hash="snapshot-hash-v1",
        manifest_path=str((REFERENCE_PHASES_DIR / "manifest.json").resolve()),
        entries=(),
    )


def _instrument_profile(instprm_path: str | None) -> InstrumentProfile:
    return InstrumentProfile(
        instrument_label="CuKa lab data",
        instrument_parameter_path=instprm_path,
        geometry="Bragg-Brentano",
        sample_holder="flat plate",
        metadata_items=(
            MetadataItem(key="divergence_slit", value="0.5"),
            MetadataItem(key="detector", value="1D"),
        ),
    )


def _analysis_input(*, instprm_path: str | None = None) -> XRDAnalysisInput:
    return XRDAnalysisInput(
        material_auid="M:test",
        recipe_auid="M:test:R:test",
        trial_id="T1",
        raw_file_hash="ab" * 32,
        raw_file_reference=RawFileReference(
            reference_kind="stored_path",
            locator="/tmp/test_scan.csv",
            original_filename="test_scan.csv",
        ),
        nominal_composition="NaCl",
        elements=("Na", "Cl"),
        stoichiometric_amounts=(
            StoichiometricAmount("Na", 1.0),
            StoichiometricAmount("Cl", 1.0),
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
        step_size=0.02,
        scan_speed=1.0,
        instrument_profile=_instrument_profile(instprm_path),
        synthesis_context=SynthesisContext(
            ordered_steps=(),
            precursor_records=(),
            temperatures_c=(),
            ramp_rates_c_min=(),
            hold_times_hours=(),
            atmospheres=("air",),
            furnace_types=("box",),
            preparation_notes=("pressed pellet",),
        ),
        warnings=(),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        provenance=XRDAnalysisProvenance(
            source_material={"display_name": "NaCl sample"},
            source_recipe={"operator": "alice"},
            source_trial={"phase_status": "human-entered-label"},
            source_raw_file={"stored_path": "/tmp/test_scan.csv"},
            measurement_metadata=(),
        ),
    )


def _candidate() -> RankedPhaseCandidate:
    cif_path = (REFERENCE_PHASES_DIR / "NaCl_rocksalt.cif").resolve()
    return RankedPhaseCandidate(
        candidate_id="curated_nacl_rocksalt",
        source="curated_reference",
        source_identifier="local:NaCl_rocksalt",
        source_snapshot="reference-phases-v1",
        cif_path=str(cif_path),
        cif_hash=sha256_file(cif_path),
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
            reflection_relative_intensities=(1.0, 0.8, 0.65),
            measured_coordinate_min=20.0,
            measured_coordinate_max=80.0,
            wavelength_angstrom=1.5406,
            settings_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        ),
    )


def _parsed_pattern() -> ParsedPatternMetadata:
    return ParsedPatternMetadata(
        parser_type="dataframe",
        pattern_type="continuous",
        original_coordinate_type="two_theta",
        coordinate_column="Angle",
        intensity_column="Intensity",
        original_coordinates=(27.4, 31.7, 45.5),
        original_intensities=(120.0, 90.0, 70.0),
        normalized_two_theta=(27.4, 31.7, 45.5),
        normalized_d_spacing=(3.25, 2.82, 1.99),
        normalized_q=(1.93, 2.23, 3.15),
        normalized_intensities=(120.0, 90.0, 70.0),
        wavelength_angstrom=1.5406,
        usable_point_count=3,
        coordinate_order="ascending",
        median_step_size=4.15,
        step_size_variation=0.0,
        provenance=PatternProvenance(parser_type="dataframe", source_label="unit_test"),
    )


def _quality_control() -> PatternQualityControlResult:
    return PatternQualityControlResult(
        status="pattern accepted for later analysis",
        pattern_type="continuous",
        usable_point_count=3,
        coordinate_min=27.4,
        coordinate_max=45.5,
        range_width=18.1,
        median_step_size=4.15,
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


def _single_phase_hypothesis(candidate: RankedPhaseCandidate) -> SinglePhaseHypothesisResult:
    return SinglePhaseHypothesisResult(
        hypothesis_id="single-hypothesis-1",
        candidate_id=candidate.candidate_id,
        candidate_source=candidate.source,
        candidate_source_identifier=candidate.source_identifier,
        candidate_cif_hash=candidate.cif_hash,
        reference_snapshot="snapshot-hash-v1",
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        gsasii_version="GSAS-II-test",
        instrument_profile_serialization='{"geometry":"Bragg-Brentano"}',
        screening_pre_rank_score=candidate.combined_pre_rank_score,
        refinement_status="completed",
        convergence_status="converged",
        completed_stages=(),
        failed_stage=None,
        runtime_seconds=0.1,
        warnings=(),
        failure_codes=(),
        exception_summary=None,
        rwp=6.5,
        rp=5.9,
        goodness_of_fit=1.1,
        chi_squared=1.0,
        weighted_residual=0.5,
        observation_count=3,
        refined_parameter_count=2,
        degrees_of_freedom=1,
        phase_scale_factor=1.0,
        refined_lattice_parameters=RefinedLatticeParameters(
            length_a=5.64,
            length_b=5.64,
            length_c=5.64,
            angle_alpha=90.0,
            angle_beta=90.0,
            angle_gamma=90.0,
            volume=179.4,
        ),
        zero_shift=0.001,
        sample_displacement=None,
        refined_profile_terms={"U": 0.01},
        observed_two_theta=(27.4, 31.7, 45.5),
        observed_intensities=(120.0, 90.0, 70.0),
        calculated_total_pattern=(118.0, 88.0, 68.0),
        calculated_background=(10.0, 10.0, 10.0),
        difference_pattern=(2.0, 2.0, 2.0),
        expected_reflections=(
            ExpectedReflectionRecord(
                h=1,
                k=1,
                l=1,
                multiplicity=8,
                d_spacing=3.25,
                two_theta=27.4,
                predicted_intensity=100.0,
            ),
        ),
        significant_positive_residual_regions=(),
        unsupported_strong_predicted_regions=(),
        raw_residual_metrics={"wrss": 1.23},
        provenance=("unit_test_refinement",),
    )


def _analysis_result() -> XRDAnalysisResult:
    candidate = _candidate()
    hypothesis = _single_phase_hypothesis(candidate)
    return XRDAnalysisResult(
        phase_state="likely single-phase",
        warnings=(),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        best_hypothesis=PhaseHypothesis(
            hypothesis_id=hypothesis.hypothesis_id,
            crystalline_phase_count=1,
            candidate_ids=(candidate.candidate_id,),
            description="Best one-phase fit",
            evidence_score=0.85,
            provenance={"selected_model": "single_phase"},
        ),
        alternative_hypotheses=(),
        evidence_score=0.85,
        provenance=XRDAnalysisProvenance(
            source_material={"id": "M:test"},
            source_recipe={"id": "M:test:R:test"},
            source_trial={"trial_id": "T1"},
            source_raw_file={"file_hash": "ab" * 32},
            measurement_metadata=(),
        ),
        parsed_pattern=_parsed_pattern(),
        quality_control=_quality_control(),
        phase_candidates=(candidate,),
        ranked_candidate_shortlist=(candidate,),
        successful_single_phase_hypotheses=(hypothesis,),
        failed_single_phase_attempts=(),
        successful_two_phase_hypotheses=(),
        failed_two_phase_attempts=(),
        best_single_phase_hypothesis=hypothesis,
        best_two_phase_hypothesis=None,
        selected_best_model=SelectedBestModel(
            model_type="single_phase",
            hypothesis_id=hypothesis.hypothesis_id,
            candidate_ids=(candidate.candidate_id,),
            selection_reason="best_penalized_fit",
        ),
        model_comparison=None,
        decision_criteria=None,
        stability_results=None,
        evidence_components=None,
        failure_codes=(),
        analysis_provenance_notes=("unit_test_analysis",),
    )


class AnalysisIdentityTests(SimpleTestCase):
    def test_analysis_id_ignores_display_only_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            instprm = Path(tmp) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            reference_snapshot = {
                "version": "reference-phases-v1",
                "hash": "snapshot-hash-v1",
            }
            payload = build_analysis_identity_payload(
                analysis_input,
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot=reference_snapshot,
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(
                    DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                ),
            )
            changed_provenance_input = replace(
                analysis_input,
                provenance=replace(
                    analysis_input.provenance,
                    source_trial={
                        "phase_status": "changed human label",
                        "experimenter": "bob",
                    },
                ),
            )
            changed_payload = build_analysis_identity_payload(
                changed_provenance_input,
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot=reference_snapshot,
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(
                    DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
                ),
            )
            self.assertEqual(sha256_digest(payload), sha256_digest(changed_payload))

    def test_analysis_id_changes_when_wavelength_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instprm = Path(tmp) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            payload = build_analysis_identity_payload(
                analysis_input,
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
            )
            changed = replace(analysis_input, wavelength_angstrom=1.5418)
            changed_payload = build_analysis_identity_payload(
                changed,
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
            )
            self.assertNotEqual(sha256_digest(payload), sha256_digest(changed_payload))

    def test_analysis_id_changes_when_instrument_profile_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            instprm_a = Path(tmp) / "instrument_a.instprm"
            instprm_b = Path(tmp) / "instrument_b.instprm"
            instprm_a.write_text("INST PROFILE A\n", encoding="utf-8")
            instprm_b.write_text("INST PROFILE B\n", encoding="utf-8")
            payload_a = build_analysis_identity_payload(
                _analysis_input(instprm_path=str(instprm_a)),
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
            )
            payload_b = build_analysis_identity_payload(
                _analysis_input(instprm_path=str(instprm_b)),
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
            )
            self.assertNotEqual(sha256_digest(payload_a), sha256_digest(payload_b))

    def test_analysis_id_normalizes_unordered_element_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            instprm = Path(tmp) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            changed = replace(
                analysis_input,
                elements=("Cl", "Na"),
                stoichiometric_amounts=(
                    StoichiometricAmount("Cl", 1.0),
                    StoichiometricAmount("Na", 1.0),
                ),
            )
            payload = build_analysis_identity_payload(
                analysis_input,
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
            )
            changed_payload = build_analysis_identity_payload(
                changed,
                configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                linked_structure_snapshots=(),
                gsasii_version="GSAS-II-test",
                candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
            )
            self.assertEqual(sha256_digest(payload), sha256_digest(changed_payload))

    def test_analysis_id_is_stable_across_media_root_locations_for_relative_raw_reference(self):
        relative_locator = "xrd/M:test/R:test/T1/raw.csv"
        analysis_input = replace(
            _analysis_input(instprm_path=None),
            raw_file_reference=RawFileReference(
                reference_kind="raw_db_path",
                locator=relative_locator,
                original_filename="raw.csv",
            ),
            provenance=replace(
                _analysis_input(instprm_path=None).provenance,
                source_raw_file={"stored_path": relative_locator},
            ),
        )
        with tempfile.TemporaryDirectory() as media_a, tempfile.TemporaryDirectory() as media_b:
            with override_settings(MEDIA_ROOT=media_a, MEDIA_URL="/media/"):
                payload_a = build_analysis_identity_payload(
                    analysis_input,
                    configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                    configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                    reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                    linked_structure_snapshots=(),
                    gsasii_version="GSAS-II-test",
                    candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
                )
            with override_settings(MEDIA_ROOT=media_b, MEDIA_URL="/media/"):
                payload_b = build_analysis_identity_payload(
                    analysis_input,
                    configuration=DEFAULT_XRD_ANALYSIS_CONFIG,
                    configuration_hash=DEFAULT_XRD_ANALYSIS_CONFIG.content_hash(),
                    reference_snapshot={"version": "reference-phases-v1", "hash": "snapshot-hash-v1"},
                    linked_structure_snapshots=(),
                    gsasii_version="GSAS-II-test",
                    candidate_simulation_versions=(DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,),
                )
        self.assertEqual(sha256_digest(payload_a), sha256_digest(payload_b))


class PersistenceTests(SimpleTestCase):
    def _rawfile_stub(self, archive_folder: str):
        row = type("RawFileRow", (), {"archive_folder": archive_folder})()

        class Query:
            def first(self_inner):
                return row

        class RawFileStub:
            @staticmethod
            def objects(**kwargs):
                del kwargs
                return Query()

        return RawFileStub

    def test_persist_writes_expected_artifacts_summary_and_archive(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            result = _analysis_result()
            recorded_calls = []

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch(
                    "catalog.xrd_analysis.persistence.raw_db.record_derived_file",
                    side_effect=lambda **kwargs: recorded_calls.append(kwargs),
                ):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                persisted = persist_xrd_analysis_result(
                                    analysis_input,
                                    result,
                                    started_at=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
                                    completed_at=datetime(2026, 7, 27, 12, 5, tzinfo=timezone.utc),
                                )

            analysis_root = Path(persisted.analysis_directory)
            self.assertTrue((analysis_root / "result.json").is_file())
            self.assertTrue((analysis_root / "reproducibility.json").is_file())
            self.assertTrue((analysis_root / "candidates.json").is_file())
            self.assertTrue((analysis_root / "single_phase_hypotheses.json").is_file())
            self.assertTrue((analysis_root / "two_phase_hypotheses.json").is_file())
            self.assertTrue((analysis_root / "model_comparison.json").is_file())
            self.assertTrue((analysis_root / "pattern_observed.csv").is_file())
            self.assertTrue((analysis_root / "pattern_best_model.csv").is_file())
            self.assertTrue((analysis_root / "reflections.json").is_file())
            self.assertTrue((analysis_root / "selected_cifs.json").is_file())
            self.assertTrue((analysis_root / "selected_cifs" / "01_curated_nacl_rocksalt.cif").is_file())

            trial_root = Path(media) / "xrd" / "M:test" / "R:test" / "T1"
            analyses_index = json.loads((trial_root / "analyses" / "index.json").read_text(encoding="utf-8"))
            trial_index = json.loads((trial_root / "index.json").read_text(encoding="utf-8"))
            self.assertIn(persisted.analysis_id, analyses_index)
            self.assertIn(persisted.analysis_id, trial_index["analyses"])

            reproducibility_payload = json.loads(
                (analysis_root / "reproducibility.json").read_text(encoding="utf-8")
            )
            self.assertEqual(reproducibility_payload["analysis_id"], persisted.analysis_id)
            self.assertEqual(reproducibility_payload["result_hash"], sha256_file(analysis_root / "result.json"))
            self.assertTrue(reproducibility_payload["created_artifacts"])
            self.assertEqual(
                trial_index["analyses"][persisted.analysis_id]["result_manifest_path"],
                f"xrd/M:test/R:test/T1/analyses/{persisted.analysis_id}/reproducibility.json",
            )

            archived_result = (
                Path(archive)
                / "trial-archive"
                / "analyses"
                / persisted.analysis_id
                / "result.json"
            )
            self.assertTrue(archived_result.is_file())

            self.assertFalse(persisted.reused_existing)
            self.assertEqual(
                sorted(call["artifact_type"] for call in recorded_calls),
                sorted(artifact.artifact_type for artifact in persisted.persisted_artifacts),
            )
            self.assertTrue(all(call["analysis_id"] == persisted.analysis_id for call in recorded_calls))

    def test_repeat_persist_reuses_same_analysis_id(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            result = _analysis_result()

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch("catalog.xrd_analysis.persistence.raw_db.record_derived_file"):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                first = persist_xrd_analysis_result(analysis_input, result)
                                second = persist_xrd_analysis_result(analysis_input, result)

            self.assertEqual(first.analysis_id, second.analysis_id)
            self.assertEqual(first.analysis_directory, second.analysis_directory)
            self.assertFalse(first.reused_existing)
            self.assertTrue(second.reused_existing)

    def test_persist_honors_established_worker_analysis_id_and_validates_immediately(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            result = _analysis_result()

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch("catalog.xrd_analysis.persistence.raw_db.record_derived_file"):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                (
                                    analysis_id,
                                    identity_payload,
                                    reference_snapshot,
                                    linked_snapshots,
                                    gsasii_version,
                                    candidate_versions,
                                ) = compute_analysis_identity(analysis_input)
                                persisted = persist_xrd_analysis_result(
                                    analysis_input,
                                    result,
                                    analysis_id=analysis_id,
                                    identity_payload=identity_payload,
                                    reference_snapshot=reference_snapshot,
                                    linked_structure_snapshots=linked_snapshots,
                                    gsasii_version=gsasii_version,
                                    candidate_simulation_versions=candidate_versions,
                                )
                                validation = validate_persisted_xrd_analysis(
                                    analysis_input,
                                    analysis_id=analysis_id,
                                    reference_snapshot=reference_snapshot,
                                    linked_structure_snapshots=linked_snapshots,
                                    gsasii_version=gsasii_version,
                                    candidate_simulation_versions=candidate_versions,
                                )

            self.assertEqual(persisted.analysis_id, analysis_id)
            self.assertTrue(persisted.analysis_directory.endswith(analysis_id))
            self.assertTrue(validation.valid)

    def test_persist_rejects_identity_payload_mismatch_before_writing_artifacts(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            result = _analysis_result()

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch("catalog.xrd_analysis.persistence.raw_db.record_derived_file"):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                (
                                    analysis_id,
                                    identity_payload,
                                    reference_snapshot,
                                    linked_snapshots,
                                    gsasii_version,
                                    candidate_versions,
                                ) = compute_analysis_identity(analysis_input)
                                mismatched_payload = dict(identity_payload)
                                mismatched_payload["candidate_simulation_versions"] = []
                                with self.assertRaises(XRDAnalysisPersistenceError) as exc_info:
                                    persist_xrd_analysis_result(
                                        analysis_input,
                                        result,
                                        analysis_id=analysis_id,
                                        identity_payload=mismatched_payload,
                                        reference_snapshot=reference_snapshot,
                                        linked_structure_snapshots=linked_snapshots,
                                        gsasii_version=gsasii_version,
                                        candidate_simulation_versions=candidate_versions,
                                    )

            self.assertEqual(exc_info.exception.code, "analysis_identity_mismatch")
            analyses_root = Path(media) / "xrd" / "M:test" / "R:test" / "T1" / "analyses"
            self.assertFalse(analyses_root.exists())

    def test_missing_instrument_profile_still_persists_under_established_id(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            analysis_input = replace(_analysis_input(instprm_path=None), instrument_profile=None)
            result = _analysis_result()

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch("catalog.xrd_analysis.persistence.raw_db.record_derived_file"):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                (
                                    analysis_id,
                                    identity_payload,
                                    reference_snapshot,
                                    linked_snapshots,
                                    gsasii_version,
                                    candidate_versions,
                                ) = compute_analysis_identity(analysis_input)
                                persisted = persist_xrd_analysis_result(
                                    analysis_input,
                                    result,
                                    analysis_id=analysis_id,
                                    identity_payload=identity_payload,
                                    reference_snapshot=reference_snapshot,
                                    linked_structure_snapshots=linked_snapshots,
                                    gsasii_version=gsasii_version,
                                    candidate_simulation_versions=candidate_versions,
                                )

            self.assertEqual(persisted.analysis_id, analysis_id)
            self.assertTrue(Path(persisted.analysis_directory, "result.json").is_file())

    def test_validate_detects_incomplete_or_corrupted_cache(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            result = _analysis_result()

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch("catalog.xrd_analysis.persistence.raw_db.record_derived_file"):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                persisted = persist_xrd_analysis_result(analysis_input, result)
                                analysis_root = Path(persisted.analysis_directory)
                                (analysis_root / "candidates.json").unlink()
                                validation = validate_persisted_xrd_analysis(analysis_input)
                                self.assertFalse(validation.valid)
                                self.assertEqual(validation.failure_code, "analysis_cache_incomplete")

                                persist_xrd_analysis_result(analysis_input, result)
                                analysis_root = Path(media) / "xrd" / "M:test" / "R:test" / "T1" / "analyses" / persisted.analysis_id
                                (analysis_root / "result.json").write_text('{"corrupt":true}', encoding="utf-8")
                                validation = validate_persisted_xrd_analysis(analysis_input)
                                self.assertFalse(validation.valid)
                                self.assertEqual(validation.failure_code, "analysis_cache_hash_mismatch")

    def test_run_and_persist_uses_valid_cache_without_rerunning_pipeline(self):
        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _analysis_input(instprm_path=str(instprm))
            result = _analysis_result()

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                with patch("catalog.xrd_analysis.persistence.raw_db.record_derived_file"):
                    with patch(
                        "catalog.xrd_analysis.persistence.raw_db.RawFile",
                        self._rawfile_stub("trial-archive"),
                    ):
                        with patch(
                            "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                            return_value=_reference_snapshot(),
                        ):
                            with patch(
                                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                                return_value="GSAS-II-test",
                            ):
                                persist_xrd_analysis_result(analysis_input, result)
                                with patch(
                                    "catalog.xrd_analysis.pipeline.run_xrd_analysis_pipeline",
                                    side_effect=AssertionError("pipeline should not rerun"),
                                ):
                                    loaded = run_and_persist_xrd_analysis(analysis_input)

            self.assertTrue(loaded.reused_existing)
            self.assertEqual(loaded.analysis_id, json.loads(Path(loaded.analysis_directory, "result.json").read_text(encoding="utf-8"))["analysis_id"])


class PipelinePersistenceWrapperTests(SimpleTestCase):
    def test_pipeline_wrapper_delegates_to_persistence_boundary(self):
        from catalog.xrd_analysis import pipeline

        analysis_input = _analysis_input(instprm_path=None)
        sentinel = object()
        with patch(
            "catalog.xrd_analysis.persistence.run_and_persist_xrd_analysis",
            return_value=sentinel,
        ) as delegated:
            returned = pipeline.run_and_persist_xrd_analysis(analysis_input)

        delegated.assert_called_once()
        self.assertIs(returned, sentinel)
