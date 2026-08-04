import os
import tempfile
import unittest

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.contrib.auth import get_user_model


# A LOOP-format CSV with one clear, well-resolved peak around 20.29 deg so the
# fast peak finder reliably returns at least one peak.
LOOP_CSV = (
    "Angle,Intensity\n"
    "20.0000,50.0\n20.0105,50.0\n20.0211,50.0\n20.0316,50.0\n20.0421,50.0\n"
    "20.0526,50.0\n20.0632,50.0\n20.0737,50.0\n20.0842,50.0\n20.0947,50.0\n"
    "20.1053,50.0\n20.1158,50.0\n20.1263,50.0\n20.1368,50.0\n20.1474,50.0\n"
    "20.1579,50.0\n20.1684,50.0\n20.1789,50.0\n20.1895,50.0\n20.2000,50.0\n"
    "20.2105,50.1\n20.2211,50.8\n20.2316,53.7\n20.2421,64.0\n20.2526,92.4\n"
    "20.2632,152.9\n20.2737,250.1\n20.2842,361.7\n20.2947,439.1\n20.3053,439.1\n"
    "20.3158,361.7\n20.3263,250.1\n20.3368,152.9\n20.3474,92.4\n20.3579,64.0\n"
    "20.3684,53.7\n20.3789,50.8\n20.3895,50.1\n20.4000,50.0\n20.4105,50.0\n"
    "20.4211,50.0\n20.4316,50.0\n20.4421,50.0\n20.4526,50.0\n20.4632,50.0\n"
    "20.4737,50.0\n20.4842,50.0\n20.4947,50.0\n20.5053,50.0\n20.5158,50.0\n"
    "20.5263,50.0\n20.5368,50.0\n20.5474,50.0\n20.5579,50.0\n20.5684,50.0\n"
    "20.5789,50.0\n20.5895,50.0\n20.6000,50.0\n"
)


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


@override_settings(EMBEDDINGS_ON_WRITE=False)
class XrdCacheApiRoutingTests(TestCase):
    """Routing / access-control checks that do not need MongoDB."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "apiuser", "api@example.com", "pass", is_staff=True, is_superuser=True
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_url_name_resolves(self):
        url = reverse(
            "trial_xrd_cache",
            kwargs={"recipe_id": "M:abc:R:def", "trial_id": "T1"},
        )
        self.assertEqual(url, "/api/recipe/M:abc:R:def/trial/T1/xrd-cache/")

    @unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
    def test_returns_json_404_for_unknown_recipe(self):
        resp = self.client.get("/api/recipe/M:0000:R:0000/trial/x/xrd-cache/")
        self.assertEqual(resp.status_code, 404)
        # JSON content type + payload proves our view ran (not Django's HTML 404).
        self.assertEqual(resp["Content-Type"], "application/json")
        self.assertEqual(resp.json()["error"], "not found")


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
@override_settings(EMBEDDINGS_ON_WRITE=False)
class XrdCacheApiSuccessTests(TestCase):
    """End-to-end success path through the endpoint -> read_manifest/get_or_build."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "apisuccess", "success@example.com", "pass", is_staff=True, is_superuser=True
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_success_returns_manifest(self):
        from datetime import datetime, timezone

        from catalog import xrd_store
        from catalog.documents import (
            ExpCondition,
            EmbeddedTrial,
            Material,
            Recipe,
            compute_material_auid,
            compute_recipe_auid,
        )

        elements = {"Co": 0.5, "Ni": 0.5, "O": 1.0}
        material_auid = compute_material_auid(elements, "rocksalt")
        recipe_auid = compute_recipe_auid(material_auid, [])
        trial_id = "1"

        media_root = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=media_root, MEDIA_URL="/media/"):
                # Write the raw scan into the unified per-trial store (keyed by recipe).
                stored = xrd_store.store_raw_file(
                    recipe_auid,
                    trial_id,
                    SimpleUploadedFile("scan.csv", LOOP_CSV.encode()),
                )

                Material(
                    id=material_auid,
                    elements=elements,
                    structure_family="rocksalt",
                    default_visibility_affiliations=["S4E"],
                ).save()

                trial = EmbeddedTrial(
                    trial_id=trial_id,
                    trial_date=datetime.now(timezone.utc),
                    phase_status="single_phase",
                    exp_condition=ExpCondition(),
                    file_hash=stored.sha256,
                    visibility_affiliations=["S4E"],
                )
                trial.exp_condition.additional_params = {"file_hash": stored.sha256}

                Recipe(
                    id=recipe_auid,
                    material_auid=material_auid,
                    elements=elements,
                    structure_family="rocksalt",
                    synthesis_steps=[],
                    trials=[trial],
                    visibility_affiliations=["S4E"],
                ).save()

                url = reverse(
                    "trial_xrd_cache",
                    kwargs={"recipe_id": recipe_auid, "trial_id": trial_id},
                )
                resp = self.client.get(url)

                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(resp["Content-Type"], "application/json")
                data = resp.json()
                self.assertIn("raw_url", data)
                self.assertTrue(data["raw_url"])
                self.assertIn("pattern_url", data)
                self.assertTrue(data["pattern_url"])
                self.assertIn("variants", data)
                self.assertIn("fast", data["variants"])
                self.assertTrue(data["peaks"], "expected a non-empty peaks list")
        finally:
            from catalog.documents import Material as _M, Recipe as _R
            import shutil

            _R.objects(id=recipe_auid).delete()
            _M.objects(id=material_auid).delete()
            shutil.rmtree(media_root, ignore_errors=True)
