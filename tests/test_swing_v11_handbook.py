from __future__ import annotations

from pathlib import Path
import unittest

from etf_rotation.swing_v11 import (
    V11Context,
    V11Setup,
    V11State,
    calculate_v11_environment,
    calculate_v11_indicators,
    evaluate_v11,
    load_v11_config,
    size_v11_order,
)
from tests.swing_helpers import swing_strategy_bars
from tests.test_swing_v11_p1 import SwingV11P1Tests


ROOT = Path(__file__).resolve().parents[1]


class SwingV11HandbookAlignmentTests(unittest.TestCase):
    """Handbook v1.1 sections three, four and 4.6 that the entry evaluator enforces."""

    def setUp(self) -> None:
        self.config = load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")
        self.bars = swing_strategy_bars(300)

    def evaluate(self, **overrides: object):
        context = {
            "indicator": SwingV11P1Tests.evidence(),
            "environment_state": "ATTACK",
            "category": "BROAD",
            "environment_index": "000300",
            "environment_states": {"000300": "ATTACK", "000852": "ATTACK"},
            "account_known": True,
            "equity_cny": 100_000.0,
            "cash_cny": 50_000.0,
        }
        context.update(overrides)
        return evaluate_v11(self.bars, config=self.config, context=V11Context(**context))

    def test_neutral_market_only_opens_the_healthy_side(self) -> None:
        states = {"000300": "ATTACK", "000852": "DEFENSE"}
        large_cap = self.evaluate(
            environment_state="NEUTRAL", category="SECTOR",
            environment_index="000300", environment_states=states,
        )
        self.assertTrue(large_cap.evidence["t5_environment_category"])
        self.assertNotIn("T5_ENVIRONMENT_CATEGORY", large_cap.blocked_reasons)
        small_cap = self.evaluate(
            environment_state="NEUTRAL", category="SECTOR",
            environment_index="000852", environment_states=states,
        )
        self.assertFalse(small_cap.evidence["t5_environment_category"])
        self.assertIn("T5_ENVIRONMENT_CATEGORY", small_cap.blocked_reasons)
        broker = self.evaluate(
            environment_state="NEUTRAL", category="SECTOR",
            environment_index="ANY", environment_states=states,
        )
        self.assertTrue(broker.evidence["t5_environment_category"])
        missing_side = self.evaluate(
            environment_state="NEUTRAL", category="BROAD",
            environment_index="000300", environment_states={},
        )
        self.assertIn("T5_ENVIRONMENT_CATEGORY", missing_side.blocked_reasons)

    def test_neutral_market_forces_half_size(self) -> None:
        decision = self.evaluate(environment_state="NEUTRAL")
        self.assertEqual(decision.setup, V11Setup.A_PULLBACK)
        self.assertTrue(decision.evidence["forced_half_size"])
        self.assertIn("ENVIRONMENT_NEUTRAL", decision.evidence["half_size_reasons"])
        attack = self.evaluate()
        self.assertFalse(attack.evidence["forced_half_size"])

    def test_defense_recovery_window_admits_broad_half_only(self) -> None:
        sector = self.evaluate(
            environment_state="NEUTRAL", category="SECTOR",
            defense_recovery_sessions=3,
        )
        self.assertIn("DEFENSE_RECOVERY_WINDOW", sector.blocked_reasons)
        broad = self.evaluate(environment_state="NEUTRAL", defense_recovery_sessions=3)
        self.assertNotIn("DEFENSE_RECOVERY_WINDOW", broad.blocked_reasons)
        self.assertTrue(broad.evidence["forced_half_size"])
        after_window = self.evaluate(
            environment_state="NEUTRAL", category="SECTOR",
            defense_recovery_sessions=6,
        )
        self.assertNotIn("DEFENSE_RECOVERY_WINDOW", after_window.blocked_reasons)

    def test_gold_and_cross_border_skip_the_market_state(self) -> None:
        decision = self.evaluate(
            environment_state="DEFENSE", category="GOLD", environment_index=None,
            environment_states={"000300": "DEFENSE", "000852": "DEFENSE"},
        )
        self.assertTrue(decision.evidence["t5_environment_category"])
        self.assertNotIn("ENVIRONMENT_DEFENSE", decision.blocked_reasons)

    def test_related_group_position_count_and_cooldown_veto_new_entries(self) -> None:
        occupied = self.evaluate(
            correlation_group="BROAD_CN", held_correlation_groups=("BROAD_CN",),
        )
        self.assertIn("RELATED_GROUP_OCCUPIED", occupied.blocked_reasons)
        full = self.evaluate(open_position_count=4)
        self.assertIn("MAX_POSITIONS", full.blocked_reasons)
        cooling = self.evaluate(reentry_cooldown_sessions=2)
        self.assertIn("REENTRY_COOLDOWN", cooling.blocked_reasons)
        held = self.evaluate(
            has_position=True, correlation_group="BROAD_CN",
            held_correlation_groups=("BROAD_CN",), open_position_count=4,
        )
        self.assertNotIn("RELATED_GROUP_OCCUPIED", held.blocked_reasons)
        self.assertNotIn("MAX_POSITIONS", held.blocked_reasons)

    def test_calendar_and_account_vetoes(self) -> None:
        self.assertIn(
            "LONG_HOLIDAY_PRECLOSE",
            self.evaluate(long_holiday_sessions_ahead=1).blocked_reasons,
        )
        self.assertIn(
            "LONG_HOLIDAY_PRECLOSE",
            self.evaluate(long_holiday_sessions_ahead=2).blocked_reasons,
        )
        self.assertNotIn(
            "LONG_HOLIDAY_PRECLOSE",
            self.evaluate(long_holiday_sessions_ahead=3).blocked_reasons,
        )
        self.assertIn("EX_DIVIDEND_WINDOW", self.evaluate(ex_dividend_window=True).blocked_reasons)
        self.assertIn("ACCOUNT_RISK_PAUSE", self.evaluate(entry_pause_sessions=5).blocked_reasons)

    def test_cross_border_premium_fails_closed_and_vetoes_over_two_percent(self) -> None:
        missing = self.evaluate(category="CROSS_BORDER", environment_index=None)
        self.assertIn("PREMIUM_EVIDENCE_UNAVAILABLE", missing.blocked_reasons)
        rich = self.evaluate(category="CROSS_BORDER", environment_index=None, premium_pct=2.5)
        self.assertIn("PREMIUM_OVER_2", rich.blocked_reasons)
        fine = self.evaluate(category="CROSS_BORDER", environment_index=None, premium_pct=1.0)
        self.assertNotIn("PREMIUM_OVER_2", fine.blocked_reasons)
        self.assertNotIn("PREMIUM_EVIDENCE_UNAVAILABLE", fine.blocked_reasons)

    def test_liquidity_rsi_and_ma250_vetoes(self) -> None:
        thin = self.evaluate(indicator=SwingV11P1Tests.evidence(avg_amount20_cny=150_000_000.0))
        self.assertIn("LIQUIDITY_TURNOVER", thin.blocked_reasons)
        small = self.evaluate(metadata={"fund_size_cny": 800_000_000.0})
        self.assertIn("LIQUIDITY_FUND_SIZE", small.blocked_reasons)
        oversold = self.evaluate(indicator=SwingV11P1Tests.evidence(rsi_current=25.0, new_low_20d=True))
        self.assertIn("RSI_OVERSOLD_NEW_LOW", oversold.blocked_reasons)
        pressure = self.evaluate(indicator=SwingV11P1Tests.evidence(ma250=102.0))
        self.assertIn("MA250_PRESSURE", pressure.blocked_reasons)
        self.assertTrue(pressure.evidence["ma250_pressure"])
        far = self.evaluate(indicator=SwingV11P1Tests.evidence(ma250=104.0))
        self.assertNotIn("MA250_PRESSURE", far.blocked_reasons)

    def test_relative_strength_veto_applies_to_a_setup_only(self) -> None:
        a_setup = self.evaluate(relative_strength_20=-4.0)
        self.assertIn("RELATIVE_STRENGTH_TOO_WEAK", a_setup.blocked_reasons)
        b_indicator = SwingV11P1Tests.evidence(
            return_60d_pct=-5.0, weekly_close=99.0, weekly_ma20=110.0,
            pullback_window_ok=False, pullback_recovery_ok=False,
            volume_contraction_ok=False, macd_trigger=False,
            macd_histogram_improving_2d=False, rsi_current=45.0, volume_ratio20=1.6,
        )
        b_setup = self.evaluate(indicator=b_indicator, relative_strength_20=-4.0)
        self.assertEqual(b_setup.setup, V11Setup.B_BREAKOUT)
        self.assertNotIn("RELATIVE_STRENGTH_TOO_WEAK", b_setup.blocked_reasons)

    def test_sector_stop_between_six_and_seven_percent_forces_half(self) -> None:
        indicator = SwingV11P1Tests.evidence(
            pullback_low=94.5, atr14=5.0, pullback_depth_pct=6.5,
        )
        decision = self.evaluate(indicator=indicator, category="SECTOR")
        self.assertEqual(decision.setup, V11Setup.A_PULLBACK)
        self.assertAlmostEqual(decision.evidence["stop_width_pct"], 0.06445, places=4)
        self.assertIn("SECTOR_STOP_6_7", decision.evidence["half_size_reasons"])
        self.assertTrue(decision.evidence["forced_half_size"])

    def test_f1_consecutive_losses_halve_size_and_risk(self) -> None:
        decision = self.evaluate(consecutive_losses=3)
        self.assertIn("F1_CONSECUTIVE_LOSSES", decision.evidence["half_size_reasons"])
        sizing = decision.evidence["sizing"]
        self.assertAlmostEqual(sizing["risk_rate_multiplier"], 0.5)
        self.assertAlmostEqual(sizing["risk_rate_applied"], self.config.shadow_risk_rate * 0.5)

    def test_cross_border_and_gold_use_seventy_percent_of_the_r_budget(self) -> None:
        context = V11Context(
            category="GOLD", equity_cny=100_000.0, cash_cny=100_000.0,
            environment_state="ATTACK",
        )
        _, evidence = size_v11_order(
            entry_price=2.0, stop_price=1.94, config=self.config, context=context,
        )
        self.assertAlmostEqual(evidence["risk_rate_multiplier"], 0.7)
        self.assertAlmostEqual(evidence["risk_budget_cny"], 100_000.0 * self.config.shadow_risk_rate * 0.7)

    def test_single_symbol_and_total_exposure_caps_shrink_shares(self) -> None:
        base = dict(
            entry_price=10.0, stop_price=9.8, config=self.config,
        )
        roomy = V11Context(
            category="BROAD", equity_cny=10_000.0, cash_cny=10_000.0,
            environment_state="ATTACK",
        )
        shares, evidence = size_v11_order(**base, context=roomy)
        self.assertGreater(shares, 0)
        self.assertAlmostEqual(evidence["single_symbol_cap_shares"], 300.0)
        self.assertAlmostEqual(evidence["total_exposure_cap_pct"], 1.0)
        capped = V11Context(
            category="BROAD", equity_cny=10_000.0, cash_cny=10_000.0,
            environment_state="ATTACK", symbol_market_value_cny=2_950.0,
        )
        shares, evidence = size_v11_order(**base, context=capped)
        self.assertEqual(shares, 0)
        self.assertIn("SINGLE_SYMBOL_CAP", evidence["blocked_reasons"])
        defensive = V11Context(
            category="BROAD", equity_cny=10_000.0, cash_cny=10_000.0,
            environment_state="DEFENSE", etf_market_value_cny=2_950.0,
        )
        shares, evidence = size_v11_order(**base, context=defensive)
        self.assertEqual(shares, 0)
        self.assertIn("TOTAL_EXPOSURE_CAP", evidence["blocked_reasons"])
        self.assertAlmostEqual(evidence["total_exposure_cap_pct"], 0.3)

    def test_environment_confirmation_defaults_to_the_same_day(self) -> None:
        attack = {
            "price": 105.0, "ma20": 103.0, "ma60": 100.0,
            "ma20_slope_pct_10d": 1.0, "weekly_close": 105.0, "weekly_ma20": 100.0,
        }
        defense = {
            "price": 95.0, "ma20": 97.0, "ma60": 100.0,
            "ma20_slope_pct_10d": -1.0, "weekly_close": 105.0, "weekly_ma20": 100.0,
        }
        same_day = calculate_v11_environment(
            {"000300": (defense, attack), "000852": (attack, attack)},
        )
        self.assertEqual(same_day["state"], "ATTACK")
        self.assertEqual(same_day["latest_states"], {"000300": "ATTACK", "000852": "ATTACK"})
        two_day = calculate_v11_environment(
            {"000300": (defense, attack), "000852": (attack, attack)},
            confirmation_days=2,
        )
        self.assertEqual(two_day["state"], "NEUTRAL")

    def test_config_windows_drive_pullback_and_box_evidence(self) -> None:
        indicators = calculate_v11_indicators(self.bars, box_days=30, pullback_window_max=20)
        self.assertEqual(indicators["setups"]["box_days"], 30)
        self.assertIn("pullback_low_close", indicators)
        self.assertIn("avg_amount20_cny", indicators)
        self.assertIn("bullish_candle", indicators)
        self.assertIn("new_low_20d", indicators)
        self.assertIn("ma250_slope_pct_10d", indicators)
        with self.assertRaises(ValueError):
            calculate_v11_indicators(self.bars, box_days=0)

    def test_pullback_depth_uses_closing_prices(self) -> None:
        indicators = calculate_v11_indicators(self.bars)
        closes = [bar.adjusted_close for bar in self.bars]
        start = max(range(len(closes) - 30, len(closes)), key=closes.__getitem__)
        low = min(range(start, len(closes)), key=closes.__getitem__)
        self.assertEqual(indicators["pullback_start_high"], closes[start])
        self.assertEqual(indicators["pullback_low_close"], closes[low])
        self.assertEqual(indicators["pullback_low"], self.bars[low].adjusted_low)
        expected_depth = (closes[start] - closes[low]) / closes[start] * 100.0
        self.assertAlmostEqual(indicators["pullback_depth_pct"], expected_depth)

    def test_optional_environment_confirmation_key_is_accepted(self) -> None:
        self.assertEqual(self.config.environment_confirmation_days, 1)

    def test_candidate_state_survives_the_new_vetoes_when_evidence_is_clean(self) -> None:
        indicator = SwingV11P1Tests.evidence(
            price=10.0, ma20=9.8, ma60=9.5, ma250=9.0, pullback_low=9.7,
            atr14=0.1, box_high=9.8, box_low=9.0, box_latter_half_low=9.1,
            box_first_half_low=9.0, bollinger_upper=11.0,
            weekly_close=10.1, weekly_ma10=9.8, weekly_ma20=9.6,
        )
        decision = self.evaluate(indicator=indicator)
        self.assertEqual(decision.state, V11State.TECHNICAL_CANDIDATE)
        self.assertEqual(decision.blocked_reasons, ())


if __name__ == "__main__":
    unittest.main()
