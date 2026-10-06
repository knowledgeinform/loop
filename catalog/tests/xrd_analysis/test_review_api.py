"""Regression coverage for persisted expert reviews and API discovery."""
import unittest
from catalog.tests.mongo_guard import mongo_reachable
from contextlib import ExitStack
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from catalog.documents import XRDAnalysisReview
from catalog.models import APIKey
from catalog.tests.xrd_analysis.test_ui import (
    _TEST_STORAGES, _fake_recipe, _fake_trial, _fake_job, _fake_persisted,
)


@override_settings(STORAGES=_TEST_STORAGES, EMBEDDINGS_ON_WRITE=False)
class ExpertReviewIntegrationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not mongo_reachable():
            raise unittest.SkipTest("MongoDB not reachable")

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            'pr45-reviewer', is_staff=True, is_superuser=True,
        )
        self.client.force_login(self.user)
        self.analysis_id = 'pr45-review-probe'
        self.job = _fake_job()
        self.job.analysis_id = self.analysis_id
        self.job.recipe_auid = 'M:test:R:test'
        self.job.trial_id = 'T1'
        self.url = reverse('api-v1-xrd-analysis-reviews', kwargs={'analysis_id': self.analysis_id})
        self.ui_kwargs = dict(recipe_id='M:test:R:test', trial_id='T1', analysis_id=self.analysis_id)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target, value in (
            ('catalog.views._resolve_visible_trial', (_fake_recipe(), _fake_trial())),
            ('catalog.views._analysis_job_for_trial', self.job),
            ('catalog.views._load_visible_persisted_analysis', (_fake_persisted(), None)),
            ('catalog.views._user_affiliations', ['S4E']),
            ('catalog.api.views._job_by_analysis_id', self.job),
            ('catalog.api.views._visible_trial_or_problem', (_fake_recipe(), _fake_trial(), None)),
        ):
            self.stack.enter_context(mock.patch(target, return_value=value))
        self.addCleanup(lambda: XRDAnalysisReview.objects(analysis_id=self.analysis_id).delete())

    def submit(self, **fields):
        payload = dict(review_status='confirmed', reviewed_phase_state='likely multiphase',
                       identified_structures=['spinel', 'other'], other_structure='UniquePr45Structure',
                       identified_phases='UniquePr45Phase', amorphous='1', ambiguous='1',
                       notes='UniquePr45Note', confidence='high')
        payload.update(fields)
        response = self.client.post(reverse('xrd_analysis_review_create', kwargs=self.ui_kwargs), payload)
        self.assertEqual(response.status_code, 302)
        return XRDAnalysisReview.objects(analysis_id=self.analysis_id, is_active=True).first()

    def test_round_trip_save_to_api_and_supersession(self):
        first = self.submit()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertEqual(data['active_review']['identified_structures'], ['spinel', 'other'])
        self.assertEqual(data['active_review']['other_structure'], 'UniquePr45Structure')
        self.assertEqual(data['active_review']['identified_phases'], 'UniquePr45Phase')
        self.assertTrue(data['active_review']['amorphous'])
        self.assertTrue(data['active_review']['ambiguous'])
        self.assertFalse(data['active_review']['no_identifiable_structure'])
        second = self.submit(identified_structures=['rock_salt'], review_status='corrected', amorphous='', ambiguous='')
        data = self.client.get(self.url).json()['data']
        self.assertEqual(data['active_review']['review_id'], str(second.id))
        self.assertEqual(data['active_review']['supersedes_review_id'], str(first.id))
        self.assertEqual(data['active_review']['other_structure'], '')
        self.assertEqual(len(data['reviews']), 2)
        self.assertFalse(data['reviews'][1]['is_active'])

    def test_saved_annotations_are_visible_in_history(self):
        self.submit()
        response = self.client.get(reverse('xrd_analysis_detail', kwargs=self.ui_kwargs))
        self.assertContains(response, 'UniquePr45Note')
        self.assertContains(response, 'UniquePr45Structure')
        self.assertContains(response, 'UniquePr45Phase')

    def test_history_mapping_preserves_all_new_fields(self):
        from catalog.views import _collect_review_history
        self.submit()
        rows, active = _collect_review_history(self.analysis_id)
        for field in ('identified_structures', 'other_structure', 'identified_phases',
                      'no_identifiable_structure', 'amorphous', 'ambiguous'):
            with self.subTest(field=field):
                self.assertIn(field, active)

    def test_empty_reviews(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['reviews'], [])
        self.assertIsNone(response.json()['data']['active_review'])

    def test_legacy_review_defaults(self):
        doc = XRDAnalysisReview(analysis_id=self.analysis_id, material_auid='M:test',
            recipe_auid='M:test:R:test', trial_id='T1', reviewer_username='legacy', review_status='confirmed')
        doc.save()
        XRDAnalysisReview._get_collection().update_one({'_id': doc.id}, {'$unset': {
            field: '' for field in ('identified_structures', 'other_structure', 'identified_phases',
                                   'no_identifiable_structure', 'amorphous', 'ambiguous')
        }})
        data = self.client.get(self.url).json()['data']['active_review']
        self.assertEqual(data['identified_structures'], [])
        self.assertEqual(data['identified_phases'], '')
        self.assertFalse(data['amorphous'])

    def test_missing_analysis(self):
        with mock.patch('catalog.api.views._job_by_analysis_id', return_value=None):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_hidden_analysis_does_not_query_reviews(self):
        from rest_framework.response import Response
        with mock.patch('catalog.api.views._visible_trial_or_problem', return_value=(None, None, Response(status=404))), mock.patch('catalog.api.views.XRDAnalysisReview') as reviews:
            self.assertEqual(self.client.get(self.url).status_code, 404)
            reviews.objects.assert_not_called()

    def test_unauthenticated(self):
        self.client.logout()
        self.assertIn(self.client.get(self.url).status_code, [401, 403])

    def test_read_scope_is_required(self):
        from catalog.api.views import xrd_analysis_reviews
        factory = APIRequestFactory()
        for scopes, expected in [(['data:read'], 200), (['data:write'], 403)]:
            with self.subTest(scopes=scopes):
                request = factory.get(self.url)
                force_authenticate(request, user=self.user, token=APIKey(user=self.user, scopes=scopes))
                self.assertEqual(xrd_analysis_reviews(request, analysis_id=self.analysis_id).status_code, expected)

    def test_other_validation_keeps_previous_review_active(self):
        first = self.submit()
        self.submit(other_structure='')
        first.reload()
        self.assertTrue(first.is_active)
        self.assertEqual(XRDAnalysisReview.objects(analysis_id=self.analysis_id).count(), 1)

    def test_trial_export_discovers_existing_review(self):
        from catalog.api.views import _trial_data
        from catalog.documents import EmbeddedTrial
        self.submit()
        payload = _trial_data(_fake_recipe(), EmbeddedTrial(trial_id='T1'))
        from catalog.documents import XRDAnalysisJob
        job = XRDAnalysisJob(analysis_id=self.analysis_id, material_auid='M:test',
            recipe_auid='M:test:R:test', trial_id='T1', algorithm_version='test',
            configuration_version='test', status='succeeded')
        job.save()
        self.addCleanup(lambda: XRDAnalysisJob.objects(analysis_id=self.analysis_id).delete())
        with mock.patch('catalog.api.views.submit_xrd_analysis_job') as submit:
            response = self.client.get(payload['xrd_analyses_url'])
        self.assertEqual(response.status_code, 200)
        submit.assert_not_called()
        analysis = response.json()['data'][0]
        self.assertEqual(analysis['analysis_id'], self.analysis_id)
        self.assertEqual(analysis['reviews_url'], self.url)
        self.assertEqual(self.client.get(analysis['reviews_url']).status_code, 200)

    def test_schema_describes_review_fields_and_nullable_active_review(self):
        schema = self.client.get('/api/v1/openapi.json').json()
        operation = schema['paths']['/api/v1/xrd-analyses/{analysis_id}/reviews/']['get']
        self.assertIn('application/json', operation['responses']['200']['content'])
        review = schema['components']['schemas']['XRDAnalysisReview']['properties']
        self.assertIn('identified_structures', review)
        self.assertEqual(review['amorphous']['type'], 'boolean')
        envelope = schema['components']['schemas']['XRDAnalysisReviews']['properties']
        self.assertTrue(envelope['active_review']['nullable'])
        self.assertEqual(envelope['reviews']['type'], 'array')
