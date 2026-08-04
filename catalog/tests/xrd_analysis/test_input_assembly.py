from datetime import datetime, timezone
from dataclasses import fields, is_dataclass

from django.test import SimpleTestCase

from catalog.documents import EmbeddedTrial, ExpCondition, Material, Recipe
from catalog.raw_db import RawFile
from catalog.xrd_analysis.reporting import dumps_canonical_json
from catalog.xrd_analysis.schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    XRDAnalysisInputError,
    assemble_xrd_analysis_input,
)


def _fixture_records():
    material = Material(
        id="M:testCoNiO",
        elements={"O": 1.0, "Ni": 0.5, "Co": 0.5},
        element_symbols=["O", "Co", "Ni"],
        structure_family="rocksalt",
    )
    recipe = Recipe(
        id="M:testCoNiO:R:testRoute",
        material_auid="M:testCoNiO",
        elements={"Ni": 0.5, "O": 1.0, "Co": 0.5},
        structure_family="rocksalt",
        synthesis_steps=[
            {
                "step_number": 1,
                "step_type": "weighing",
                "notes": "Initial powder blend",
                "precursors_list": [
                    {"name": "Nickel oxide", "formula": "NiO"},
                    {"name": "Cobalt oxide", "formula": "CoO"},
                ],
            },
            {
                "step_number": 2,
                "step_type": "ball_milling",
                "milling_time_hours": 8.0,
                "milling_rpm": 250.0,
                "atmosphere": "air",
            },
            {
                "step_number": 3,
                "step_type": "heat_treatment",
                "max_temp_c": 950.0,
                "ramp_rate_c_min": 5.0,
                "hold_time_hours": 12.0,
                "atmosphere": "oxygen",
                "furnace_type": "tube",
                "notes": "Calcination step",
            },
            {
                "step_number": 4,
                "step_type": "xrd_measurement",
                "radiation": "cu_ka",
                "two_theta_range": "10-80",
                "step_size_deg": 0.02,
                "scan_speed_deg_min": 1.5,
                "notes": "Room temperature scan",
            },
        ],
    )
    exp_condition = ExpCondition(
        additional_params={
            "file_hash": "ab" * 32,
            "xrd_metadata": [
                ("Instrument profile", "CuKa lab data"),
                ("Coordinate column", "Angle"),
                ("Intensity column", "Intensity"),
                ("K-Alpha1 wavelength", "1.5405980"),
                ("Geometry", "Bragg-Brentano"),
                ("Sample holder", "Zero background silicon"),
            ],
            "synthesis_steps": list(recipe.synthesis_steps),
        }
    )
    trial = EmbeddedTrial(
        trial_id="T7",
        trial_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        phase_status="multi_phase",
        exp_condition=exp_condition,
        raw_data_type="xrd",
        file_hash="ab" * 32,
        experimenter="researcher",
        notes="Human entered result should stay out of scientific input",
        spacegroup="Fm-3m (#225)",
        element_sites={"O": "4b", "Co": "4a"},
    )
    raw_file = RawFile(
        id="ab" * 32,
        original_filename="scan.raw",
        stored_path="/tmp/xrd/M:testCoNiO/R:testRoute/T7/raw.raw",
        content_type="application/octet-stream",
        size_bytes=1234,
        material_auid="M:testCoNiO",
        recipe_auid="M:testCoNiO:R:testRoute",
        trial_id="T7",
    )
    return material, recipe, trial, raw_file


