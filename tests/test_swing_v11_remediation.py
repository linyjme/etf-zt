from __future__ import annotations

from pathlib import Path
import unittest

from etf_rotation.swing_v11 import (
    V11Context,
    V11Position,
    classify_v11_environment,
    evaluate_v11,
    evaluate_v11_position,
    load_v11_config,
)
from tests.swing_helpers import swing_strategy_bars


ROOT = Path(__file__).resolve().parents[1]


class SwingV11RemediationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")

    @staticmethod
    def position(**overrides):
        fields = dict(
            shares=500, sellable_shares=500, entry_price=100.0,
            stop_price=95.0, current_price=100.0,
            initial_risk_per_share=5.0,
        )
        fields.update(overrides)
        return V11Position(**fields)

    def test_position_profit_r_is_derived_from_prices(self):
        result = evaluate_v11_position(
            V11Position(shares=500, sellable_shares=500, entry_price=100.0, stop_price=95.0,
                current_price=102.5, initial_risk_per_share=5.0, profit_r=9.0),
            config=self.config,
            context=V11Context(environment_state="ATTACK", category="BROAD"),
        )
        self.assertEqual(result.evidence["profit_r"], 0.5)

    def test_s1_can_reduce_before_one_r(self):
        result = evaluate_v11_position(
            self.position(current_price=102.5), config=self.config,
            context=V11Context(environment_state="ATTACK", category="BROAD", indicator={"bias20_pct": 9.0}),
        )
        self.assertEqual(result.action, "REDUCE")
        self.assertIn("S1", result.evidence["reduce_reason"])

    def test_s2_can_reduce_before_one_r(self):
        result = evaluate_v11_position(
            self.position(current_price=102.5), config=self.config,
            context=V11Context(environment_state="ATTACK", category="BROAD", indicator={"rsi": 80.0, "close": 102.5, "bollinger_upper": 100.0}),
        )
        self.assertEqual(result.action, "REDUCE")
        self.assertIn("S2", result.evidence["reduce_reason"])

    def test_e3_applies_after_ten_sessions_and_stage_can_override(self):
        context = V11Context(environment_state="ATTACK", category="BROAD")
        result = evaluate_v11_position(self.position(current_price=100.0, holding_session=12), config=self.config, context=context)
        self.assertIn("E3_NO_PROGRESS", result.blocked_reasons)
        stage_result = evaluate_v11_position(
            self.position(current_price=100.0, holding_session=7), config=self.config,
            context=V11Context(environment_state="ATTACK", category="BROAD", valuation_stage={"e3_session": 7}, valuation_stage_enforcement=True),
        )
        self.assertIn("E3_NO_PROGRESS", stage_result.blocked_reasons)

    def test_e4_requires_attack_entry_environment_and_neutral_current_environment(self):
        missing_entry = evaluate_v11_position(
            self.position(current_price=98.0), config=self.config,
            context=V11Context(environment_state="NEUTRAL", category="BROAD"),
        )
        self.assertNotIn("E4", missing_entry.blocked_reasons)
        valid_entry = evaluate_v11_position(
            self.position(current_price=98.0, entry_environment="ATTACK"), config=self.config,
            context=V11Context(environment_state="NEUTRAL", category="BROAD"),
        )
        self.assertIn("E4", valid_entry.blocked_reasons)

    def test_below_ma60_is_defense_even_when_slope_rises(self):
        self.assertEqual(classify_v11_environment({
            "price": 99.0, "ma20": 100.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 1.0,
        }), "DEFENSE")

    def test_unverified_category_fails_closed(self):
        decision = evaluate_v11(
            swing_strategy_bars(300), config=self.config,
            context=V11Context(environment_state="ATTACK", category=None, indicator={
                "bar_count": 300, "price": 100.0, "ma20": 98.0, "ma60": 95.0,
                "weekly_close": 101.0, "weekly_ma20": 96.0, "weekly_ma10_down_3w": False,
                "return_60d_pct": 4.0,
            }),
        )
        self.assertIn("CATEGORY_UNVERIFIED", decision.blocked_reasons)

    def test_macd_recovery_does_not_require_dif_above_dea(self):
        data = self._entry_evidence()
        data.update(macd_histogram_improving_2d=True, macd_dif=0.1, macd_dea=0.2)
        decision = evaluate_v11(swing_strategy_bars(300), config=self.config,
            context=V11Context(environment_state="ATTACK", category="BROAD", indicator=data))
        self.assertTrue(decision.evidence["macd_trigger"])

    def test_rsi_legacy_35_to_60_rising_is_not_trigger(self):
        data = self._entry_evidence()
        data.update(rsi_pullback_min=35.0, rsi_current=60.0, rsi_previous_1=49.0)
        decision = evaluate_v11(swing_strategy_bars(300), config=self.config,
            context=V11Context(environment_state="ATTACK", category="BROAD", indicator=data))
        self.assertFalse(decision.evidence["rsi_trigger"])

    @staticmethod
    def _entry_evidence():
        return {
            "bar_count": 300, "price": 100.0, "ma20": 98.0, "ma60": 95.0,
            "ma20_slope_pct_10d": 1.0, "weekly_close": 101.0, "weekly_ma20": 96.0,
            "weekly_ma10_down_3w": False, "return_60d_pct": 4.0,
            "pullback_window_sessions": 7, "pullback_depth_pct": 4.0,
            "pullback_recovery_within_3d": True, "recovery_long_upper_shadow": False,
            "volume_contraction_majority": True, "bias20_pct": 1.0,
        }


if __name__ == "__main__":
    unittest.main()
