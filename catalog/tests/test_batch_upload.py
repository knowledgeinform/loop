"""Unit tests for catalog.batch_upload normalization (no Mongo required)."""

from django.test import SimpleTestCase

from catalog.batch_upload import (
    normalize_experiment_payload,
    normalize_literature_payload,
    normalize_record,
    normalize_synthesis_steps,
)


VALID_ELEMENTS = {"Mg": 1, "Co": 1, "Ni": 1, "Cu": 1, "Zn": 1, "O": 5}


class ExperimentPayloadTests(SimpleTestCase):
    def test_valid_record(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "phase_status": "single_phase",
        }
        normalized, errors = normalize_experiment_payload(raw)
        self.assertEqual(errors, [])
        self.assertTrue(normalized["material_auid"].startswith("M:"))
        self.assertEqual(normalized["phase_status"], "single_phase")
        # Optional free-text fields default to "na".
        self.assertEqual(normalized["notes"], "na")
        self.assertEqual(normalized["raw_data_type"], "xrd")
        self.assertEqual(normalized["spacegroup"], "unknown")

    def test_missing_required_fields(self):
        normalized, errors = normalize_experiment_payload({})
        joined = " ".join(errors)
        self.assertIn("elements", joined)
        self.assertIn("structure_family", joined)
        self.assertIn("phase_status", joined)

    def test_invalid_enums(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "not_a_family",
            "phase_status": "maybe",
        }
        _, errors = normalize_experiment_payload(raw)
        joined = " ".join(errors)
        self.assertIn("structure_family", joined)
        self.assertIn("phase_status", joined)

    def test_empty_elements_rejected(self):
        raw = {"elements": {}, "structure_family": "rocksalt", "phase_status": "single_phase"}
        _, errors = normalize_experiment_payload(raw)
        self.assertTrue(any("elements" in e for e in errors))


class LiteraturePayloadTests(SimpleTestCase):
    def test_valid_record(self):
        raw = {
            "doi": "10.1126/science.aaq1057",
            "synthesis_successful": True,
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "authors": ["A B", "C D"],
        }
        normalized, errors = normalize_literature_payload(raw)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["doi"], "10.1126/science.aaq1057")
        self.assertTrue(normalized["synthesis_successful"])
        self.assertEqual(normalized["authors"], ["A B", "C D"])
        self.assertEqual(normalized["title"], "na")

    def test_missing_doi_and_outcome(self):
        raw = {"elements": VALID_ELEMENTS, "structure_family": "rocksalt"}
        _, errors = normalize_literature_payload(raw)
        joined = " ".join(errors)
        self.assertIn("doi", joined)
        self.assertIn("synthesis_successful", joined)

    def test_authors_as_comma_string(self):
        raw = {
            "doi": "10.1/x",
            "synthesis_successful": "false",
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "authors": "A B, C D",
        }
        normalized, errors = normalize_literature_payload(raw)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["authors"], ["A B", "C D"])
        self.assertFalse(normalized["synthesis_successful"])


class SynthesisStepTests(SimpleTestCase):
    def test_ball_milling_shaping(self):
        steps, errors = normalize_synthesis_steps([
            {
                "step_type": "ball_milling",
                "milling_time_hours": "12",
                "milling_rpm": 300,
                "ball_powder_ratio": "10 : 1",
                "atmosphere": "air",
                "unused_key": "dropped",
            }
        ])
        self.assertEqual(errors, [])
        self.assertEqual(len(steps), 1)
        step = steps[0]
        self.assertEqual(step["step_number"], 1)
        self.assertEqual(step["milling_time_hours"], 12.0)
        self.assertEqual(step["ball_powder_ratio"], "10:1")
        self.assertNotIn("unused_key", step)

    def test_bad_ratio_errors(self):
        _, errors = normalize_synthesis_steps([
            {"step_type": "ball_milling", "ball_powder_ratio": "ten-to-one"}
        ])
        self.assertTrue(any("ratio" in e for e in errors))

    def test_invalid_step_type(self):
        _, errors = normalize_synthesis_steps([{"step_type": "teleportation"}])
        self.assertTrue(any("step_type" in e for e in errors))

    def test_empty_list(self):
        steps, errors = normalize_synthesis_steps([])
        self.assertEqual((steps, errors), ([], []))