def _contains_model_instance(value):
    if isinstance(value, (Material, Recipe, EmbeddedTrial, RawFile, ExpCondition)):
        return True
    if is_dataclass(value):
        return any(_contains_model_instance(getattr(value, item.name)) for item in fields(value))
    if isinstance(value, dict):
        return any(_contains_model_instance(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_model_instance(item) for item in value)
    return False


class InputAssemblyTests(SimpleTestCase):
    def test_complete_input_assembly_from_repository_records(self):
        material, recipe, trial, raw_file = _fixture_records()

        assembled = assemble_xrd_analysis_input(
            material=material,
            recipe=recipe,
            trial=trial,
            raw_file=raw_file,
        )

        self.assertEqual(assembled.material_auid, "M:testCoNiO")
        self.assertEqual(assembled.recipe_auid, "M:testCoNiO:R:testRoute")
        self.assertEqual(assembled.trial_id, "T7")
        self.assertEqual(assembled.raw_file_hash, "ab" * 32)
        self.assertEqual(assembled.raw_file_reference.locator, "/tmp/xrd/M:testCoNiO/R:testRoute/T7/raw.raw")
        self.assertEqual(assembled.nominal_composition, "Co0.5Ni0.5O")
        self.assertEqual(assembled.elements, ("Co", "Ni", "O"))
        self.assertEqual(
            [(item.element, item.amount) for item in assembled.stoichiometric_amounts],
            [("Co", 0.5), ("Ni", 0.5), ("O", 1.0)],
        )
        self.assertEqual(assembled.structure_family, "rocksalt")
        self.assertEqual(assembled.expected_space_group, "Fm-3m (#225)")
        self.assertEqual(
            [(item.element, item.site_label) for item in assembled.expected_site_assignments],
            [("Co", "4a"), ("O", "4b")],
        )
        self.assertEqual(assembled.radiation_source, "cu_ka")
        self.assertAlmostEqual(assembled.wavelength_angstrom, 1.5405980)
        self.assertEqual(assembled.coordinate_type, "two_theta")
        self.assertEqual(assembled.coordinate_column, "Angle")
        self.assertEqual(assembled.intensity_column, "Intensity")
        self.assertEqual((assembled.scan_min, assembled.scan_max), (10.0, 80.0))
        self.assertEqual(assembled.step_size, 0.02)
        self.assertEqual(assembled.scan_speed, 1.5)
        self.assertEqual(assembled.instrument_profile.instrument_label, "CuKa lab data")
        self.assertEqual(
            [step.step_type for step in assembled.synthesis_context.ordered_steps],
            ["weighing", "ball_milling", "heat_treatment", "xrd_measurement"],
        )
        self.assertEqual(
            [step.step_number for step in assembled.synthesis_context.ordered_steps],
            [1, 2, 3, 4],
        )
        self.assertEqual(
            [(precursor.name, precursor.formula) for precursor in assembled.synthesis_context.precursor_records],
            [("Nickel oxide", "NiO"), ("Cobalt oxide", "CoO")],
        )
        self.assertEqual(assembled.synthesis_context.temperatures_c, (950.0,))
        self.assertEqual(assembled.synthesis_context.ramp_rates_c_min, (5.0,))
        self.assertEqual(assembled.synthesis_context.hold_times_hours, (12.0,))
        self.assertEqual(assembled.synthesis_context.atmospheres, ("air", "oxygen"))
        self.assertEqual(assembled.synthesis_context.furnace_types, ("tube",))
        self.assertIn("Calcination step", assembled.synthesis_context.preparation_notes)
        self.assertEqual(assembled.algorithm_version, DEFAULT_XRD_ANALYSIS_CONFIG.algorithm_version)
        self.assertEqual(assembled.configuration_version, DEFAULT_XRD_ANALYSIS_CONFIG.configuration_version)
        self.assertEqual(assembled.warnings, ())

    def test_missing_optional_metadata_emits_warnings_instead_of_failure(self):
        material, recipe, trial, _raw_file = _fixture_records()
        trial.file_hash = None
        trial.raw_data_link = None
        trial.spacegroup = ""
        trial.element_sites = {}
        trial.exp_condition.additional_params = {
            "synthesis_steps": [
                {
                    "step_number": 1,
                    "step_type": "xrd_measurement",
                    "radiation": "unknown",
                }
            ]
        }

        assembled = assemble_xrd_analysis_input(
            material=material,
            recipe=recipe,
            trial=trial,
            raw_file_path="/tmp/only-reference.csv",
        )

        warning_codes = {warning.code for warning in assembled.warnings}
        self.assertEqual(
            warning_codes,
            {
                "missing_raw_file_hash",
                "missing_wavelength",
                "missing_coordinate_column",
                "missing_intensity_column",
                "missing_instrument_profile",
                "missing_scan_range",
            },
        )
        self.assertIsNone(assembled.expected_space_group)
        self.assertEqual(assembled.expected_site_assignments, ())

    def test_human_entered_phase_status_does_not_enter_analysis_input(self):
        material, recipe, trial, raw_file = _fixture_records()
        assembled = assemble_xrd_analysis_input(
            material=material,
            recipe=recipe,
            trial=trial,
            raw_file=raw_file,
        )
        payload = dumps_canonical_json(assembled)
        self.assertFalse(hasattr(assembled, "phase_status"))
        self.assertNotIn("multi_phase", payload)
        self.assertNotIn("phase_status", payload)

    def test_target_structure_fields_are_preserved(self):
        material, recipe, trial, raw_file = _fixture_records()
        assembled = assemble_xrd_analysis_input(
            material=material,
            recipe=recipe,
            trial=trial,
            raw_file=raw_file,
        )
        self.assertEqual(assembled.structure_family, "rocksalt")
        self.assertEqual(assembled.expected_space_group, "Fm-3m (#225)")
        self.assertEqual(
            [(item.element, item.site_label) for item in assembled.expected_site_assignments],
            [("Co", "4a"), ("O", "4b")],
        )

    def test_final_input_retains_no_model_instances(self):
        material, recipe, trial, raw_file = _fixture_records()
        assembled = assemble_xrd_analysis_input(
            material=material,
            recipe=recipe,
            trial=trial,
            raw_file=raw_file,
        )
        self.assertFalse(_contains_model_instance(assembled))

    def test_missing_identity_or_raw_file_reference_raises_clear_error(self):
        material, recipe, trial, _raw_file = _fixture_records()
        trial.trial_id = ""
        with self.assertRaises(XRDAnalysisInputError):
            assemble_xrd_analysis_input(material=material, recipe=recipe, trial=trial)

        material, recipe, trial, _raw_file = _fixture_records()
        trial.file_hash = None
        trial.raw_data_link = None
        trial.exp_condition.additional_params = {}
        with self.assertRaises(XRDAnalysisInputError):
            assemble_xrd_analysis_input(material=material, recipe=recipe, trial=trial)
