from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, timedelta
import json
import math
from pathlib import Path
import unittest

from etf_rotation.swing_config import load_strategy
from etf_rotation.swing_strategy import (
    IntradayOverlay,
    PortfolioContext,
    PositionContext,
    SwingState,
    SwingStrategyError,
    evaluate_intraday_overlay,
    evaluate_swing,
)
from tests.swing_helpers import (
    replace_latest_adjusted,
    swing_strategy_bars,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SwingStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_strategy(PROJECT_ROOT / "data" / "swing" / "strategy.json")

    def portfolio(self, **overrides: object) -> PortfolioContext:
        values: dict[str, object] = {
            "equity": 1_000_000.0,
            "cash": 1_000_000.0,
            "lot_size": 100,
        }
        values.update(overrides)
        return PortfolioContext.empty(**values)

    def position(self, **overrides: object) -> PositionContext:
        values: dict[str, object] = {
            "shares": 10_000,
            "sellable_shares": 10_000,
            "average_cost": 100.0,
            "initial_risk_per_share": 2.0,
            "entry_trading_date": date(2026, 2, 2),
            "highest_completed_close": 106.0,
            "hard_stop": 94.0,
            "first_reduction_completed": False,
        }
        values.update(overrides)
        return PositionContext(**values)

    def with_position(self, position: PositionContext | None = None, **overrides: object) -> PortfolioContext:
        values: dict[str, object] = {
            "equity": 1_000_000.0,
            "cash": 500_000.0,
            "current_etf_market_value": 200_000.0,
            "current_planned_risk_amount": 5_000.0,
            "lot_size": 100,
            "position": position or self.position(),
        }
        values.update(overrides)
        return PortfolioContext(**values)

    def test_exactly_70_bars_can_form_trial_candidate(self) -> None:
        decision = evaluate_swing(swing_strategy_bars(70), self.config, self.portfolio())
        self.assertEqual(decision.state, SwingState.TRIAL_ENTRY_CANDIDATE)
        self.assertTrue(decision.evidence["sample_ok"])
        self.assertGreaterEqual(decision.planned_shares, 100)

    def test_69_bars_is_data_unavailable(self) -> None:
        decision = evaluate_swing(swing_strategy_bars(69), self.config, self.portfolio())
        self.assertEqual(decision.state, SwingState.DATA_UNAVAILABLE)
        self.assertIn("insufficient_daily_bars", decision.blocked_reasons)

    def test_falling_ma60_is_trend_blocked(self) -> None:
        decision = evaluate_swing(
            swing_strategy_bars(pattern="falling_ma60"), self.config, self.portfolio(),
        )
        self.assertEqual(decision.state, SwingState.TREND_BLOCKED)
        self.assertFalse(decision.evidence["trend_ma60_rising"])

    def test_trend_equalities_are_blocked_not_rounded_up(self) -> None:
        decision = evaluate_swing(
            swing_strategy_bars(pattern="flat"), self.config, self.portfolio(),
        )
        self.assertEqual(decision.state, SwingState.TREND_BLOCKED)
        self.assertFalse(decision.evidence["trend_close_above_ma60"])
        self.assertFalse(decision.evidence["trend_ma20_above_ma60"])
        self.assertFalse(decision.evidence["trend_ma60_rising"])

    def test_pullback_distance_and_anti_chase_equalities_are_inclusive(self) -> None:
        bars = swing_strategy_bars()
        baseline = evaluate_swing(bars, self.config, self.portfolio())
        distance = float(baseline.evidence["close_ma20_distance_adjusted"])
        atr = float(baseline.evidence["atr14_adjusted"])
        pullback_boundary = distance / atr
        exact_pullback = replace(
            self.config, pullback_atr_distance=pullback_boundary,
        )
        self.assertTrue(evaluate_swing(
            bars, exact_pullback, self.portfolio(),
        ).evidence["pullback_distance_ok"])
        below_pullback_ratio = math.nextafter(pullback_boundary, 0.0)
        while below_pullback_ratio * atr >= distance:
            below_pullback_ratio = math.nextafter(below_pullback_ratio, 0.0)
        below_pullback = replace(
            exact_pullback, pullback_atr_distance=below_pullback_ratio,
        )
        self.assertFalse(evaluate_swing(
            bars, below_pullback, self.portfolio(),
        ).evidence["pullback_distance_ok"])

        anti_boundary = distance / atr
        ma20 = float(baseline.evidence["ma20_adjusted"])
        latest_close = bars[-1].adjusted_close
        exact_anti = replace(self.config, anti_chase_atr_distance=anti_boundary)
        self.assertTrue(evaluate_swing(
            bars, exact_anti, self.portfolio(),
        ).evidence["anti_chase_ok"])
        below_anti_ratio = math.nextafter(anti_boundary, 0.0)
        while ma20 + below_anti_ratio * atr >= latest_close:
            below_anti_ratio = math.nextafter(below_anti_ratio, 0.0)
        below_anti = replace(
            exact_anti, anti_chase_atr_distance=below_anti_ratio,
        )
        self.assertFalse(evaluate_swing(
            bars, below_anti, self.portfolio(),
        ).evidence["anti_chase_ok"])

    def test_trial_gate_boundaries_are_exact(self) -> None:
        base = swing_strategy_bars()
        decision = evaluate_swing(base, self.config, self.portfolio())
        ma20 = float(decision.evidence["ma20_adjusted"])
        atr = float(decision.evidence["atr14_adjusted"])
        previous_high = base[-2].adjusted_high
        reclaim_boundary = sum(bar.adjusted_close for bar in base[-20:-1]) / 19
        cases = (
            ("pullback_low_touched", replace_latest_adjusted(base, low=ma20), True),
            ("pullback_low_touched", replace_latest_adjusted(base, low=math.nextafter(ma20, math.inf)), False),
            ("reclaim_close_above_ma20", replace_latest_adjusted(base, close=reclaim_boundary, high=max(base[-1].adjusted_high, reclaim_boundary), low=min(base[-1].adjusted_low, reclaim_boundary)), False),
            ("confirmation_above_previous_high", replace_latest_adjusted(base, close=previous_high, high=max(base[-1].adjusted_high, previous_high)), False),
            ("confirmation_above_previous_high", replace_latest_adjusted(base, close=math.nextafter(previous_high, math.inf), high=base[-1].adjusted_high), True),
        )
        for gate, bars, expected in cases:
            with self.subTest(gate=gate, expected=expected):
                result = evaluate_swing(bars, self.config, self.portfolio())
                self.assertIs(result.evidence[gate], expected)
        anti_chase = ma20 + self.config.anti_chase_atr_distance * atr
        self.assertLessEqual(base[-1].adjusted_close, anti_chase)
        self.assertTrue(decision.evidence["anti_chase_ok"])

    def test_adjusted_indicators_map_back_to_latest_raw_scale(self) -> None:
        decision = evaluate_swing(
            swing_strategy_bars(raw_scale=2.5), self.config, self.portfolio(),
        )
        self.assertAlmostEqual(decision.evidence["raw_scale"], 2.5)
        self.assertAlmostEqual(
            decision.evidence["ma20_raw"], decision.evidence["ma20_adjusted"] * 2.5,
        )
        self.assertAlmostEqual(
            decision.evidence["atr14_raw"], decision.evidence["atr14_adjusted"] * 2.5,
        )

    def test_atr_uses_adjusted_previous_close_for_gap_true_range(self) -> None:
        bars = swing_strategy_bars(pattern="gap")
        decision = evaluate_swing(bars, self.config, self.portfolio())
        true_ranges = []
        for index in range(len(bars) - 14, len(bars)):
            current = bars[index]
            previous = bars[index - 1]
            true_ranges.append(max(
                current.adjusted_high - current.adjusted_low,
                abs(current.adjusted_high - previous.adjusted_close),
                abs(current.adjusted_low - previous.adjusted_close),
            ))
        self.assertAlmostEqual(decision.evidence["atr14_adjusted"], sum(true_ranges) / 14)

    def test_entry_sizing_respects_each_cap_and_lot_floor(self) -> None:
        bars = swing_strategy_bars()
        cases = (
            ("cash_cap", self.config, {"cash": 1.0}, "cash_cap"),
            (
                "symbol_cap",
                replace(self.config, max_symbol_weight=0.00001),
                {},
                "single_symbol_cap",
            ),
            ("total_cap", self.config, {"current_etf_market_value": 800_000.0}, "total_exposure_cap"),
            ("portfolio_risk", self.config, {"current_planned_risk_amount": 20_000.0}, "portfolio_risk_cap"),
        )
        for label, config, overrides, reason in cases:
            with self.subTest(label=label):
                result = evaluate_swing(bars, config, self.portfolio(**overrides))
                self.assertNotEqual(result.state, SwingState.TRIAL_ENTRY_CANDIDATE)
                self.assertIn(reason, result.blocked_reasons)
        result = evaluate_swing(bars, self.config, self.portfolio(cash=25_050.0))
        self.assertEqual(result.planned_shares % 100, 0)
        self.assertLessEqual(result.planned_shares * result.planned_entry_high, 25_050.0)

        trade_limited = evaluate_swing(
            bars,
            replace(self.config, risk_per_trade=0.000001),
            self.portfolio(),
        )
        self.assertIn("trade_risk_cap", trade_limited.blocked_reasons)
        self.assertIn("minimum_lot", trade_limited.blocked_reasons)
        for gate in (
            "cash_cap_ok", "single_symbol_cap_ok", "total_exposure_cap_ok",
            "trade_risk_cap_ok", "portfolio_risk_cap_ok", "minimum_lot_ok",
            "health_gates_ok", "entry_hard_gates_ok",
        ):
            self.assertIn(gate, trade_limited.evidence)

    def test_contexts_reject_bool_nonfinite_and_malformed_values(self) -> None:
        invalid = (
            lambda: PortfolioContext.empty(True),
            lambda: PortfolioContext.empty(float("nan")),
            lambda: PortfolioContext.empty(1_000.0, lot_size=True),
            lambda: self.position(shares=True),
            lambda: self.position(average_cost=float("inf")),
            lambda: self.position(sellable_shares=10_001),
        )
        for build in invalid:
            with self.subTest(build=build):
                with self.assertRaises(SwingStrategyError):
                    build()

    def test_portfolio_context_has_only_required_logical_fields(self) -> None:
        self.assertEqual(
            tuple(field.name for field in fields(PortfolioContext)),
            (
                "equity", "cash", "current_etf_market_value",
                "current_planned_risk_amount", "lot_size", "data_healthy",
                "metadata_complete", "ledger_healthy", "tradable",
                "next_trading_date", "last_stop_trading_date", "position",
            ),
        )

    def test_health_gates_suppress_entry_with_named_reason(self) -> None:
        for field in ("data_healthy", "metadata_complete", "ledger_healthy", "tradable"):
            with self.subTest(field=field):
                result = evaluate_swing(
                    swing_strategy_bars(), self.config, self.portfolio(**{field: False}),
                )
                self.assertEqual(result.state, SwingState.PULLBACK_WATCH)
                self.assertIn(field, result.blocked_reasons)

    def test_each_health_gate_suppresses_reduce_and_add(self) -> None:
        bars = swing_strategy_bars()
        for field in ("data_healthy", "metadata_complete", "ledger_healthy", "tradable"):
            with self.subTest(field=field, action="reduce"):
                result = evaluate_swing(
                    bars,
                    self.config,
                    self.with_position(
                        self.position(average_cost=100.0, initial_risk_per_share=2.0),
                        **{field: False},
                    ),
                )
                self.assertEqual(result.state, SwingState.HOLDING)
                self.assertIn(field, result.blocked_reasons)
            with self.subTest(field=field, action="add"):
                result = evaluate_swing(
                    bars,
                    self.config,
                    self.with_position(
                        self.position(
                            shares=1_000,
                            sellable_shares=1_000,
                            average_cost=100.0,
                            initial_risk_per_share=5.0,
                            hard_stop=100.0,
                            first_reduction_completed=True,
                        ),
                        **{field: False},
                    ),
                )
                self.assertEqual(result.state, SwingState.HOLDING)
                self.assertIn(field, result.blocked_reasons)

    def test_add_respects_post_add_trade_risk_cap(self) -> None:
        result = evaluate_swing(
            swing_strategy_bars(),
            replace(self.config, risk_per_trade=0.000001),
            self.with_position(self.position(
                shares=1_000,
                sellable_shares=1_000,
                average_cost=100.0,
                initial_risk_per_share=5.0,
                hard_stop=100.0,
                first_reduction_completed=True,
            )),
        )
        self.assertEqual(result.state, SwingState.HOLDING)
        self.assertIn("trade_risk_cap", result.blocked_reasons)
        self.assertFalse(result.evidence["trade_risk_cap_ok"])

    def test_cooldown_counts_completed_sessions_and_day_six_is_eligible(self) -> None:
        bars = swing_strategy_bars(75)
        for elapsed in range(1, 6):
            stop_date = bars[-1 - elapsed].trading_date
            result = evaluate_swing(
                bars, self.config, self.portfolio(last_stop_trading_date=stop_date),
            )
            self.assertEqual(result.state, SwingState.COOLDOWN, elapsed)
            self.assertEqual(result.evidence["cooldown_sessions_elapsed"], elapsed)
        result = evaluate_swing(
            bars, self.config,
            self.portfolio(last_stop_trading_date=bars[-7].trading_date),
        )
        self.assertNotEqual(result.state, SwingState.COOLDOWN)

    def test_holding_add_reduce_and_exit_priority(self) -> None:
        rising = swing_strategy_bars(pattern="rising")
        holding = evaluate_swing(
            rising, self.config,
            self.with_position(self.position(average_cost=110.0, hard_stop=90.0)),
        )
        self.assertEqual(holding.state, SwingState.HOLDING)

        breakout = swing_strategy_bars()
        add = evaluate_swing(
            breakout, self.config,
            self.with_position(self.position(
                shares=1_000,
                sellable_shares=1_000,
                average_cost=100.0,
                initial_risk_per_share=5.0,
                hard_stop=100.0,
                first_reduction_completed=True,
            )),
        )
        self.assertEqual(add.state, SwingState.ADD_CANDIDATE)
        self.assertTrue(add.evidence["add_breakout_ok"])
        self.assertTrue(add.evidence["add_stop_to_cost_ok"])

        no_loss_add = evaluate_swing(
            breakout, self.config,
            self.with_position(self.position(average_cost=200.0, hard_stop=200.0)),
        )
        self.assertNotEqual(no_loss_add.state, SwingState.ADD_CANDIDATE)

        reduce = evaluate_swing(
            breakout, self.config,
            self.with_position(self.position(average_cost=100.0, initial_risk_per_share=2.0)),
        )
        self.assertEqual(reduce.state, SwingState.REDUCE_CANDIDATE)
        self.assertEqual(reduce.planned_shares, 5_000)
        reduced_once = evaluate_swing(
            breakout, self.config,
            self.with_position(self.position(first_reduction_completed=True, hard_stop=90.0)),
        )
        self.assertNotEqual(reduced_once.state, SwingState.REDUCE_CANDIDATE)
        too_small = evaluate_swing(
            breakout, self.config,
            self.with_position(self.position(sellable_shares=199, shares=199)),
        )
        self.assertNotEqual(too_small.state, SwingState.REDUCE_CANDIDATE)

    def test_exit_rules_and_exit_is_never_suppressed(self) -> None:
        cases = (
            (swing_strategy_bars(pattern="exit"), self.position(hard_stop=1.0)),
            (swing_strategy_bars(pattern="falling_ma60"), self.position(hard_stop=1.0)),
            (swing_strategy_bars(pattern="rising"), self.position(highest_completed_close=120.0, hard_stop=1.0)),
            (swing_strategy_bars(pattern="rising"), self.position(hard_stop=200.0)),
        )
        for bars, position in cases:
            with self.subTest(close=bars[-1].close, hard_stop=position.hard_stop):
                result = evaluate_swing(
                    bars, self.config,
                    self.with_position(
                        position,
                        cash=0.0,
                        current_planned_risk_amount=1_000_000.0,
                        data_healthy=False,
                        metadata_complete=False,
                        ledger_healthy=False,
                        tradable=False,
                        last_stop_trading_date=bars[-2].trading_date,
                    ),
                )
                self.assertEqual(result.state, SwingState.EXIT_CANDIDATE)

    def test_degraded_exit_still_exposes_derived_position_risk(self) -> None:
        result = evaluate_swing(
            swing_strategy_bars(pattern="exit"),
            self.config,
            self.with_position(
                self.position(hard_stop=1.0),
                data_healthy=False,
                metadata_complete=False,
                ledger_healthy=False,
                tradable=False,
            ),
        )
        self.assertEqual(result.state, SwingState.EXIT_CANDIDATE)
        self.assertGreater(result.evidence["position_risk_amount"], 0.0)
        self.assertGreater(result.evidence["position_risk_rate"], 0.0)
        self.assertEqual(
            result.planned_risk_rate, result.evidence["position_risk_rate"],
        )

    def test_formal_valid_date_uses_calendar_or_weekday_fallback(self) -> None:
        bars = swing_strategy_bars()
        specified = bars[-1].trading_date + timedelta(days=4)
        explicit = evaluate_swing(
            bars, self.config, self.portfolio(next_trading_date=specified),
        )
        self.assertEqual(explicit.valid_for_trading_date, specified)
        self.assertFalse(explicit.evidence["calendar_fallback_used"])
        fallback = evaluate_swing(bars, self.config, self.portfolio())
        self.assertIsNotNone(fallback.valid_for_trading_date)
        self.assertTrue(fallback.evidence["calendar_fallback_used"])
        self.assertLess(fallback.valid_for_trading_date.weekday(), 5)

    def test_intraday_overlay_never_changes_formal_state(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        inside = (formal.planned_entry_low + formal.planned_entry_high) / 2
        overlay = evaluate_intraday_overlay(formal, inside, has_position=False)
        self.assertEqual(overlay.formal_state, formal.state)
        self.assertEqual(overlay.overlay, IntradayOverlay.APPROACHING_ENTRY_ZONE)
        unavailable = evaluate_intraday_overlay(
            formal, inside, feed_healthy=False, has_position=False,
        )
        self.assertEqual(unavailable.overlay, IntradayOverlay.INTRADAY_FEED_UNAVAILABLE)
        self.assertFalse(unavailable.evidence["stop_touched"])

    def test_intraday_stop_has_priority_and_prospective_stop_cannot_touch(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        prospective = evaluate_intraday_overlay(
            formal, formal.planned_stop, has_position=False,
        )
        self.assertNotEqual(prospective.overlay, IntradayOverlay.PREDEFINED_STOP_TOUCHED)
        held = evaluate_swing(
            swing_strategy_bars(pattern="rising"), self.config,
            self.with_position(self.position(average_cost=120.0, hard_stop=90.0)),
        )
        touched = evaluate_intraday_overlay(
            held, held.planned_stop, has_position=True,
        )
        self.assertEqual(touched.overlay, IntradayOverlay.PREDEFINED_STOP_TOUCHED)

    def test_intraday_exact_boundaries_and_hostile_numeric_inputs(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        for price in (formal.planned_entry_low, formal.planned_entry_high):
            with self.subTest(entry_price=price):
                result = evaluate_intraday_overlay(formal, price, has_position=False)
                self.assertEqual(result.overlay, IntradayOverlay.APPROACHING_ENTRY_ZONE)
        for price in (True, float("nan"), float("inf"), -1.0, None, "100"):
            with self.subTest(hostile_price=price):
                result = evaluate_intraday_overlay(formal, price, has_position=False)
                self.assertEqual(
                    result.overlay, IntradayOverlay.INTRADAY_FEED_UNAVAILABLE,
                )

    def test_decision_and_evidence_are_immutable_json_safe_and_inputs_unchanged(self) -> None:
        source = list(swing_strategy_bars())
        snapshot = tuple(source)
        decision = evaluate_swing(source, self.config, self.portfolio())
        self.assertEqual(tuple(source), snapshot)
        with self.assertRaises(FrozenInstanceError):
            decision.planned_shares = 0
        with self.assertRaises(TypeError):
            decision.evidence["sample_ok"] = False
        json.dumps(dict(decision.evidence))

    def test_valid_decisions_expose_complete_gate_and_sizing_evidence(self) -> None:
        decisions = (
            evaluate_swing(swing_strategy_bars(), self.config, self.portfolio()),
            evaluate_swing(
                swing_strategy_bars(pattern="exit"),
                self.config,
                self.with_position(self.position(hard_stop=1.0)),
            ),
        )
        required = {
            "trend_allowed", "trial_technical_ok", "health_gates_ok",
            "entry_hard_gates_ok", "cash_cap_shares",
            "single_symbol_cap_shares", "total_exposure_cap_shares",
            "trade_risk_cap_shares", "portfolio_risk_cap_shares",
            "selected_shares", "exit_any", "reduce_candidate_technical_ok",
            "add_candidate_technical_ok",
        }
        for decision in decisions:
            with self.subTest(state=decision.state):
                self.assertTrue(required.issubset(decision.evidence))

    def test_invalid_bar_sequences_return_data_unavailable(self) -> None:
        bars = swing_strategy_bars()
        cases = (
            (),
            (*bars[:-1], bars[-2]),
            (*bars[:-1], {**bars[-1].to_dict(), "symbol": "510500"}),
            (*bars[:-1], {**bars[-1].to_dict(), "close": float("nan")}),
        )
        for candidate in cases:
            with self.subTest(length=len(candidate)):
                result = evaluate_swing(candidate, self.config, self.portfolio())
                self.assertEqual(result.state, SwingState.DATA_UNAVAILABLE)
                self.assertTrue(result.blocked_reasons)

    def test_unrepresentable_sizing_arithmetic_is_data_unavailable(self) -> None:
        result = evaluate_swing(
            swing_strategy_bars(raw_scale=1e-306),
            self.config,
            self.portfolio(equity=1e308, cash=1e308),
        )
        self.assertEqual(result.state, SwingState.DATA_UNAVAILABLE)
        self.assertIn("sizing_calculation_failed", result.blocked_reasons)


if __name__ == "__main__":
    unittest.main()
