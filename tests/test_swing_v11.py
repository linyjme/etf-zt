from __future__ import annotations

from pathlib import Path
import unittest
from dataclasses import replace
from datetime import date

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_v11 import (
    V11Context,
    V11State,
    calculate_v11_indicators,
    classify_v11_environment,
    evaluate_v11,
    evaluate_v11_position,
    load_v11_config,
    size_v11_order,
    V11Position,
)
from tests.swing_helpers import retime_daily_bars, swing_strategy_bars


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SwingV11ContractTests(unittest.TestCase):
    def test_metadata_exposes_v11_category_and_optional_evidence_fields(self) -> None:
        metadata = EtfMetadataStore(
            PROJECT_ROOT / "data" / "monitor" / "etf_metadata.json",
        ).load()
        self.assertEqual(metadata["510300"].category, "BROAD")
        self.assertEqual(metadata["159915"].category, "GROWTH")
        self.assertIsNone(metadata["510300"].fund_size_cny)
        self.assertEqual(metadata["510300"].dividend_dates, ())

    def test_environment_classifier_is_explicit_and_fail_closed(self) -> None:
        self.assertEqual(classify_v11_environment(self._indicator()), "ATTACK")
        self.assertEqual(classify_v11_environment({"price": 1.0}), "UNKNOWN")

    def test_load_v11_config_exposes_manual_capital_and_risk_defaults(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        self.assertEqual(config.strategy_version, "SWING_V11_SHADOW")
        self.assertEqual(config.target_order_cny, 2000.0)
        self.assertEqual(config.max_order_cny, 5000.0)
        self.assertEqual(config.shadow_risk_rate, 0.003)

    def test_v11_decision_is_never_executable(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11((), config=config, context=V11Context())
        self.assertFalse(decision.executable)
        self.assertEqual(decision.state, V11State.DATA_UNAVAILABLE)

    def test_weekly_context_excludes_incomplete_current_week(self) -> None:
        bars = retime_daily_bars(
            swing_strategy_bars(140), ending_on=date(2026, 9, 17),
        )
        snapshot = calculate_v11_indicators(bars)
        self.assertEqual(snapshot["weekly"]["as_of_trading_date"], "2026-09-11")

    def test_volume_ratio_uses_prior_completed_twenty_day_average(self) -> None:
        bars = list(swing_strategy_bars(140))
        latest = bars[-1]
        bars[-1] = replace(latest, volume=100_000.0)
        snapshot = calculate_v11_indicators(tuple(bars))
        self.assertGreater(snapshot["volume"]["ratio20"], 4.0)

    @staticmethod
    def _indicator(**overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "bar_count": 260,
            "price": 100.0,
            "ma10": 99.0,
            "ma20": 98.0,
            "ma60": 95.0,
            "ma250": 90.0,
            "ma20_slope_pct_10d": 0.5,
            "weekly_close": 101.0,
            "weekly_ma10": 98.0,
            "weekly_ma20": 96.0,
            "pullback_window_ok": True,
            "pullback_recovery_ok": True,
            "volume_contraction_ok": True,
            "bias20_pct": 2.0,
            "macd_trigger": True,
            "rsi_trigger": False,
            "volume_recovery_trigger": False,
            "box_ok": False,
            "box_breakout_ok": False,
            "volume_ratio20": 1.0,
            "macd_dif": 0.2,
            "macd_dea": 0.1,
            "macd_dif_nonnegative": True,
            "weekly_above_ma10": True,
            "stop_distance_pct": 0.03,
        }
        value.update(overrides)
        return value

    def test_a_setup_accepts_one_macd_or_rsi_or_volume_confirmation(self) -> None:
        context = V11Context(indicator=self._indicator(), environment_state="ATTACK")
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11(swing_strategy_bars(260), config=config, context=context)
        self.assertEqual(decision.setup.value, "A_PULLBACK")
        self.assertEqual(decision.state, V11State.TECHNICAL_CANDIDATE)

    def test_b_setup_requires_box_and_zero_axis_macd(self) -> None:
        context = V11Context(
            indicator=self._indicator(
                pullback_window_ok=False, pullback_recovery_ok=False,
                volume_contraction_ok=False, box_ok=False, box_breakout_ok=False,
            ), environment_state="ATTACK",
        )
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11(swing_strategy_bars(260), config=config, context=context)
        self.assertIn("BOX_NOT_CONFIRMED", decision.blocked_reasons)

    def test_uncertain_environment_cannot_be_a_candidate(self) -> None:
        context = V11Context(indicator=self._indicator(), environment_state="UNKNOWN")
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11(swing_strategy_bars(260), config=config, context=context)
        self.assertEqual(decision.state, V11State.UNCERTAIN)
        self.assertIn("ENVIRONMENT_UNKNOWN", decision.blocked_reasons)
        self.assertEqual(decision.action, "OBSERVE")

    def test_unverified_data_cannot_display_entry_action(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data/swing/v11_strategy.json")
        decision = evaluate_v11(swing_strategy_bars(260), config=config, context=V11Context(
            indicator=self._indicator(), environment_state="ATTACK", data_quality="UNVERIFIED",
        ))
        self.assertEqual(decision.action, "OBSERVE")

    def test_valid_breakout_does_not_require_pullback_confirmation(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data/swing/v11_strategy.json")
        decision = evaluate_v11(swing_strategy_bars(260), config=config, context=V11Context(
            indicator=self._indicator(
                pullback_window_ok=False, pullback_recovery_ok=False,
                volume_contraction_ok=False, macd_trigger=False,
                box_ok=True, box_breakout_ok=True, volume_ratio20=1.6,
                weekly_ma20=102.0,
            ), environment_state="ATTACK",
        ))
        self.assertEqual(decision.setup.value, "B_BREAKOUT")
        self.assertEqual(decision.state, V11State.TECHNICAL_CANDIDATE)
        self.assertEqual(decision.blocked_reasons, ())

    def test_target_cap_and_half_size_apply_even_when_risk_cap_is_large(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data/swing/v11_strategy.json")
        context = V11Context(equity_cny=200_000.0, cash_cny=10_000.0)
        normal, _ = size_v11_order(entry_price=2.0, stop_price=1.94, config=config, context=context)
        half, _ = size_v11_order(entry_price=2.0, stop_price=1.94, config=config, context=context, force_half=True)
        self.assertEqual(normal, 1000)
        self.assertEqual(half, 500)

    def test_ma20_slope_compares_twenty_day_windows_ten_sessions_apart(self) -> None:
        bars = swing_strategy_bars(260)
        indicator = calculate_v11_indicators(bars)
        closes = [bar.adjusted_close for bar in bars]
        expected = (sum(closes[-20:]) / sum(closes[-30:-10]) - 1.0) * 100.0
        self.assertAlmostEqual(indicator["moving_averages"]["ma20_slope_pct_10d"], expected)

    def test_relative_strength_between_negative_three_and_zero_forces_half_size(self) -> None:
        context = V11Context(
            indicator=self._indicator(), environment_state="ATTACK",
            relative_strength_20=-1.0,
        )
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        decision = evaluate_v11(swing_strategy_bars(260), config=config, context=context)
        self.assertTrue(decision.evidence["forced_half_size"])

    def test_order_size_is_lot_aligned_and_never_over_five_thousand(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        shares, evidence = size_v11_order(
            entry_price=4.6, stop_price=4.3, config=config,
            context=V11Context(equity_cny=100_000.0, cash_cny=10_000.0),
        )
        self.assertEqual(shares % 100, 0)
        self.assertLessEqual(shares * 4.6, 5000.0)
        self.assertIn("risk_budget_cny", evidence)

    def test_stop_distance_over_category_cap_blocks_instead_of_moving_stop(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        shares, evidence = size_v11_order(
            entry_price=100.0, stop_price=92.89, config=config,
            context=V11Context(equity_cny=100_000.0, cash_cny=10_000.0),
        )
        self.assertEqual(shares, 0)
        self.assertIn("STOP_WIDTH_OVER_CAP", evidence["blocked_reasons"])

    def test_action_priority_exit_beats_reduce_and_entry(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        action = evaluate_v11_position(
            V11Position(
                shares=500, sellable_shares=500, entry_price=100.0,
                stop_price=95.0, current_price=94.0,
                initial_risk_per_share=5.0, profit_r=2.0,
            ), config=config, context=V11Context(environment_state="ATTACK"),
        )
        self.assertEqual(action.action, "EXIT")
        self.assertEqual(action.planned_shares, 500)

    def test_t_plus_one_uses_sellable_shares_not_total_shares(self) -> None:
        config = load_v11_config(PROJECT_ROOT / "data" / "swing" / "v11_strategy.json")
        action = evaluate_v11_position(
            V11Position(
                shares=500, sellable_shares=100, entry_price=100.0,
                stop_price=95.0, current_price=94.0,
                initial_risk_per_share=5.0, profit_r=2.0,
            ), config=config, context=V11Context(environment_state="ATTACK"),
        )
        self.assertEqual(action.planned_shares, 100)


if __name__ == "__main__":
    unittest.main()
