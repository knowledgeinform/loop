"""Regression coverage for the unified natural-language prediction table."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from catalog.prediction import parse_composition_input
from catalog.chemscreen import ChemScreenExperimentalOutcome
from catalog.prediction_table import (
    PROPERTY_SPECS,
    _synthesis_route_text,
    chemscreen_route_prior,
    derived_d2h,
    dft_property_value,
    parse_prediction_query,
    predict_experimental_outlook,
    run_prediction_query,
)


class CompositionInputTests(SimpleTestCase):
    def test_decimal_high_entropy_formula_is_not_split_as_a_hydrate(self):
        self.assertEqual(
            parse_composition_input("(H0.2Cr0.2N0.2He0.2Ar0.2)O", ""),
            {"H": 0.2, "Cr": 0.2, "N": 0.2, "He": 0.2, "Ar": 0.2, "O": 1.0},
        )

    def test_prompt_accepts_lowercase_terminal_oxygen(self):
        intent = parse_prediction_query("Predict (H0.2Cr0.2N0.2He0.2Ar0.2)o")
        self.assertEqual(intent.formula, "(H0.2Cr0.2N0.2He0.2Ar0.2)O")


class PredictionIntentTests(SimpleTestCase):
    def test_lowest_conductivity(self):
        intent = parse_prediction_query("Give me a material with the lowest conductivity")
        self.assertEqual(intent.property_spec.key, "thermal_conductivity_300k")
        self.assertTrue(intent.ascending)

    def test_top_count_and_highest_efa(self):
        intent = parse_prediction_query("Show the top 7 materials with the highest EFA")
        self.assertEqual(intent.property_spec.key, "efa")
        self.assertFalse(intent.ascending)
        self.assertEqual(intent.limit, 7)
        self.assertEqual(intent.formula, "")


class ModelPayloadTests(SimpleTestCase):
    def test_bellatrix_style_ml_payload_is_case_and_nesting_insensitive(self):
        efa = next(spec for spec in PROPERTY_SPECS if spec.key == "efa")
        dft = SimpleNamespace(
            extended_data={},
            ml_predictions={
                "model": "Bellatrix-v2",
                "thermodynamics": {"Entropy Forming Ability": "42.5"},
            },
        )
        self.assertEqual(dft_property_value(dft, efa), (42.5, "Bellatrix-v2"))

    def test_typed_dft_value_wins_over_model_payload(self):
        conductivity = next(
            spec for spec in PROPERTY_SPECS if spec.key == "thermal_conductivity_300k"
        )
        dft = SimpleNamespace(
            thermal_conductivity_300k=1.25,
            extended_data={},
            ml_predictions={"conductivity": 2.5, "model": "Bellatrix"},
        )
        self.assertEqual(dft_property_value(dft, conductivity), (1.25, "DFT"))

    def test_direct_chemscreen_equation_provenance_is_preserved(self):
        efa = next(spec for spec in PROPERTY_SPECS if spec.key == "efa")
        dft = SimpleNamespace(
            extended_data={
                "EFA": 42.5,
                "calculation_method": "LOOP direct calculation",
            },
            ml_predictions={},
        )
        self.assertEqual(
            dft_property_value(dft, efa),
            (42.5, "LOOP direct calculation"),
        )


class ChemScreenPredictionTests(SimpleTestCase):
    def test_framework_route_text_exposes_material_specific_parameters(self):
        text = _synthesis_route_text({
            "methodology": "solid-state ceramic",
            "precursors": [{"formula": "Fe2O3"}, {"formula": "NiO"}],
            "route_steps": [{"step_type": "Sintering"}],
            "temperature": {"average": 925.0},
            "atmosphere": "Flowing Ar",
            "cooling_method": "Rapid quench",
        })
        self.assertIn("Fe2O3 + NiO", text)
        self.assertIn("925.0 °C in Flowing Ar", text)
        self.assertIn("Rapid quench", text)

    def test_d2h_is_derived_from_the_chemscreen_deed_relation(self):
        self.assertAlmostEqual(derived_d2h(36.0, 12.0), 0.25)
        self.assertIsNone(derived_d2h(36.0, 0.0))

    def test_exact_experimental_match_is_reported_as_observed(self):
        elements = {"Co": 0.1, "Cu": 0.1, "Mn": 0.1, "Ni": 0.1, "Zn": 0.1, "O": 0.5}
        outcome = ChemScreenExperimentalOutcome(
            chem_id="known",
            elements=elements,
            single_phase=True,
            source="published experiment",
        )
        prediction = predict_experimental_outlook(elements, outcomes=[outcome])
        self.assertTrue(prediction["observed"])
        self.assertEqual(prediction["status"], "Single phase")
        self.assertEqual(prediction["source"], "published experiment")

    def test_neighbor_experimental_outlook_is_labelled_unvalidated(self):
        outcomes = [
            ChemScreenExperimentalOutcome(
                chem_id="single",
                elements={"Co": 0.125, "Cu": 0.125, "Ni": 0.125, "Zn": 0.125, "O": 0.5},
                single_phase=True,
            ),
            ChemScreenExperimentalOutcome(
                chem_id="multi",
                elements={"Co": 0.125, "Cu": 0.125, "Mn": 0.125, "Zn": 0.125, "O": 0.5},
                single_phase=False,
            ),
        ]
        prediction = predict_experimental_outlook(
            {"Co": 0.1, "Cu": 0.1, "Fe": 0.1, "Ni": 0.1, "Zn": 0.1, "O": 0.5},
            outcomes=outcomes,
        )
        self.assertFalse(prediction["observed"])
        self.assertIn("single-phase likelihood", prediction["status"])
        self.assertIn("unvalidated", prediction["detail"])

    def test_multivalent_oxide_route_uses_controlled_atmosphere_prior(self):
        prediction = chemscreen_route_prior(
            {"Cr": 0.1, "Fe": 0.1, "Mn": 0.1, "Co": 0.1, "Ni": 0.1, "O": 0.5},
            "rocksalt",
        )
        self.assertEqual(prediction["temperature"]["average"], 1100.0)
        self.assertIn("Controlled-atmosphere sintering", [
            step["step_type"] for step in prediction["route_steps"]
        ])
        self.assertTrue(prediction["is_literature_prior"])


class PredictionRankingTests(SimpleTestCase):
    @patch("catalog.prediction_table._material_row")
    @patch("catalog.prediction_table._catalog_materials")
    def test_lowest_request_sorts_numeric_rows(self, catalog_materials, material_row):
        catalog_materials.return_value = ["a", "b", "c"]
        material_row.side_effect = [
            {"ranking_value": 9.0},
            {"ranking_value": 1.5},
            {"ranking_value": None},
        ]
        result = run_prediction_query("lowest conductivity")
        self.assertIsNone(result["error"])
        self.assertEqual([row["ranking_value"] for row in result["rows"]], [1.5, 9.0])
