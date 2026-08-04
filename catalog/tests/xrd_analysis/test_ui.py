from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse


_TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": settings.MEDIA_ROOT},
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


def _fake_recipe():
    return SimpleNamespace(
        id="M:test:R:test",
        material_auid="M:test",
        elements={"Na": 1.0, "Cl": 1.0},
        structure_family="rocksalt",
        synthesis_steps=[],
    )


def _fake_trial():
    return SimpleNamespace(
        trial_id="T1",
        trial_date=datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc),
        phase_status="single_phase",
        success=True,
        experimenter="scientist",
        raw_data_link="https://example.com/raw.csv",
        raw_data_type="xrd",
        spacegroup="Fm-3m (#225)",
        element_sites={"Na": "A-site", "Cl": "X-site"},
        notes="Trial notes",
        visibility_affiliations=["S4E"],
        exp_condition=SimpleNamespace(additional_params={"file_hash": "abc123"}),
    )


def _fake_job(status="succeeded"):
    return SimpleNamespace(
        id="job-1",
        analysis_id="analysis-1",
        status=status,
        progress_stage="persisting_result" if status == "running" else status,
        progress_message="Running automated XRD analysis." if status == "running" else "Persisted result ready.",
        warning_codes=[],
        warnings=[],
        failure_codes=[],
        queued_at=datetime(2026, 7, 27, 12, 5, tzinfo=timezone.utc),
        completed_at=datetime(2026, 7, 27, 12, 6, tzinfo=timezone.utc) if status == "succeeded" else None,
    )


