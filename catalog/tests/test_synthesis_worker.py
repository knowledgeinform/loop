"""Tests for the background synthesis-discretization worker.

The happy/fallback paths need MongoDB (they create and re-key real recipes), so
they're gated on ``_mongo_reachable`` like the batch integration tests. The
uploader lookup and the LLM call are monkeypatched so no Django auth DB or
network is required.
"""
import os
import tempfile
import unittest
import uuid
from unittest import mock

from django.test import SimpleTestCase, override_settings
from django.test.client import RequestFactory

from catalog.llm_synthesis import SynthesisParseUnavailable
from catalog.services.batch_experiment_upload import parse_composition_formula


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


class _FakeUser:
    is_authenticated = True
    is_superuser = False

    def __init__(self, username="worktester", uid=424242):
        self.username = username
        self.id = uid

    def get_username(self):
        return self.username


class EnqueueGateTests(SimpleTestCase):
    def test_enqueue_noop_when_disabled(self):
        from catalog.synthesis_worker import enqueue_synthesis_job

        # Feature ships off by default -> no job created, no Mongo write.
        job = enqueue_synthesis_job(
            kind="experiment",
            recipe_auid="M:x:R:y",
            material_auid="M:x",
            route_text="Ball milled then fired",
            username="someone",
            trial_id="t1",
        )
        self.assertIsNone(job)

    @override_settings(SYNTHESIS_LLM_ENABLED=True, OPENROUTER_API_KEY="k")
    def test_enqueue_noop_when_no_route_text(self):
        from catalog.synthesis_worker import enqueue_synthesis_job

        self.assertIsNone(
            enqueue_synthesis_job(
                kind="experiment",
                recipe_auid="M:x:R:y",
                material_auid="M:x",
                route_text="   ",
                username="someone",
                trial_id="t1",
            )
        )


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
@override_settings(EMBEDDINGS_ON_WRITE=False)
class SynthesisWorkerTests(SimpleTestCase):
    databases = set()

    def _seed_other_step_trial(self, route, batch_id="WK-1"):
        """Create a recipe/trial whose route is a single 'other' step, like a batch import."""
        from catalog.views import persist_experimental_trial

        request = RequestFactory().get("/", HTTP_HOST="localhost:8000")
        user = _FakeUser()
        request.user = user
        elements = parse_composition_formula("(Co0.5Ni0.5)O")
        steps = [{"step_number": 1, "step_type": "other", "description": route}]
        result = persist_experimental_trial(
            user=user,
            request=request,
            raw_elements=elements,
            structure_family="rocksalt",
            synthesis_steps=steps,
            phase_status="single_phase",
            spacegroup="unknown",
            element_sites={},
            raw_data_type="xrd",
            notes=None,
            csv_file=None,
            source_batch_id=batch_id,
        )
        return user, result

    def _enqueue(self, result, route, username):
        from catalog.documents import SynthesisParseJob

        return SynthesisParseJob(
            kind="experiment",
            recipe_auid=result["recipe_auid"],
            material_auid=result["material_auid"],
            trial_id=result["trial_id"],
            route_text=route,
            username=username,
            status="pending",
        ).save()

    def _cleanup(self, material_auid):
        from catalog.documents import MLEmbedding, Material, Recipe, SynthesisParseJob

        Recipe.objects(material_auid=material_auid).delete()
        Material.objects(id=material_auid).delete()
        MLEmbedding.objects(material_auid=material_auid).delete()
        SynthesisParseJob.objects(material_auid=material_auid).delete()

    def test_worker_rekeys_recipe_into_discrete_steps(self):
        from catalog.documents import Recipe, SynthesisParseJob
        from catalog import synthesis_worker

        route = f"Ball milled 12h at 300rpm then fired at 900C {uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                user, result = self._seed_other_step_trial(route)
                old_recipe_id = result["recipe_auid"]
                material_auid = result["material_auid"]
                job = self._enqueue(result, route, user.username)

                discretized = [
                    {"step_type": "ball_milling", "milling_time_hours": 12, "milling_rpm": 300},
                    {"step_type": "heat_treatment", "max_temp_c": 900},
                ]
                try:
                    with mock.patch.object(
                        synthesis_worker, "parse_synthesis_route", return_value=discretized
                    ), mock.patch.object(
                        synthesis_worker, "_resolve_user", return_value=user
                    ):
                        counts = synthesis_worker.process_pending_jobs(limit=5)

                    self.assertEqual(counts["done"], 1)
                    job.reload()
                    self.assertEqual(job.status, "done")
                    self.assertNotEqual(job.recipe_auid, old_recipe_id)

                    # Old single-"other" recipe is gone; new recipe has discrete steps.
                    self.assertIsNone(Recipe.objects(id=old_recipe_id).first())
                    new_recipe = Recipe.objects(id=job.recipe_auid).first()
                    self.assertIsNotNone(new_recipe)
                    types = [s.get("step_type") for s in new_recipe.synthesis_steps]
                    self.assertEqual(types, ["ball_milling", "heat_treatment"])
                    self.assertNotIn("other", types)
                    self.assertEqual(len(new_recipe.trials), 1)
                    self.assertEqual(new_recipe.trials[0].trial_id, result["trial_id"])
                finally:
                    self._cleanup(material_auid)

    def test_worker_retries_then_leaves_original_on_transient_failure(self):
        from catalog.documents import Recipe
        from catalog import synthesis_worker

        route = f"Fired at 900C {uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root, SYNTHESIS_LLM_MAX_ATTEMPTS=1):
                user, result = self._seed_other_step_trial(route)
                old_recipe_id = result["recipe_auid"]
                material_auid = result["material_auid"]
                job = self._enqueue(result, route, user.username)
                try:
                    with mock.patch.object(
                        synthesis_worker, "parse_synthesis_route",
                        side_effect=SynthesisParseUnavailable("api down"),
                    ), mock.patch.object(
                        synthesis_worker, "_resolve_user", return_value=user
                    ):
                        counts = synthesis_worker.process_pending_jobs(limit=5)

                    self.assertEqual(counts["failed"], 1)
                    job.reload()
                    self.assertEqual(job.status, "failed")
                    # Original recipe untouched: still a single "other" step.
                    recipe = Recipe.objects(id=old_recipe_id).first()
                    self.assertIsNotNone(recipe)
                    self.assertEqual(
                        [s.get("step_type") for s in recipe.synthesis_steps], ["other"]
                    )
                finally:
                    self._cleanup(material_auid)

    def test_worker_skips_when_model_returns_no_steps(self):
        from catalog.documents import Recipe
        from catalog import synthesis_worker

        route = f"Some vague description {uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                user, result = self._seed_other_step_trial(route)
                old_recipe_id = result["recipe_auid"]
                material_auid = result["material_auid"]
                job = self._enqueue(result, route, user.username)
                try:
                    with mock.patch.object(
                        synthesis_worker, "parse_synthesis_route", return_value=[]
                    ), mock.patch.object(
                        synthesis_worker, "_resolve_user", return_value=user
                    ):
                        counts = synthesis_worker.process_pending_jobs(limit=5)

                    self.assertEqual(counts["skipped"], 1)
                    job.reload()
                    self.assertEqual(job.status, "skipped")
                    self.assertIsNotNone(Recipe.objects(id=old_recipe_id).first())
                finally:
                    self._cleanup(material_auid)
