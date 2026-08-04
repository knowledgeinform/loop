"""Unit tests for the multi-format XRD file parser in ``catalog.utils``."""

import io
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase

from catalog.utils import (
    is_reflection_list_text,
    is_rigaku_asc_text,
    parse_columnar_xrd_text,
    parse_reflection_list_text,
    parse_rigaku_asc_text,
    parse_xrd_file,
)


# Rigaku ASCII export: implicit 2theta axis in the header, intensity-only data
# (several counts per line). Often saved with a .csv extension.
RIGAKU_ASC = (
    "*TYPE = Raw\n"
    "*GONIO = RIGAKU\n"
    "*XUNIT = deg.\n"
    "*BEGIN\n"
    "*START = 10.0\n"
    "*STOP = 14.0\n"
    "*STEP = 1.0\n"
    "*COUNT = 5\n"
    "100, 200, 300\n"
    "400, 500\n"
    "*END\n"
    "*EOF\n"
)


# Abbreviated WO3 ICDD PDF card, including the preamble lines that must NOT be
# mistaken for data (e.g. the CELL line, which has three+ numbers but no hkl).
PDF_CARD = (
    "PDF#32-1395: QM=Common(+); d=Other/Unknown; I=(Unknown)\n"
    "Tungsten Oxide\n"
    "WO3\n"
    "Radiation=CuKa1\tLambda=1.5406\tFilter=\n"
    "CELL: 7.309 x 7.522 x 7.678 <88.81 x 90.92 x 90.93>\n"
    "Strong Lines: 3.76/X  3.65/X  3.84/9  2.63/4\n"
    "\n"
    "2-Theta    d(?)   I(f)  ( h k l)   Theta  1/(2d)   2pi/d  n^2  \n"
    " 23.143  3.8400   85.0  ( 0 0 2)  11.572  0.1302  1.6362     \n"
    " 23.643  3.7600  100.0  ( 0 2 0)  11.821  0.1330  1.6711     \n"
    " 26.490  3.3620    9.0  (-1 2 0)  13.245  0.1487  1.8689     \n"
)


LOOP_CSV = (
    "[Measurement conditions]\n"
    "K-Alpha1 wavelength,1.5405980\n"
    "Angle,Intensity\n"
    "20.0,100\n"
    "20.1,150\n"
    "20.2,90\n"
)

POWDLL_TXT = (
    "000000-0000 - Converted with PowDLL\n"
    "9.998    412\n"
    "10.01854 415\n"
    "10.03909 427\n"
)


class ColumnarParserTests(SimpleTestCase):
    def test_skips_comments_and_keeps_sigma(self):
        df = parse_columnar_xrd_text(
            "# two-theta intensity sigma\n10.0 100 5\n11.0 150 6\n"
        )
        self.assertEqual(list(df.columns), ["Angle", "Intensity", "Sigma"])
        self.assertEqual(df.shape, (2, 3))

    def test_two_column_drops_sigma(self):
        df = parse_columnar_xrd_text(POWDLL_TXT)
        self.assertEqual(list(df.columns), ["Angle", "Intensity"])
        self.assertAlmostEqual(float(df.iloc[0]["Angle"]), 9.998)
        self.assertAlmostEqual(float(df.iloc[1]["Intensity"]), 415.0)

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            parse_columnar_xrd_text("# nothing parseable\n\n")


class ParseXrdFileDispatchTests(SimpleTestCase):
    def test_csv_returns_loop_metadata_and_data(self):
        metadata, df = parse_xrd_file(io.StringIO(LOOP_CSV), "trial.csv")
        self.assertIn(("K-Alpha1 wavelength", "1.5405980"), metadata)
        self.assertEqual(list(df.columns), ["Angle", "Intensity"])
        self.assertEqual(df.shape, (3, 2))

    def test_txt_uses_columnar_parser_with_no_metadata(self):
        metadata, df = parse_xrd_file(io.StringIO(POWDLL_TXT), "trial.txt")
        self.assertEqual(metadata, [])
        self.assertEqual(list(df.columns), ["Angle", "Intensity"])
        self.assertEqual(df.shape, (3, 2))

    def test_raw_dispatches_to_gsas_reader(self):
        fake = pd.DataFrame({"Angle": [20.0, 21.0], "Intensity": [10.0, 20.0]})
        with patch("catalog.gsas_runtime.read_powder_pattern", return_value=fake) as reader:
            metadata, df = parse_xrd_file("/tmp/some_file.raw", "some_file.raw")
        reader.assert_called_once_with("/tmp/some_file.raw")
        self.assertEqual(metadata, [])
        self.assertEqual(list(df.columns), ["Angle", "Intensity"])

    def test_raw_stream_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_xrd_file(io.BytesIO(b"RAW4.00"), "some_file.raw")


