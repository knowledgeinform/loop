from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


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
        structure_family="rocksalt",
    )


def _fake_trial():
    return SimpleNamespace(
        trial_id="T1",
        phase_status="single_phase",
        success=True,
        visibility_affiliations=["S4E"],
        exp_condition=SimpleNamespace(additional_params={"file_hash": "abc123"}),
    )


def _fake_job():
    return SimpleNamespace(
        id="job-1",
        analysis_id="analysis-1",
        status="succeeded",
    )


def _fake_persisted():
    return SimpleNamespace(
        result={
            "successful_single_phase_hypotheses": [
                {"hypothesis_id": "hyp-1", "candidate_id": "curated_nacl"},
            ],
            "successful_two_phase_hypotheses": [],
        }
    )


class _FakeQuerySet:
    def __init__(self, existing):
        self._existing = existing

    def first(self):
        return self._existing


class _FakeReviewModel:
    existing_review = None
    created = []

    @classmethod
    def objects(cls, **kwargs):
        return _FakeQuerySet(cls.existing_review)

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.saved = False
        _FakeReviewModel.created.append(self)

    def save(self):
        self.saved = True


class _ExistingReview:
    def __init__(self):
        self.id = "review-1"
        self.is_active = True
        self.saved = False

    def save(self):
        self.saved = True


@override_settings(STORAGES=_TEST_STORAGES)
class XRDAnalysisReviewViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "xrd-review-user",
            "xrd-review-user@example.com",
            "pass",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client()
        self.client.force_login(self.user)
        _FakeReviewModel.created = []
        _FakeReviewModel.existing_review = None

    def test_review_post_creates_separate_record_and_supersedes_previous_active_review(self):
        existing = _ExistingReview()
        _FakeReviewModel.existing_review = existing
        with mock.patch("catalog.views._resolve_visible_trial", return_value=(_fake_recipe(), _fake_trial())), mock.patch(
            "catalog.views._analysis_job_for_trial", return_value=_fake_job()
        ), mock.patch(
            "catalog.views._load_visible_persisted_analysis", return_value=(_fake_persisted(), None)
        ), mock.patch("catalog.views.XRDAnalysisReview", _FakeReviewModel), mock.patch(
            "catalog.views._user_affiliations", return_value=["S4E"]
        ):
            response = self.client.post(
                reverse(
                    "xrd_analysis_review_create",
                    kwargs={"recipe_id": "M:test:R:test", "trial_id": "T1", "analysis_id": "analysis-1"},
                ),
                {
                    "review_status": "confirmed",
                    "reviewed_phase_state": "likely single-phase",
                    "selected_hypothesis_id": "hyp-1",
                    "added_candidate_identifiers": "curated_nacl, curated_nacl3",
                    "confidence": "high",
                    "notes": "Keep the automated result and the expert review separate.",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(existing.saved)
        self.assertFalse(existing.is_active)
        self.assertEqual(len(_FakeReviewModel.created), 1)
        created = _FakeReviewModel.created[0]
        self.assertTrue(created.saved)
        self.assertEqual(created.kwargs["analysis_id"], "analysis-1")
        self.assertEqual(created.kwargs["review_status"], "confirmed")
        self.assertEqual(created.kwargs["reviewed_phase_state"], "likely single-phase")
        self.assertEqual(created.kwargs["selected_hypothesis_id"], "hyp-1")
        self.assertEqual(created.kwargs["added_candidate_identifiers"], ["curated_nacl", "curated_nacl3"])
        self.assertEqual(created.kwargs["supersedes_review_id"], "review-1")

    def test_review_post_rejects_unknown_hypothesis(self):
        with mock.patch("catalog.views._resolve_visible_trial", return_value=(_fake_recipe(), _fake_trial())), mock.patch(
            "catalog.views._analysis_job_for_trial", return_value=_fake_job()
        ), mock.patch(
            "catalog.views._load_visible_persisted_analysis", return_value=(_fake_persisted(), None)
        ), mock.patch("catalog.views.XRDAnalysisReview", _FakeReviewModel), mock.patch(
            "catalog.views._user_affiliations", return_value=["S4E"]
        ):
            response = self.client.post(
                reverse(
                    "xrd_analysis_review_create",
                    kwargs={"recipe_id": "M:test:R:test", "trial_id": "T1", "analysis_id": "analysis-1"},
                ),
                {
                    "review_status": "confirmed",
                    "selected_hypothesis_id": "missing-hypothesis",
                    "notes": "This should not be accepted.",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(_FakeReviewModel.created), 0)


@override_settings(STORAGES=_TEST_STORAGES)
@override_settings(EMBEDDINGS_ON_WRITE=False)
class XRDAnalysisReviewDocumentTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _mongo_reachable():
            raise unittest.SkipTest("MongoDB not reachable")

    def tearDown(self):
        from catalog.documents import XRDAnalysisReview

        XRDAnalysisReview.objects(analysis_id="analysis-review-test").delete()

    def test_review_document_persists_superseding_history(self):
        from catalog.documents import XRDAnalysisReview

        first = XRDAnalysisReview(
            analysis_id="analysis-review-test",
            material_auid="M:test",
            recipe_auid="M:test:R:test",
            trial_id="T1",
            reviewer_username="reviewer-1",
            reviewer_display_name="Reviewer 1",
            reviewer_organization="S4E",
            review_status="confirmed",
            reviewed_phase_state="likely single-phase",
            notes="Initial review.",
            is_active=True,
        )
        first.save()
        first.is_active = False
        first.save()

        second = XRDAnalysisReview(
            analysis_id="analysis-review-test",
            material_auid="M:test",
            recipe_auid="M:test:R:test",
            trial_id="T1",
            reviewer_username="reviewer-2",
            reviewer_display_name="Reviewer 2",
            reviewer_organization="S4E",
            review_status="corrected",
            reviewed_phase_state="unresolved",
            supersedes_review_id=str(first.id),
            notes="Superseding review.",
            is_active=True,
        )
        second.save()

        persisted = list(XRDAnalysisReview.objects(analysis_id="analysis-review-test").order_by("-created_at"))
        self.assertEqual(len(persisted), 2)
        self.assertEqual(persisted[0].reviewer_username, "reviewer-2")
        self.assertEqual(persisted[0].supersedes_review_id, str(first.id))
        self.assertTrue(persisted[0].is_active)
        self.assertFalse(persisted[1].is_active)
