from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from etf_rotation.swing_indicators import calculate_indicator_context, calculate_indicator_snapshot
from tests.swing_helpers import swing_strategy_bars


class SwingIndicatorReferenceTests(unittest.TestCase):
    def test_cross_age_does_not_recalculate_every_historical_kdj_prefix(self):
        from etf_rotation import swing_indicators as module
        with patch.object(module, "_kdj", wraps=module._kdj) as calculate:
            calculate_indicator_context(swing_strategy_bars(140), lookback=3)
        self.assertLessEqual(calculate.call_count, 6)

    def test_indicator_context_exposes_recent_direction_without_lookahead(self):
        bars = swing_strategy_bars(140)
        context = calculate_indicator_context(bars, lookback=3)
        self.assertEqual(context["indicator_version"], "INDICATORS_V1")
        self.assertEqual(len(context["recent"]), 3)
        self.assertEqual(
            context["latest"]["as_of_trading_date"],
            bars[-1].trading_date.isoformat(),
        )
        self.assertIn("macd_histogram_rising_days", context["latest"])

    def test_indicator_context_changes_when_only_the_last_completed_bar_changes(self):
        bars = swing_strategy_bars(140)
        first = calculate_indicator_context(bars[:-1], lookback=3)
        second = calculate_indicator_context(bars, lookback=3)
        self.assertNotEqual(
            first["latest"]["as_of_trading_date"],
            second["latest"]["as_of_trading_date"],
        )

    def test_context_rejects_non_ascending_completed_dates(self):
        bars = list(swing_strategy_bars(10))
        bars[4], bars[5] = bars[5], bars[4]
        with self.assertRaises(ValueError):
            calculate_indicator_context(bars)

    def test_every_point_matches_its_own_prefix_including_warmup(self):
        bars = swing_strategy_bars(140, pattern="rising")
        full = calculate_indicator_context(bars, lookback=140)
        for count, point in enumerate(full["recent"], 1):
            snapshot = calculate_indicator_snapshot(bars[:count])
            for section in ("macd", "kdj", "rsi", "moving_averages"):
                self.assertEqual(point[section], snapshot[section], (count, section))
            prefix = calculate_indicator_context(bars[:count])["latest"]
            self.assertEqual(point, prefix, count)

    def test_warmup_not_ready_and_unfinished_bars_rejected(self):
        bars = swing_strategy_bars(20)
        self.assertEqual(calculate_indicator_context(bars)["status"], "WARMUP")
        for calculate in (calculate_indicator_context, calculate_indicator_snapshot):
            with self.assertRaises(ValueError):
                calculate((*bars[:-1], replace(bars[-1], is_final=False)))

    def test_direction_contract_and_zero_atr_are_explicit(self):
        bars = swing_strategy_bars(140, pattern="flat")
        context = calculate_indicator_context(bars)
        point = context["latest"]
        expected = {
            "macd_histogram_previous_1": 0.0, "macd_histogram_previous_2": 0.0,
            "macd_histogram_rising_days": 0, "macd_dif_above_dea": False,
            "macd_cross_age": None, "rsi_previous_1": 50.0, "rsi_previous_2": 50.0,
            "rsi_rising_days": 0, "kdj_k_above_d": False, "kdj_cross_age": None,
            "ma20_slope_pct_5d": 0.0, "ma60_slope_pct_10d": 0.0,
            "close_ma20_atr_distance": 0.0,
        }
        for key, value in expected.items():
            self.assertEqual(point[key], value, key)
        self.assertEqual(context["price_basis"], "adjusted_ohlc")
        self.assertTrue(context["data_version"].startswith("sha256:"))
        flat = tuple(replace(b, adjusted_high=100., adjusted_low=100., adjusted_open=100.) for b in bars)
        self.assertIsNone(calculate_indicator_context(flat)["latest"]["close_ma20_atr_distance"])


if __name__ == "__main__":
    unittest.main()
