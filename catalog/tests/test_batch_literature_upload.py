import io
import json
import os
import unittest

from django.test import SimpleTestCase

from catalog.services.batch_literature_upload import (
    LitManifestRow,
    build_preview,
    build_synthesis_steps,
    create_preview,
    format_elements_formula,
    generate_manifest_template_csv,
    parse_literature_json,
    parse_literature_manifest,
    parse_literature_manifest_csv,
    resolve_elements,
)


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


class _FakeUser:
    is_authenticated = True

    def __init__(self, username="littester", uid=876543):
        self.username = username
        self.id = uid

    def get_username(self):
        return self.username


class LiteratureManifestTests(SimpleTestCase):
    def test_parse_manifest(self):
        csv_text = (
            "DOI,Target composition,Structure Family,Synthesis successful,Synthesis route,Year\n"
            "10.1000/abc,(Co0.5Ni0.5)O,rocksalt,true,Solid state,2024\n"
        )
        rows = parse_literature_manifest_csv(io.BytesIO(csv_text.encode("utf-8")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].doi, "10.1000/abc")
        self.assertEqual(rows[0].structure_family, "rocksalt")
        self.assertEqual(rows[0].year, "2024")

    def test_preview_missing_doi_and_composition(self):
        rows = [
            LitManifestRow(doi="", target_composition="(Co0.5Ni0.5)O"),
            LitManifestRow(doi="10.1/x", target_composition=""),
            LitManifestRow(doi="10.1/y", target_composition="(Co0.5Ni0.5)O"),
        ]
        preview = build_preview(rows)
        self.assertEqual(preview.total_rows, 3)
        self.assertEqual(preview.valid_count, 1)
        self.assertEqual(preview.error_count, 2)

    def test_build_synthesis_steps(self):
        steps, errors = build_synthesis_steps(LitManifestRow(synthesis_route="Mixed and fired"))
        self.assertEqual(errors, [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["step_type"], "other")
        self.assertEqual(steps[0]["description"], "Mixed and fired")

        empty, _ = build_synthesis_steps(LitManifestRow())
        self.assertEqual(empty, [])

    def test_parse_xlsx_manifest(self):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["DOI", "Target composition", "Synthesis successful"])
        ws.append(["10.1000/xlsx", "(Co0.5Ni0.5)O", "true"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        rows = parse_literature_manifest_csv(buf)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].doi, "10.1000/xlsx")
        self.assertEqual(rows[0].target_composition, "(Co0.5Ni0.5)O")

    def test_template_has_headers(self):
        csv_text = generate_manifest_template_csv()
        for header in ("DOI", "Target composition", "Synthesis successful"):
            self.assertIn(header, csv_text)


def _named(payload, name):
    """An uploaded-file stand-in: BytesIO carrying a filename, like Django's."""
    buf = io.BytesIO(payload.encode("utf-8") if isinstance(payload, str) else payload)
    buf.name = name
    return buf


# A trimmed copy of a real submitted record: elements as a map, structured
# multi-step synthesis, authors as a list, and a cooling step carrying the two
# fields the API's SynthesisStepSerializer drops.
SAMPLE_RECORD = {
    "doi": "10.1111/jace.18588",
    "synthesis_successful": True,
    "elements": {"Y": 2.0, "Ti": 0.4, "Zr": 0.4, "Hf": 0.4, "O": 7.0},
    "structure_family": "pyrochlore",
    "title": "High-entropy yttrium pyrochlore ceramics",
    "authors": ["Hu-Lin Liu", "Sha Pang", "Guo-Jun Zhang"],
    "journal": "Journal of the American Ceramic Society",
    "year": 2022,
    "findings": "thermal_conductivity~1.8W/mK",
    "spacegroup": "unknown",
    "element_sites": {"Symbol": "na"},
    "synthesis_steps": [
        {"step_type": "weighing", "notes": "na"},
        {"step_type": "ball_milling", "milling_time_hours": 16.0, "atmosphere": "air"},
        {"step_type": "heat_treatment", "max_temp_c": 1600.0, "hold_time_hours": 2.0},
        {"step_type": "cooling", "cooling_method": "furnace", "cooling_rate_c_min": 10.0},
    ],
}


