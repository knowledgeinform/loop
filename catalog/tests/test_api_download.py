"""Tests for the API directory-download zip builders and endpoints."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
import uuid
import zipfile
from datetime import datetime, timezone

from django.contrib.auth.models import AnonymousUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, RequestFactory, override_settings

from catalog import api_download, views, xrd_store
from catalog.documents import (
    EmbeddedDFT,
    EmbeddedLiterature,
    EmbeddedTrial,
    ExpCondition,
    Material,
    Recipe,
)
from catalog.raw_db import RawFile
from catalog.tests.test_xrd_store import LOOP_CSV


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


def _names(zip_source):
    """Accept a file object or raw bytes and return the archive's member names."""
    if isinstance(zip_source, bytes):
        zip_source = io.BytesIO(zip_source)
    return set(zipfile.ZipFile(zip_source).namelist())


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class ApiDownloadBuilderTests(SimpleTestCase):
    databases = {}  # Mongo only, not the Django DB

    def setUp(self):
        suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:testDl{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:r{suffix}"
        Material(
            id=self.material_auid,
            elements={"Za": 1, "Zb": 1},
            structure_family="rocksalt",
            default_visibility_affiliations=["S4E"],
            dft_calculations=[
                EmbeddedDFT(comp_auid=f"{self.material_auid}:C:c{suffix}",
                            dft_bandgap_ev=1.2, visibility_affiliations=["S4E"]),
            ],
        ).save()
        Recipe(
            id=self.recipe_auid,
            material_auid=self.material_auid,
            elements={"Za": 1, "Zb": 1},
            structure_family="rocksalt",
            synthesis_steps=[{"step_type": "grind"}],
            trials=[
                EmbeddedTrial(trial_id="t1",
                              trial_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                              exp_condition=ExpCondition(), phase_status="single_phase",
                              visibility_affiliations=["S4E"]),
            ],
            literature=[
                EmbeddedLiterature(lit_id="L:abc", doi="10.1/x",
                                   exp_condition=ExpCondition(),
                                   visibility_affiliations=["S4E"]),
            ],
            visibility_affiliations=["S4E"],
        ).save()

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()

    def test_build_trial_zip_no_raw_omits_raw_folder(self):
        result = api_download.build_trial_zip(self.recipe_auid, "t1", ["S4E"])
        self.assertIsNotNone(result)
        zip_bytes, filename = result
        names = _names(zip_bytes)
        self.assertIn("manifest.json", names)
        self.assertIn("trial.json", names)
        self.assertFalse(any(n.startswith("raw/") for n in names))
        self.assertTrue(filename.endswith(".zip"))
        self.assertNotIn(":", filename)

    def test_build_trial_zip_with_raw_file_includes_derived_artifacts(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                xrd_store.store_raw_file(
                    self.recipe_auid, "t1", SimpleUploadedFile("scan.csv", LOOP_CSV.encode())
                )
                # Downloads are cache-only, so build the artifacts first.
                xrd_store.get_or_build(self.recipe_auid, "t1", None)
                result = api_download.build_trial_zip(self.recipe_auid, "t1", ["S4E"])
                self.assertIsNotNone(result)
                zip_bytes, _filename = result
                names = _names(zip_bytes)
                self.assertIn("manifest.json", names)
                self.assertIn("trial.json", names)
                self.assertIn("raw/raw.csv", names)
                self.assertIn("raw/pattern.csv", names)
                self.assertIn("raw/overlay.png", names)
                self.assertIn("raw/peaks.json", names)

    def test_build_trial_zip_with_raw_file_prefers_original_filename(self):
        file_hash = "feed" * 16  # 64 hex chars
        RawFile.objects(id=file_hash).delete()
        self.addCleanup(lambda: RawFile.objects(id=file_hash).delete())
        RawFile(id=file_hash, original_filename="scan.csv").save()

        recipe = Recipe.objects(id=self.recipe_auid).first()
        recipe.trials[0].exp_condition.additional_params = {"file_hash": file_hash}
        recipe.save()

        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                xrd_store.store_raw_file(
                    self.recipe_auid, "t1", SimpleUploadedFile("scan.csv", LOOP_CSV.encode())
                )
                result = api_download.build_trial_zip(self.recipe_auid, "t1", ["S4E"])
                self.assertIsNotNone(result)
                zip_bytes, _filename = result
                names = _names(zip_bytes)
                self.assertIn("raw/scan.csv", names)
                self.assertNotIn("raw/raw.csv", names)

    def test_build_trial_zip_sanitizes_malicious_original_filename(self):
        file_hash = "beef" * 16  # 64 hex chars
        RawFile.objects(id=file_hash).delete()
        self.addCleanup(lambda: RawFile.objects(id=file_hash).delete())
        RawFile(id=file_hash, original_filename="../../evil.csv").save()

        recipe = Recipe.objects(id=self.recipe_auid).first()
        recipe.trials[0].exp_condition.additional_params = {"file_hash": file_hash}
        recipe.save()

        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                xrd_store.store_raw_file(
                    self.recipe_auid, "t1", SimpleUploadedFile("scan.csv", LOOP_CSV.encode())
                )
                result = api_download.build_trial_zip(self.recipe_auid, "t1", ["S4E"])
                self.assertIsNotNone(result)
                zip_bytes, _filename = result
                names = _names(zip_bytes)
                self.assertIn("raw/evil.csv", names)
                self.assertFalse(any(".." in n for n in names))

    def test_build_trial_zip_missing_trial_returns_none(self):
        self.assertIsNone(api_download.build_trial_zip(self.recipe_auid, "nope", ["S4E"]))

    def test_build_trial_zip_not_visible_returns_none(self):
        self.assertIsNone(api_download.build_trial_zip(self.recipe_auid, "t1", ["APL"]))

    def test_build_recipe_zip_has_recipe_and_trials(self):
        result = api_download.build_recipe_zip(self.recipe_auid, ["S4E"])
        self.assertIsNotNone(result)
        zip_bytes, filename = result
        names = _names(zip_bytes)
        self.assertIn("manifest.json", names)
        self.assertIn("recipe.json", names)
        self.assertTrue(any(n.startswith("trials/t1/") for n in names))
        self.assertIn("trials/t1/trial.json", names)
        self.assertTrue(filename.startswith("recipe_"))

    def test_build_composition_zip_nests_recipes(self):
        result = api_download.build_composition_zip(self.material_auid, ["S4E"])
        self.assertIsNotNone(result)
        zip_bytes, filename = result
        names = _names(zip_bytes)
        self.assertIn("manifest.json", names)
        self.assertIn("material.json", names)
        self.assertTrue(any(n.startswith("recipes/") and n.endswith("/recipe.json") for n in names))
        self.assertTrue(any("/trials/t1/trial.json" in n for n in names))
        self.assertTrue(filename.startswith("composition_"))

    def test_composition_hidden_when_no_visible_children(self):
        self.assertIsNone(api_download.build_composition_zip(self.material_auid, ["APL"]))

    def test_missing_material_returns_none(self):
        self.assertIsNone(api_download.build_composition_zip("M:doesnotexist", ["S4E"]))


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class ApiDownloadViewTests(SimpleTestCase):
    databases = {}

    def setUp(self):
        self.factory = RequestFactory()
        suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:testDlv{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:r{suffix}"
        Material(id=self.material_auid, elements={"Za": 1, "Zb": 1},
                 structure_family="rocksalt",
                 default_visibility_affiliations=["S4E"]).save()
        Recipe(id=self.recipe_auid, material_auid=self.material_auid,
               elements={"Za": 1, "Zb": 1}, structure_family="rocksalt",
               trials=[EmbeddedTrial(trial_id="t1",
                                     trial_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
                                     exp_condition=ExpCondition(),
                                     phase_status="single_phase",
                                     visibility_affiliations=["S4E"])],
               literature=[], visibility_affiliations=["S4E"]).save()

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()

    def _get(self, view, **kwargs):
        request = self.factory.get("/api/")
        request.user = AnonymousUser()
        return view(request, **kwargs)

    def test_composition_download_headers_and_zip(self):
        resp = self._get(views.composition_download, material_auid=self.material_auid)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(".zip", resp["Content-Disposition"])
        self.assertIn("material.json", _names(b"".join(resp.streaming_content)))

    def test_trial_download_ok(self):
        resp = self._get(views.trial_download,
                         recipe_id=self.recipe_auid, trial_id="t1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("trial.json", _names(b"".join(resp.streaming_content)))

    def test_recipe_download_ok(self):
        resp = self._get(views.recipe_download, recipe_id=self.recipe_auid)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("recipe.json", _names(b"".join(resp.streaming_content)))

    def test_download_malformed_auid_404(self):
        resp = self._get(views.composition_download, material_auid="not-an-auid")
        self.assertEqual(resp.status_code, 404)

    def test_download_missing_material_404(self):
        resp = self._get(views.composition_download, material_auid="M:missing123456")
        self.assertEqual(resp.status_code, 404)


