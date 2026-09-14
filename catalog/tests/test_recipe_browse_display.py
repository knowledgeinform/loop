"""Recipe summaries and the actual browse partial, without database fixtures."""

from pathlib import Path
from unittest import TestCase

from django.template import Context, Engine
from django.test import SimpleTestCase, override_settings
from django.urls import path
from django.utils.html import strip_tags

from catalog.synthesis_display import format_steps_preview


urlpatterns = [
    path("add/", lambda request: None, name="add_data"),
    path("material/<path:material_auid>/", lambda request: None, name="composition_detail"),
    path("recipe/<path:recipe_id>/", lambda request: None, name="recipe_detail"),
]


class SynthesisPreviewTests(TestCase):
    def test_imported_free_text_route_uses_description(self):
        description = "Ball mill for 2 h, then anneal at 1200 °C for 4 h in air."
        self.assertEqual(format_steps_preview([{
            "step_number": 1, "step_type": "other", "description": description,
            "notes": "Batch imported from APL-001",
        }]), description)

    def test_generic_step_without_description_does_not_dump_metadata(self):
        for description in (None, "", {}, ["Mixing"]):
            with self.subTest(description=description):
                self.assertEqual(format_steps_preview([{
                    "step_type": "other", "description": description,
                    "notes": "Batch imported from APL-001",
                }]), "Other")

    def test_imported_description_wraps_whitespace_and_is_bounded(self):
        self.assertEqual(format_steps_preview([{
            "step_type": "other", "description": "  Mix powders.\n\tAnneal overnight.  ",
        }]), "Mix powders. Anneal overnight.")
        result = format_steps_preview([{
            "step_type": "other", "description": "Anneal overnight. " * 30,
        }], max_len=80)
        self.assertLessEqual(len(result), 80)
        self.assertTrue(result.endswith("…"))

    def test_ordered_route_with_recorded_conditions(self):
        steps = [
            {"step_number": 1, "step_type": "ball_milling", "milling_time_hours": 2},
            {"step_number": 2, "step_type": "heat_treatment", "max_temp_c": 1200,
             "hold_time_hours": 4, "atmosphere": "air"},
        ]
        self.assertEqual(
            format_steps_preview(steps),
            "Ball milling (2 h) → Heat treatment (1200 °C, 4 h, air)",
        )

    def test_legacy_temperature_and_duration_fields(self):
        self.assertEqual(format_steps_preview([
            {"step_type": "sintering", "temperature_c": "900", "duration_hours": 1.5},
        ]), "Sintering (900 °C, 1.5 h)")

    def test_zero_is_a_recorded_temperature(self):
        self.assertEqual(format_steps_preview([
            {"step_type": "cooling", "max_temp_c": 0},
        ]), "Cooling (0 °C)")

    def test_invalid_numeric_fields_are_omitted(self):
        for value in (float("nan"), float("inf"), True, {}, [], "unknown"):
            with self.subTest(value=value):
                self.assertEqual(format_steps_preview([
                    {"step_type": "annealing", "max_temp_c": value},
                ]), "Annealing")

    def test_nested_precursor_data_never_becomes_a_repr(self):
        self.assertEqual(format_steps_preview([
            {"step_type": "weighing", "precursors_list": [{"name": "MgO", "mass": 1}]},
        ]), "Weighing")

    def test_empty_and_malformed_routes(self):
        self.assertEqual(format_steps_preview([]), "—")
        self.assertEqual(format_steps_preview(None), "—")
        self.assertEqual(format_steps_preview("raw legacy text"), "Steps not reported")
        self.assertEqual(format_steps_preview([None, {}]), "Unspecified step → Unspecified step")

    def test_unknown_type_retains_recorded_conditions(self):
        self.assertEqual(format_steps_preview([
            {"step_type": "unknown", "temperature_c": 700},
        ]), "Unspecified step (700 °C)")

    def test_single_step_dictionary(self):
        self.assertEqual(format_steps_preview({"step_type": "mixing"}), "Mixing")

    def test_long_route_is_bounded(self):
        result = format_steps_preview([{"step_type": "ball_milling"}] * 20, max_len=40)
        self.assertLessEqual(len(result), 40)
        self.assertTrue(result.endswith("…"))


