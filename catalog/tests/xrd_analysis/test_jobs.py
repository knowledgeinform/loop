from __future__ import annotations

from dataclasses import replace
from dataclasses import dataclass
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from catalog.xrd_analysis.schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    RawFileReference,
    XRDAnalysisInput,
    XRDAnalysisProvenance,
)


def _analysis_input() -> XRDAnalysisInput:
    return XRDAnalysisInput(
        material_auid="M:test",
        recipe_auid="M:test:R:test",
        trial_id="T1",
        raw_file_hash="ab" * 32,
        raw_file_reference=RawFileReference(
            reference_kind="stored_path",
            locator="/tmp/raw.csv",
            original_filename="raw.csv",
        ),
        nominal_composition="NaCl",
        elements=("Cl", "Na"),
        stoichiometric_amounts=(),
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
        instrument_profile=None,
        synthesis_context=None,
        warnings=(),
        algorithm_version=DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version,
        configuration_version=DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,
        provenance=XRDAnalysisProvenance(
            source_material={},
            source_recipe={},
            source_trial={},
            source_raw_file={},
            measurement_metadata=(),
        ),
    )


@dataclass
class _FakeContext:
    material: object
    recipe: object
    trial: object
    raw_file: object | None
    raw_file_path: str
    analysis_input: XRDAnalysisInput


class _FakeJob:
    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", "job-1")
        self.analysis_id = kwargs.pop("analysis_id", "analysis-1")
        self.recipe_auid = kwargs.pop("recipe_auid", "M:test:R:test")
        self.trial_id = kwargs.pop("trial_id", "T1")
        self.status = kwargs.pop("status", "queued")
        self.cache_hit = kwargs.pop("cache_hit", False)
        self.progress_stage = kwargs.pop("progress_stage", "queued")
        self.progress_message = kwargs.pop("progress_message", "Queued")
        self.attempt_count = kwargs.pop("attempt_count", 0)
        self.maximum_attempts = kwargs.pop("maximum_attempts", 3)
        self.warnings = kwargs.pop("warnings", [])
        self.failure_codes = kwargs.pop("failure_codes", [])
        self.error_summary = kwargs.pop("error_summary", None)
        self.completed_at = kwargs.pop("completed_at", None)
        self.created_at = kwargs.pop("created_at", None)
        self.queued_at = kwargs.pop("queued_at", None)
        self.started_at = kwargs.pop("started_at", None)
        self.last_heartbeat_at = kwargs.pop("last_heartbeat_at", None)
        self.worker_identifier = kwargs.pop("worker_identifier", None)
        self.result_manifest_relative_path = kwargs.pop("result_manifest_relative_path", None)
        self.automated_summary = kwargs.pop("automated_summary", {})
        self.diagnostic_metadata = kwargs.pop("diagnostic_metadata", {})
        self.raw_file_hash = kwargs.pop("raw_file_hash", "ab" * 32)
        self.save_calls = 0
        for key, value in kwargs.items():
            setattr(self, key, value)

    def save(self):
        self.save_calls += 1
        return self