def _fake_persisted():
    return SimpleNamespace(
        result={
            "analysis_id": "analysis-1",
            "phase_state": "likely single-phase",
            "evidence_score": 0.84,
            "warnings": [
                {
                    "code": "stick_pattern_limited",
                    "message": "Stick patterns remain limited for some downstream stages.",
                    "severity": "warning",
                    "field": "pattern_type",
                    "stage": "quality_control",
                }
            ],
            "failure_codes": [],
            "quality_control": {
                "status": "pattern accepted for later analysis",
                "pattern_type": "continuous",
                "usable_point_count": 240,
                "coordinate_min": 20.0,
                "coordinate_max": 80.0,
                "range_width": 60.0,
                "median_step_size": 0.02,
                "step_size_variation": 0.001,
                "fraction_invalid_rows_removed": 0.0,
                "duplicate_count": 0,
                "negative_intensity_fraction": 0.0,
                "non_positive_intensity_fraction": 0.0,
                "approximate_signal_to_noise": 12.4,
                "detectable_peak_region_count": 4,
                "missing_interval_count": 0,
                "clipping_detected": False,
            },
            "parsed_pattern": {
                "normalized_two_theta": [20.0, 30.0, 40.0, 50.0],
                "original_intensities": [10.0, 40.0, 25.0, 15.0],
                "normalized_intensities": [0.25, 1.0, 0.63, 0.38],
            },
            "ranked_candidate_shortlist": [
                {
                    "candidate_id": "curated_nacl",
                    "formula": "NaCl",
                    "source": "curated_reference",
                    "space_group": "Fm-3m",
                    "structure_family": "rocksalt",
                    "combined_pre_rank_score": 0.91,
                },
                {
                    "candidate_id": "curated_nacl3",
                    "formula": "NaCl3",
                    "source": "curated_reference",
                    "space_group": "Pm-3m",
                    "structure_family": "other",
                    "combined_pre_rank_score": 0.44,
                },
            ],
            "selected_best_model": {
                "model_type": "single_phase",
                "hypothesis_id": "hyp-1",
                "candidate_ids": ["curated_nacl"],
                "selection_reason": "Best single-phase fit.",
            },
            "best_single_phase_hypothesis": {
                "hypothesis_id": "hyp-1",
                "candidate_id": "curated_nacl",
                "candidate_source": "curated_reference",
                "refinement_status": "completed",
                "convergence_status": "converged",
                "rwp": 0.071,
                "rp": 0.052,
                "goodness_of_fit": 1.18,
                "runtime_seconds": 2.4,
                "phase_scale_factor": 1.0,
                "refined_lattice_parameters": {"length_a": 5.64, "volume": 179.0},
                "observed_two_theta": [20.0, 30.0, 40.0, 50.0],
                "observed_intensities": [10.0, 40.0, 25.0, 15.0],
                "calculated_total_pattern": [8.0, 38.0, 24.0, 14.0],
                "calculated_background": [2.0, 2.0, 2.0, 2.0],
                "difference_pattern": [2.0, 2.0, 1.0, 1.0],
                "expected_reflections": [
                    {"h": 1, "k": 1, "l": 1, "multiplicity": 8, "two_theta": 31.7, "d_spacing": 2.82, "predicted_intensity": 100.0},
                    {"h": 2, "k": 0, "l": 0, "multiplicity": 6, "two_theta": 45.5, "d_spacing": 1.99, "predicted_intensity": 60.0},
                ],
                "significant_positive_residual_regions": [],
                "unsupported_strong_predicted_regions": [],
            },
            "successful_single_phase_hypotheses": [
                {
                    "hypothesis_id": "hyp-1",
                    "candidate_id": "curated_nacl",
                }
            ],
            "successful_two_phase_hypotheses": [],
            "algorithm_version": "loop-xrd-phase-analysis-mvp-v5",
            "configuration_version": "loop-xrd-config-v5",
        },
        summary={
            "analysis_id": "analysis-1",
            "analysis_status": "succeeded",
            "phase_state": "likely single-phase",
            "evidence_score": 0.84,
            "warning_count": 1,
            "algorithm_version": "loop-xrd-phase-analysis-mvp-v5",
            "configuration_version": "loop-xrd-config-v5",
            "selected_model_type": "single_phase",
            "best_hypothesis_id": "hyp-1",
        },
        reproducibility_manifest={
            "analysis_id": "analysis-1",
            "algorithm_version": "loop-xrd-phase-analysis-mvp-v5",
            "configuration_version": "loop-xrd-config-v5",
            "configuration_hash": "cfg-hash",
            "reference_phase_snapshot_version": "snapshot-v1",
            "gsasii_version": "GSAS-II-test",
            "python_version": "3.12",
            "raw_file_hash": "abc123",
            "parsing_method": "loop-parse",
            "candidate_simulation_method": "gsas-screening",
            "refinement_method": "gsas-single-phase",
            "completed_at": "2026-07-27T12:06:00+00:00",
        },
        persisted_artifacts=(
            {
                "relative_path": "xrd/M:test/R:test/T1/analyses/analysis-1/result.json",
                "artifact_type": "result_json",
                "content_type": "application/json",
                "size_bytes": 2048,
                "sha256": "result-hash",
            },
            {
                "relative_path": "xrd/M:test/R:test/T1/analyses/analysis-1/reproducibility.json",
                "artifact_type": "reproducibility_json",
                "content_type": "application/json",
                "size_bytes": 1024,
                "sha256": "manifest-hash",
            },
        ),
    )


