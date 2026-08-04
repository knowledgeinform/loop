"""Tests for the composition/recipe/trial read APIs (requires live MongoDB)."""

from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timezone

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase, RequestFactory

from catalog import views
from catalog.documents import EmbeddedTrial, ExpCondition, Material, Recipe


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


def _json(response):
    return json.loads(response.content.decode())


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class ApiReadEndpointsTests(SimpleTestCase):
    databases = {}  # Mongo only, not the Django DB

    def setUp(self):
        self.factory = RequestFactory()
        suffix = uuid.uuid4().hex[:10]
        # Uncommon element symbols so the composition search isolates this material.
        self.el_a = f"Za{suffix[:4]}"
        self.el_b = f"Zb{suffix[:4]}"
        self.material_auid = f"M:testApiRead{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:test{suffix}"
        Material(
            id=self.material_auid,
            elements={self.el_a: 1, self.el_b: 1},
            structure_family="rocksalt",
            default_visibility_affiliations=["S4E"],
        ).save()
        Recipe(
            id=self.recipe_auid,
            material_auid=self.material_auid,
            elements={self.el_a: 1, self.el_b: 1},
            structure_family="rocksalt",
            synthesis_steps=[{"step_type": "grind"}],
            trials=[
                EmbeddedTrial(trial_id="ta", trial_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                              exp_condition=ExpCondition(), phase_status="single_phase",
                              visibility_affiliations=["S4E"]),
                EmbeddedTrial(trial_id="tb", trial_date=datetime(2024, 2, 1, tzinfo=timezone.utc),
                              exp_condition=ExpCondition(), phase_status="multi_phase",
                              visibility_affiliations=["S4E"]),
            ],
            literature=[],
            visibility_affiliations=["S4E"],
        ).save()

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()

    def _get(self, view, **params):
        request = self.factory.get("/api/", params)
        request.user = AnonymousUser()
        return view(request)

    # --- search-composition --------------------------------------------------
    def test_search_composition_finds_material(self):
        data = _json(self._get(views.search_composition_api,
                               elements=f"{self.el_a},{self.el_b}"))
        auids = [r["material_auid"] for r in data["results"]]
        self.assertIn(self.material_auid, auids)
        row = next(r for r in data["results"] if r["material_auid"] == self.material_auid)
        self.assertEqual(row["recipe_count"], 1)
        self.assertEqual(row["trial_count"], 2)

    def test_search_composition_requires_elements(self):
        resp = self._get(views.search_composition_api, elements="")
        self.assertEqual(resp.status_code, 400)

    # --- composition-recipes -------------------------------------------------
    def test_composition_recipes_lists_recipe(self):
        data = _json(self._get(views.composition_recipes_api,
                               material_auid=self.material_auid))
        self.assertTrue(data["exists"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["recipes"][0]["recipe_auid"], self.recipe_auid)
        self.assertEqual(data["recipes"][0]["trial_count"], 2)

    def test_composition_recipes_missing_material(self):
        resp = self._get(views.composition_recipes_api, material_auid="M:doesnotexist")
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(_json(resp)["exists"])

    # --- recipe-trials -------------------------------------------------------
    def test_recipe_trials_returns_ids(self):
        data = _json(self._get(views.recipe_trials_api, recipe_id=self.recipe_auid))
        self.assertTrue(data["exists"])
        self.assertEqual(data["count"], 2)
        self.assertEqual(sorted(data["trial_ids"]), ["ta", "tb"])
        phases = {t["trial_id"]: t["phase_status"] for t in data["trials"]}
        self.assertEqual(phases, {"ta": "single_phase", "tb": "multi_phase"})

    def test_recipe_trials_missing_recipe(self):
        resp = self._get(views.recipe_trials_api, recipe_id="M:x:R:y")
        self.assertEqual(resp.status_code, 404)
