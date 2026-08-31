from dataclasses import replace
from datetime import timedelta
import math
import unittest

from etf_rotation.regime import RegimeDetector
from tests.regime_fixtures import (
    alternating_points,
    boundary_slope_points,
    one_sided_points,
    trending_points,
)


class RegimeTests(unittest.TestCase):
    def test_one_sided_points_never_form_range(self) -> None:
        result = RegimeDetector().evaluate(one_sided_points(24))
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.one_side_ratio, 1.0)
        self.assertEqual(result.vwap_crossings, 0)
        self.assertEqual(result.above_vwap_count, 20)
        self.assertEqual(result.below_vwap_count, 0)
        self.assertEqual(result.range_confirmation_count, 0)
        self.assertIn("RANGE_BELOW_VWAP_SAMPLES_INSUFFICIENT", result.reasons)

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

    def test_trend_slope_boundary_is_symmetric(self) -> None:
        for direction, expected_state in ((1, "UPTREND"), (-1, "DOWNTREND")):
            with self.subTest(direction=direction):
                result = RegimeDetector().evaluate(
                    boundary_slope_points(21, direction, alternating=False)
                )
                self.assertEqual(result.state, expected_state)
                self.assertTrue(math.isclose(
                    result.vwap_slope,
                    direction * 0.001,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ))

    def test_range_slope_boundary_is_symmetric(self) -> None:
        for direction in (1, -1):
            with self.subTest(direction=direction):
                result = RegimeDetector().evaluate(
                    boundary_slope_points(22, direction, alternating=True)
                )
                self.assertEqual(result.state, "RANGE")
                self.assertEqual(result.range_confirmation_count, 3)
                self.assertTrue(math.isclose(
                    result.vwap_slope,
                    direction * 0.001,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ))

    def test_zero_path_inside_neutral_band_never_forms_trend(self) -> None:
        points = tuple(
            replace(
                point,
                price=10.0,
                average_price=10.0,
                open=10.0,
                high=10.0,
                low=10.0,
            )
            for point in alternating_points(22)
        )
        result = RegimeDetector().evaluate(points)
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertEqual(result.path_efficiency, 0.0)
        self.assertEqual(result.one_side_ratio, 0.0)
        self.assertEqual(result.vwap_crossings, 0)
        self.assertEqual(result.trend_confirmation_count, 0)
        self.assertIn("TREND_PATH_NOT_EFFICIENT", result.reasons)

    def test_invalid_first_vwap_is_uncertain_instead_of_raising(self) -> None:
        points = list(alternating_points(20))
        points[0] = replace(points[0], average_price=0.0)
        result = RegimeDetector().evaluate(points)
        self.assertEqual(result.state, "UNCERTAIN")
        self.assertIsNone(result.vwap_slope)
        self.assertEqual(result.reasons, ("INVALID_WINDOW_VALUES",))

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