@override_settings(STORAGES=_TEST_STORAGES)
class XRDAnalysisUITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "xrd-ui-user",
            "xrd-ui-user@example.com",
            "pass",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_trial_detail_separates_human_and_automated_results(self):
        latest_analysis = {
            "job": _fake_job(),
            "summary": {
                "selected_model_type": "single_phase",
                "evidence_score": 0.84,
                "warning_count": 1,
            },
            "phase_state_meta": {"label": "Likely single-phase", "badge_class": "bg-success"},
            "status_meta": {"label": "Likely single-phase", "badge_class": "bg-success"},
            "detail_url": "/catalog/detail/",
            "submit_url": "/api/v1/submit/",
            "status_url": "/api/v1/status/",
            "result_url": "/api/v1/result/",
            "can_submit": True,
            "allow_submit_now": True,
            "active_review": {
                "review_status": "confirmed",
                "reviewed_phase_state": "likely single-phase",
                "reviewer_display_name": "Dr. Loop",
            },
        }
        with mock.patch("catalog.views._resolve_visible_trial", return_value=(_fake_recipe(), _fake_trial())), mock.patch(
            "catalog.views.xrd_store.resolve_raw_path", return_value=Path("/tmp/raw.csv")
        ), mock.patch(
            "catalog.views.xrd_store.get_or_build",
            return_value=SimpleNamespace(
                peaks=[{"two_theta": 31.7, "intensity": 100.0, "area": 20.0}],
                overlay_url="/media/plot.png",
                plot_style="continuous",
            ),
        ), mock.patch("catalog.views._latest_analysis_card_context", return_value=latest_analysis):
            response = self.client.get(
                reverse("trial_detail", kwargs={"recipe_id": "M:test:R:test", "trial_id": "T1"})
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Human result")
        self.assertContains(response, "Automated status")
        self.assertContains(response, "Likely single-phase")
        self.assertContains(response, "Open analysis detail")
        self.assertContains(response, "Analyze XRD pattern")
        self.assertNotContains(response, "Run full GSAS-II refinement")

    def test_analysis_detail_renders_persisted_sections(self):
        review_history = (
            [
                {
                    "reviewer_display_name": "Dr. Loop",
                    "review_status": "confirmed",
                    "reviewed_phase_state": "likely single-phase",
                    "selected_hypothesis_id": "hyp-1",
                    "confidence": "high",
                    "notes": "The single-phase hypothesis is consistent with the trial notes.",
                    "reviewer_organization": "S4E",
                    "is_active": True,
                    "created_at": datetime(2026, 7, 27, 12, 15, tzinfo=timezone.utc),
                }
            ],
            {
                "reviewer_display_name": "Dr. Loop",
                "review_status": "confirmed",
                "reviewed_phase_state": "likely single-phase",
                "selected_hypothesis_id": "hyp-1",
                "confidence": "high",
                "notes": "The single-phase hypothesis is consistent with the trial notes.",
                "reviewer_organization": "S4E",
                "is_active": True,
                "created_at": datetime(2026, 7, 27, 12, 15, tzinfo=timezone.utc),
            },
        )
        with mock.patch("catalog.views._resolve_visible_trial", return_value=(_fake_recipe(), _fake_trial())), mock.patch(
            "catalog.views._analysis_job_for_trial", return_value=_fake_job()
        ), mock.patch(
            "catalog.views._load_visible_persisted_analysis", return_value=(_fake_persisted(), None)
        ), mock.patch("catalog.views._collect_review_history", return_value=review_history):
            response = self.client.get(
                reverse(
                    "xrd_analysis_detail",
                    kwargs={"recipe_id": "M:test:R:test", "trial_id": "T1", "analysis_id": "analysis-1"},
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Data quality")
        self.assertContains(response, "Pattern evidence")
        self.assertContains(response, "Per-phase evidence")
        self.assertContains(response, "Expert review")
        self.assertContains(response, "curated_nacl")
        self.assertContains(response, "reproducibility.json")
        self.assertContains(response, "Save expert review")

    def test_analysis_detail_shows_persisted_error_without_result_sections(self):
        with mock.patch("catalog.views._resolve_visible_trial", return_value=(_fake_recipe(), _fake_trial())), mock.patch(
            "catalog.views._analysis_job_for_trial", return_value=_fake_job()
        ), mock.patch(
            "catalog.views._load_visible_persisted_analysis",
            return_value=(None, {"code": "analysis_result_integrity_failed", "detail": "hash mismatch"}),
        ):
            response = self.client.get(
                reverse(
                    "xrd_analysis_detail",
                    kwargs={"recipe_id": "M:test:R:test", "trial_id": "T1", "analysis_id": "analysis-1"},
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "analysis_result_integrity_failed")
        self.assertContains(response, "hash mismatch")
