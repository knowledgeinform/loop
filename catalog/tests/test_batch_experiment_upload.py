import io
import os
import tempfile
import unittest
import uuid
import zipfile

from django.core.files.storage import default_storage
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.client import RequestFactory

from catalog.services.batch_experiment_upload import (
    ManifestRow,
    build_preview,
    build_synthesis_steps,
    generate_manifest_template_csv,
    normalize_batch_id,
    parse_composition_formula,
    parse_manifest_csv,
    resolve_phase_status,
    resolve_raw_data_type,
    scan_xrd_zip,
)


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


class _FakeUser:
    is_authenticated = True

    def __init__(self, username="batchtester", uid=987654):
        self.username = username
        self.id = uid

    def get_username(self):
        return self.username


class BatchExperimentUploadTests(TestCase):
    def test_normalize_batch_id(self):
        self.assertEqual(normalize_batch_id("SP15M-24"), "SP15M-024")
        self.assertEqual(normalize_batch_id("SP15M-0024"), "SP15M-024")
        self.assertEqual(normalize_batch_id("SP15M_24"), "SP15M-024")

    def test_parse_subscript_formula(self):
        elements = parse_composition_formula("(Co₀.₂Cu₀.₂Mg₀.₂Ni₀.₂Zn₀.₂)O")

        self.assertAlmostEqual(elements["Co"], 0.2)
        self.assertAlmostEqual(elements["Cu"], 0.2)
        self.assertAlmostEqual(elements["Mg"], 0.2)
        self.assertAlmostEqual(elements["Ni"], 0.2)
        self.assertAlmostEqual(elements["Zn"], 0.2)
        self.assertAlmostEqual(elements["O"], 1.0)

    def test_scan_xrd_zip_groups_files_by_normalized_folder(self):
        archive = io.BytesIO()

        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("SP15M-0024/SP15M-24-1300.asc", "10 100\n11 120\n")
            zf.writestr("SP15M-0024/SP15M-24-1300.raw", "raw data")

        archive.seek(0)

        folders = scan_xrd_zip(archive)

        self.assertIn("SP15M-024", folders)
        self.assertEqual(len(folders["SP15M-024"]), 2)

    def test_scan_xrd_zip_batch_id_from_filename(self):
        """Files named by batch ID (SP15M-001.txt) with no per-batch subfolder —
        the common MIT-style export — must still be grouped, whether zipped flat
        or inside a single container folder. Regression: these were dropped."""
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("SP15M-001.txt", "10 100\n")               # flat root
            zf.writestr("MIT_XRDs/SP15M-002.txt", "10 100\n")      # container folder
            zf.writestr("__MACOSX/._SP15M-001.txt", "junk")        # macOS cruft
            zf.writestr("MIT_XRDs/.DS_Store", "junk")              # unsupported ext
        archive.seek(0)

        folders = scan_xrd_zip(archive)

        self.assertEqual(set(folders), {"SP15M-001", "SP15M-002"})
        self.assertEqual([f.filename for f in folders["SP15M-001"]], ["SP15M-001.txt"])
        self.assertEqual([f.filename for f in folders["SP15M-002"]], ["SP15M-002.txt"])

    def test_parse_manifest_headers_case_insensitive(self):
        """Header matching must be case-insensitive so a real spreadsheet with
        "Target Composition"/"TARGET COMPOSITION" still populates composition.
        Regression: batch IDs parsed but composition came back empty."""
        for header in (
            "Batch ID,Target Composition",
            "BATCH ID,TARGET COMPOSITION",
            "batch id,composition",
        ):
            csv_text = f"{header}\nSP15M-001,(Co0.5Ni0.5)O\n"
            rows = parse_manifest_csv(io.BytesIO(csv_text.encode("utf-8")))
            self.assertIn("SP15M-001", rows, header)
            self.assertEqual(
                rows["SP15M-001"].target_composition, "(Co0.5Ni0.5)O", header
            )

    def test_preview_with_manifest_ready_item(self):
        rows = {
            "SP15M-024": ManifestRow(
                batch_id="SP15M-024",
                material_name="Co-Cu-Mg-Mn-Zn",
                target_composition="(Co₀.₂Cu₀.₂Mg₀.₂Mn₀.₂Zn₀.₂)O",
                synthesis_route="Test synthesis route",
                xrd_status="XRD completed.",
            )
        }

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("SP15M-0024/SP15M-24-1300.asc", "10 100\n")
        archive.seek(0)

        folders = scan_xrd_zip(archive)

        preview = build_preview(
            manifest_rows=rows,
            folders=folders,
            zip_only=False,
        )

        self.assertEqual(preview.error_count, 0)
        self.assertEqual(preview.valid_count, 1)
        self.assertEqual(preview.items[0].primary_file.extension, ".asc")

    def test_preview_missing_composition_is_error(self):
        rows = {
            "SP15M-001": ManifestRow(
                batch_id="SP15M-001",
                target_composition="",
                synthesis_route="route",
            )
        }

        preview = build_preview(
            manifest_rows=rows,
            folders={},
            zip_only=False,
        )

        self.assertEqual(preview.error_count, 1)
        self.assertIn("Missing target composition.", preview.items[0].errors)

    def test_zip_only_preview_does_not_require_manifest(self):
        archive = io.BytesIO()

        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("SP15M-001/SP15M-001.txt", "metadata")

        archive.seek(0)

        folders = scan_xrd_zip(archive)

        preview = build_preview(
            manifest_rows={},
            folders=folders,
            zip_only=True,
        )

        self.assertEqual(preview.error_count, 0)
        self.assertEqual(preview.total_folders, 1)

    def test_generate_manifest_template(self):
        archive = io.BytesIO()

        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("SP15M-001/SP15M-001.txt", "metadata")
            zf.writestr("SP15M-024/SP15M-24-1300.asc", "10 100\n")

        archive.seek(0)

        folders = scan_xrd_zip(archive)

        preview = build_preview(
            manifest_rows={},
            folders=folders,
            zip_only=True,
        )

        csv_text = generate_manifest_template_csv(preview)

        self.assertIn("Batch ID", csv_text)
        self.assertIn("SP15M-001", csv_text)
        self.assertIn("SP15M-024", csv_text)


