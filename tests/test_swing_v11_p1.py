from __future__ import annotations

from pathlib import Path
import unittest

from etf_rotation.swing_v11 import (
    V11Context,
    V11Setup,
    V11State,
    evaluate_v11,
    load_v11_config,
    size_v11_order,
)
from tests.swing_helpers import swing_strategy_bars


ROOT = Path(__file__).resolve().parents[1]


class SwingV11P1Tests(unittest.TestCase):
    def config(self):
        return load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")

    @staticmethod
    def evidence(**overrides: object) -> dict[str, object]:
        result: dict[str, object] = {
            "bar_count": 300,
            "price": 100.0,
            "ma20": 98.0,
            "ma60": 95.0,
            "ma250": 90.0,
            "ma20_slope_pct_10d": 0.5,
            "weekly_close": 101.0,
            "weekly_ma10": 98.0,
            "weekly_ma20": 96.0,
            "weekly_ma10_down_3w": False,
            "return_60d_pct": 5.0,
            "return_250d_pct": 12.0,
            "box_days": 25,
            "pullback_window_ok": True,
            "pullback_recovery_ok": True,
            "volume_contraction_ok": True,
            "pullback_window_sessions": 8,
            "pullback_depth_pct": 4.0,
            "pullback_low": 95.0,
            "pullback_recovery_within_3d": True,
            "recovery_long_upper_shadow": False,
            "volume_contraction_majority": True,
            "bias20_pct": 2.0,
            "macd_trigger": True,
            "rsi_trigger": False,
            "volume_recovery_trigger": False,
            "macd_dif": 0.2,
            "macd_dea": 0.1,
            "macd_histogram_improving_2d": True,
            "rsi_pullback_min": 45.0,
            "rsi_current": 55.0,
            "volume_ratio20": 1.6,
            "box_ok": True,
            "box_breakout_ok": True,
            "box_high": 98.0,
            "box_low": 90.0,
            "box_latter_half_low": 91.0,
            "box_first_half_low": 90.0,
            "bollinger_upper": 110.0,
            "macd_dif_nonnegative": True,
            "weekly_above_ma10": True,
            "atr14": 2.0,
        }
        result.update(overrides)
        return result

    def evaluate(self, indicator: dict[str, object], *, category: str = "BROAD"):
        return evaluate_v11(
            swing_strategy_bars(300),
            config=self.config(),
            context=V11Context(
                indicator=indicator, environment_state="ATTACK", category=category,
                account_known=False,
            ),
        )

    def evaluate_with_account(self, indicator: dict[str, object], *, category: str = "BROAD"):
        return evaluate_v11(
            swing_strategy_bars(300),
            config=self.config(),
            context=V11Context(
                indicator=indicator, environment_state="ATTACK", category=category,
                account_known=True, equity_cny=100_000.0, cash_cny=10_000.0,
            ),
        )

    def test_a_requires_positive_sixty_day_return(self):
        decision = self.evaluate(self.evidence(return_60d_pct=-0.1))
        self.assertFalse(decision.evidence["a_setup_ok"])
        self.assertFalse(decision.evidence["t4_return_60d"])

    def test_a_rejects_three_week_continuous_weekly_ma10_decline(self):
        decision = self.evaluate(self.evidence(weekly_ma10_down_3w=True))
        self.assertFalse(decision.evidence["a_setup_ok"])
        self.assertFalse(decision.evidence["t3_weekly_trend"])

    def test_b_uses_t1_t2_t5_and_b7_without_a_t3_or_t4(self):
        decision = self.evaluate(self.evidence(return_60d_pct=-5.0, weekly_close=99.0, weekly_ma20=110.0,
                                               pullback_window_ok=False, pullback_recovery_ok=False,
                                               volume_contraction_ok=False, macd_trigger=False))
        self.assertEqual(decision.setup, V11Setup.B_BREAKOUT)
        self.assertEqual(decision.state, V11State.TECHNICAL_CANDIDATE)

    def test_b_requires_box_latter_half_not_lower_and_bollinger_cap(self):
        lower = self.evaluate(self.evidence(box_latter_half_low=89.0))
        self.assertNotEqual(lower.setup, V11Setup.B_BREAKOUT)
        capped = self.evaluate(self.evidence(price=111.5, bollinger_upper=110.0))
        self.assertFalse(capped.evidence["b_setup_ok"])
        self.assertFalse(capped.evidence["b6_overheated_ok"])

    def test_a_stop_uses_pullback_reference_and_two_atr(self):
        decision = self.evaluate_with_account(self.evidence(
            pullback_low=96.0, atr14=2.0, stop_distance_pct=None,
        ))
        self.assertEqual(decision.setup, V11Setup.A_PULLBACK)
        self.assertAlmostEqual(decision.stop_price, 96.0)
        self.assertEqual(decision.evidence["stop_reference"], "pullback_low")

    def test_b_stop_uses_box_reference_and_two_atr(self):
        decision = self.evaluate_with_account(self.evidence(
            return_60d_pct=-5.0, weekly_close=99.0, weekly_ma20=110.0,
            pullback_window_ok=False, pullback_recovery_ok=False,
            volume_contraction_ok=False, macd_trigger=False,
            box_high=98.0, atr14=2.0, stop_distance_pct=None,
        ))
        self.assertEqual(decision.setup, V11Setup.B_BREAKOUT)
        self.assertAlmostEqual(decision.stop_price, 97.02)
        self.assertEqual(decision.evidence["stop_reference"], "box_high")

    def test_missing_setup_stop_context_blocks_candidate(self):
        indicator = self.evidence(atr14=None, pullback_low=None, stop_distance_pct=None)
        decision = self.evaluate_with_account(indicator)
        self.assertIn("STOP_CONTEXT_UNAVAILABLE", decision.blocked_reasons)
        self.assertIsNone(decision.stop_price)

    def test_a_macd_below_zero_forces_exactly_one_half(self):
        decision = self.evaluate_with_account(self.evidence(
            macd_dif=-0.2, macd_dea=-0.3,
        ))
        self.assertTrue(decision.evidence["forced_half_size"])
        self.assertTrue(decision.evidence["sizing"]["forced_half_size"])

    def test_b_always_forces_exactly_one_half(self):
        decision = self.evaluate_with_account(self.evidence(
            return_60d_pct=-5.0, weekly_close=99.0, weekly_ma20=110.0,
            pullback_window_ok=False, pullback_recovery_ok=False,
            volume_contraction_ok=False, macd_trigger=False,
            box_high=98.0, atr14=2.0, stop_distance_pct=None,
        ))
        self.assertEqual(decision.setup, V11Setup.B_BREAKOUT)
        self.assertTrue(decision.evidence["forced_half_size"])
        self.assertTrue(decision.evidence["sizing"]["forced_half_size"])

    def test_a_macd_histogram_recovery_requires_dif_above_dea(self):
        blocked = self.evaluate_with_account(self.evidence(
            macd_dif=-0.4, macd_dea=-0.3,
            macd_histogram_improving_2d=True,
            rsi_current=45.0,
            volume_ratio20=1.0,
        ))
        self.assertFalse(blocked.evidence["macd_trigger"])
        self.assertNotEqual(blocked.setup, V11Setup.A_PULLBACK)
        allowed = self.evaluate_with_account(self.evidence(
            macd_dif=-0.2, macd_dea=-0.3,
            macd_histogram_improving_2d=True,
            rsi_current=45.0,
            volume_ratio20=1.0,
        ))
        self.assertTrue(allowed.evidence["macd_trigger"])
        self.assertEqual(allowed.setup, V11Setup.A_PULLBACK)

    def test_b_missing_long_trend_box_or_bollinger_evidence_blocks(self):
        decision = self.evaluate(self.evidence(
            return_250d_pct=None,
            ma250_slope_pct_20d=None,
            box_days=None,
            bollinger_upper=None,
        ))
        self.assertFalse(decision.evidence["b_setup_ok"])
        self.assertFalse(decision.evidence["b1_long_trend"])
        self.assertFalse(decision.evidence["b2_box_structure"])
        self.assertFalse(decision.evidence["b6_overheated_ok"])

if __name__ == "__main__":
    unittest.main()