class XRDJobSubmissionTests(SimpleTestCase):
    def test_submission_computes_identity_before_queueing(self):
        from catalog.xrd_analysis import worker

        context = _FakeContext(
            material=object(),
            recipe=object(),
            trial=object(),
            raw_file=None,
            raw_file_path="/tmp/raw.csv",
            analysis_input=_analysis_input(),
        )
        fake_job = _FakeJob(analysis_id="analysis-123")
        cache_validation = SimpleNamespace(valid=False)
        manager = mock.Mock()
        manager.first.return_value = None

        with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context), mock.patch.object(
            worker,
            "compute_analysis_identity",
            return_value=("analysis-123", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
        ) as compute_mock, mock.patch.object(
            worker, "validate_persisted_xrd_analysis", return_value=cache_validation
        ), mock.patch.object(worker.XRDAnalysisJob, "objects", return_value=manager), mock.patch.object(
            worker, "_create_new_job", return_value=fake_job
        ) as create_mock:
            submission = worker.submit_xrd_analysis_job("M:test:R:test", "T1")

        self.assertEqual(submission.analysis_id, "analysis-123")
        compute_mock.assert_called_once()
        create_mock.assert_called_once()

    def test_submission_reuses_active_job(self):
        from catalog.xrd_analysis import worker

        context = _FakeContext(
            material=object(),
            recipe=object(),
            trial=object(),
            raw_file=None,
            raw_file_path="/tmp/raw.csv",
            analysis_input=_analysis_input(),
        )
        existing = _FakeJob(status="running", progress_stage="quality_control")
        manager = mock.Mock()
        manager.first.return_value = existing

        with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context), mock.patch.object(
            worker,
            "compute_analysis_identity",
            return_value=("analysis-1", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
        ), mock.patch.object(
            worker, "validate_persisted_xrd_analysis", return_value=SimpleNamespace(valid=False)
        ), mock.patch.object(worker.XRDAnalysisJob, "objects", return_value=manager):
            submission = worker.submit_xrd_analysis_job("M:test:R:test", "T1")

        self.assertTrue(submission.reused_active_job)
        self.assertEqual(submission.status, "running")
        self.assertEqual(submission.job, existing)

    def test_submission_rejects_retry_exhausted_failed_job(self):
        from catalog.xrd_analysis import worker

        context = _FakeContext(
            material=object(),
            recipe=object(),
            trial=object(),
            raw_file=None,
            raw_file_path="/tmp/raw.csv",
            analysis_input=_analysis_input(),
        )
        existing = _FakeJob(status="failed", attempt_count=3, maximum_attempts=3)
        manager = mock.Mock()
        manager.first.return_value = existing

        with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context), mock.patch.object(
            worker,
            "compute_analysis_identity",
            return_value=("analysis-1", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
        ), mock.patch.object(
            worker, "validate_persisted_xrd_analysis", return_value=SimpleNamespace(valid=False)
        ), mock.patch.object(worker.XRDAnalysisJob, "objects", return_value=manager):
            with self.assertRaises(worker.XRDAnalysisJobError) as exc_info:
                worker.submit_xrd_analysis_job("M:test:R:test", "T1")

        self.assertEqual(exc_info.exception.code, "analysis_job_retry_exhausted")

    def test_claim_next_job_uses_atomic_modify(self):
        from catalog.xrd_analysis import worker

        queryset = mock.Mock()
        ordered = mock.Mock()
        queryset.order_by.return_value = ordered
        ordered.modify.return_value = _FakeJob(status="running")

        with mock.patch.object(worker.XRDAnalysisJob, "objects", return_value=queryset):
            claimed = worker.claim_next_xrd_analysis_job(worker_identifier="worker-a")

        self.assertEqual(claimed.status, "running")
        ordered.modify.assert_called_once()
        self.assertEqual(ordered.modify.call_args.kwargs["set__worker_identifier"], "worker-a")
        self.assertEqual(ordered.modify.call_args.kwargs["set__progress_stage"], "assembling_input")


class XRDWorkerExecutionTests(SimpleTestCase):
    def test_worker_reaches_pattern_parsing_with_media_relative_raw_reference(self):
        from catalog.xrd_analysis import worker
        from catalog.xrd_analysis.pattern import parse_pattern_from_input

        job = _FakeJob(status="running", analysis_id="analysis-1", attempt_count=1)
        relative_locator = "xrd/M:test/R:test/T1/raw.csv"
        with tempfile.TemporaryDirectory() as media_root:
            raw_dir = f"{media_root}/{relative_locator.rsplit('/', 1)[0]}"
            import os

            os.makedirs(raw_dir, exist_ok=True)
            with open(f"{media_root}/{relative_locator}", "w", encoding="utf-8") as handle:
                handle.write("Angle,Intensity\n20,100\n21,150\n22,120\n")

            analysis_input = replace(
                _analysis_input(),
                raw_file_reference=RawFileReference(
                    reference_kind="raw_db_path",
                    locator=relative_locator,
                    original_filename="raw.csv",
                ),
            )
            context = _FakeContext(
                material=object(),
                recipe=object(),
                trial=object(),
                raw_file=None,
                raw_file_path=f"{media_root}/{relative_locator}",
                analysis_input=analysis_input,
            )
            persisted = SimpleNamespace(
                summary={"result_manifest_path": "xrd/M/test/T1/analyses/analysis-1/reproducibility.json"},
                persistence_warning_codes=(),
            )

            def _scientific_runner(assembled_input, *, configuration=None, progress_callback=None):
                del configuration
                del progress_callback
                parsed = parse_pattern_from_input(assembled_input)
                return {
                    "phase_state": "unresolved",
                    "warnings": [],
                    "algorithm_version": assembled_input.algorithm_version,
                    "configuration_version": assembled_input.configuration_version,
                    "best_hypothesis": None,
                    "alternative_hypotheses": [],
                    "evidence_score": float(parsed.usable_point_count),
                    "provenance": assembled_input.provenance,
                }

            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"), mock.patch.object(
                worker, "assemble_repository_xrd_input", return_value=context
            ), mock.patch.object(
                worker,
                "compute_analysis_identity",
                return_value=("analysis-1", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
            ), mock.patch.object(
                worker,
                "validate_persisted_xrd_analysis",
                side_effect=[SimpleNamespace(valid=False), SimpleNamespace(valid=True)],
            ):
                result = worker.process_one_xrd_analysis_job(
                    job,
                    scientific_runner=_scientific_runner,
                    persistence_runner=mock.Mock(return_value=persisted),
                )

        self.assertEqual(result.final_status, "succeeded")
        self.assertEqual(job.progress_stage, "succeeded")

    def test_worker_processes_job_and_marks_succeeded_after_validation(self):
        from catalog.xrd_analysis import worker

        job = _FakeJob(status="running", attempt_count=1, progress_stage="assembling_input")
        context = _FakeContext(
            material=object(),
            recipe=object(),
            trial=object(),
            raw_file=None,
            raw_file_path="/tmp/raw.csv",
            analysis_input=_analysis_input(),
        )
        persisted = SimpleNamespace(
            summary={"result_manifest_path": "xrd/M/test/T1/analyses/analysis-1/reproducibility.json"},
            persistence_warning_codes=("selected_gsas_project_unavailable",),
        )
        scientific_result = {"phase_state": "unresolved"}

        with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context), mock.patch.object(
            worker,
            "compute_analysis_identity",
            return_value=("analysis-1", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
        ), mock.patch.object(
            worker,
            "validate_persisted_xrd_analysis",
            side_effect=[SimpleNamespace(valid=False), SimpleNamespace(valid=True)],
        ) as validate_mock:
            result = worker.process_one_xrd_analysis_job(
                job,
                scientific_runner=mock.Mock(return_value=scientific_result),
                persistence_runner=mock.Mock(return_value=persisted),
            )

        self.assertEqual(result.final_status, "succeeded")
        self.assertEqual(job.status, "succeeded")
        self.assertEqual(job.progress_stage, "succeeded")
        self.assertIn("selected_gsas_project_unavailable", [item["code"] for item in job.warnings])
        self.assertEqual(validate_mock.call_count, 2)

    def test_worker_fails_on_identity_mismatch(self):
        from catalog.xrd_analysis import worker

        job = _FakeJob(status="running", analysis_id="old-analysis", attempt_count=1)
        context = _FakeContext(
            material=object(),
            recipe=object(),
            trial=object(),
            raw_file=None,
            raw_file_path="/tmp/raw.csv",
            analysis_input=_analysis_input(),
        )

        with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context), mock.patch.object(
            worker,
            "compute_analysis_identity",
            return_value=("new-analysis", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
        ):
            result = worker.process_one_xrd_analysis_job(
                job,
                scientific_runner=mock.Mock(),
                persistence_runner=mock.Mock(),
            )

        self.assertEqual(result.final_status, "failed")
        self.assertIn("analysis_identity_mismatch", job.failure_codes)

    def test_worker_uses_valid_cache_without_scientific_execution(self):
        from catalog.xrd_analysis import worker

        job = _FakeJob(status="running", analysis_id="analysis-1", attempt_count=1)
        context = _FakeContext(
            material=object(),
            recipe=object(),
            trial=object(),
            raw_file=None,
            raw_file_path="/tmp/raw.csv",
            analysis_input=_analysis_input(),
        )
        persisted = SimpleNamespace(
            summary={"result_manifest_path": "xrd/M/test/T1/analyses/analysis-1/reproducibility.json"},
            persistence_warning_codes=(),
        )
        scientific_runner = mock.Mock()
        persistence_runner = mock.Mock()

        with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context), mock.patch.object(
            worker,
            "compute_analysis_identity",
            return_value=("analysis-1", {}, {}, (), "gsas", (DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version,)),
        ), mock.patch.object(
            worker, "validate_persisted_xrd_analysis", return_value=SimpleNamespace(valid=True)
        ), mock.patch.object(worker, "load_persisted_xrd_analysis", return_value=persisted):
            result = worker.process_one_xrd_analysis_job(
                job,
                scientific_runner=scientific_runner,
                persistence_runner=persistence_runner,
            )

        self.assertEqual(result.final_status, "succeeded")
        self.assertTrue(result.cache_hit)
        scientific_runner.assert_not_called()
        persistence_runner.assert_not_called()

    def test_worker_with_real_persistence_succeeds_under_established_analysis_id(self):
        from catalog.xrd_analysis import worker
        from catalog.tests.xrd_analysis.test_persistence import (
            _analysis_input as _persistence_analysis_input,
            _analysis_result,
            _instrument_profile,
            _reference_snapshot,
        )

        with tempfile.TemporaryDirectory() as media, tempfile.TemporaryDirectory() as archive:
            instprm = Path(media) / "instrument.instprm"
            instprm.write_text("INST PROFILE A\n", encoding="utf-8")
            analysis_input = _persistence_analysis_input(instprm_path=str(instprm))
            analysis_input = replace(
                analysis_input,
                raw_file_reference=RawFileReference(
                    reference_kind="stored_path",
                    locator="/tmp/raw.csv",
                    original_filename="raw.csv",
                ),
                instrument_profile=_instrument_profile(str(instprm)),
            )
            context = _FakeContext(
                material=object(),
                recipe=object(),
                trial=object(),
                raw_file=None,
                raw_file_path="/tmp/raw.csv",
                analysis_input=analysis_input,
            )

            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/", RAW_UPLOADS_ROOT=archive), mock.patch(
                "catalog.xrd_analysis.persistence.raw_db.record_derived_file"
            ), mock.patch(
                "catalog.xrd_analysis.persistence.raw_db.RawFile",
                type(
                    "RawFileStub",
                    (),
                    {
                        "objects": staticmethod(
                            lambda **kwargs: type(
                                "Query",
                                (),
                                {"first": lambda self: type("Row", (), {"archive_folder": "trial-archive"})()},
                            )()
                        )
                    },
                ),
            ), mock.patch(
                "catalog.xrd_analysis.persistence.load_reference_phase_snapshot",
                return_value=_reference_snapshot(),
            ), mock.patch(
                "catalog.xrd_analysis.persistence._detect_gsasii_version",
                return_value="GSAS-II-test",
            ):
                (Path(archive) / "trial-archive").mkdir(parents=True, exist_ok=True)
                analysis_id, _, _, _, _, _ = worker.compute_analysis_identity(analysis_input)
                job = _FakeJob(status="running", analysis_id=analysis_id, attempt_count=1)

                with mock.patch.object(worker, "assemble_repository_xrd_input", return_value=context):
                    result = worker.process_one_xrd_analysis_job(
                        job,
                        scientific_runner=mock.Mock(return_value=_analysis_result()),
                    )

            self.assertEqual(result.final_status, "succeeded")
            self.assertEqual(job.status, "succeeded")
            self.assertTrue(str(job.result_manifest_relative_path).endswith(f"{analysis_id}/reproducibility.json"))
