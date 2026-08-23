"""The published Python samples, executed against a live LOOP server.

These are not unit tests of a client library. Each test runs the exact file a
researcher copies off the developer pages, as a subprocess, over real HTTP —
so a sample that stops working (or drifts from the documentation that renders
it) fails here rather than in someone's lab notebook.
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import Client, LiveServerTestCase, TestCase, override_settings

from catalog.api import code_samples
from catalog.documents import Material, Recipe
from catalog.models import APIKey
from catalog.raw_db import RawFile
from catalog.tests.mongo_guard import mongo_reachable


SAMPLES_DIR = code_samples.SAMPLES_DIR

# Two columns with a header row — the shape LOOP's XRD parser expects.
def _pattern_csv(offset=0.0):
    rows = ["Angle,Intensity"]
    for step in range(40):
        angle = 10.0 + step * 0.5 + offset
        rows.append(f"{angle:.2f},{100 + step * 3}")
    return ("\n".join(rows) + "\n").encode("utf-8")


def _first_json_object(text):
    """Return the first JSON object printed by a sample."""
    start = text.index("{")
    return json.JSONDecoder().raw_decode(text[start:])[0]


# The throttles count in the default cache, which is file-backed and outlives
# the process. Give this class its own so repeated runs do not spend each
# other's budget. See the same note in test_api_v1.py.
ISOLATED_THROTTLE_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "api-python-sample-tests",
    }
}


@skipUnless(mongo_reachable(), "MongoDB is not reachable")
class PythonSampleExecutionTests(LiveServerTestCase):
    """Run each documented sample end to end against a real server."""

    @classmethod
    def setUpClass(cls):
        cls._media_dir = tempfile.mkdtemp(prefix="loop_samples_media_")
        # Enabled before super() so the live server thread starts with them.
        cls._overrides = override_settings(
            MEDIA_ROOT=cls._media_dir,
            EMBEDDINGS_ON_WRITE=False,
            RAW_UPLOADS_ROOT="",
            CACHES=ISOLATED_THROTTLE_CACHE,
        )
        cls._overrides.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._overrides.disable()
        shutil.rmtree(cls._media_dir, ignore_errors=True)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "sample-runner", "sample-runner@example.com", "pass", is_staff=True
        )
        _, self.raw_key = APIKey.issue(
            user=self.user,
            name="Documented Python samples",
            scopes=["data:read", "data:write", "files:write", "imports:write"],
        )
        self.workdir = tempfile.mkdtemp(prefix="loop_samples_work_")
        # Mongo has no per-test rollback: every test in the run shares one
        # database. Remember what existed before, so teardown can remove what
        # this test added without guessing from chemistry — deleting by element
        # would take other suites' fixtures with it (the pagination fixture in
        # test_api_v1 is built from Ho, Er, Dy, Yb and Lu).
        self._materials_before = self._ids(Material)
        self._recipes_before = self._ids(Recipe)

    @staticmethod
    def _ids(document):
        return {doc.id for doc in document.objects.only("id")}

    def tearDown(self):
        shutil.rmtree(self.workdir, ignore_errors=True)
        new_recipes = self._ids(Recipe) - self._recipes_before
        if new_recipes:
            Recipe.objects(id__in=list(new_recipes)).delete()
        new_materials = self._ids(Material) - self._materials_before
        if new_materials:
            Material.objects(id__in=list(new_materials)).delete()
        # An upload also registers the pattern in the raw-file database.
        RawFile.objects(uploaded_by=self.user.username).delete()

    def run_sample(self, name, env=None, expect_success=True):
        """Run one sample the way a researcher would, and return its output."""
        sample_env = dict(os.environ)
        sample_env.update(
            {
                # No PYTHONPATH: each sample is self-contained, so it must run
                # from a bare directory exactly as a downloaded copy would.
                "LOOP_API_BASE_URL": f"{self.live_server_url}/api/v1",
                "LOOP_API_KEY": self.raw_key,
            }
        )
        sample_env.pop("PYTHONPATH", None)
        sample_env.update(env or {})
        result = subprocess.run(
            [sys.executable, str(SAMPLES_DIR / f"{name}.py")],
            capture_output=True,
            text=True,
            env=sample_env,
            cwd=self.workdir,
            timeout=180,
        )
        if expect_success:
            self.assertEqual(
                result.returncode,
                0,
                msg=f"{name}.py failed:\n{result.stdout}\n{result.stderr}",
            )
        else:
            self.assertNotEqual(result.returncode, 0, msg=result.stdout)
        return result

    def write_pattern(self, filename="pattern.csv", offset=0.0):
        path = Path(self.workdir) / filename
        path.write_bytes(_pattern_csv(offset))
        return path

    def test_create_experiment_sample_writes_a_trial_that_read_sample_finds(self):
        created = self.run_sample("create_experiment")
        identity = _first_json_object(created.stdout)
        self.assertTrue(identity["material_auid"].startswith("M:"))

        listed = self.run_sample("read_materials")

        self.assertIn(identity["material_auid"], listed.stdout)
        self.assertIn("visible materials", listed.stdout)

    def test_validate_sample_accepts_the_documented_record_without_writing(self):
        before = Material.objects.count()

        result = self.run_sample("validate_record")

        self.assertIn("Accepted", result.stdout)
        # What matters is that this sample wrote nothing — not that the shared
        # database is empty, which it never is by this point in a full run.
        self.assertEqual(Material.objects.count(), before)

    def test_pagination_sample_walks_the_whole_result_set(self):
        self.run_sample("create_experiment")

        result = self.run_sample("paginate_materials")

        self.assertIn("Walked", result.stdout)
        self.assertRegex(result.stdout, r"Walked [1-9]\d* materials")

    def test_minimal_xrd_sample_uploads_a_pattern(self):
        self.write_pattern()

        result = self.run_sample("upload_xrd_minimal")
        identity = _first_json_object(result.stdout)

        recipe = Recipe.objects(id=identity["recipe_auid"]).first()
        self.assertIsNotNone(recipe)
        trial = next(t for t in recipe.trials if t.trial_id == identity["trial_id"])
        self.assertEqual(trial.raw_data_type, "xrd")
        self.assertTrue(trial.file_hash)

    def test_full_xrd_sample_validates_uploads_and_reads_the_pattern_back(self):
        self.write_pattern(offset=0.01)

        result = self.run_sample("upload_xrd_experiment")

        self.assertIn("Stored trial:", result.stdout)
        self.assertIn("Parsed XRD metadata:", result.stdout)
        identity = _first_json_object(result.stdout)
        recipe = Recipe.objects(id=identity["recipe_auid"]).first()
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe.material_auid, identity["material_auid"])

    def test_download_sample_retrieves_the_bytes_that_were_uploaded(self):
        source = self.write_pattern(offset=0.02)
        uploaded = self.run_sample("upload_xrd_minimal")
        identity = _first_json_object(uploaded.stdout)

        result = self.run_sample(
            "download_xrd",
            env={
                "LOOP_RECIPE_AUID": identity["recipe_auid"],
                "LOOP_TRIAL_ID": identity["trial_id"],
                "LOOP_XRD_OUT": "downloaded.csv",
            },
        )

        downloaded = Path(self.workdir) / "downloaded.csv"
        self.assertEqual(downloaded.read_bytes(), source.read_bytes())
        self.assertIn("Wrote", result.stdout)

    def test_reuploading_the_same_pattern_is_refused_with_a_readable_message(self):
        self.write_pattern(offset=0.03)
        self.run_sample("upload_xrd_experiment")

        repeated = self.run_sample("upload_xrd_experiment", expect_success=False)

        # The sample turns LOOP's 409 into a sentence, not a traceback.
        self.assertIn("Already in LOOP", repeated.stderr)

    def test_batch_import_sample_dry_runs_before_committing(self):
        records = [
            {
                "elements": {"Er": 2, "Ti": 2, "O": 7},
                "structure_family": "pyrochlore",
                "phase_status": "single_phase",
                "synthesis_steps": [
                    {"step_type": "heat_treatment", "max_temp_c": 1400, "hold_time_hours": 4}
                ],
            },
            {
                "elements": {"Er": 2, "Zr": 2, "O": 7},
                "structure_family": "pyrochlore",
                "phase_status": "multi_phase",
                "synthesis_steps": [
                    {"step_type": "heat_treatment", "max_temp_c": 1450, "hold_time_hours": 6}
                ],
            },
        ]
        import_file = Path(self.workdir) / "experiments.jsonl"
        import_file.write_text("\n".join(json.dumps(row) for row in records) + "\n")
        # Counting Er materials across the whole database is not this test's to
        # assert: test_api_v1's pagination fixture is built from Er too, so a
        # stray record fails this for reasons unrelated to the import. Measure
        # what the sample added instead.
        before = Material.objects.count()

        result = self.run_sample(
            "batch_import", env={"LOOP_IMPORT_FILE": str(import_file)}
        )

        self.assertIn("Dry run:", result.stdout)
        self.assertIn("2 rows: 0 created, 2 validated, 0 failed", result.stdout)
        self.assertIn("2 rows: 2 created", result.stdout)
        self.assertEqual(Material.objects.count() - before, 2)

    def test_a_missing_key_fails_with_an_instruction_rather_than_a_traceback(self):
        result = self.run_sample(
            "read_materials", env={"LOOP_API_KEY": ""}, expect_success=False
        )

        self.assertIn("Set LOOP_API_KEY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class PythonSampleDocumentationTests(TestCase):
    """The published pages and Markdown must show the sources that were tested."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "sample-reader", "sample-reader@example.com", "pass", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_every_sample_is_readable_and_renders_with_the_deployment_base_url(self):
        for name in code_samples.PYTHON_SAMPLES:
            rendered = code_samples.render_sample(name, "https://loop.example.org/api/v1")
            self.assertNotIn(code_samples.PLACEHOLDER_BASE_URL, rendered)

    def test_unknown_sample_names_are_rejected(self):
        with self.assertRaises(KeyError):
            code_samples.sample_source("../../settings")

    def test_developer_pages_render_the_sample_sources(self):
        portal = self.client.get("/developers/")
        guide = self.client.get("/developers/guide/")

        self.assertEqual(portal.status_code, 200)
        self.assertEqual(guide.status_code, 200)
        # A distinctive line from the shared client and from the XRD upload.
        # Plain substrings only: the templates HTML-escape quotes and braces.
        self.assertContains(portal, "Filters LOOP applied")
        self.assertContains(guide, "Fix the record before uploading the pattern.")
        # The rendered pages must point at this deployment, not the placeholder.
        self.assertContains(portal, "http://testserver/api/v1")
        self.assertNotContains(portal, code_samples.PLACEHOLDER_BASE_URL)

    def test_pages_offer_both_languages_for_the_xrd_upload(self):
        guide = self.client.get("/developers/guide/")

        self.assertContains(guide, 'data-language="curl"')
        self.assertContains(guide, 'data-language="Python"')
        self.assertContains(guide, "csv_file=@pattern.csv")

    def test_every_sample_is_self_contained_and_stdlib_only(self):
        """One file, no helper module, no pip install — or it is not copy-paste.

        The whole point of these examples is that a reader saves a single file
        and runs it. A shared helper reintroduces a download step, and a
        third-party import reintroduces an install step.
        """
        stdlib_only = {
            "json", "os", "sys", "time", "uuid", "csv", "mimetypes",
            "pathlib", "urllib", "urllib.error", "urllib.parse",
            "urllib.request", "datetime", "hashlib",
        }
        for name in code_samples.PYTHON_SAMPLES:
            source = code_samples.sample_source(name)
            tree = ast.parse(source, filename=f"{name}.py")
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module or "")
            unexpected = imported - stdlib_only
            self.assertEqual(
                unexpected, set(), msg=f"{name}.py imports {unexpected}"
            )

    def test_every_sample_downloads_as_a_python_file_without_a_login(self):
        anonymous = Client()

        for name in code_samples.PYTHON_SAMPLES:
            response = anonymous.get(f"/developers/samples/{name}.py")

            self.assertEqual(response.status_code, 200, msg=name)
            self.assertEqual(response["Content-Type"], "text/x-python; charset=utf-8")
            self.assertIn(f'filename="{name}.py"', response["Content-Disposition"])
            body = response.content.decode()
            # Served with this deployment's base URL, exactly as the pages render it.
            self.assertNotIn(code_samples.PLACEHOLDER_BASE_URL, body)
            self.assertEqual(body, code_samples.sample_source(name).replace(
                code_samples.PLACEHOLDER_BASE_URL, "http://testserver/api/v1"
            ))

    def test_an_unknown_sample_name_is_a_404_not_a_traceback(self):
        # The route only accepts a bare name, so a traversal attempt cannot even
        # match it. An unknown but well-formed name must still 404 rather than
        # raise, since the loader reads from disk by name.
        response = Client().get("/developers/samples/not_a_published_sample.py")

        self.assertEqual(response.status_code, 404)

    def test_markdown_guide_embeds_the_sample_sources_verbatim(self):
        markdown = Path(SAMPLES_DIR).parents[2] / "docs" / "API.md"
        text = markdown.read_text(encoding="utf-8")

        for name in ("read_materials", "upload_xrd_experiment"):
            self.assertIn(
                code_samples.sample_source(name).rstrip("\n"),
                text,
                msg=f"docs/API.md has drifted from {name}.py",
            )
