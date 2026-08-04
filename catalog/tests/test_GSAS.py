"""Smoke tests for catalog URLs (superuser bypasses approval gate)."""

import io
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings

from catalog.gsas_tools import peak_finder, peak_finder_fast
from catalog.rietveld_refinement import (
    RietveldRefinementError,
    _aggregate_element_fractions,
    _build_cod_search_params,
    _build_indexing_peak_list,
    _build_structure_summary,
    _extract_candidate_cells_with_retry,
    _extract_indexing_peak_list,
    derive_structures_from_xrd,
    _infer_wavelength_from_text,
    _load_xrd_dataframe,
    _match_sparse_simple_lattices,
    _normalize_optional_elements,
    _parse_formula,
    _rank_cod_query_candidates,
    _rank_candidate_cells,
    search_cod_by_indexing_or_refine,
    _score_cod_entry,
    _to_builtin,
    refine_element_amounts,
)


def _gsas_runtime_available():
    try:
        from GSASII import GSASIIscriptable  # type: ignore  # noqa: F401
        from GSASII import defaultIparms  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


class CatalogURLSmokeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "smokeuser",
            "smoke@example.com",
            "pass",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)


class PeakFinderContractTests(SimpleTestCase):
    def _clean_dataframe(self):
        theta = np.linspace(20.0, 20.6, 58)
        intensity = 50.0 + 389.1 * np.exp(-0.5 * ((theta - 20.3) / 0.025) ** 2)
        return pd.DataFrame({"Angle": theta, "Intensity": intensity})

    def _noisy_dataframe(self):
        theta = np.linspace(18.0, 34.0, 121)
        intensity = (
            62.0
            + 6.5 * np.sin(np.linspace(0.0, 7.5, 121))
            + 3.0 * np.cos(np.linspace(0.0, 15.0, 121))
            + 78.0 * np.exp(-0.5 * ((theta - 23.2) / 0.24) ** 2)
            + 42.0 * np.exp(-0.5 * ((theta - 29.1) / 0.36) ** 2)
        )
        return pd.DataFrame({"Angle": theta, "Intensity": intensity})

    def _assert_peak_contract(self, peaks):
        self.assertIsInstance(peaks, list)
        self.assertTrue(peaks)
        for peak in peaks:
            self.assertIsInstance(peak, dict)
            self.assertIn("two_theta", peak)
            self.assertIn("intensity", peak)
            self.assertIn("area", peak)

    def test_peak_finder_fast_clean_pattern_contract(self):
        peaks, gpx_bytes, overlay_uri = peak_finder_fast(self._clean_dataframe())

        self._assert_peak_contract(peaks)
        self.assertEqual(gpx_bytes, b"")
        self.assertTrue(overlay_uri.startswith("data:image/png;base64,"))
        self.assertGreaterEqual(len(peaks), 1)

    def test_peak_finder_fast_noisy_pattern_contract(self):
        peaks, gpx_bytes, overlay_uri = peak_finder_fast(self._noisy_dataframe())

        self._assert_peak_contract(peaks)
        self.assertEqual(gpx_bytes, b"")
        self.assertTrue(overlay_uri.startswith("data:image/png;base64,"))
        self.assertGreaterEqual(len(peaks), 1)
        self.assertAlmostEqual(peaks[0]["two_theta"], 23.2, delta=0.35)

    def test_peak_finder_wrapper_preserves_gsas_contract_when_backend_succeeds(self):
        fake_peaks = [{"two_theta": 20.3, "intensity": 439.1, "area": 37.5}]
        with patch("catalog.gsas_tools.find_gsas_peaks", return_value=(fake_peaks, b"gpx")) as mocked_backend:
            peaks, gpx_bytes, overlay_uri = peak_finder(
                self._clean_dataframe(),
                use_gsas=True,
            )

        mocked_backend.assert_called_once()
        self.assertEqual(peaks, fake_peaks)
        self.assertEqual(gpx_bytes, b"gpx")
        self.assertTrue(overlay_uri.startswith("data:image/png;base64,"))

    @unittest.skipUnless(_gsas_runtime_available(), "GSAS-II runtime not available")
    def test_peak_finder_real_gsas_integration_contract(self):
        peaks, gpx_bytes, overlay_uri = peak_finder(
            self._clean_dataframe(),
            use_gsas=True,
        )

        self._assert_peak_contract(peaks)
        self.assertTrue(gpx_bytes)
        self.assertTrue(overlay_uri.startswith("data:image/png;base64,"))


