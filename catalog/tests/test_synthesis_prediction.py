"""Tests for the composition-specific synthesis fallback."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from catalog.composition_model import CompositionKNNModel, TrainingRecord
from catalog.synthesis_prediction import build_framework_prediction


class SynthesisFrameworkTests(SimpleTestCase):
    def test_route_contains_stoichiometry_temperature_and_validation(self):
        prediction = build_framework_prediction(
            {"Co": 0.2, "Cr": 0.2, "Fe": 0.2, "Mn": 0.2, "Ti": 0.2, "O": 1.0},
            "rocksalt",
            d2h=0.05,
            experimental_probability=0.6,
        )
        self.assertEqual(prediction["methodology"], "solid-state ceramic")
        self.assertEqual(len(prediction["precursors"]), 5)
        self.assertTrue(prediction["temperature"]["is_estimate"])
        self.assertIn("Ar", prediction["atmosphere"])
        self.assertEqual(prediction["route_steps"][-1]["step_type"], "Validation")
        self.assertLessEqual(prediction["confidence"], 0.65)

    def test_high_hull_distance_changes_method_and_temperature(self):
        elements = {"Co": 0.2, "Cu": 0.2, "Fe": 0.2, "Ni": 0.2, "Zn": 0.2, "O": 1.0}
        stable = build_framework_prediction(elements, "rocksalt", d2h=0.01)
        metastable = build_framework_prediction(elements, "rocksalt", d2h=0.2)
        self.assertEqual(stable["methodology"], "solid-state ceramic")
        self.assertEqual(metastable["methodology"], "citrate sol-gel")
        self.assertNotEqual(stable["temperature"]["average"], metastable["temperature"]["average"])
        self.assertIn("quench", metastable["cooling_method"].lower())

    def test_precursor_fractions_are_composition_specific(self):
        prediction = build_framework_prediction(
            {"Fe": 2.0, "Ni": 1.0, "O": 4.0},
            "unknown",
        )
        by_element = {row["element"]: row for row in prediction["precursors"]}
        self.assertAlmostEqual(by_element["Fe"]["target_cation_fraction"], 2 / 3, places=5)
        self.assertAlmostEqual(by_element["Ni"]["target_cation_fraction"], 1 / 3, places=5)

    def test_temperature_varies_with_precursor_set(self):
        first = build_framework_prediction(
            {"Sc": 1, "Ti": 1, "Cr": 1, "Ni": 1, "Zn": 1, "O": 5},
            "rocksalt",
            d2h=0.04,
        )
        second = build_framework_prediction(
            {"V": 1, "Fe": 1, "Co": 1, "Cu": 1, "Mn": 1, "O": 5},
            "rocksalt",
            d2h=0.04,
        )
        self.assertNotEqual(first["temperature"]["average"], second["temperature"]["average"])

    def test_verified_recipe_outranks_exact_low_weight_pseudo_label(self):
        composition = {"Co": 1, "Cr": 1, "Fe": 1, "Mn": 1, "Ni": 1, "O": 5}
        verified = SimpleNamespace(id="verified", structure_family="rocksalt")
        generated = SimpleNamespace(id="generated", structure_family="rocksalt")
        model = CompositionKNNModel([
            TrainingRecord(verified, composition, sample_weight=1.0),
            TrainingRecord(generated, composition, sample_weight=0.2, source_type="synthesis_prediction"),
        ])
        nearest = model.nearest(composition, "rocksalt")
        self.assertEqual(nearest[0][0].id, "verified")
        self.assertGreater(nearest[0][1], nearest[1][1])
