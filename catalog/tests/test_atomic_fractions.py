"""Second line under a browse row carries fractions, not a restated formula.

The label already gives stoichiometry (Fe3O4). Repeating raw counts underneath
(Fe:3 O:4) says the same thing twice. Fractions answer what share of the atoms
each element is, which is what matters for a high-entropy oxide where being off
equimolar is the whole question.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from catalog.views import _format_atomic_fractions


class AtomicFractionTests(SimpleTestCase):
    def test_fe3o4_reads_as_fractions(self) -> None:
        out = _format_atomic_fractions({"Fe": 3.0, "O": 4.0})
        self.assertEqual(out, "Fe 0.43 · O 0.57")

    def test_equimolar_is_suppressed(self) -> None:
        """Five cations all at 0.20 adds nothing the label did not already say."""
        elements = {"Al": 1.0, "Ca": 1.0, "Co": 1.0, "Mg": 1.0, "Ni": 1.0}
        self.assertEqual(_format_atomic_fractions(elements), "")

    def test_off_equimolar_is_shown(self) -> None:
        """The case the label hides: not quite equimolar."""
        out = _format_atomic_fractions({"Co": 1.0, "Cr": 1.0, "Fe": 1.5, "Mn": 1.0})
        self.assertIn("Fe 0.33", out)
        self.assertIn("Co 0.22", out)

    def test_oxygen_sorts_last(self) -> None:
        out = _format_atomic_fractions({"O": 4.0, "Fe": 3.0})
        self.assertTrue(out.endswith("O 0.57"))

    def test_single_element_and_empty_are_blank(self) -> None:
        for elements in ({}, {"Fe": 1.0}, None):
            self.assertEqual(_format_atomic_fractions(elements), "")

    def test_non_numeric_values_ignored(self) -> None:
        out = _format_atomic_fractions({"Fe": 3.0, "O": 4.0, "X": "na"})
        self.assertEqual(out, "Fe 0.43 · O 0.57")
