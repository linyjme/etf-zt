from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from collections.abc import Iterator, Mapping
from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
from types import MappingProxyType
import unittest

from etf_rotation.swing_config import load_strategy
from etf_rotation.swing_strategy import (
    IntradayDecision,
    IntradayOverlay,
    PortfolioContext,
    PositionContext,
    SwingState,
    SwingStrategyError,
    evaluate_intraday_overlay,
    evaluate_swing,
)
from tests.swing_helpers import (
    replace_adjusted_bar,
    replace_latest_adjusted,
    swing_strategy_bars,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class HostileInt(int):
    def __float__(self) -> float:
        raise AssertionError("hostile int conversion invoked")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("hostile int comparison invoked")


class HostileFloat(float):
    def __float__(self) -> float:
        raise AssertionError("hostile float conversion invoked")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("hostile float comparison invoked")


class HostileString(str):
    def strip(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("hostile string method invoked")


class HostileMapping(Mapping[str, object]):
    hooks_called = 0

    def __getitem__(self, key: str) -> object:
        type(self).hooks_called += 1
        raise AssertionError("hostile mapping read invoked")

    def __iter__(self) -> Iterator[str]:
        type(self).hooks_called += 1
        raise AssertionError("hostile mapping iteration invoked")

    def __len__(self) -> int:
        type(self).hooks_called += 1
        raise AssertionError("hostile mapping length invoked")

    def items(self):
        type(self).hooks_called += 1
        raise AssertionError("hostile mapping items invoked")


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

    def test_each_trend_gate_changes_only_above_its_local_ulp_boundary(self) -> None:
        base = swing_strategy_bars(pattern="flat")

        def at_close(close: float):
            return evaluate_swing(
                replace_latest_adjusted(
                    base,
                    close=close,
                    high=max(base[-1].adjusted_high, close),
                    low=min(base[-1].adjusted_low, close),
                ),
                self.config,
                self.portfolio(),
            )

        gate_names = (
            "trend_close_above_ma60",
            "trend_ma20_above_ma60",
            "trend_ma60_rising",
        )
        equality = 100.0
        below = math.nextafter(equality, 0.0)
        for gate in gate_names:
            with self.subTest(gate=gate, side="below"):
                self.assertFalse(at_close(below).evidence[gate])
            with self.subTest(gate=gate, side="equal"):
                self.assertFalse(at_close(equality).evidence[gate])

        for gate in gate_names:
            last_false = equality
            for _ in range(128):
                first_true = math.nextafter(last_false, math.inf)
                if at_close(first_true).evidence[gate]:
                    break
                last_false = first_true
            else:
                self.fail(f"{gate} boundary was not crossed within 128 local ULPs")
            with self.subTest(gate=gate, side="adjacent_false"):
                self.assertFalse(at_close(last_false).evidence[gate])
            with self.subTest(gate=gate, side="adjacent_true"):
                self.assertTrue(at_close(first_true).evidence[gate])

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

        adjacent_pullback = math.nextafter(below_pullback_ratio, math.inf)
        self.assertTrue(evaluate_swing(
            bars,
            replace(self.config, pullback_atr_distance=adjacent_pullback),
            self.portfolio(),
        ).evidence["pullback_distance_ok"])
        adjacent_anti = math.nextafter(below_anti_ratio, math.inf)
        self.assertTrue(evaluate_swing(
            bars,
            replace(self.config, anti_chase_atr_distance=adjacent_anti),
            self.portfolio(),
        ).evidence["anti_chase_ok"])

    def test_reclaim_and_previous_high_adjacent_representable_boundaries(self) -> None:
        base = swing_strategy_bars()
        reclaim_equal = math.fsum(
            bar.adjusted_close for bar in base[-20:-1]
        ) / 19

        def with_close(value: float):
            return replace_latest_adjusted(
                base,
                close=value,
                high=max(base[-1].adjusted_high, value),
                low=min(base[-1].adjusted_low, value),
            )

        self.assertFalse(evaluate_swing(
            with_close(reclaim_equal), self.config, self.portfolio(),
        ).evidence["reclaim_close_above_ma20"])
        reclaim_below = reclaim_equal
        while True:
            reclaim_above = math.nextafter(reclaim_below, math.inf)
            if evaluate_swing(
                with_close(reclaim_above), self.config, self.portfolio(),
            ).evidence["reclaim_close_above_ma20"]:
                break
            reclaim_below = reclaim_above
        for close, expected in (
            (reclaim_below, False),
            (reclaim_above, True),
        ):
            with self.subTest(gate="reclaim", close=close):
                self.assertIs(
                    evaluate_swing(
                        with_close(close), self.config, self.portfolio(),
                    ).evidence["reclaim_close_above_ma20"],
                    expected,
                )

        previous_high = base[-2].adjusted_high
        for close, expected in (
            (math.nextafter(previous_high, 0.0), False),
            (previous_high, False),
            (math.nextafter(previous_high, math.inf), True),
        ):
            with self.subTest(gate="previous_high", close=close):
                self.assertIs(
                    evaluate_swing(
                        with_close(close), self.config, self.portfolio(),
                    ).evidence["confirmation_above_previous_high"],
                    expected,
                )

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

    def test_entry_caps_snap_local_ulp_boundary_but_reject_meaningful_shortfall(self) -> None:
        bars = swing_strategy_bars()
        baseline = evaluate_swing(bars, self.config, self.portfolio())
        entry = baseline.planned_entry_high
        per_share_risk = float(baseline.evidence["per_share_risk_raw"])
        equity = 1_000_000.0

        def evaluate_cap(name: str, cap: float):
            config = self.config
            portfolio = self.portfolio()
            if name == "cash_cap":
                portfolio = self.portfolio(cash=entry * cap)
            elif name == "single_symbol_cap":
                config = replace(
                    config, max_symbol_weight=entry * cap / equity,
                )
            elif name == "total_exposure_cap":
                weight = entry * cap / equity
                config = replace(
                    config, max_symbol_weight=weight, max_equity_weight=weight,
                )
            elif name == "trade_risk_cap":
                config = replace(
                    config, risk_per_trade=per_share_risk * cap / equity,
                )
            elif name == "portfolio_risk_cap":
                rate = per_share_risk * cap / equity
                config = replace(
                    config, risk_per_trade=rate, max_portfolio_risk=rate,
                )
            return evaluate_swing(bars, config, portfolio)

        for reason in (
            "cash_cap", "single_symbol_cap", "total_exposure_cap",
            "trade_risk_cap", "portfolio_risk_cap",
        ):
            with self.subTest(reason=reason, boundary="local_ulp"):
                exact = evaluate_cap(reason, 99.99999999999986)
                self.assertEqual(exact.state, SwingState.TRIAL_ENTRY_CANDIDATE)
                self.assertEqual(exact.planned_shares, 100)
            with self.subTest(reason=reason, boundary="meaningfully_below"):
                below = evaluate_cap(reason, 99.99)
                self.assertEqual(below.state, SwingState.PULLBACK_WATCH)
                self.assertIn(reason, below.blocked_reasons)

    def test_reduce_half_lot_exact_and_just_below(self) -> None:
        bars = swing_strategy_bars()
        exact = evaluate_swing(
            bars,
            self.config,
            self.with_position(self.position(shares=200, sellable_shares=200)),
        )
        self.assertEqual(exact.state, SwingState.REDUCE_CANDIDATE)
        self.assertEqual(exact.planned_shares, 100)
        below = evaluate_swing(
            bars,
            self.config,
            self.with_position(self.position(shares=199, sellable_shares=199)),
        )
        self.assertEqual(below.state, SwingState.HOLDING)
        self.assertIn("reduce_quantity_below_lot", below.blocked_reasons)

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

    def test_contexts_reject_hostile_numeric_subclasses_and_huge_integers(self) -> None:
        invalid = (
            lambda: PortfolioContext.empty(HostileInt(1_000)),
            lambda: PortfolioContext.empty(HostileFloat(1_000.0)),
            lambda: PortfolioContext.empty(10**400),
            lambda: self.position(average_cost=HostileInt(100)),
            lambda: self.position(hard_stop=HostileFloat(90.0)),
            lambda: self.position(highest_completed_close=10**400),
            lambda: self.position(shares=HostileInt(1_000)),
            lambda: PortfolioContext.empty(1_000.0, lot_size=HostileInt(100)),
        )
        for build in invalid:
            with self.subTest(build=build):
                with self.assertRaises(SwingStrategyError):
                    build()

    def test_contexts_store_normalized_builtin_floats(self) -> None:
        portfolio = PortfolioContext.empty(
            1_000_000,
            cash=500_000,
            current_etf_market_value=100_000,
            current_planned_risk_amount=5_000,
        )
        position = self.position(
            average_cost=100,
            initial_risk_per_share=2,
            highest_completed_close=105,
            hard_stop=95,
        )
        for value in (
            portfolio.equity,
            portfolio.cash,
            portfolio.current_etf_market_value,
            portfolio.current_planned_risk_amount,
            position.average_cost,
            position.initial_risk_per_share,
            position.highest_completed_close,
            position.hard_stop,
        ):
            self.assertIs(type(value), float)

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

    def test_add_caps_snap_local_ulp_boundary_but_reject_meaningful_shortfall(self) -> None:
        bars = swing_strategy_bars()
        position = self.position(
            shares=1_000,
            sellable_shares=1_000,
            average_cost=100.0,
            initial_risk_per_share=5.0,
            hard_stop=100.0,
            first_reduction_completed=True,
        )
        baseline_portfolio = self.with_position(position)
        baseline = evaluate_swing(bars, self.config, baseline_portfolio)
        self.assertEqual(baseline.state, SwingState.ADD_CANDIDATE)
        entry = bars[-1].close
        equity = baseline_portfolio.equity
        position_value = position.shares * entry
        position_risk = float(baseline.evidence["position_risk_amount"])
        per_share_risk = entry - baseline.planned_stop

        def evaluate_cap(name: str, cap: float):
            config = self.config
            portfolio = baseline_portfolio
            if name == "cash_cap":
                portfolio = replace(portfolio, cash=entry * cap)
            elif name == "single_symbol_cap":
                config = replace(
                    config,
                    max_symbol_weight=(position_value + entry * cap) / equity,
                )
            elif name == "total_exposure_cap":
                config = replace(
                    config,
                    max_equity_weight=(
                        portfolio.current_etf_market_value + entry * cap
                    ) / equity,
                )
            elif name == "trade_risk_cap":
                config = replace(
                    config,
                    risk_per_trade=(position_risk + per_share_risk * cap) / equity,
                )
            elif name == "portfolio_risk_cap":
                config = replace(
                    config,
                    max_portfolio_risk=(
                        portfolio.current_planned_risk_amount
                        + per_share_risk * cap
                    ) / equity,
                )
            return evaluate_swing(bars, config, portfolio)

        for reason in (
            "cash_cap", "single_symbol_cap", "total_exposure_cap",
            "trade_risk_cap", "portfolio_risk_cap",
        ):
            with self.subTest(reason=reason, boundary="local_ulp"):
                exact = evaluate_cap(reason, 99.99999999999986)
                self.assertEqual(exact.state, SwingState.ADD_CANDIDATE)
                self.assertEqual(exact.planned_shares, 100)
            with self.subTest(reason=reason, boundary="meaningfully_below"):
                below = evaluate_cap(reason, 99.99)
                self.assertEqual(below.state, SwingState.HOLDING)
                self.assertIn(reason, below.blocked_reasons)

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

    def test_reduce_exact_two_r_and_adjacent_costs(self) -> None:
        bars = swing_strategy_bars()
        latest = bars[-1].close
        initial_risk = 2.0
        average_equal = latest - self.config.reduce_profit_r * initial_risk

        def decision(average_cost: float):
            return evaluate_swing(
                bars,
                self.config,
                self.with_position(self.position(
                    shares=200,
                    sellable_shares=200,
                    average_cost=average_cost,
                    initial_risk_per_share=initial_risk,
                    entry_trading_date=bars[-1].trading_date,
                    highest_completed_close=latest,
                    hard_stop=1.0,
                )),
            )

        self.assertEqual(decision(average_equal).state, SwingState.REDUCE_CANDIDATE)
        self.assertEqual(
            decision(math.nextafter(average_equal, 0.0)).state,
            SwingState.REDUCE_CANDIDATE,
        )
        self.assertNotEqual(
            decision(math.nextafter(average_equal, math.inf)).state,
            SwingState.REDUCE_CANDIDATE,
        )

    def test_add_exact_one_r_breakout_and_stop_to_cost_boundaries(self) -> None:
        bars = swing_strategy_bars()
        latest = bars[-1].close
        prior_high = max(bar.adjusted_high for bar in bars[-21:-1])
        average_equal = latest - 0.1
        initial_risk = latest - average_equal

        def position_decision(
            *,
            average_cost: float = average_equal,
            hard_stop: float = average_equal,
            risk: float = initial_risk,
            candidate_bars=bars,
        ):
            return evaluate_swing(
                candidate_bars,
                self.config,
                self.with_position(self.position(
                    shares=1_000,
                    sellable_shares=1_000,
                    average_cost=average_cost,
                    initial_risk_per_share=risk,
                    entry_trading_date=candidate_bars[-1].trading_date,
                    highest_completed_close=candidate_bars[-1].close,
                    hard_stop=hard_stop,
                    first_reduction_completed=True,
                )),
            )

        self.assertTrue(position_decision().evidence["add_profit_ok"])
        self.assertTrue(position_decision().evidence["add_stop_to_cost_ok"])
        self.assertFalse(position_decision(
            average_cost=math.nextafter(average_equal, math.inf),
            hard_stop=math.nextafter(average_equal, math.inf),
        ).evidence["add_profit_ok"])
        self.assertTrue(position_decision(
            average_cost=math.nextafter(average_equal, 0.0),
            hard_stop=math.nextafter(average_equal, 0.0),
        ).evidence["add_profit_ok"])
        self.assertFalse(position_decision(
            hard_stop=math.nextafter(average_equal, 0.0),
        ).evidence["add_stop_to_cost_ok"])
        self.assertTrue(position_decision(
            hard_stop=math.nextafter(average_equal, math.inf),
        ).evidence["add_stop_to_cost_ok"])

        for close, expected in (
            (math.nextafter(prior_high, 0.0), False),
            (prior_high, False),
            (math.nextafter(prior_high, math.inf), True),
        ):
            candidate_bars = replace_latest_adjusted(
                bars,
                close=close,
                high=max(bars[-1].adjusted_high, close),
                low=min(bars[-1].adjusted_low, close),
            )
            candidate_average = close - 1.0
            candidate_risk = close - candidate_average
            result = position_decision(
                average_cost=candidate_average,
                hard_stop=candidate_average,
                risk=candidate_risk,
                candidate_bars=candidate_bars,
            )
            self.assertIs(result.evidence["add_breakout_ok"], expected)

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

    def test_exit_thresholds_are_exact_at_ma60_and_hard_stop(self) -> None:
        base = swing_strategy_bars(pattern="rising")
        ma60_equal = sum(bar.adjusted_close for bar in base[-60:-1]) / 59

        def ma60_decision(close: float):
            bars = replace_latest_adjusted(
                base,
                close=close,
                high=max(base[-1].adjusted_high, close),
                low=min(base[-1].adjusted_low, close),
            )
            return evaluate_swing(
                bars,
                self.config,
                self.with_position(self.position(
                    average_cost=200.0,
                    entry_trading_date=bars[-1].trading_date,
                    highest_completed_close=bars[-1].close,
                    hard_stop=1.0,
                )),
            )

        for close, expected in (
            (math.nextafter(ma60_equal, 0.0), True),
            (ma60_equal, False),
            (math.nextafter(ma60_equal, math.inf), False),
        ):
            with self.subTest(exit_gate="ma60", close=close):
                self.assertIs(
                    ma60_decision(close).evidence["exit_close_below_ma60"],
                    expected,
                )

        latest = base[-1].close
        for stop, expected in (
            (math.nextafter(latest, 0.0), False),
            (latest, True),
            (math.nextafter(latest, math.inf), True),
        ):
            with self.subTest(exit_gate="hard_stop", stop=stop):
                decision = evaluate_swing(
                    base,
                    self.config,
                    self.with_position(self.position(
                        average_cost=200.0,
                        entry_trading_date=base[-1].trading_date,
                        highest_completed_close=latest,
                        hard_stop=stop,
                    )),
                )
                self.assertIs(decision.evidence["exit_hard_stop"], expected)

    def test_two_ma20_exit_equality_and_adjacent_values(self) -> None:
        base = swing_strategy_bars(pattern="rising")
        previous_equal = sum(
            bar.adjusted_close for bar in base[-21:-2]
        ) / 19

        def decision(previous_direction: float, latest_direction: float):
            previous_close = (
                previous_equal
                if previous_direction == 0.0
                else math.nextafter(previous_equal, previous_direction)
            )
            bars = replace_adjusted_bar(
                base,
                -2,
                close=previous_close,
                high=max(base[-2].adjusted_high, previous_close),
                low=min(base[-2].adjusted_low, previous_close),
            )
            latest_equal = sum(
                bar.adjusted_close for bar in bars[-20:-1]
            ) / 19
            latest_close = (
                latest_equal
                if latest_direction == 0.0
                else math.nextafter(latest_equal, latest_direction)
            )
            bars = replace_latest_adjusted(
                bars,
                close=latest_close,
                high=max(bars[-1].adjusted_high, latest_close),
                low=min(bars[-1].adjusted_low, latest_close),
            )
            return evaluate_swing(
                bars,
                self.config,
                self.with_position(self.position(
                    average_cost=200.0,
                    entry_trading_date=bars[-1].trading_date,
                    highest_completed_close=bars[-1].close,
                    hard_stop=1.0,
                )),
            )

        cases = (
            (0.0, 0.0, False),
            (math.inf, math.inf, False),
            (-math.inf, -math.inf, True),
            (-math.inf, 0.0, False),
            (-math.inf, math.inf, False),
            (0.0, -math.inf, False),
            (math.inf, -math.inf, False),
        )
        for previous_direction, latest_direction, expected in cases:
            with self.subTest(
                previous_direction=previous_direction,
                latest_direction=latest_direction,
            ):
                self.assertIs(
                    decision(previous_direction, latest_direction).evidence[
                        "exit_two_closes_below_ma20"
                    ],
                    expected,
                )

    def test_trailing_stop_equality_and_adjacent_highs(self) -> None:
        bars = swing_strategy_bars(pattern="rising")
        baseline = evaluate_swing(bars, self.config, self.portfolio())
        distance = self.config.trailing_stop_atr * float(
            baseline.evidence["atr14_raw"],
        )
        latest = bars[-1].close
        highest_equal = latest + distance

        def decision(highest: float):
            return evaluate_swing(
                bars,
                self.config,
                self.with_position(self.position(
                    average_cost=200.0,
                    entry_trading_date=bars[-1].trading_date,
                    highest_completed_close=highest,
                    hard_stop=1.0,
                )),
            )

        equal = decision(highest_equal)
        self.assertEqual(equal.evidence["trailing_stop_raw"], latest)
        self.assertTrue(equal.evidence["exit_trailing_stop"])
        self.assertFalse(decision(
            math.nextafter(highest_equal, 0.0),
        ).evidence["exit_trailing_stop"])
        self.assertTrue(decision(
            math.nextafter(highest_equal, math.inf),
        ).evidence["exit_trailing_stop"])

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
        for price in (HostileInt(100), HostileFloat(100.0), 10**400):
            with self.subTest(hostile_subclass=type(price).__name__):
                result = evaluate_intraday_overlay(formal, price, has_position=False)
                self.assertEqual(
                    result.overlay, IntradayOverlay.INTRADAY_FEED_UNAVAILABLE,
                )

    def test_intraday_does_not_mutate_formal_or_evidence(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        before = dict(formal.evidence)
        result = evaluate_intraday_overlay(
            formal, formal.planned_entry_low, has_position=False,
        )
        self.assertEqual(dict(formal.evidence), before)
        self.assertIs(result.formal_state, formal.state)

    def test_direct_swing_decision_rejects_malformed_public_fields(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        invalid = (
            {"symbol": HostileString("510300")},
            {"symbol": "   "},
            {"strategy_version": HostileString("SWING_V1")},
            {"strategy_version": ""},
            {"state": "TRIAL_ENTRY_CANDIDATE"},
            {"as_of_trading_date": datetime(2026, 1, 1)},
            {"valid_for_trading_date": "2026-01-01"},
            {"blocked_reasons": (HostileString("blocked"),)},
            {"blocked_reasons": (reason for reason in ("blocked",))},
            {"evidence": {"nested": []}},
            {"evidence": {HostileString("key"): 1}},
            {"evidence": HostileMapping()},
            {"planned_entry_low": float("nan")},
            {"planned_entry_low": -1.0},
            {"planned_entry_low": formal.planned_entry_high + 1.0},
            {"planned_shares": True},
            {"planned_shares": -1},
            {"planned_risk_rate": float("inf")},
            {"planned_risk_rate": -1.0},
            {"first_reduce_price": HostileFloat(100.0)},
        )
        for changes in invalid:
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(SwingStrategyError):
                    replace(formal, **changes)

    def test_swing_decision_wraps_hostile_mappingproxy_evidence_failure(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        HostileMapping.hooks_called = 0
        hostile_proxy = MappingProxyType(HostileMapping())
        with self.assertRaisesRegex(
            SwingStrategyError, "^evidence must be a scalar mapping$",
        ) as raised:
            replace(formal, evidence=hostile_proxy)
        self.assertIsInstance(raised.exception.__cause__, AssertionError)
        self.assertGreater(HostileMapping.hooks_called, 0)

    def test_direct_decision_copies_mutable_inputs(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        source_evidence: dict[str, object] = {"gate": True}
        source_reasons = ["blocked"]
        copied = replace(
            formal, evidence=source_evidence, blocked_reasons=source_reasons,
        )
        source_evidence["gate"] = False
        source_reasons.append("later")
        self.assertEqual(dict(copied.evidence), {"gate": True})
        self.assertEqual(copied.blocked_reasons, ("blocked",))
        with self.assertRaises(TypeError):
            copied.evidence["gate"] = False

    def test_direct_intraday_decision_rejects_malformed_public_fields(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        intraday = evaluate_intraday_overlay(
            formal, formal.planned_entry_low, has_position=False,
        )
        invalid = (
            {"formal_state": "TRIAL_ENTRY_CANDIDATE"},
            {"overlay": "NONE"},
            {"price": float("nan")},
            {"price": -1.0},
            {"planned_entry_low": HostileInt(1)},
            {"planned_entry_low": intraday.planned_entry_high + 1.0},
            {"planned_stop": 0.0},
            {"evidence": {"nested": {}}},
            {"evidence": HostileMapping()},
        )
        HostileMapping.hooks_called = 0
        for changes in invalid:
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(SwingStrategyError):
                    replace(intraday, **changes)
        self.assertGreater(HostileMapping.hooks_called, 0)

        source = {"near": True}
        copied = replace(intraday, evidence=source)
        source["near"] = False
        self.assertEqual(dict(copied.evidence), {"near": True})
        with self.assertRaises(TypeError):
            copied.evidence["near"] = False

    def test_intraday_decision_wraps_hostile_mappingproxy_evidence_failure(self) -> None:
        formal = evaluate_swing(swing_strategy_bars(), self.config, self.portfolio())
        intraday = evaluate_intraday_overlay(
            formal, formal.planned_entry_low, has_position=False,
        )
        HostileMapping.hooks_called = 0
        hostile_proxy = MappingProxyType(HostileMapping())
        with self.assertRaisesRegex(
            SwingStrategyError, "^evidence must be a scalar mapping$",
        ) as raised:
            replace(intraday, evidence=hostile_proxy)
        self.assertIsInstance(raised.exception.__cause__, AssertionError)
        self.assertGreater(HostileMapping.hooks_called, 0)

    def test_invalid_supplied_next_trading_date_suppresses_candidate(self) -> None:
        bars = swing_strategy_bars()
        for invalid_date in (bars[-1].trading_date, bars[-2].trading_date):
            with self.subTest(invalid_date=invalid_date):
                result = evaluate_swing(
                    bars,
                    self.config,
                    self.portfolio(next_trading_date=invalid_date),
                )
                self.assertEqual(result.state, SwingState.PULLBACK_WATCH)
                self.assertIsNone(result.valid_for_trading_date)
                self.assertIn("invalid_next_trading_date", result.blocked_reasons)
                self.assertTrue(result.evidence["invalid_next_trading_date"])
                self.assertFalse(result.evidence["calendar_fallback_used"])

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

    def test_whitespace_symbol_in_invalid_bar_returns_safe_data_unavailable(self) -> None:
        bars = swing_strategy_bars()
        malformed = tuple({**bar.to_dict(), "symbol": "   "} for bar in bars)
        result = evaluate_swing(malformed, self.config, self.portfolio())
        self.assertEqual(result.state, SwingState.DATA_UNAVAILABLE)
        self.assertEqual(result.symbol, "")

    def test_unrepresentable_sizing_arithmetic_is_data_unavailable(self) -> None:
        result = evaluate_swing(
            swing_strategy_bars(raw_scale=1e-306),
            self.config,
            self.portfolio(equity=1e308, cash=1e308),
        )
        self.assertEqual(result.state, SwingState.DATA_UNAVAILABLE)
        self.assertIn("sizing_calculation_failed", result.blocked_reasons)

    def test_nonpositive_prospective_stop_is_data_unavailable(self) -> None:
        bars = swing_strategy_bars(pattern="flat")
        for index in range(len(bars) - self.config.atr_days, len(bars)):
            bars = replace_adjusted_bar(
                bars, index, open_price=100.0, high=200.0, low=1.0, close=100.0,
            )
        result = evaluate_swing(bars, self.config, self.portfolio())
        self.assertEqual(result.state, SwingState.DATA_UNAVAILABLE)
        self.assertIn("indicator_calculation_failed", result.blocked_reasons)

    def test_mapping_inputs_are_not_mutated(self) -> None:
        source = [bar.to_dict() for bar in swing_strategy_bars()]
        snapshot = [dict(bar) for bar in source]
        evaluate_swing(source, self.config, self.portfolio())
        self.assertEqual(source, snapshot)


if __name__ == "__main__":
    unittest.main()
