"""A ratio field may record that the paper did not report a ratio.

Extracted literature records fill unknown fields with a placeholder, and this
module already does the same: ``_text_or_na`` writes ``NA`` into atmosphere,
furnace type and element sites, which import fine. ``ball_powder_ratio`` alone
raised on it, so 75 of 401 records in one batch were dropped for describing a
gap the same way every other field does.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from catalog.batch_upload import NA, _normalize_ratio


class RatioPlaceholderTests(SimpleTestCase):
    def test_real_ratios_still_normalize(self) -> None:
        self.assertEqual(_normalize_ratio("10:1"), "10:1")
        self.assertEqual(_normalize_ratio(" 10 : 1 "), "10:1")
        self.assertEqual(_normalize_ratio("2.5:1"), "2.5:1")

    def test_placeholders_mean_not_recorded(self) -> None:
        for value in (NA, "na", "NA", "n/a", "N/A", "none", "null", "unknown", "-", "--"):
            self.assertEqual(_normalize_ratio(value), "", f"{value!r} should be empty")

    def test_empty_and_none_still_empty(self) -> None:
        for value in ("", "   ", None):
            self.assertEqual(_normalize_ratio(value), "")

    def test_genuinely_malformed_values_still_raise(self) -> None:
        """Widening must not turn a typo into silent data loss."""
        for value in ("10", "10:", ":1", "ten:one", "10;1", "10-1", "abc"):
            with self.assertRaises(ValueError, msg=f"{value!r} should raise"):
                _normalize_ratio(value)

    def test_a_zero_ratio_is_not_treated_as_missing(self) -> None:
        """0:1 is a real value, not a placeholder."""
        self.assertEqual(_normalize_ratio("0:1"), "0:1")