class LiteratureJsonUploadTests(SimpleTestCase):
    def test_single_object_is_one_record(self):
        rows = parse_literature_json(_named(json.dumps(SAMPLE_RECORD), "M_temp_273.json"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].doi, "10.1111/jace.18588")
        self.assertEqual(rows[0].structure_family, "pyrochlore")

    def test_array_and_records_wrapper_and_jsonl(self):
        two = [SAMPLE_RECORD, {**SAMPLE_RECORD, "doi": "10.1111/second"}]

        array_rows = parse_literature_json(_named(json.dumps(two), "batch.json"))
        wrapper_rows = parse_literature_json(
            _named(json.dumps({"records": two}), "batch.json")
        )
        jsonl_rows = parse_literature_json(
            _named("\n".join(json.dumps(r) for r in two), "batch.jsonl")
        )

        for rows in (array_rows, wrapper_rows, jsonl_rows):
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1].doi, "10.1111/second")

    def test_structured_fields_survive(self):
        row = parse_literature_json(_named(json.dumps(SAMPLE_RECORD), "s.json"))[0]

        self.assertEqual(resolve_elements(row)["Y"], 2.0)
        self.assertEqual(row.authors_list, ["Hu-Lin Liu", "Sha Pang", "Guo-Jun Zhang"])
        self.assertEqual(row.element_sites, {"Symbol": "na"})

        steps, errors = build_synthesis_steps(row)
        self.assertEqual(errors, [])
        self.assertEqual(len(steps), 4)
        self.assertEqual([s["step_type"] for s in steps][:2], ["weighing", "ball_milling"])

        # The whole point of the JSON path: step detail is not flattened away.
        cooling = steps[-1]
        self.assertEqual(cooling["cooling_method"], "furnace")
        self.assertEqual(cooling["cooling_rate_c_min"], 10.0)

    def test_preview_accepts_json_without_errors(self):
        preview = create_preview(
            manifest_file=_named(json.dumps(SAMPLE_RECORD), "M_temp_273.json"),
            filename="M_temp_273.json",
        )
        self.assertEqual(preview.total_rows, 1)
        self.assertEqual(preview.error_count, 0)
        self.assertEqual(preview.valid_count, 1)
        self.assertEqual(preview.empty_reason, "")

    def test_display_columns_are_populated(self):
        row = parse_literature_json(_named(json.dumps(SAMPLE_RECORD), "s.json"))[0]
        # The preview table reads these two; blank cells would look like data loss.
        self.assertEqual(row.target_composition, "Y2Ti0.4Zr0.4Hf0.4O7")
        self.assertIn("ball_milling", row.synthesis_route)

    def test_elements_formula_formatting(self):
        self.assertEqual(format_elements_formula({"Y": 2.0, "O": 7.0}), "Y2O7")
        self.assertEqual(format_elements_formula({"Ti": 0.4}), "Ti0.4")
        self.assertEqual(format_elements_formula({"Ni": 1.0, "O": 1.0}), "NiO")

    def test_missing_doi_is_an_error_not_a_silent_drop(self):
        preview = create_preview(
            manifest_file=_named(json.dumps({**SAMPLE_RECORD, "doi": ""}), "x.json"),
            filename="x.json",
        )
        self.assertEqual(preview.total_rows, 1)
        self.assertEqual(preview.error_count, 1)
        self.assertIn("Missing DOI.", preview.items[0].errors)

    def test_invalid_json_raises_readable_error(self):
        with self.assertRaises(ValueError) as ctx:
            parse_literature_json(_named('{"doi": ', "broken.json"))
        self.assertIn("Invalid JSON", str(ctx.exception))

    def test_bad_elements_reported_as_error(self):
        bad = {**SAMPLE_RECORD, "elements": {"Y": "not-a-number"}}
        preview = create_preview(
            manifest_file=_named(json.dumps(bad), "bad.json"), filename="bad.json"
        )
        self.assertEqual(preview.error_count, 1)
        self.assertIn("Invalid elements", preview.items[0].errors[0])

    def test_dispatch_by_extension(self):
        csv_text = (
            "DOI,Target composition,Synthesis successful\n"
            "10.1000/abc,(Co0.5Ni0.5)O,true\n"
        )
        csv_rows = parse_literature_manifest(_named(csv_text, "manifest.csv"))
        json_rows = parse_literature_manifest(
            _named(json.dumps(SAMPLE_RECORD), "records.json")
        )
        self.assertEqual(csv_rows[0].doi, "10.1000/abc")
        self.assertIsNone(csv_rows[0].elements_map)
        self.assertIsNotNone(json_rows[0].elements_map)


