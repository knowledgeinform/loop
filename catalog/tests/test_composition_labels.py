"""Records are labelled by their chemistry, and only by chemistry they contain.

Browse rows and the material page used to lead with the AUID, which is a content
hash and tells a reader nothing. The formula is the label now, with the AUID kept
as the link target and one demoted line on the record's own page.

The oxide formatter appended "O" whether or not the record had oxygen, so 21,215
of 36,530 materials rendered as oxides they are not.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from catalog.prediction_table import _format_high_entropy_oxide, format_composition


class FormulaLabelTests(SimpleTestCase):
    def test_metals_only_record_gets_no_oxygen(self) -> None:
        """The case Corey screenshotted: five metals, no O in the record."""
        elements = {"Al": 1.0, "Fe": 1.0, "Co": 1.0, "Ni": 1.0, "Zn": 1.0}
        self.assertEqual(format_composition(elements), "AlCoFeNiZn")
        self.assertNotIn("O", _format_high_entropy_oxide(elements).replace("Co", ""))

    def test_oxide_still_renders_its_oxygen(self) -> None:
        elements = {"Co": 1.0, "Cr": 1.0, "Fe": 1.0, "Mn": 1.0, "Ti": 1.0, "O": 5.0}
        self.assertTrue(_format_high_entropy_oxide(elements).endswith(")O"))

    def test_non_oxide_falls_back_to_plain_formula(self) -> None:
        for elements in ({"F": 1.0, "Mg": 1.0}, {"Ga": 1.0, "Th": 1.0}):
            rendered = _format_high_entropy_oxide(elements)
            self.assertEqual(rendered, format_composition(elements))

    def test_oxygen_last_and_ones_omitted(self) -> None:
        self.assertEqual(format_composition({"O": 1.0, "Mg": 1.0}), "MgO")

    def test_empty_composition_is_not_blank(self) -> None:
        """A blank label would leave a row with nothing to click."""
        self.assertEqual(format_composition({}), "Unknown")
