"""ChemScreen's native JSON/CSV shapes normalize cleanly for LOOP."""

from django.test import SimpleTestCase

from catalog.chemscreen import (
    calculate_efa_deed,
    parse_candidate_entry,
    parse_experimental_outcome,
    parse_observed_entry,
    parse_prediction_entry,
)


class ChemScreenArtifactTests(SimpleTestCase):
    def test_direct_efa_and_deed_equations(self):
        calculated = calculate_efa_deed(
            dg_list=[1, 1],
            formation_enthalpies=[0.0, 2.0],
            d2h=0.25,
        )
        self.assertAlmostEqual(calculated["mean_formation_enthalpy"], 1.0)
        self.assertAlmostEqual(calculated["sigma"], 2 ** 0.5)
        self.assertAlmostEqual(calculated["EFA"], 1 / (2 ** 0.5))
        self.assertAlmostEqual(
            calculated["DEED"],
            ((1 / (2 ** 0.5)) / 0.25) ** 0.5,
        )

    def test_generated_candidate_pool_shape(self):
        record = parse_candidate_entry(
            {
                "species": ["O", "Ti", "Mn", "Fe", "Ni", "Cu"],
                "composition": [0.5, 0.1, 0.1, 0.1, 0.1, 0.1],
                "chem_id": "CuFeMnNiOTi_100100100100500100",
                "DFT": "no",
                "EXP": "no",
                "single_phase": "Unknown",
            }
        )
        self.assertEqual(record.kind, "candidate")
        self.assertEqual(record.elements["O"], 0.5)
        self.assertEqual(set(record.elements) - {"O"}, {"Ti", "Mn", "Fe", "Ni", "Cu"})
        self.assertEqual(record.values, {})
        self.assertEqual(record.single_phase, "unknown")

    def test_experimental_truth_shape(self):
        outcome = parse_experimental_outcome(
            {
                "species": ["Co", "Cu", "Mn", "Ni", "Zn", "O"],
                "Composition": [0.1, 0.1, 0.1, 0.1, 0.1, 0.5],
                "chem_id": "CoCuMnNiOZn_100100100100500100",
                "single_phase": "Yes",
                "source": "10.1007/s10853-020-05183-4",
            }
        )
        self.assertTrue(outcome.single_phase)
        self.assertEqual(outcome.elements["O"], 0.5)
        self.assertEqual(outcome.source, "10.1007/s10853-020-05183-4")

    def test_observed_efa_deed_record(self):
        record = parse_observed_entry(
            {
                "species": ["Cd", "Nb", "O", "Sn", "Zn"],
                "Composition": [0.125, 0.125, 0.5, 0.125, 0.125],
                "chem_id": "CdNbOSnZn_125125500125125",
                "EFA": 4.486,
                "DEED": 3.816,
                "d2h": 0.308,
            }
        )
        self.assertEqual(record.elements["O"], 0.5)
        self.assertEqual(record.values["EFA"], 4.486)
        self.assertEqual(record.values["DEED"], 3.816)
        self.assertEqual(record.kind, "observed")

    def test_observed_record_recalculates_when_dft_inputs_exist(self):
        record = parse_observed_entry(
            {
                "species": ["Co", "Cu", "Mn", "Ni", "Zn", "O"],
                "Composition": [0.1, 0.1, 0.1, 0.1, 0.1, 0.5],
                "chem_id": "known",
                "EFA": 999,
                "DEED": 999,
                "d2h": 0.25,
                "dg_list": [1, 1],
                "eform_list": [0.0, 2.0],
            }
        )
        self.assertNotEqual(record.values["EFA"], 999)
        self.assertEqual(record.calculation_method, "LOOP direct ChemScreen equation")
        self.assertEqual(record.calculation_inputs["reported_values"]["EFA"], 999)

    def test_precomputed_model_csv_shape(self):
        record = parse_prediction_entry(
            {
                "ML_Predicted": "12.75",
                "chem_id": "CdCuNiOSrZn_100100100500100100",
                "Elements": "Ni-Cu-Zn-Sr-Cd",
                "DFT": "no",
                "EXP": "yes",
            },
            model_name="ChemScreen RF 2026-05",
        )
        self.assertEqual(record.elements["O"], 0.5)
        self.assertEqual(record.values["ML_Predicted"], 12.75)
        self.assertEqual(record.model_name, "ChemScreen RF 2026-05")
        self.assertEqual(record.exp, "yes")