@override_settings(ROOT_URLCONF=__name__)
class RecipeBrowseTemplateTests(SimpleTestCase):
    def setUp(self):
        engine = Engine(
            dirs=[str(Path(__file__).resolve().parents[1] / "templates")],
            libraries={"humanize": "django.contrib.humanize.templatetags.humanize"},
        )
        self.template = engine.get_template("catalog/_browse_results.html")
        self.row = {
            "recipe_auid": "M:e850903cd1bb:R:f2c79c3c1442",
            "material_auid": "M:e850903cd1bb",
            "composition_display": "CoCuMgNiZnO5",
            "structure_family": "unknown",
            "steps_preview": format_steps_preview([
                {"step_type": "ball_milling", "milling_time_hours": 2},
                {"step_type": "heat_treatment", "max_temp_c": 1200},
            ]),
            "temperature_display": "1200",
            "organizations": ["APL"],
            "trial_count": 2,
            "literature_count": 1,
        }

    def render(self, **context):
        return self.template.render(Context({
            "view_mode": "recipes", "composition_rows": [self.row],
            "total": 1, "total_pages": 1, **context,
        }))

    def test_material_formula_and_readable_route_replace_raw_identifiers(self):
        html = self.render()
        text = strip_tags(html)
        self.assertIn("CoCuMgNiZnO5", text)
        self.assertIn("Ball milling (2 h) → Heat treatment (1200 °C)", text)
        self.assertNotIn("step_type", text)
        for identifier in (self.row["material_auid"], self.row["recipe_auid"]):
            self.assertNotIn(identifier, text)
            self.assertIn(identifier, html)  # URLs and hover titles remain usable.

    def test_evidence_links_target_this_recipe(self):
        html = self.render()
        base = f'/recipe/{self.row["recipe_auid"]}/'
        self.assertIn(f'href="{base}#trials"', html)
        self.assertIn(f'href="{base}#literature"', html)
        self.assertIn("2 experiments", html)
        self.assertIn("1 literature record", html)

    def test_missing_structure_and_steps_are_explicit(self):
        self.row.update(steps_preview="—", temperature_display="—")
        text = strip_tags(self.render())
        self.assertIn("Not reported", text)
        self.assertIn("Steps not reported", text)
        self.assertIn("Trial temperature not reported", text)
        self.assertNotIn("Unknown", text)

    def test_known_structure_is_preserved(self):
        self.row["structure_family"] = "rocksalt"
        self.assertIn("Rocksalt", strip_tags(self.render()))

    def test_semantic_score_is_preserved(self):
        self.row["semantic_score"] = 0.91234
        text = strip_tags(self.render(search_mode="semantic"))
        self.assertIn("Score", text)
        self.assertIn("0.912", text)

    def test_route_content_is_html_escaped(self):
        self.row["steps_preview"] = '<script>alert("x")</script>'
        html = self.render()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_imported_routes_remain_distinguishable_in_results(self):
        rows = []
        for description in ("Anneal at 900 °C for 12 h.", "Quench from 1200 °C into water."):
            rows.append({**self.row, "steps_preview": format_steps_preview([{
                "step_type": "other", "description": description,
            }])})
        text = strip_tags(self.render(composition_rows=rows))
        self.assertIn("Anneal at 900 °C for 12 h.", text)
        self.assertIn("Quench from 1200 °C into water.", text)

    def test_imported_description_is_html_escaped(self):
        self.row["steps_preview"] = format_steps_preview([{
            "step_type": "other", "description": '<script>alert("x")</script>',
        }])
        html = self.render()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_pagination_and_empty_results(self):
        self.assertIn("Next", self.render(page=1, total_pages=2, query_string="view=recipes"))
        self.assertIn("No recipes match", self.render(composition_rows=[]))