class ZeroRowDiagnosticTests(SimpleTestCase):
    """The exact dead end a JSON file hit when fed to the CSV parser."""

    def test_json_uploaded_without_json_extension_explains_itself(self):
        preview = create_preview(
            manifest_file=_named(json.dumps(SAMPLE_RECORD), "M_temp_273.txt"),
            filename="M_temp_273.txt",
        )
        self.assertEqual(preview.total_rows, 0)
        self.assertEqual(preview.error_count, 0)
        self.assertTrue(preview.empty_reason)
        self.assertIn(".json", preview.empty_reason)

    def test_empty_csv_explains_itself(self):
        preview = create_preview(
            manifest_file=_named("", "empty.csv"), filename="empty.csv"
        )
        self.assertEqual(preview.total_rows, 0)
        self.assertTrue(preview.empty_reason)

    def test_valid_csv_has_no_empty_reason(self):
        csv_text = (
            "DOI,Target composition,Synthesis successful\n"
            "10.1000/abc,(Co0.5Ni0.5)O,true\n"
        )
        preview = create_preview(
            manifest_file=_named(csv_text, "manifest.csv"), filename="manifest.csv"
        )
        self.assertEqual(preview.total_rows, 1)
        self.assertEqual(preview.empty_reason, "")


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class CommitLiteratureIntegrationTests(SimpleTestCase):
    def test_commit_and_doi_dedup(self):
        import uuid

        from django.core.files.storage import default_storage

        from catalog.documents import Material, Recipe, compute_material_auid
        from catalog.services.batch_experiment_upload import parse_composition_formula
        from catalog.services.batch_literature_upload import commit_batch_literature

        doi = f"10.9999/{uuid.uuid4().hex[:8]}"
        csv_text = (
            "DOI,Target composition,Structure Family,Synthesis successful,Synthesis route\n"
            f"{doi},(Co0.5Ni0.5)O,rocksalt,true,Solid state\n"
        )
        manifest_path = default_storage.save(
            f"test_batch/{uuid.uuid4().hex}_lit.csv",
            io.BytesIO(csv_text.encode("utf-8")),
        )
        user = _FakeUser()
        elements = parse_composition_formula("(Co0.5Ni0.5)O")
        material_auid = compute_material_auid(elements, "rocksalt")

        try:
            result = commit_batch_literature(
                manifest_path=manifest_path, user=user, structure_family="rocksalt"
            )
            self.assertEqual(result["created_records"], 1)
            self.assertEqual(result["skipped"], 0)

            # Re-import the same DOI -> dedup -> skipped.
            manifest_path2 = default_storage.save(
                f"test_batch/{uuid.uuid4().hex}_lit.csv",
                io.BytesIO(csv_text.encode("utf-8")),
            )
            result2 = commit_batch_literature(
                manifest_path=manifest_path2, user=user, structure_family="rocksalt"
            )
            self.assertEqual(result2["created_records"], 0)
            self.assertEqual(result2["skipped"], 1)
        finally:
            Recipe.objects(material_auid=material_auid).delete()
            Material.objects(id=material_auid).delete()
