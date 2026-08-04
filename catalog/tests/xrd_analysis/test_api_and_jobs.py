from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


class _FakeJob:
    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", "job-1")
        self.analysis_id = kwargs.pop("analysis_id", "analysis-1")
        self.status = kwargs.pop("status", "queued")
        self.cache_hit = kwargs.pop("cache_hit", False)
        self.progress_stage = kwargs.pop("progress_stage", "queued")
        self.progress_message = kwargs.pop("progress_message", "Queued")
        self.attempt_count = kwargs.pop("attempt_count", 0)
        self.maximum_attempts = kwargs.pop("maximum_attempts", 3)
        self.created_at = kwargs.pop("created_at", datetime.now(timezone.utc))
        self.queued_at = kwargs.pop("queued_at", datetime.now(timezone.utc))
        self.started_at = kwargs.pop("started_at", None)
        self.last_heartbeat_at = kwargs.pop("last_heartbeat_at", None)
        self.completed_at = kwargs.pop("completed_at", None)
        self.warnings = kwargs.pop("warnings", [])
        self.failure_codes = kwargs.pop("failure_codes", [])
        self.error_summary = kwargs.pop("error_summary", None)
        self.recipe_auid = kwargs.pop("recipe_auid", "M:test:R:test")
        self.trial_id = kwargs.pop("trial_id", "T1")


class XRDAnalysisApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "apiuser-xrd",
            "apiuser-xrd@example.com",
            "pass",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_submission_returns_cache_hit_response(self):
        fake_job = _FakeJob(status="succeeded", cache_hit=True, progress_stage="succeeded")
        submission = SimpleNamespace(
            job=fake_job,
            analysis_id="analysis-1",
            cache_hit=True,
            status="succeeded",
            progress_stage="succeeded",
            reused_active_job=False,
            requeued_failed_job=False,
        )
        with mock.patch("catalog.api.views._visible_trial_or_problem", return_value=(object(), object(), None)), mock.patch(
            "catalog.api.views._trial_xrd_path", return_value=Path("/tmp/raw.csv")
        ), mock.patch(
            "catalog.api.views.submit_xrd_analysis_job", return_value=submission
        ) as submit_mock:
            response = self.client.post(
                reverse(
                    "api-v1-experiment-xrd-analysis-submit",
                    kwargs={"recipe_auid": "M:test:R:test", "trial_id": "T1"},
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["analysis_id"], "analysis-1")
        submit_mock.assert_called_once()

    def test_submission_returns_active_job_response(self):
        fake_job = _FakeJob(status="running", progress_stage="quality_control")
        submission = SimpleNamespace(
            job=fake_job,
            analysis_id="analysis-1",
            cache_hit=False,
            status="running",
            progress_stage="quality_control",
            reused_active_job=True,
            requeued_failed_job=False,
        )
        with mock.patch("catalog.api.views._visible_trial_or_problem", return_value=(object(), object(), None)), mock.patch(
            "catalog.api.views._trial_xrd_path", return_value=Path("/tmp/raw.csv")
        ), mock.patch("catalog.api.views.submit_xrd_analysis_job", return_value=submission):
            response = self.client.post(
                reverse(
                    "api-v1-experiment-xrd-analysis-submit",
                    kwargs={"recipe_auid": "M:test:R:test", "trial_id": "T1"},
                )
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["data"]["active_job_reused"])

    def test_submission_returns_raw_file_not_found(self):
        with mock.patch("catalog.api.views._visible_trial_or_problem", return_value=(object(), object(), None)), mock.patch(
            "catalog.api.views._trial_xrd_path", return_value=None
        ):
            response = self.client.post(
                reverse(
                    "api-v1-experiment-xrd-analysis-submit",
                    kwargs={"recipe_auid": "M:test:R:test", "trial_id": "T1"},
                )
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["errors"][0]["code"], "xrd_raw_file_not_found")

    def test_job_status_response_shape(self):
        fake_job = _FakeJob(status="failed", progress_stage="failed", failure_codes=["analysis_worker_execution_failed"])
        with mock.patch("catalog.api.views._visible_analysis_job_or_problem", return_value=(fake_job, object(), object(), None)):
            response = self.client.get(
                reverse("api-v1-xrd-analysis-job-detail", kwargs={"job_id": "job-1"})
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["failure_codes"], ["analysis_worker_execution_failed"])

    def test_result_endpoint_returns_validated_payload(self):
        fake_job = _FakeJob(status="succeeded", progress_stage="succeeded")
        persisted = SimpleNamespace(
            analysis_id="analysis-1",
            summary={"analysis_id": "analysis-1", "result_manifest_path": "xrd/a/reproducibility.json"},
            result={"phase_state": "unresolved"},
            reproducibility_manifest={"analysis_id": "analysis-1"},
            persisted_artifacts=(
                {
                    "relative_path": "xrd/M/test/R/test/T1/analyses/analysis-1/result.json",
                    "artifact_type": "result_json",
                    "content_type": "application/json",
                    "size_bytes": 10,
                    "sha256": "abc",
                },
            ),
            persistence_warning_codes=("selected_gsas_project_unavailable",),
        )
        with mock.patch("catalog.api.views._visible_analysis_job_or_problem", return_value=(fake_job, SimpleNamespace(id="M:test:R:test"), SimpleNamespace(trial_id="T1"), None)), mock.patch(
            "catalog.api.views.assemble_repository_xrd_input",
            return_value=SimpleNamespace(analysis_input=object()),
        ), mock.patch(
            "catalog.api.views.validate_persisted_xrd_analysis",
            return_value=SimpleNamespace(valid=True),
        ), mock.patch(
            "catalog.api.views.load_persisted_xrd_analysis", return_value=persisted
        ):
            response = self.client.get(
                reverse("api-v1-xrd-analysis-result", kwargs={"analysis_id": "analysis-1"})
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()["data"]
        self.assertEqual(payload["analysis_id"], "analysis-1")
        self.assertEqual(payload["result"]["phase_state"], "unresolved")
        self.assertEqual(payload["persistence_warning_codes"], ["selected_gsas_project_unavailable"])

    def test_result_endpoint_reports_integrity_failure(self):
        fake_job = _FakeJob(status="succeeded", progress_stage="succeeded")
        with mock.patch("catalog.api.views._visible_analysis_job_or_problem", return_value=(fake_job, SimpleNamespace(id="M:test:R:test"), SimpleNamespace(trial_id="T1"), None)), mock.patch(
            "catalog.api.views.assemble_repository_xrd_input",
            return_value=SimpleNamespace(analysis_input=object()),
        ), mock.patch(
            "catalog.api.views.validate_persisted_xrd_analysis",
            return_value=SimpleNamespace(valid=False, failure_code="analysis_cache_hash_mismatch", detail="hash mismatch"),
        ):
            response = self.client.get(
                reverse("api-v1-xrd-analysis-result", kwargs={"analysis_id": "analysis-1"})
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["errors"][0]["code"], "analysis_result_integrity_failed")

    def test_artifact_path_traversal_is_rejected(self):
        from catalog.api import views

        request = APIRequestFactory().get("/api/v1/xrd-analyses/analysis-1/artifacts/../secret.txt/")
        force_authenticate(request, user=self.user)
        with mock.patch("catalog.api.views._visible_analysis_job_or_problem", return_value=(_FakeJob(status="succeeded"), SimpleNamespace(id="M:test:R:test"), SimpleNamespace(trial_id="T1"), None)):
            response = views.xrd_analysis_artifact(
                request,
                analysis_id="analysis-1",
                artifact_name="../secret.txt",
            )

        self.assertEqual(response.status_code, 404)


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
@override_settings(EMBEDDINGS_ON_WRITE=False)
class XRDJobApiIntegrationTests(TestCase):

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "xrdjobint",
            "xrdjobint@example.com",
            "pass",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_worker_iteration_persists_and_api_returns_result(self):
        from catalog import xrd_store
        from catalog.documents import ExpCondition, EmbeddedTrial, Material, Recipe
        from catalog.xrd_analysis.worker import process_one_xrd_analysis_job, submit_xrd_analysis_job

        media_root = tempfile.mkdtemp()
        recipe_auid = "M:test:R:test"
        material_auid = "M:test"
        trial_id = "T1"
        try:
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                raw_path = xrd_store.trial_dir(recipe_auid, trial_id) / "raw.csv"
                raw_path.write_text("Angle,Intensity\n20,100\n21,150\n22,120\n", encoding="utf-8")

                Material(
                    id=material_auid,
                    elements={"Na": 1.0, "Cl": 1.0},
                    structure_family="rocksalt",
                    default_visibility_affiliations=["S4E"],
                ).save()
                trial = EmbeddedTrial(
                    trial_id=trial_id,
                    trial_date=datetime.now(timezone.utc),
                    phase_status="single_phase",
                    exp_condition=ExpCondition(additional_params={}),
                    visibility_affiliations=["S4E"],
                )
                Recipe(
                    id=recipe_auid,
                    material_auid=material_auid,
                    elements={"Na": 1.0, "Cl": 1.0},
                    structure_family="rocksalt",
                    synthesis_steps=[],
                    trials=[trial],
                    visibility_affiliations=["S4E"],
                ).save()

                submission = submit_xrd_analysis_job(recipe_auid, trial_id)
                fake_result = {
                    "phase_state": "unresolved",
                    "warnings": [],
                    "algorithm_version": "loop-xrd-phase-analysis-mvp-v5",
                    "configuration_version": "loop-xrd-config-v5",
                    "best_hypothesis": None,
                    "alternative_hypotheses": [],
                    "evidence_score": 0.0,
                    "provenance": {
                        "source_material": {},
                        "source_recipe": {},
                        "source_trial": {},
                        "source_raw_file": {},
                        "measurement_metadata": [],
                    },
                }
                persisted = SimpleNamespace(
                    summary={"analysis_id": submission.analysis_id, "result_manifest_path": f"xrd/M:test/R:test/{trial_id}/analyses/{submission.analysis_id}/reproducibility.json"},
                    persistence_warning_codes=("selected_gsas_project_unavailable",),
                )
                with mock.patch("catalog.xrd_analysis.worker.persist_xrd_analysis_result", return_value=persisted), mock.patch(
                    "catalog.xrd_analysis.worker.validate_persisted_xrd_analysis",
                    side_effect=[SimpleNamespace(valid=False), SimpleNamespace(valid=True)],
                ), mock.patch(
                    "catalog.xrd_analysis.worker.compute_analysis_identity",
                    return_value=(submission.analysis_id, {}, {}, (), "gsas", ("loop-xrd-config-v5",)),
                ):
                    result = process_one_xrd_analysis_job(
                        submission.job,
                        scientific_runner=mock.Mock(return_value=fake_result),
                        persistence_runner=mock.Mock(return_value=persisted),
                    )

                self.assertEqual(result.final_status, "succeeded")

                with mock.patch(
                    "catalog.api.views._visible_analysis_job_or_problem",
                    return_value=(submission.job, SimpleNamespace(id=recipe_auid), SimpleNamespace(trial_id=trial_id), None),
                ), mock.patch(
                    "catalog.api.views.validate_persisted_xrd_analysis", return_value=SimpleNamespace(valid=True)
                ), mock.patch(
                    "catalog.api.views.load_persisted_xrd_analysis",
                    return_value=SimpleNamespace(
                        analysis_id=submission.analysis_id,
                        summary={"analysis_id": submission.analysis_id, "result_manifest_path": persisted.summary["result_manifest_path"]},
                        result={"phase_state": "unresolved"},
                        reproducibility_manifest={"analysis_id": submission.analysis_id},
                        persisted_artifacts=(),
                        persistence_warning_codes=("selected_gsas_project_unavailable",),
                    ),
                ):
                    response = self.client.get(
                        reverse("api-v1-xrd-analysis-result", kwargs={"analysis_id": submission.analysis_id})
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["data"]["analysis_id"], submission.analysis_id)
        finally:
            from catalog.documents import Material as _M, Recipe as _R, XRDAnalysisJob as _J

            _J.objects(recipe_auid=recipe_auid).delete()
            _R.objects(id=recipe_auid).delete()
            _M.objects(id=material_auid).delete()
            shutil.rmtree(media_root, ignore_errors=True)