class DispatchTests(SimpleTestCase):
    def test_unknown_type(self):
        _, errors = normalize_record("computational", {})
        self.assertTrue(any("Unknown record type" in e for e in errors))

    def test_locked_composition_mismatch(self):
        raw = {
            "elements": {"Fe": 2, "O": 3},
            "structure_family": "other",
            "phase_status": "single_phase",
        }
        _, errors = normalize_record(
            "experiment",
            raw,
            locked_elements=VALID_ELEMENTS,
            locked_structure="rocksalt",
        )
        self.assertTrue(any("does not match the locked material" in e for e in errors))


# ---------------------------------------------------------------------------
# Additional edge-case tests for the experiment normalizer
# ---------------------------------------------------------------------------

class ExperimentNormalizerEdgeCaseTests(SimpleTestCase):
    def test_invalid_raw_data_type_silently_reverts_to_xrd(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "phase_status": "single_phase",
            "raw_data_type": "neutron_diffraction",
        }
        normalized, errors = normalize_experiment_payload(raw)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["raw_data_type"], "xrd")

    def test_known_raw_data_types_accepted(self):
        for rdt in ("xrd", "sem", "tem", "eds", "other", "unknown", "na"):
            with self.subTest(raw_data_type=rdt):
                raw = {
                    "elements": VALID_ELEMENTS,
                    "structure_family": "rocksalt",
                    "phase_status": "single_phase",
                    "raw_data_type": rdt,
                }
                normalized, errors = normalize_experiment_payload(raw)
                self.assertEqual(errors, [])
                self.assertEqual(normalized["raw_data_type"], rdt)

    def test_comments_field_mapped_to_notes(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "phase_status": "single_phase",
            "comments": "sample was wet during synthesis",
        }
        normalized, errors = normalize_experiment_payload(raw)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["notes"], "sample was wet during synthesis")

    def test_spacegroup_passthrough(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "phase_status": "single_phase",
            "spacegroup": "Fm-3m",
        }
        normalized, errors = normalize_experiment_payload(raw)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["spacegroup"], "Fm-3m")

    def test_element_sites_normalized(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "phase_status": "single_phase",
            "element_sites": {"Mg": "4a", "O": "4b"},
        }
        normalized, errors = normalize_experiment_payload(raw)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["element_sites"], {"Mg": "4a", "O": "4b"})

    def test_all_phase_status_values_accepted(self):
        for status in ("single_phase", "multi_phase", "not_confirmed"):
            with self.subTest(phase_status=status):
                raw = {
                    "elements": VALID_ELEMENTS,
                    "structure_family": "rocksalt",
                    "phase_status": status,
                }
                normalized, errors = normalize_experiment_payload(raw)
                self.assertEqual(errors, [])
                self.assertEqual(normalized["phase_status"], status)

    def test_non_dict_record_rejected(self):
        _, errors = normalize_experiment_payload("not a dict")
        self.assertTrue(any("JSON object" in e for e in errors))

    def test_material_auid_present_when_valid(self):
        raw = {
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
            "phase_status": "single_phase",
        }
        normalized, errors = normalize_experiment_payload(raw)
        self.assertEqual(errors, [])
        self.assertIsNotNone(normalized["material_auid"])
        self.assertTrue(normalized["material_auid"].startswith("M:"))


# ---------------------------------------------------------------------------
# Additional edge-case tests for the literature normalizer
# ---------------------------------------------------------------------------

