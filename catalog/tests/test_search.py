"""Unit tests for catalog.search match-stage builders (no Mongo)."""

from django.test import SimpleTestCase

from catalog.search import (
    combine_match_stages,
    element_match_stage,
    num_elements_match_stage,
    structure_family_match_stage,
    text_match_stage,
    visibility_match_stage,
)


class ElementMatchStageTests(SimpleTestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(element_match_stage([]))
        self.assertIsNone(element_match_stage(None))

    def test_all_operator(self):
        self.assertEqual(
            element_match_stage(["Fe", "O"]),
            {"$match": {"element_symbols": {"$all": ["Fe", "O"]}}},
        )


class StructureFamilyMatchStageTests(SimpleTestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(structure_family_match_stage(""))
        self.assertIsNone(structure_family_match_stage("   "))

    def test_lowercased_exact_match(self):
        self.assertEqual(
            structure_family_match_stage("PerOvSkite"),
            {"$match": {"structure_family": "perovskite"}},
        )


class NumElementsMatchStageTests(SimpleTestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(num_elements_match_stage(None, None))

    def test_min_only(self):
        self.assertEqual(
            num_elements_match_stage(3, None),
            {"$match": {"num_elements": {"$gte": 3}}},
        )

    def test_max_only(self):
        self.assertEqual(
            num_elements_match_stage(None, 5),
            {"$match": {"num_elements": {"$lte": 5}}},
        )

    def test_range(self):
        self.assertEqual(
            num_elements_match_stage(2, 4),
            {"$match": {"num_elements": {"$gte": 2, "$lte": 4}}},
        )


class TextMatchStageTests(SimpleTestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(text_match_stage(""))
        self.assertIsNone(text_match_stage("  "))

    def test_substring_regex_default_field(self):
        self.assertEqual(
            text_match_stage("abc"),
            {
                "$match": {
                    "material_auid": {"$regex": "abc", "$options": "i"},
                }
            },
        )

    def test_custom_field_unescaped_metacharacters(self):
        """Pattern is passed through to MongoDB ``$regex`` (not ``re.escape``)."""
        self.assertEqual(
            text_match_stage("a+b", field="title"),
            {"$match": {"title": {"$regex": "a+b", "$options": "i"}}},
        )


class VisibilityMatchStageTests(SimpleTestCase):
    def test_s4e_bypass(self):
        self.assertIsNone(visibility_match_stage(["S4E", "APL"]))

    def test_empty_tags_public_s4e_only(self):
        expected = {"$match": {"visibility_affiliations": "S4E"}}
        self.assertEqual(visibility_match_stage([]), expected)
        self.assertEqual(visibility_match_stage(None), expected)

    def test_org_tags_include_s4e_in_clause(self):
        self.assertEqual(
            visibility_match_stage(["zebra", "apl"]),
            {
                "$match": {
                    "visibility_affiliations": {"$in": ["zebra", "apl", "S4E"]},
                }
            },
        )


class CombineMatchStagesTests(SimpleTestCase):
    def test_skips_falsy_varargs(self):
        a = {"$match": {"x": 1}}
        self.assertEqual(combine_match_stages(None, a, {}), [a])

    def test_all_empty(self):
        self.assertEqual(combine_match_stages(None, {}, None), [])
