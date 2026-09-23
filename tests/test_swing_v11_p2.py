from __future__ import annotations

from pathlib import Path
import unittest

from etf_rotation.swing_v11 import (
    V11Context,
    V11Position,
    V11Setup,
    evaluate_v11_position,
    load_v11_config,
)


ROOT = Path(__file__).resolve().parents[1]


class SwingV11P2CoreTests(unittest.TestCase):
    def config(self):
        return load_v11_config(ROOT / "data" / "swing" / "v11_strategy.json")

    @staticmethod
    def position(**overrides: object) -> V11Position:
        values: dict[str, object] = {
            "shares": 500,
            "sellable_shares": 500,
            "entry_price": 100.0,
            "stop_price": 95.0,
            "current_price": 100.0,
            "initial_risk_per_share": 5.0,
        }
        values.update(overrides)
        values.pop("profit_r", None)
        return V11Position(**values)

    def evaluate(self, position: V11Position, **context_overrides: object):
        context_overrides.setdefault("environment_state", "ATTACK")
        return evaluate_v11_position(
            position,
            config=self.config(),
            context=V11Context(**context_overrides),
        )

    def test_stop_exit_has_priority_over_environment_and_reduce(self):
        decision = self.evaluate(
            self.position(current_price=94.0, profit_r=2.0),
            environment_state="DEFENSE",
        )
        self.assertEqual(decision.action, "EXIT")
        self.assertEqual(decision.planned_shares, 500)
        self.assertEqual(decision.evidence["priority"], "STOP_OR_EXIT > ENVIRONMENT > REDUCE > TOP_UP > ENTRY")
        self.assertIn("C1", decision.blocked_reasons)

    def test_neutral_loss_is_e4_exit_and_reduction_enters_tracking(self):
        exit_decision = self.evaluate(
            self.position(current_price=98.0, entry_environment="ATTACK"),
            environment_state="NEUTRAL",
            category="BROAD",
        )
        self.assertEqual(exit_decision.action, "EXIT")
        self.assertIn("E4", exit_decision.blocked_reasons)
        reduce_decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5),
            category="BROAD",
            indicator={"bias20_pct": 9.0, "ma20": 106.0},
        )
        self.assertEqual(reduce_decision.action, "REDUCE")
        self.assertEqual(reduce_decision.evidence["tracking_price"], 106.0)
        self.assertEqual(reduce_decision.stop_price, 106.0)

    def test_bad_data_blocks_non_exit_position_actions(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5),
            data_quality="UNVERIFIED",
            environment_state="UNKNOWN",
            indicator={"bias20_pct": 9.0, "ma20": 106.0},
        )
        self.assertEqual(decision.action, "HOLD")
        self.assertIn("DATA_QUALITY_UNVERIFIED", decision.blocked_reasons)
        self.assertIn("ENVIRONMENT_UNKNOWN", decision.blocked_reasons)

    def test_stop_relation_is_rejected_when_reference_is_above_entry(self):
        from tests.test_swing_v11_p1 import SwingV11P1Tests

        helper = SwingV11P1Tests.evidence(
            pullback_low=102.0,
            atr14=1.0,
            stop_distance_pct=None,
        )
        from etf_rotation.swing_v11 import evaluate_v11
        decision = evaluate_v11(
            __import__("tests.swing_helpers", fromlist=["swing_strategy_bars"]).swing_strategy_bars(300),
            config=self.config(),
            context=V11Context(
                indicator=helper,
                environment_state="ATTACK",
                category="BROAD",
                account_known=False,
            ),
        )
        self.assertIn("INVALID_ENTRY_STOP", decision.blocked_reasons)

    def test_one_r_moves_stop_to_entry_without_reducing(self):
        decision = self.evaluate(self.position(current_price=105.0, profit_r=1.0))
        self.assertEqual(decision.action, "MOVE_STOP")
        self.assertEqual(decision.planned_shares, 0)
        self.assertEqual(decision.evidence["new_stop"], 100.0)

    def test_one_r_s1_reduction_is_not_gated_by_profit_threshold(self):
        decision = self.evaluate(
            self.position(current_price=105.0, profit_r=1.0),
            category="BROAD",
            indicator={"bias20_pct": 9.0},
        )
        self.assertEqual(decision.action, "REDUCE")
        self.assertEqual(decision.planned_shares, 200)

    def test_two_r_starts_category_tracking_without_reducing(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=2.0),
            category="BROAD",
            indicator={"ma20": 106.0},
        )
        self.assertEqual(decision.action, "TRACK")
        self.assertEqual(decision.planned_shares, 0)
        self.assertEqual(decision.evidence["tracking_ma"], "MA20")
        self.assertEqual(decision.evidence["tracking_price"], 106.0)
        self.assertEqual(decision.stop_price, 106.0)

    def test_two_r_tracking_never_lowers_an_existing_tighter_stop(self):
        decision = self.evaluate(
            self.position(current_price=110.0, stop_price=108.0, profit_r=2.0),
            category="BROAD",
            indicator={"ma20": 106.0},
        )
        self.assertEqual(decision.action, "TRACK")
        self.assertEqual(decision.stop_price, 108.0)

    def test_two_r_sector_uses_ma10_and_never_sets_tracking_below_entry(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=2.0),
            category="SECTOR",
            indicator={"ma10": 95.0},
        )
        self.assertEqual(decision.action, "TRACK")
        self.assertEqual(decision.evidence["tracking_ma"], "MA10")
        self.assertEqual(decision.evidence["tracking_price"], 100.0)
        self.assertGreaterEqual(decision.stop_price, 100.0)

    def test_existing_reduction_and_tracking_does_not_emit_repeated_reduce(self):
        decision = self.evaluate(
            self.position(
                current_price=110.0,
                profit_r=2.0,
                reduced=True,
                tracking_price=106.0,
            ),
            category="BROAD",
            indicator={"ma20": 106.0},
        )
        self.assertNotEqual(decision.action, "REDUCE")
        self.assertEqual(decision.action, "HOLD")

    def test_tracking_started_state_does_not_emit_repeated_track(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=2.0, tracking_started=True),
            category="BROAD",
            indicator={"ma20": 106.0},
        )
        self.assertEqual(decision.action, "HOLD")

    def test_reduced_state_suppresses_repeated_s1_reduction(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5, reduced=True),
            category="BROAD",
            indicator={"bias20_pct": 9.0},
        )
        self.assertNotEqual(decision.action, "REDUCE")

    def test_defense_exits_loss_and_reduces_profit_with_sellable_lot_floor(self):
        loss = self.evaluate(
            self.position(current_price=98.0, profit_r=-0.4, sellable_shares=100),
            environment_state="DEFENSE",
            category="BROAD",
        )
        self.assertEqual(loss.action, "EXIT")
        self.assertEqual(loss.planned_shares, 100)
        profit = self.evaluate(
            self.position(current_price=102.0, profit_r=0.4, sellable_shares=350),
            environment_state="DEFENSE",
            category="BROAD",
        )
        self.assertEqual(profit.action, "REDUCE")
        self.assertEqual(profit.planned_shares, 100)

    def test_cross_border_and_gold_positions_are_exempt_from_defense_action(self):
        for category in ("CROSS_BORDER", "GOLD"):
            decision = self.evaluate(
                self.position(current_price=102.0, profit_r=0.4),
                environment_state="DEFENSE",
                category=category,
            )
            self.assertEqual(decision.action, "HOLD")

    def test_s1_bias_reduces_once_and_beats_s2(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5),
            category="BROAD",
            indicator={"bias20_pct": 9.0, "rsi": 90.0, "close": 110.0, "bollinger_upper": 100.0},
        )
        self.assertEqual(decision.action, "REDUCE")
        self.assertIn("S1", decision.evidence["reduce_reason"])

    def test_s2_requires_rsi_and_close_outside_upper_when_s1_not_triggered(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5),
            category="BROAD",
            indicator={"bias20_pct": 2.0, "rsi": 80.0, "close": 110.0, "bollinger_upper": 100.0},
        )
        self.assertEqual(decision.action, "REDUCE")
        self.assertIn("S2", decision.evidence["reduce_reason"])

    def test_t_plus_one_plans_only_sellable_shares(self):
        decision = self.evaluate(
            self.position(current_price=94.0, sellable_shares=100),
            category="BROAD",
        )
        self.assertEqual(decision.action, "EXIT")
        self.assertEqual(decision.planned_shares, 100)

    def test_s3_emotion_volume_reduces_only_after_second_holding_day(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5, holding_session=2),
            category="BROAD",
            indicator={
                "volume_ratio20": 2.6,
                "daily_return_pct": 1.0,
                "s3_next_day_10am": True,
                "s3_no_new_high": True,
            },
        )
        self.assertEqual(decision.action, "REDUCE")
        self.assertEqual(decision.planned_shares, 200)
        self.assertIn("S3", decision.evidence["reduce_reason"])

    def test_s3_does_not_apply_on_entry_day(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5, holding_session=1),
            category="BROAD",
            indicator={
                "volume_ratio20": 3.0,
                "daily_return_pct": 5.0,
                "bollinger_upper": 100.0,
                "s3_next_day_10am": True,
                "s3_no_new_high": True,
            },
        )
        self.assertNotEqual(decision.action, "REDUCE")

    def test_s4_pressure_long_shadow_reduces_once(self):
        decision = self.evaluate(
            self.position(current_price=110.0, profit_r=1.5, holding_session=4),
            category="BROAD",
            indicator={
                "near_ma250_or_prior_high": True,
                "volume_ratio20": 1.5,
                "long_upper_shadow": True,
            },
        )
        self.assertEqual(decision.action, "REDUCE")
        self.assertIn("S4", decision.evidence["reduce_reason"])

    def test_s6_cross_border_premium_reduces_and_c6_exits(self):
        reduce = self.evaluate(
            self.position(current_price=110.0, profit_r=0.2),
            category="CROSS_BORDER",
            indicator={"premium_pct": 6.0},
        )
        self.assertEqual(reduce.action, "REDUCE")
        self.assertIn("S6", reduce.evidence["reduce_reason"])
        exit_decision = self.evaluate(
            self.position(current_price=110.0, profit_r=-0.2),
            category="CROSS_BORDER",
            indicator={"premium_pct": 8.1},
        )
        self.assertEqual(exit_decision.action, "EXIT")
        self.assertIn("C6_PREMIUM_OVER_8", exit_decision.blocked_reasons)

    def test_s7_long_holiday_reduces_profitable_below_one_r_position(self):
        decision = self.evaluate(
            self.position(current_price=102.0, profit_r=0.5),
            category="BROAD",
            indicator={"long_holiday_preclose": True},
        )
        self.assertEqual(decision.action, "REDUCE")
        self.assertIn("S7", decision.evidence["reduce_reason"])

    def test_c2_to_c5_and_c7_exit_reasons_are_fail_closed(self):
        cases = [
            ({"close": 97.0, "ma60": 100.0}, "C2_MA60_BREAK"),
            ({"weekly_close": 95.0, "weekly_ma20": 100.0}, "C3_WEEKLY_MA20_BREAK"),
            ({"csi300_weekly_break": True}, "C4_CSI300_WEEKLY_BREAK"),
            ({"fund_size_cny": 400_000_000}, "C5_FUND_SIZE"),
            ({"rule_violation": True}, "C7_RULE_VIOLATION"),
        ]
        for indicator, reason in cases:
            with self.subTest(reason=reason):
                decision = self.evaluate(
                    self.position(current_price=110.0, profit_r=1.5),
                    category="BROAD",
                    indicator=indicator,
                )
                self.assertEqual(decision.action, "EXIT")
                self.assertIn(reason, decision.blocked_reasons)

    def test_e1_e2_e3_and_t25_time_rules(self):
        e1 = self.evaluate(
            self.position(current_price=98.0, holding_session=3, setup=V11Setup.B_BREAKOUT),
            category="BROAD",
            indicator={"box_high": 100.0},
        )
        self.assertEqual(e1.action, "EXIT")
        self.assertIn("E1_BOX_BREAK", e1.blocked_reasons)
        e2 = self.evaluate(
            self.position(current_price=98.0, holding_session=2, setup=V11Setup.A_PULLBACK),
            category="BROAD",
            indicator={"ma20": 100.0, "macd_dead_cross": True},
        )
        self.assertEqual(e2.action, "EXIT")
        self.assertIn("E2", e2.blocked_reasons)
        e3 = self.evaluate(
            self.position(current_price=101.0, holding_session=10, profit_r=0.5, new_high=False),
            category="BROAD",
        )
        self.assertEqual(e3.action, "EXIT")
        self.assertIn("E3", e3.blocked_reasons)
        t25 = self.evaluate(
            self.position(current_price=108.0, holding_session=25, profit_r=1.5),
            category="BROAD",
            indicator={"ma20": 106.0},
        )
        self.assertEqual(t25.action, "TRACK")
        self.assertIn("T25", t25.blocked_reasons)

    def test_top_up_requires_attack_profit_room_and_one_trigger(self):
        decision = self.evaluate(
            self.position(
                current_price=102.0,
                profit_r=0.2,
                shares=100,
                sellable_shares=100,
                initial_shares=100,
                setup=V11Setup.B_BREAKOUT,
            ),
            environment_state="ATTACK",
            category="BROAD",
            indicator={
                "standard_shares": 300,
                "bias20_pct": 1.0,
                "topup_pullback_ok": True,
                "portfolio_room": True,
            },
        )
        self.assertEqual(decision.action, "TOP_UP")
        self.assertEqual(decision.planned_shares, 200)

    def test_top_up_cooldown_and_no_switch_are_blocked_without_affecting_exit(self):
        cooldown = self.evaluate(
            self.position(current_price=102.0, profit_r=0.2),
            category="BROAD",
            indicator={
                "standard_shares": 300,
                "topup_pullback_ok": True,
                "reentry_cooldown_sessions": 3,
            },
        )
        self.assertEqual(cooldown.action, "HOLD")
        self.assertIn("REENTRY_COOLDOWN", cooldown.blocked_reasons)
        no_switch = self.evaluate(
            self.position(current_price=102.0, profit_r=0.2),
            category="BROAD",
            indicator={"stronger_related_signal": True, "related_group_occupied": True},
        )
        self.assertEqual(no_switch.action, "HOLD")
        self.assertIn("NO_SWITCH", no_switch.blocked_reasons)
        stop = self.evaluate(
            self.position(current_price=94.0, profit_r=-1.0),
            category="BROAD",
            indicator={"reentry_cooldown_sessions": 3},
        )
        self.assertEqual(stop.action, "EXIT")


if __name__ == "__main__":
    unittest.main()