class ExpandedManifestTests(SimpleTestCase):
    def test_parse_manifest_csv_reads_expanded_columns(self):
        csv_text = (
            "Batch ID,Target composition,Phase status,Spacegroup,Raw data type,"
            "Milling time (h),Milling rpm,Atmosphere,Temp profile,Cooling method,Notes\n"
            "SP15M-001,(Co0.5Ni0.5)O,single_phase,Fm-3m,xrd,"
            "12,300,Ar,900C/12h,furnace_cool,looks good\n"
        )
        rows = parse_manifest_csv(io.BytesIO(csv_text.encode("utf-8")))
        row = rows["SP15M-001"]

        self.assertEqual(row.phase_status, "single_phase")
        self.assertEqual(row.spacegroup, "Fm-3m")
        self.assertEqual(row.raw_data_type, "xrd")
        self.assertEqual(row.milling_time_hours, "12")
        self.assertEqual(row.milling_rpm, "300")
        self.assertEqual(row.atmosphere, "Ar")
        self.assertEqual(row.temp_profile, "900C/12h")
        self.assertEqual(row.cooling_method, "furnace_cool")
        self.assertEqual(row.notes, "looks good")

    def test_build_synthesis_steps_typed(self):
        row = ManifestRow(
            batch_id="SP15M-001",
            target_composition="(Co0.5Ni0.5)O",
            synthesis_route="Mixed and fired",
            milling_time_hours="12",
            milling_rpm="300",
            atmosphere="Ar",
            temp_profile="900C/12h",
            cooling_method="furnace_cool",
        )
        steps, errors = build_synthesis_steps(row)
        self.assertEqual(errors, [])

        by_type = {s["step_type"]: s for s in steps}
        self.assertIn("ball_milling", by_type)
        self.assertEqual(by_type["ball_milling"]["milling_time_hours"], 12.0)
        self.assertEqual(by_type["ball_milling"]["milling_rpm"], 300.0)
        self.assertEqual(by_type["ball_milling"]["atmosphere"], "Ar")

        self.assertIn("heat_treatment", by_type)
        self.assertEqual(by_type["heat_treatment"]["max_temp_c"], 900.0)
        self.assertEqual(by_type["heat_treatment"]["hold_time_hours"], 12.0)

        self.assertIn("cooling", by_type)
        self.assertEqual(by_type["cooling"]["cooling_method"], "furnace_cool")

        self.assertIn("other", by_type)
        self.assertEqual(by_type["other"]["description"], "Mixed and fired")

    def test_build_synthesis_steps_empty_when_no_data(self):
        steps, errors = build_synthesis_steps(ManifestRow(batch_id="SP15M-001"))
        self.assertEqual(steps, [])
        self.assertEqual(errors, [])

    def test_resolve_phase_status(self):
        self.assertEqual(
            resolve_phase_status(ManifestRow(batch_id="x", phase_status="multi_phase")),
            "multi_phase",
        )
        self.assertEqual(
            resolve_phase_status(ManifestRow(batch_id="x", xrd_status="single phase confirmed")),
            "single_phase",
        )
        self.assertEqual(
            resolve_phase_status(ManifestRow(batch_id="x", xrd_status="multiphase mixture")),
            "multi_phase",
        )
        self.assertEqual(
            resolve_phase_status(ManifestRow(batch_id="x", xrd_status="XRD completed.")),
            "not_confirmed",
        )

    def test_resolve_raw_data_type(self):
        from catalog.services.batch_experiment_upload import FolderFile

        asc = FolderFile(folder="x", filename="a.asc", zip_path="x/a.asc", extension=".asc", size_bytes=1)
        self.assertEqual(resolve_raw_data_type(ManifestRow(batch_id="x"), asc), "xrd")
        self.assertEqual(
            resolve_raw_data_type(ManifestRow(batch_id="x", raw_data_type="sem"), asc), "sem"
        )
        self.assertIsNone(resolve_raw_data_type(ManifestRow(batch_id="x"), None))

    def test_parse_xlsx_manifest(self):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Manifest"
        ws.append(["Batch ID", "Target composition", "Phase status", "Milling time (h)"])
        ws.append(["SP15M-009", "(Co0.5Ni0.5)O", "single_phase", 12])
        # A second sheet ensures the parser selects the one with the key header.
        info = wb.create_sheet("Instructions")
        info.append(["Batch ID is required."])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        rows = parse_manifest_csv(buf)
        self.assertIn("SP15M-009", rows)
        self.assertEqual(rows["SP15M-009"].target_composition, "(Co0.5Ni0.5)O")
        self.assertEqual(rows["SP15M-009"].phase_status, "single_phase")
        self.assertEqual(rows["SP15M-009"].milling_time_hours, "12")

    def test_load_manifest_text_rejects_legacy_xls(self):
        from catalog.services.batch_experiment_upload import load_manifest_text

        with self.assertRaises(ValueError):
            load_manifest_text(io.BytesIO(b"\xd0\xcf\x11\xe0legacy"), "Batch ID")

    def test_template_has_expanded_headers(self):
        preview = build_preview(manifest_rows={}, folders={}, zip_only=True)
        csv_text = generate_manifest_template_csv(preview)
        for header in ("Phase status", "Spacegroup", "Raw data type", "Milling time (h)", "Cooling method"):
            self.assertIn(header, csv_text)

    def test_preview_flags_invalid_structure_family(self):
        rows = {
            "SP15M-001": ManifestRow(
                batch_id="SP15M-001",
                target_composition="(Co0.5Ni0.5)O",
                structure_family="not-a-family",
            )
        }
        preview = build_preview(manifest_rows=rows, folders={}, zip_only=False)
        self.assertEqual(preview.error_count, 1)


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
@override_settings(EMBEDDINGS_ON_WRITE=False)
class CommitBatchIntegrationTests(SimpleTestCase):
    databases = set()

    def _make_inputs(self, batch_id="SP15M-777"):
        manifest = (
            "Batch ID,Target composition,Phase status,Synthesis route,Milling time (h)\n"
            f"{batch_id},(Co0.5Ni0.5)O,single_phase,Ball milled then fired,12\n"
        ).encode("utf-8")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            # Two files in the folder: a primary .csv and a secondary .txt.
            zf.writestr(f"{batch_id}/{batch_id}.csv", "10 100\n11 120\n12 95\n")
            zf.writestr(f"{batch_id}/{batch_id}-notes.txt", "metadata")
        archive.seek(0)
        manifest_path = default_storage.save(
            f"test_batch/{uuid.uuid4().hex}_manifest.csv",
            io.BytesIO(manifest),
        )
        archive_path = default_storage.save(
            f"test_batch/{uuid.uuid4().hex}_archive.zip",
            io.BytesIO(archive.getvalue()),
        )
        return manifest_path, archive_path

    def test_commit_routes_through_persist_and_dedups(self):
        from catalog.documents import Material, Recipe
        from catalog.services.batch_experiment_upload import commit_batch

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                batch_id = f"SP15M-{uuid.uuid4().int % 900 + 100}"
                user = _FakeUser()
                # A host with a port (or dot) is required by mongoengine's
                # URLField regex; RequestFactory's default "testserver" is not a
                # valid URL host and would fail trial validation.
                request = RequestFactory().get("/", HTTP_HOST="localhost:8000")
                request.user = user

                manifest_path, archive_path = self._make_inputs(batch_id)
                result = commit_batch(
                    manifest_path=manifest_path,
                    archive_path=archive_path,
                    user=user,
                    request=request,
                    structure_family="rocksalt",
                )

                try:
                    self.assertEqual(result["created_trials"], 1)
                    # Primary (persist) + secondary (.txt) both recorded.
                    self.assertEqual(result["recorded_files"], 2)
                    self.assertEqual(result["skipped"], 0)

                    elements = parse_composition_formula("(Co0.5Ni0.5)O")
                    from catalog.documents import compute_material_auid

                    material_auid = compute_material_auid(elements, "rocksalt")
                    recipe = Recipe.objects(material_auid=material_auid).first()
                    self.assertIsNotNone(recipe)
                    trial = recipe.trials[-1]
                    self.assertEqual(trial.phase_status, "single_phase")
                    self.assertTrue(trial.content_hash)
                    self.assertEqual(
                        trial.exp_condition.additional_params.get("source_batch_id"), batch_id
                    )

                    # Re-uploading the same files dedups by hash -> skipped.
                    manifest_path2, archive_path2 = self._make_inputs(batch_id)
                    result2 = commit_batch(
                        manifest_path=manifest_path2,
                        archive_path=archive_path2,
                        user=user,
                        request=request,
                        structure_family="rocksalt",
                    )
                    self.assertEqual(result2["created_trials"], 0)
                    self.assertEqual(result2["skipped"], 1)
                finally:
                    Recipe.objects(material_auid=material_auid).delete()
                    Material.objects(id=material_auid).delete()

    def test_commit_txt_primary_preserves_extension_and_plots(self):
        from catalog.documents import Material, Recipe, compute_material_auid
        from catalog.services.batch_experiment_upload import commit_batch

        with tempfile.TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                batch_id = f"SP15M-{uuid.uuid4().int % 900 + 100}"
                user = _FakeUser()
                request = RequestFactory().get("/", HTTP_HOST="localhost:8000")
                request.user = user

                manifest = (
                    "Batch ID,Target composition,Phase status\n"
                    f"{batch_id},(Co0.5Ni0.5)O,single_phase\n"
                ).encode("utf-8")
                archive = io.BytesIO()
                with zipfile.ZipFile(archive, "w") as zf:
                    # Only a .txt primary: PowDLL-style whitespace columns.
                    zf.writestr(
                        f"{batch_id}/{batch_id}.txt",
                        "Converted with PowDLL\n9.998 412\n10.0 415\n10.1 427\n",
                    )
                archive.seek(0)
                manifest_path = default_storage.save(
                    f"test_batch/{uuid.uuid4().hex}_manifest.csv", io.BytesIO(manifest)
                )
                archive_path = default_storage.save(
                    f"test_batch/{uuid.uuid4().hex}_archive.zip",
                    io.BytesIO(archive.getvalue()),
                )

                elements = parse_composition_formula("(Co0.5Ni0.5)O")
                material_auid = compute_material_auid(elements, "rocksalt")
                try:
                    result = commit_batch(
                        manifest_path=manifest_path,
                        archive_path=archive_path,
                        user=user,
                        request=request,
                        structure_family="rocksalt",
                    )
                    self.assertEqual(result["created_trials"], 1)

                    recipe = Recipe.objects(material_auid=material_auid).first()
                    trial = recipe.trials[-1]
                    # Stored under the real .txt extension, not forced to .csv.
                    self.assertTrue(trial.raw_data_link.endswith(".txt"))
                    from catalog import xrd_store
                    stored = xrd_store.trial_path(recipe.id, trial.trial_id) / "raw.txt"
                    self.assertTrue(stored.exists())
                    self.assertEqual(trial.raw_data_type, "xrd")
                finally:
                    Recipe.objects(material_auid=material_auid).delete()
                    Material.objects(id=material_auid).delete()