class ReflectionListParserTests(SimpleTestCase):
    def test_detection(self):
        self.assertTrue(is_reflection_list_text(PDF_CARD))
        self.assertFalse(is_reflection_list_text(LOOP_CSV))
        self.assertFalse(is_reflection_list_text(POWDLL_TXT))

    def test_uses_if_column_not_d_column(self):
        df = parse_reflection_list_text(PDF_CARD)
        self.assertEqual(list(df.columns), ["Angle", "Intensity"])
        self.assertEqual(df.shape, (3, 2))
        # Angle is 2-Theta (col 0); Intensity is I(f) (col 2), NOT d (col 1).
        self.assertAlmostEqual(float(df.iloc[0]["Angle"]), 23.143)
        self.assertAlmostEqual(float(df.iloc[0]["Intensity"]), 85.0)
        self.assertAlmostEqual(float(df.iloc[1]["Intensity"]), 100.0)
        # d-spacings (3.84, 3.76, 3.36) must never appear as intensity.
        self.assertNotIn(3.84, list(df["Intensity"]))

    def test_preamble_lines_are_not_parsed_as_data(self):
        # The CELL line has 4 parseable numbers but no (h k l) -> must be skipped.
        df = parse_reflection_list_text(PDF_CARD)
        self.assertNotIn(7.309, list(df["Angle"]))

    def test_tagged_as_stick_pattern(self):
        df = parse_reflection_list_text(PDF_CARD)
        self.assertEqual(df.attrs.get("plot_style"), "stick")

    def test_dispatch_extracts_metadata_and_routes(self):
        metadata, df = parse_xrd_file(io.StringIO(PDF_CARD), "CoO PDF#97-005-3929.txt")
        self.assertIn(("Source", "ICDD PDF reference card"), metadata)
        self.assertIn(("Lambda", "1.5406"), metadata)
        self.assertEqual(df.attrs.get("plot_style"), "stick")
        self.assertAlmostEqual(float(df.iloc[0]["Intensity"]), 85.0)


class RigakuAscParserTests(SimpleTestCase):
    def test_detection(self):
        self.assertTrue(is_rigaku_asc_text(RIGAKU_ASC))
        self.assertFalse(is_rigaku_asc_text(LOOP_CSV))
        self.assertFalse(is_rigaku_asc_text(POWDLL_TXT))

    def test_reconstructs_angle_axis_from_header(self):
        df = parse_rigaku_asc_text(RIGAKU_ASC)
        self.assertEqual(list(df.columns), ["Angle", "Intensity"])
        # 5 intensities flattened across the two data lines.
        self.assertEqual(list(df["Intensity"]), [100.0, 200.0, 300.0, 400.0, 500.0])
        # Angle reconstructed from START=10 .. STOP=14 over 5 points (step 1.0).
        self.assertAlmostEqual(float(df.iloc[0]["Angle"]), 10.0)
        self.assertAlmostEqual(float(df.iloc[-1]["Angle"]), 14.0)

    def test_dispatch_routes_rigaku_csv(self):
        # Extension is .csv but content is Rigaku ASCII -> content wins.
        _, df = parse_xrd_file(io.StringIO(RIGAKU_ASC), "scan.csv")
        self.assertEqual(list(df["Intensity"]), [100.0, 200.0, 300.0, 400.0, 500.0])

    def test_dispatch_extracts_header_metadata(self):
        metadata, _ = parse_xrd_file(io.StringIO(RIGAKU_ASC), "scan.csv")
        self.assertIn(("Source", "Rigaku ASCII export"), metadata)
        self.assertIn(("GONIO", "RIGAKU"), metadata)
        self.assertIn(("XUNIT", "deg."), metadata)
        self.assertIn(("START", "10.0"), metadata)
        # Markers without '=' must not leak in as metadata keys.
        self.assertNotIn("BEGIN", [k for k, _ in metadata])


class PlausibilityGuardTests(SimpleTestCase):
    def test_unrecognised_layout_with_huge_angles_is_rejected(self):
        # Two intensity-only columns (no angle) -> "angle" would exceed 180.
        garbage = "534, 562\n566, 567\n1050, 970\n"
        with self.assertRaises(ValueError):
            parse_xrd_file(io.StringIO(garbage), "mystery.csv")
