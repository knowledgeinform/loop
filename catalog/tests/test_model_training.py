"""Unit coverage for continuous EFA/DEED training and AFLOW enrichment."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from catalog.aflow_client import build_aflux_query, species_key
from catalog.model_training import (
    FEATURE_NAMES,
    _train_target,
    material_features,
    material_has_training_labels,
)


def _dft(*, source="DFT", extended=None, formation=None):
    return SimpleNamespace(
        dft_source=source,
        extended_data=extended or {},
        dft_formation_energy_ev=formation,
        comp_auid="C:test",
    )


class AFLOWClientTests(SimpleTestCase):
    def test_exact_species_query_requests_formation_and_entropy_inputs(self):
        query = build_aflux_query(
            {"Co": 0.1, "Cr": 0.1, "Fe": 0.1, "Mn": 0.1, "Ti": 0.1, "O": 0.5}
        )
        self.assertIn("species(Co,Cr,Fe,Mn,O,Ti)", query)
        self.assertIn("nspecies(6)", query)
        self.assertIn("enthalpy_formation_atom(*)", query)
        self.assertIn("eentropy_atom", query)

    def test_species_cache_key_ignores_input_order(self):
        self.assertEqual(
            species_key(["O", "Co", "Fe"]),
            species_key(["Fe", "Co", "O"]),
        )


class ModelFeatureTests(SimpleTestCase):
    @patch("catalog.aflow_client.cached_aflow_summary", return_value={})
    def test_feature_vector_is_fixed_and_uses_cation_normalization(self, _summary):
        material = SimpleNamespace(
            elements={"Co": 0.1, "Cr": 0.1, "Fe": 0.1, "Mn": 0.1, "Ti": 0.1, "O": 0.5},
            dft_calculations=[_dft(formation=-1.2)],
        )
        features = material_features(material)
        self.assertEqual(len(features), len(FEATURE_NAMES))
        feature_map = dict(zip(FEATURE_NAMES, features))
        self.assertAlmostEqual(feature_map["fraction_Co"], 0.2)
        self.assertAlmostEqual(feature_map["oxygen_to_cation_ratio"], 1.0)
        self.assertAlmostEqual(feature_map["formation_mean"], -1.2)

    def test_model_predictions_are_never_reused_as_training_truth(self):
        predicted = SimpleNamespace(
            elements={"Co": 1, "O": 1},
            dft_calculations=[
                _dft(source="ChemScreen model", extended={"EFA": 42, "DEED": 12})
            ],
        )
        observed = SimpleNamespace(
            elements={"Co": 1, "O": 1},
            dft_calculations=[
                _dft(source="DFT", extended={"EFA": 42, "DEED": 12})
            ],
        )
        self.assertFalse(material_has_training_labels(predicted))
        self.assertTrue(material_has_training_labels(observed))


class RandomForestTrainingTests(SimpleTestCase):
    @override_settings(
        CHEMSCREEN_MIN_TRAINING_ROWS=10,
        CHEMSCREEN_RF_ESTIMATORS=10,
        CHEMSCREEN_RF_MAX_DEPTH=4,
        CHEMSCREEN_RF_N_JOBS=1,
    )
    def test_target_training_reports_cross_validated_metrics(self):
        rows = [
            {
                "features": [float(i), float(i % 3)],
                "labels": {"efa": float(i * 2 + 1)},
                "sample_weight": 2.0 if i == 11 else 1.0,
            }
            for i in range(12)
        ]
        model, metrics = _train_target(rows, "efa")
        self.assertEqual(metrics["count"], 12)
        self.assertGreaterEqual(metrics["mae"], 0)
        self.assertIn("normalized_mae", metrics)
        self.assertEqual(model.n_features_in_, 2)
