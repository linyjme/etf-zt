from dataclasses import replace
from datetime import timedelta
import unittest

from etf_rotation.regime import RegimeDetector
from tests.regime_fixtures import alternating_points, one_sided_points, trending_points


class RegimeTests(unittest.TestCase):
    def test_one_sided_points_never_form_range(self) -> None:
        result = RegimeDetector().evaluate(one_sided_points(24))
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.one_side_ratio, 1.0)
        self.assertEqual(result.vwap_crossings, 0)
        self.assertEqual(result.above_vwap_count, 20)
        self.assertEqual(result.below_vwap_count, 0)
        self.assertEqual(result.range_confirmation_count, 0)
        self.assertIn("RANGE_BELOW_VWAP_COUNT_BELOW_2", result.reasons)

    def test_range_requires_three_consecutive_windows(self) -> None:
        self.assertEqual(RegimeDetector().evaluate(alternating_points(21)).state, "UNCERTAIN")
        result = RegimeDetector().evaluate(alternating_points(22))
        self.assertEqual(result.state, "RANGE")
        self.assertEqual(result.range_confirmation_count, 3)
        self.assertGreaterEqual(result.vwap_crossings, 2)
        self.assertLessEqual(result.one_side_ratio, 0.70)
        self.assertEqual(result.above_vwap_count, 10)
        self.assertEqual(result.below_vwap_count, 10)
        self.assertEqual(result.vwap_slope, 0.0)
        self.assertEqual(result.reasons, ("RANGE_CONFIRMED",))

    def test_trend_requires_two_aligned_windows(self) -> None:
        result = RegimeDetector().evaluate(trending_points(21, direction=1))
        self.assertEqual(result.state, "UPTREND")
        self.assertEqual(result.trend_confirmation_count, 2)
        self.assertEqual(result.range_confirmation_count, 0)
        self.assertGreaterEqual(result.path_efficiency, 0.55)
        self.assertGreaterEqual(result.vwap_slope, 0.001)
        self.assertEqual(result.reasons, ("UPTREND_CONFIRMED",))

    def test_downtrend_requires_two_aligned_windows(self) -> None:
        result = RegimeDetector().evaluate(trending_points(21, direction=-1))
        self.assertEqual(result.state, "DOWNTREND")
        self.assertEqual(result.trend_confirmation_count, 2)
        self.assertLessEqual(result.vwap_slope, -0.001)
        self.assertEqual(result.reasons, ("DOWNTREND_CONFIRMED",))

    def test_lunch_break_resets_window(self) -> None:
        points = alternating_points(20, start="11:11") + alternating_points(10, start="13:00")
        result = RegimeDetector().evaluate(points)
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.sample_count, 10)
        self.assertEqual(result.range_confirmation_count, 0)
        self.assertEqual(result.trend_confirmation_count, 0)
        self.assertEqual(result.reasons, ("INSUFFICIENT_SAMPLES",))

    def test_trading_date_change_resets_window(self) -> None:
        next_day = tuple(
            replace(point, timestamp=point.timestamp + timedelta(days=1))
            for point in alternating_points(10)
        )
        result = RegimeDetector().evaluate(alternating_points(20) + next_day)
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.sample_count, 10)
        self.assertEqual(result.reasons, ("INSUFFICIENT_SAMPLES",))

    def test_minute_gap_resets_window(self) -> None:
        result = RegimeDetector().evaluate(
            alternating_points(20, start="09:30") + alternating_points(10, start="10:00")
        )
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.sample_count, 10)
        self.assertEqual(result.reasons, ("INSUFFICIENT_SAMPLES",))


if __name__ == "__main__":
    unittest.main()