class RietveldRefinementHelpersTests(SimpleTestCase):
    def test_parse_formula_supports_parentheses_and_hydrates(self):
        self.assertEqual(
            _parse_formula("Ca3(PO4)2"),
            {"Ca": 3.0, "P": 2.0, "O": 8.0},
        )
        self.assertEqual(
            _parse_formula("CuSO4·5H2O"),
            {"Cu": 1.0, "S": 1.0, "O": 9.0, "H": 10.0},
        )

    def test_generic_xrd_loader_supports_whitespace_files(self):
        df = _load_xrd_dataframe(
            io.StringIO(
                "# two-theta intensity sigma\n"
                "10.0 100 5\n"
                "11.0 150 6\n"
                "12.0 90 4\n"
            )
        )

        self.assertEqual(list(df.columns), ["Angle", "Intensity", "Sigma"])
        self.assertEqual(df.shape, (3, 3))
        self.assertAlmostEqual(float(df.iloc[1]["Intensity"]), 150.0)

    def test_wavelength_inference_supports_loop_metadata_and_inline_comments(self):
        self.assertAlmostEqual(
            _infer_wavelength_from_text(
                "[Measurement conditions]\n"
                "K-Alpha1 wavelength,1.5405980\n"
                "[Scan points]\n"
                "Angle,Intensity\n20,100\n"
            ),
            1.5405980,
        )
        self.assertAlmostEqual(
            _infer_wavelength_from_text(
                "# Radiation: Cu Kα, lambda = 1.5406 Å\n"
                "Angle,Intensity\n20,100\n"
            ),
            1.5406,
        )

    def test_indexing_peak_list_returns_d_spacings_sorted_descending(self):
        theta = np.array([20.0, 20.1, 20.2, 30.0, 30.1, 30.2, 40.0, 40.1, 40.2])
        intensity = np.array([10.0, 100.0, 10.0, 8.0, 80.0, 8.0, 6.0, 60.0, 6.0])

        peaks = _build_indexing_peak_list(theta, intensity, wavelength=1.5406, max_peaks=3)

        self.assertEqual(len(peaks), 3)
        self.assertTrue(all(row[2] is True for row in peaks))
        self.assertGreaterEqual(float(peaks[0][-2]), float(peaks[1][-2]))
        self.assertGreaterEqual(float(peaks[1][-2]), float(peaks[2][-2]))

    def test_aggregate_element_fractions_reports_requested_and_unexpected(self):
        phase_results = [
            {
                "name": "rocksalt",
                "normalized_phase_fraction": 0.75,
                "phase_composition": {"Co": 1.0, "O": 1.0},
            },
            {
                "name": "spinel",
                "normalized_phase_fraction": 0.25,
                "phase_composition": {"Co": 1.0, "Fe": 2.0, "O": 4.0},
            },
        ]

        all_fractions, requested_fractions, missing, unexpected = _aggregate_element_fractions(
            phase_results,
            {"Co": 1.0, "Fe": 1.0},
        )

        self.assertAlmostEqual(sum(all_fractions.values()), 1.0)
        self.assertAlmostEqual(sum(requested_fractions.values()), 1.0)
        self.assertEqual(missing, [])
        self.assertEqual(unexpected, ["O"])
        self.assertGreater(all_fractions["O"], all_fractions["Co"])

    def test_summary_uses_builtin_types_and_best_candidate(self):
        summary = _build_structure_summary(
            [
                {
                    "bravais_index": 9,
                    "bravais_name": "Orthorhombic-A",
                    "m20": 195.0,
                    "x20": 6.0,
                    "generated_hkls": 9,
                    "unit_cell": {
                        "length_a": np.float64(3.25),
                        "length_b": np.float64(2.52),
                        "length_c": np.float64(3.26),
                        "angle_alpha": np.float64(90.0),
                        "angle_beta": np.float64(90.0),
                        "angle_gamma": np.float64(90.0),
                        "volume": np.float64(26.7),
                    },
                    "space_group_candidates": [],
                    "crystal_system": "orthorhombic",
                },
                {
                    "bravais_index": 0,
                    "bravais_name": "Cubic-F",
                    "m20": 88.1,
                    "x20": 4.0,
                    "generated_hkls": 9,
                    "unit_cell": {
                        "length_a": np.float64(5.64),
                        "length_b": np.float64(5.64),
                        "length_c": np.float64(5.64),
                        "angle_alpha": np.float64(90.0),
                        "angle_beta": np.float64(90.0),
                        "angle_gamma": np.float64(90.0),
                        "volume": np.float64(179.4),
                    },
                    "space_group_candidates": [],
                    "crystal_system": "cubic",
                },
                {
                    "bravais_index": 7,
                    "bravais_name": "Orthorhombic-F",
                    "m20": 10.0,
                    "x20": 0.0,
                    "generated_hkls": 25,
                    "unit_cell": {
                        "length_a": np.float64(4.9),
                        "length_b": np.float64(5.6),
                        "length_c": np.float64(7.5),
                        "angle_alpha": np.float64(90.0),
                        "angle_beta": np.float64(90.0),
                        "angle_gamma": np.float64(90.0),
                        "volume": np.float64(204.5),
                    },
                    "space_group_candidates": [],
                    "crystal_system": "orthorhombic",
                },
            ],
            [
                {
                    "name": "phase1",
                    "space_group": "F m -3 m",
                    "unit_cell": {
                        "length_a": np.float64(5.64),
                        "length_b": np.float64(5.64),
                        "length_c": np.float64(5.64),
                        "angle_alpha": np.float64(90.0),
                        "angle_beta": np.float64(90.0),
                        "angle_gamma": np.float64(90.0),
                        "volume": np.float64(179.4),
                    },
                    "exported_cif_path": "/tmp/phase1.cif",
                    "solution_strategy": "template_refinement",
                }
            ],
            np.float64(12.5),
            solution_mode="template_refinement",
        )

        normalized = _to_builtin(summary)

        self.assertEqual(normalized["candidate_count"], 3)
        self.assertEqual(normalized["best_candidate_cell"]["bravais_name"], "Cubic-F")
        self.assertEqual(normalized["refined_phase_count"], 1)
        self.assertEqual(normalized["fit_quality"], "good")
        self.assertIsInstance(normalized["weighted_r_factor"], float)
        self.assertIsInstance(
            normalized["refined_phases"][0]["unit_cell"]["length_a"],
            float,
        )

    def test_rank_candidate_cells_penalizes_tiny_cells_and_prefers_template_match(self):
        ranked = _rank_candidate_cells(
            [
                {
                    "bravais_index": 9,
                    "bravais_name": "Orthorhombic-A",
                    "m20": 195.0,
                    "x20": 6.0,
                    "generated_hkls": 9,
                    "unit_cell": {
                        "length_a": 3.25,
                        "length_b": 2.52,
                        "length_c": 3.26,
                        "angle_alpha": 90.0,
                        "angle_beta": 90.0,
                        "angle_gamma": 90.0,
                        "volume": 26.7,
                    },
                    "space_group_candidates": [],
                    "crystal_system": "orthorhombic",
                },
                {
                    "bravais_index": 0,
                    "bravais_name": "Cubic-F",
                    "m20": 88.1,
                    "x20": 4.0,
                    "generated_hkls": 9,
                    "unit_cell": {
                        "length_a": 5.64,
                        "length_b": 5.64,
                        "length_c": 5.64,
                        "angle_alpha": 90.0,
                        "angle_beta": 90.0,
                        "angle_gamma": 90.0,
                        "volume": 179.4,
                    },
                    "space_group_candidates": [],
                    "crystal_system": "cubic",
                },
            ],
            [
                {
                    "unit_cell": {
                        "length_a": 5.643,
                        "length_b": 5.643,
                        "length_c": 5.643,
                        "angle_alpha": 90.0,
                        "angle_beta": 90.0,
                        "angle_gamma": 90.0,
                        "volume": 179.7,
                    }
                }
            ],
        )

        self.assertEqual(ranked[0]["bravais_name"], "Cubic-F")

    def test_rank_cod_query_candidates_prioritizes_highest_m20(self):
        ranked = _rank_cod_query_candidates(
            [
                {
                    "bravais_index": 2,
                    "bravais_name": "Cubic-P",
                    "m20": 3.9,
                    "x20": 0.0,
                    "generated_hkls": 48,
                    "unit_cell": {
                        "length_a": 7.5,
                        "length_b": 7.5,
                        "length_c": 7.5,
                        "angle_alpha": 90.0,
                        "angle_beta": 90.0,
                        "angle_gamma": 90.0,
                        "volume": 421.9,
                    },
                },
                {
                    "bravais_index": 0,
                    "bravais_name": "Cubic-F",
                    "m20": 88.1,
                    "x20": 4.0,
                    "generated_hkls": 9,
                    "unit_cell": {
                        "length_a": 5.64,
                        "length_b": 5.64,
                        "length_c": 5.64,
                        "angle_alpha": 90.0,
                        "angle_beta": 90.0,
                        "angle_gamma": 90.0,
                        "volume": 179.4,
                    },
                },
            ]
        )

        self.assertEqual(ranked[0]["bravais_name"], "Cubic-F")

    def test_sparse_simple_lattice_matcher_identifies_cubic_f(self):
        wavelength = 1.5406
        lattice_a = 5.64
        n_values = [3, 4, 8, 11, 12]
        indexing_peaks = []
        for idx, n_value in enumerate(n_values, start=1):
            d_obs = lattice_a / np.sqrt(float(n_value))
            two_theta = float(
                2.0 * np.degrees(np.arcsin(wavelength / (2.0 * d_obs)))
            )
            indexing_peaks.append(
                [two_theta, float(1000 - idx * 50), True, False, 0, 0, 0, d_obs, d_obs]
            )

        matched = _match_sparse_simple_lattices(indexing_peaks)

        self.assertTrue(matched)
        self.assertEqual(matched[0]["bravais_name"], "Cubic-F")
        self.assertAlmostEqual(matched[0]["unit_cell"]["length_a"], lattice_a, places=2)

    def test_extract_candidate_cells_with_retry_prefers_successful_scipy_retry(self):
        df = pd.DataFrame(
            {
                "Angle": [10.0, 11.0, 12.0, 13.0, 14.0],
                "Intensity": [5.0, 9.0, 7.0, 6.0, 4.0],
            }
        )
        theta = np.asarray(df["Angle"], dtype=float)
        intensity = np.asarray(df["Intensity"], dtype=float)
        first_peaks = [[20.0, 100.0, True, False, 0, 0, 0, 4.0, 4.0]]
        retry_peaks = [[30.0, 80.0, True, False, 0, 0, 0, 3.0, 3.0]]
        retry_cells = [
            {
                "bravais_index": 0,
                "bravais_name": "Cubic-F",
                "crystal_system": "cubic",
                "m20": 88.1,
                "x20": 4.0,
                "unit_cell": {
                    "length_a": 5.64,
                    "length_b": 5.64,
                    "length_c": 5.64,
                    "angle_alpha": 90.0,
                    "angle_beta": 90.0,
                    "angle_gamma": 90.0,
                    "volume": 179.4,
                },
                "generated_hkls": 5,
                "space_group_candidates": [],
            }
        ]

        with (
            patch(
                "catalog.rietveld_refinement._extract_indexing_peak_list",
                side_effect=[
                    (first_peaks, ["initial peak warning"], "/tmp/first_overlay.png"),
                    (retry_peaks, ["retry peak warning"], "/tmp/retry_overlay.png"),
                ],
            ),
            patch(
                "catalog.rietveld_refinement._solve_candidate_cells",
                side_effect=[([], ["initial indexing warning"]), (retry_cells, ["retry indexing warning"])],
            ),
        ):
            peaks, cells, warnings, overlay_path = _extract_candidate_cells_with_retry(
                df,
                theta,
                intensity,
                wavelength=1.5406,
                max_peaks=5,
                bravais_flags=[True] * 18,
                volume_guess=200.0,
                use_gsas_peak_finder=True,
            )

        self.assertEqual(peaks, retry_peaks)
        self.assertEqual(cells, retry_cells)
        self.assertIn("initial peak warning", warnings)
        self.assertIn("retry peak warning", warnings)
        self.assertIn("initial indexing warning", warnings)
        self.assertIn("retry indexing warning", warnings)
        self.assertEqual(overlay_path, "/tmp/retry_overlay.png")
        self.assertIn(
            "GSAS-II peak-based indexing returned no candidate cells; retried indexing with SciPy peak picking.",
            warnings,
        )

    def test_extract_indexing_peak_list_saves_overlay_for_scipy_path(self):
        df = pd.DataFrame(
            {
                "Angle": [10.0, 11.0, 12.0, 13.0, 14.0],
                "Intensity": [5.0, 9.0, 7.0, 6.0, 4.0],
            }
        )
        theta = np.asarray(df["Angle"], dtype=float)
        intensity = np.asarray(df["Intensity"], dtype=float)

        with tempfile.TemporaryDirectory() as tmpdir:
            overlay_path = f"{tmpdir}/overlay.png"

            def fake_peak_finder(_df, **kwargs):
                with open(kwargs["overlay_output_path"], "wb") as handle:
                    handle.write(b"png")
                return (
                    [
                        {"two_theta": 11.0, "intensity": 9.0},
                        {"two_theta": 13.0, "intensity": 6.0},
                    ],
                    b"",
                    "data:image/png;base64,",
                )

            with patch("catalog.gsas_tools.peak_finder", side_effect=fake_peak_finder):
                indexing_peaks, warnings, returned_overlay_path = _extract_indexing_peak_list(
                    df,
                    theta,
                    intensity,
                    wavelength=1.5406,
                    max_peaks=5,
                    use_gsas_peak_finder=False,
                    overlay_output_path=overlay_path,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(returned_overlay_path, overlay_path)
        self.assertEqual(len(indexing_peaks), 2)
        self.assertGreater(indexing_peaks[0][7], indexing_peaks[1][7])

    def test_search_cod_queries_strongest_indexed_cells_first(self):
        strongest = {
            "bravais_index": 0,
            "bravais_name": "Cubic-F",
            "m20": 88.1,
            "x20": 4.0,
            "generated_hkls": 9,
            "unit_cell": {
                "length_a": 5.64,
                "length_b": 5.64,
                "length_c": 5.64,
                "angle_alpha": 90.0,
                "angle_beta": 90.0,
                "angle_gamma": 90.0,
                "volume": 179.4,
            },
            "space_group_candidates": [],
            "crystal_system": "cubic",
        }
        weaker = {
            "bravais_index": 2,
            "bravais_name": "Cubic-P",
            "m20": 3.9,
            "x20": 0.0,
            "generated_hkls": 48,
            "unit_cell": {
                "length_a": 7.5,
                "length_b": 7.5,
                "length_c": 7.5,
                "angle_alpha": 90.0,
                "angle_beta": 90.0,
                "angle_gamma": 90.0,
                "volume": 421.9,
            },
            "space_group_candidates": [],
            "crystal_system": "cubic",
        }

        indexing_result = {
            "candidate_cells": [weaker, strongest],
            "warnings": [],
            "summary": {
                "top_candidate_cells": [weaker, strongest],
                "best_candidate_cell": weaker,
            },
        }
        queried_a_values: list[str] = []

        def record_fetch(params):
            queried_a_values.append(params["amin"])
            return []

        with (
            patch("catalog.rietveld_refinement.derive_structures_from_xrd", return_value=indexing_result),
            patch("catalog.rietveld_refinement._fetch_cod_entries", side_effect=record_fetch),
        ):
            result = search_cod_by_indexing_or_refine(
                "dummy.csv",
                {"Na": 1.0, "Cl": 1.0},
                cod_candidate_cells=2,
                cod_hit_limit=2,
                cod_score_threshold=0.0,
            )

        self.assertEqual(result["status"], "fallback_refinement")
        self.assertEqual(len(queried_a_values), 2)
        self.assertEqual(queried_a_values[0], "5.56000")
        self.assertEqual(queried_a_values[1], "7.42000")

    def test_search_cod_fallback_dedupes_repeated_warnings(self):
        indexing_result = {
            "candidate_cells": [],
            "warnings": [
                "GSAS-II peak-based indexing returned no candidate cells; retried indexing with SciPy peak picking.",
                "Unit-cell indexing can propose lattice candidates, but space group cannot be determined uniquely from indexing alone.",
            ],
            "summary": {},
        }
        fallback_result = {
            "candidate_cells": [],
            "warnings": [
                "GSAS-II peak-based indexing returned no candidate cells; retried indexing with SciPy peak picking.",
                "Unit-cell indexing can propose lattice candidates, but space group cannot be determined uniquely from indexing alone.",
                "No fallback phase models were supplied.",
            ],
            "summary": {},
        }

        with (
            patch(
                "catalog.rietveld_refinement.derive_structures_from_xrd",
                side_effect=[indexing_result, fallback_result],
            ),
            patch("catalog.rietveld_refinement._fetch_cod_entries", return_value=[]),
        ):
            result = search_cod_by_indexing_or_refine(
                "dummy.csv",
                {"Na": 1.0, "Cl": 1.0},
                cod_candidate_cells=1,
                cod_hit_limit=1,
            )

        self.assertEqual(result["status"], "fallback_refinement")
        self.assertEqual(
            result["warnings"].count(
                "GSAS-II peak-based indexing returned no candidate cells; retried indexing with SciPy peak picking."
            ),
            1,
        )
        self.assertEqual(
            result["warnings"].count(
                "Unit-cell indexing can propose lattice candidates, but space group cannot be determined uniquely from indexing alone."
            ),
            1,
        )
        self.assertIn("No fallback phase models were supplied.", result["warnings"])
        self.assertIn("No suitable COD match found; falling back to local refinement pathway.", result["warnings"])

    def test_search_cod_summary_uses_matched_candidate_cell(self):
        strongest = {
            "bravais_index": 0,
            "bravais_name": "Cubic-F",
            "m20": 88.1,
            "x20": 4.0,
            "generated_hkls": 9,
            "unit_cell": {
                "length_a": 5.64,
                "length_b": 5.64,
                "length_c": 5.64,
                "angle_alpha": 90.0,
                "angle_beta": 90.0,
                "angle_gamma": 90.0,
                "volume": 179.4,
            },
            "space_group_candidates": [],
            "crystal_system": "cubic",
        }
        misleading_best = {
            "bravais_index": 1,
            "bravais_name": "Cubic-I",
            "m20": 5.4,
            "x20": 0.0,
            "generated_hkls": 56,
            "unit_cell": {
                "length_a": 10.46,
                "length_b": 10.46,
                "length_c": 10.46,
                "angle_alpha": 90.0,
                "angle_beta": 90.0,
                "angle_gamma": 90.0,
                "volume": 1146.4,
            },
            "space_group_candidates": [],
            "crystal_system": "cubic",
        }
        indexing_result = {
            "candidate_cells": [misleading_best, strongest],
            "warnings": [],
            "summary": {
                "top_candidate_cells": [misleading_best, strongest],
                "best_candidate_cell": misleading_best,
            },
        }
        cod_entry = {
            "file": "9003308",
            "a": "5.6401",
            "b": "5.6401",
            "c": "5.6401",
            "alpha": "90",
            "beta": "90",
            "gamma": "90",
            "vol": "179.416",
            "formula": "- Cl Na -",
            "sg": "F m -3 m",
        }

        with (
            patch("catalog.rietveld_refinement.derive_structures_from_xrd", return_value=indexing_result),
            patch("catalog.rietveld_refinement._fetch_cod_entries", return_value=[cod_entry]),
            patch("catalog.rietveld_refinement._download_cod_cif", return_value="/tmp/cod_9003308.cif"),
        ):
            result = search_cod_by_indexing_or_refine(
                "dummy.csv",
                {"Na": 1.0, "Cl": 1.0},
                cod_candidate_cells=2,
                cod_hit_limit=2,
                cod_score_threshold=0.0,
            )

        self.assertEqual(result["status"], "cod_match")
        self.assertEqual(result["summary"]["best_candidate_cell"]["bravais_name"], "Cubic-F")
        self.assertEqual(result["cod_match"]["matched_candidate_cell"]["bravais_name"], "Cubic-F")

    @override_settings(MEDIA_ROOT="/tmp/loop_test_media")
    def test_derive_structures_and_refinement_default_to_media_artifact_dirs(self):
        df = pd.DataFrame(
            {
                "Angle": [10.0, 11.0, 12.0, 13.0, 14.0],
                "Intensity": [5.0, 9.0, 7.0, 6.0, 4.0],
            }
        )
        candidate = {
            "bravais_index": 0,
            "bravais_name": "Cubic-F",
            "m20": 88.1,
            "x20": 4.0,
            "generated_hkls": 9,
            "unit_cell": {
                "length_a": 5.64,
                "length_b": 5.64,
                "length_c": 5.64,
                "angle_alpha": 90.0,
                "angle_beta": 90.0,
                "angle_gamma": 90.0,
                "volume": 179.4,
            },
            "space_group_candidates": [],
            "crystal_system": "cubic",
        }
        refinement_payload = {
            "fit_limits": [10.0, 14.0],
            "phase_results": [
                {
                    "name": "NaCl_rocksalt",
                    "cif_path": "/tmp/input.cif",
                    "unit_cell": candidate["unit_cell"],
                    "space_group": "F m -3 m",
                    "phase_composition": {"Na": 1.0, "Cl": 1.0},
                    "raw_phase_fraction": 1.0,
                    "exported_cif_path": "/tmp/loop_test_media/derived_cifs/tmp__sample__NaCl_rocksalt.cif",
                    "solution_strategy": "template_refinement",
                }
            ],
            "cif_exports": ["/tmp/loop_test_media/derived_cifs/tmp__sample__NaCl_rocksalt.cif"],
            "weighted_r_factor": 12.5,
            "project_path": None,
            "gpx_bytes": b"",
        }

        with (
            patch(
                "catalog.rietveld_refinement._load_xrd_dataframe",
                return_value=df,
            ),
            patch(
                "catalog.rietveld_refinement._infer_wavelength",
                return_value=1.5406,
            ),
            patch(
                "catalog.rietveld_refinement._extract_candidate_cells_with_retry",
                return_value=(
                    [[30.0, 100.0, True, False, 0, 0, 0, 2.9, 2.9]],
                    [candidate],
                    [],
                    "/tmp/loop_test_media/phase_overlays/sample.png",
                ),
            ),
            patch(
                "catalog.rietveld_refinement._run_template_refinement",
                return_value=refinement_payload,
            ) as mocked_refine,
        ):
            derived = derive_structures_from_xrd(
                "/tmp/sample.csv",
                {"Na": 1.0, "Cl": 1.0},
                phase_models=["/tmp/input.cif"],
            )
            refined = refine_element_amounts(
                "/tmp/sample.csv",
                {"Na": 1.0, "Cl": 1.0},
                phase_models=["/tmp/input.cif"],
            )

        self.assertEqual(derived["phase_overlay_path"], "/tmp/loop_test_media/phase_overlays/sample.png")
        self.assertEqual(
            str(mocked_refine.call_args_list[0].kwargs["export_root"]),
            "/tmp/loop_test_media/derived_cifs",
        )
        self.assertEqual(mocked_refine.call_args_list[0].kwargs["export_prefix"], "tmp__sample")
        self.assertEqual(mocked_refine.call_args_list[1].kwargs["export_prefix"], "tmp__sample")
        self.assertEqual(
            str(mocked_refine.call_args_list[1].kwargs["export_root"]),
            "/tmp/loop_test_media/derived_cifs",
        )
        self.assertEqual(
            refined["cif_exports"],
            ["/tmp/loop_test_media/derived_cifs/tmp__sample__NaCl_rocksalt.cif"],
        )

    def test_build_cod_search_params_uses_cell_ranges_and_elements(self):
        params = _build_cod_search_params(
            {
                "unit_cell": {
                    "length_a": 5.64,
                    "length_b": 5.64,
                    "length_c": 5.64,
                    "angle_alpha": 90.0,
                    "angle_beta": 90.0,
                    "angle_gamma": 90.0,
                    "volume": 179.4,
                }
            },
            {"Na": 1.0, "Cl": 1.0},
            length_tol_angstrom=0.1,
            angle_tol_deg=2.0,
            volume_tol_fraction=0.1,
        )

        self.assertEqual(params["format"], "json")
        self.assertEqual(params["strictmin"], "2")
        self.assertEqual(params["strictmax"], "2")
        self.assertEqual({params["el1"], params["el2"]}, {"Na", "Cl"})
        self.assertEqual(params["amin"], "5.54000")
        self.assertEqual(params["amax"], "5.74000")

    def test_build_cod_search_params_omits_element_filter_when_unknown(self):
        params = _build_cod_search_params(
            {
                "unit_cell": {
                    "length_a": 5.64,
                    "length_b": 5.64,
                    "length_c": 5.64,
                    "angle_alpha": 90.0,
                    "angle_beta": 90.0,
                    "angle_gamma": 90.0,
                    "volume": 179.4,
                }
            },
            {},
        )

        self.assertNotIn("strictmin", params)
        self.assertNotIn("strictmax", params)
        self.assertFalse(any(key.startswith("el") for key in params))

    def test_normalize_optional_elements_allows_unknown(self):
        self.assertEqual(_normalize_optional_elements(None), {})
        self.assertEqual(_normalize_optional_elements(""), {})
        self.assertEqual(_normalize_optional_elements([]), {})
        self.assertEqual(_normalize_optional_elements({}), {})

    def test_score_cod_entry_prefers_exact_elements_and_close_cell(self):
        candidate = {
            "unit_cell": {
                "length_a": 5.64,
                "length_b": 5.64,
                "length_c": 5.64,
                "angle_alpha": 90.0,
                "angle_beta": 90.0,
                "angle_gamma": 90.0,
                "volume": 179.4,
            }
        }
        exact = _score_cod_entry(
            {
                "file": "1234567",
                "a": "5.641",
                "b": "5.641",
                "c": "5.641",
                "alpha": "90",
                "beta": "90",
                "gamma": "90",
                "vol": "179.5",
                "formula": "Cl 3 Na 1",
                "sg": "P m -3 m",
            },
            candidate,
            {"Na": 1.0, "Cl": 1.0},
        )
        inexact = _score_cod_entry(
            {
                "file": "7654321",
                "a": "5.9",
                "b": "5.9",
                "c": "5.9",
                "alpha": "90",
                "beta": "90",
                "gamma": "90",
                "vol": "205.0",
                "formula": "Cl 1 K 1",
                "sg": "P m -3 m",
            },
            candidate,
            {"Na": 1.0, "Cl": 1.0},
        )

        self.assertTrue(exact["all_requested_present"])
        self.assertGreater(exact["score"], inexact["score"])

    def test_refinement_requires_phase_models(self):
        with self.assertRaises(RietveldRefinementError):
            refine_element_amounts(
                io.StringIO("Angle,Intensity\n10,100\n11,150\n12,90\n13,80\n14,60\n"),
                ["Co", "Fe", "O"],
                phase_models=[],
            )
