"""Unit tests for small helpers in catalog.views (no Mongo)."""

import json

from django.test import SimpleTestCase

from catalog.views import (
    AFFILIATION_CANONICAL,
    _canonical_affiliation,
    _display_elements,
    _format_steps_preview,
    _format_temperature_display,
    _is_visible_to_user,
    _nominal_composition_html,
    _normalize_visibility_tags,
    _parse_composition_query,
)


class CanonicalAffiliationTests(SimpleTestCase):
    def test_known_aliases(self):
        for raw, expected in AFFILIATION_CANONICAL.items():
            with self.subTest(raw=raw):
                self.assertEqual(_canonical_affiliation(raw), expected)

    def test_s4e_passes_through_allowed_list(self):
        self.assertEqual(_canonical_affiliation("S4E"), "S4E")


class NormalizeVisibilityTagsTests(SimpleTestCase):
    def test_none_and_empty_list_default_to_s4e(self):
        self.assertEqual(_normalize_visibility_tags(None), ["S4E"])
        self.assertEqual(_normalize_visibility_tags([]), ["S4E"])

    def test_empty_string_default_to_s4e(self):
        self.assertEqual(_normalize_visibility_tags(""), ["S4E"])
        self.assertEqual(_normalize_visibility_tags("   "), ["S4E"])

    def test_comma_split(self):
        self.assertEqual(
            _normalize_visibility_tags("APL, S4E , Oak Ridge"),
            ["APL", "S4E", "Oak Ridge"],
        )

    def test_json_list_string_requires_brackets(self):
        self.assertEqual(
            _normalize_visibility_tags('["APL", "S4E"]'),
            ["APL", "S4E"],
        )

    def test_list_passthrough(self):
        self.assertEqual(
            _normalize_visibility_tags(["x", "APL"]),
            ["APL"],
        )


class IsVisibleToUserTests(SimpleTestCase):
    def test_s4e_user_sees_everything(self):
        self.assertTrue(_is_visible_to_user(["Oak Ridge"], ["S4E"]))

    def test_pure_s4e_item_hidden_from_non_s4e_user(self):
        self.assertFalse(_is_visible_to_user(["S4E"], ["APL"]))

    def test_shared_org_tag(self):
        self.assertTrue(_is_visible_to_user(["S4E", "APL"], ["APL"]))

    def test_no_overlap(self):
        self.assertFalse(_is_visible_to_user(["Oak Ridge"], ["APL"]))


class ParseCompositionQueryTests(SimpleTestCase):
    def test_json_list_of_dicts(self):
        raw = json.dumps(
            [
                {"symbol": "La", "ratio": 1},
                {"symbol": "Co", "ratio": 1},
                {"symbol": "O", "ratio": 3},
            ]
        )
        self.assertEqual(
            _parse_composition_query(raw),
            [
                {"symbol": "La", "ratio": 1},
                {"symbol": "Co", "ratio": 1},
                {"symbol": "O", "ratio": 3},
            ],
        )

    def test_invalid_json_returns_empty(self):
        self.assertEqual(_parse_composition_query("La:1 Co"), [])


class FormatTemperatureDisplayTests(SimpleTestCase):
    def test_none(self):
        self.assertEqual(_format_temperature_display(None), "—")
        self.assertEqual(_format_temperature_display([]), "—")

    def test_single_integer_style(self):
        self.assertEqual(_format_temperature_display([300.0]), "300")

    def test_range(self):
        self.assertEqual(_format_temperature_display([100.0, 200.0]), "100–200")


class NominalCompositionHtmlTests(SimpleTestCase):
    def test_subscripts_and_o_last(self):
        html = _nominal_composition_html({"La": 1, "Co": 1, "O": 3})
        self.assertIn("La<sub>1</sub>", html)
        self.assertIn("Co<sub>1</sub>", html)
        self.assertIn("O<sub>3</sub>", html)
        self.assertLess(html.index("Co"), html.index("La"))
        self.assertLess(html.index("La"), html.index("O"))

    def test_display_elements_normalizes_ratios(self):
        de = _display_elements({"O": 3, "La": 1})
        self.assertEqual(de["La"], 1)
        self.assertEqual(de["O"], 3)


class FormatStepsPreviewTests(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(_format_steps_preview(None), "—")
        self.assertEqual(_format_steps_preview([]), "—")

    def test_truncation(self):
        long_steps = [{"k": "x" * 200}]
        out = _format_steps_preview(long_steps, max_len=20)
        self.assertTrue(out.endswith("…"))
        self.assertEqual(len(out), 20)