class LiteratureNormalizerEdgeCaseTests(SimpleTestCase):
    def _base(self, **overrides):
        base = {
            "doi": "10.1126/test.example",
            "synthesis_successful": True,
            "elements": VALID_ELEMENTS,
            "structure_family": "rocksalt",
        }
        base.update(overrides)
        return base

    def test_synthesis_successful_truthy_coercions(self):
        for val in ("true", "1", "yes", "y", "True", 1):
            with self.subTest(value=val):
                normalized, errors = normalize_literature_payload(self._base(synthesis_successful=val))
                self.assertEqual(errors, [])
                self.assertTrue(normalized["synthesis_successful"])

    def test_synthesis_successful_falsy_coercions(self):
        for val in ("false", "0", "no", "n", 0):
            with self.subTest(value=val):
                normalized, errors = normalize_literature_payload(self._base(synthesis_successful=val))
                self.assertEqual(errors, [])
                self.assertFalse(normalized["synthesis_successful"])

    def test_year_string_coerced_to_int(self):
        normalized, errors = normalize_literature_payload(self._base(year="2024"))
        self.assertEqual(errors, [])
        self.assertEqual(normalized["year"], 2024)

    def test_invalid_year_becomes_none(self):
        normalized, errors = normalize_literature_payload(self._base(year="not-a-year"))
        self.assertEqual(errors, [])
        self.assertIsNone(normalized["year"])

    def test_optional_text_fields_default_to_na(self):
        normalized, errors = normalize_literature_payload(self._base())
        self.assertEqual(errors, [])
        self.assertEqual(normalized["title"], "na")
        self.assertEqual(normalized["journal"], "na")
        self.assertEqual(normalized["findings"], "na")

    def test_empty_authors_list_preserved(self):
        normalized, errors = normalize_literature_payload(self._base(authors=[]))
        self.assertEqual(errors, [])
        self.assertEqual(normalized["authors"], [])

    def test_non_dict_record_rejected(self):
        _, errors = normalize_literature_payload(42)
        self.assertTrue(any("JSON object" in e for e in errors))


# ---------------------------------------------------------------------------
# Additional edge-case tests for synthesis-step normalizer
# ---------------------------------------------------------------------------

class SynthesisStepEdgeCaseTests(SimpleTestCase):
    def test_multiple_steps_get_sequential_step_numbers(self):
        steps, errors = normalize_synthesis_steps([
            {"step_type": "weighing"},
            {"step_type": "ball_milling"},
            {"step_type": "heat_treatment"},
        ])
        self.assertEqual(errors, [])
        self.assertEqual([s["step_number"] for s in steps], [1, 2, 3])

    def test_step_notes_field_preserved(self):
        steps, errors = normalize_synthesis_steps([
            {"step_type": "mixing", "notes": "mixed by hand"},
        ])
        self.assertEqual(errors, [])
        self.assertEqual(steps[0]["notes"], "mixed by hand")

    def test_heat_treatment_float_fields_coerced(self):
        steps, errors = normalize_synthesis_steps([{
            "step_type": "heat_treatment",
            "max_temp_c": "1200",
            "hold_time_hours": "4",
            "atmosphere": "air",
        }])
        self.assertEqual(errors, [])
        self.assertEqual(steps[0]["max_temp_c"], 1200.0)
        self.assertEqual(steps[0]["hold_time_hours"], 4.0)

    def test_arc_melting_number_of_remelts_coerced_to_int(self):
        steps, errors = normalize_synthesis_steps([{
            "step_type": "arc_melting",
            "number_of_remelts": "3",
        }])
        self.assertEqual(errors, [])
        self.assertIsInstance(steps[0]["number_of_remelts"], int)
        self.assertEqual(steps[0]["number_of_remelts"], 3)

    def test_unknown_and_na_step_types_need_no_fields(self):
        for step_type in ("unknown", "na"):
            with self.subTest(step_type=step_type):
                steps, errors = normalize_synthesis_steps([{"step_type": step_type}])
                self.assertEqual(errors, [])
                self.assertEqual(steps[0]["step_type"], step_type)

    def test_non_list_synthesis_steps_returns_error(self):
        _, errors = normalize_synthesis_steps("not a list")
        self.assertTrue(errors)

    def test_non_dict_step_produces_per_item_error_others_still_process(self):
        steps, errors = normalize_synthesis_steps([
            "bad step",
            {"step_type": "mixing"},
        ])
        self.assertTrue(any("must be a JSON object" in e for e in errors))
        self.assertTrue(any(s["step_type"] == "mixing" for s in steps))

    def test_unknown_keys_on_step_are_dropped(self):
        steps, _ = normalize_synthesis_steps([{
            "step_type": "mixing",
            "mixing_time_min": "5",
            "irrelevant_key": "dropped",
        }])
        self.assertNotIn("irrelevant_key", steps[0])
        self.assertEqual(steps[0]["mixing_time_min"], 5.0)
