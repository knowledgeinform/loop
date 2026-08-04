"""
Trapezoidal-integration guards for the NumPy rename shim (catalog.numpy_compat).

``np.trapz`` was renamed ``np.trapezoid`` in NumPy 2.0 and removed in 2.4, so
these assert real integrated values at both call sites: the next numpy bump
should break CI here rather than an XRD refinement run.
"""

import numpy as np
from django.test import SimpleTestCase

from catalog.gsas_tools import _local_peak_area
from catalog.numpy_compat import trapezoid
from catalog.xrd_analysis.refinement import _extract_positive_residual_regions
from catalog.xrd_analysis.schemas import DEFAULT_XRD_ANALYSIS_CONFIG


class TrapezoidAliasTests(SimpleTestCase):
    def test_integrates_a_triangle(self):
        # Height 1 over a base of 2.
        self.assertAlmostEqual(
            float(trapezoid(np.array([0.0, 1.0, 0.0]), np.array([0.0, 1.0, 2.0]))),
            1.0,
        )

    def test_respects_uneven_sample_spacing(self):
        self.assertAlmostEqual(
            float(trapezoid(np.array([0.0, 2.0, 2.0]), np.array([0.0, 1.0, 4.0]))),
            7.0,
        )


class IntegratedResidualTests(SimpleTestCase):
    def test_local_peak_area_integrates_the_clipped_shoulder(self):
        theta = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        residual = np.array([-1.0, 2.0, 6.0, 2.0, -1.0])
        # Negative tails clip to zero, leaving [0, 2, 6, 2, 0] on a unit grid.
        self.assertAlmostEqual(_local_peak_area(theta, residual, 2, 5), 10.0)

    def test_positive_residual_region_reports_its_integral(self):
        regions = _extract_positive_residual_regions(
            observed_two_theta=(24.8, 24.9, 25.0, 25.1, 25.2, 30.0),
            residuals=(0.0, 5.0, 9.0, 4.0, 0.0, -1.0),
            observed_intensities=(10.0, 30.0, 60.0, 25.0, 10.0, 5.0),
            expected_reflections=(),
            settings=DEFAULT_XRD_ANALYSIS_CONFIG.single_phase_refinement,
        )
        self.assertEqual(len(regions), 1)
        self.assertAlmostEqual(regions[0].start_two_theta, 24.9)
        self.assertAlmostEqual(regions[0].end_two_theta, 25.1)
        # [5, 9, 4] over a 0.1-degree grid.
        self.assertAlmostEqual(regions[0].integrated_positive_residual, 1.35, places=6)
