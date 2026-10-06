"""Table-driven tests for ``aggregation._visible_to`` (pure Python, no Mongo writes)."""

from django.test import SimpleTestCase

from catalog.aggregation import _visible_to


class VisibleToTests(SimpleTestCase):
    def test_matrix(self):
        cases = [
            (["S4E"], ["MIT"], True),
            (["S4E"], None, True),
            (["APL"], ["S4E"], True),
            (["APL"], ["S4E", "MIT"], True),
            (["APL"], ["APL"], True),
            (["APL"], ["MIT"], False),
            ([], None, False),
            ([], ["S4E"], False),
            ([], ["APL"], False),
            (["APL"], [], True),
        ]
        for user_aff, tags, expected in cases:
            with self.subTest(user=user_aff, tags=tags):
                self.assertIs(_visible_to(user_aff, tags), expected)
