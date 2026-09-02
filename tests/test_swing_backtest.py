from __future__ import annotations

from dataclasses import fields, replace
from datetime import date
import json
import math
from pathlib import Path
import unittest
from unittest.mock import patch

from etf_rotation import swing_strategy as swing_strategy_module
from etf_rotation.etf_metadata import TradingMetadata
from etf_rotation.swing_backtest import (
    BacktestAccount,
    PendingAction,
    SwingBacktestError,
    SwingBacktester,
    strategy_lookback,
)
from etf_rotation.swing_config import load_strategy
from etf_rotation.swing_strategy import (
    PortfolioContext,
    SwingDecision,
    SwingState,
    evaluate_swing,
)
from tests.swing_helpers import swing_strategy_bars, with_raw_scales


ROOT = Path(__file__).resolve().parents[1]


def _decision(
    state: SwingState,
    signal_date: date,
    execution_date: date,
    *,
    shares: int = 1_000,
    stop: float | None = 99.0,
    entry_low: float = 1.0,
    entry_high: float = 1_000_000.0,
    evidence: dict[str, object] | None = None,
) -> SwingDecision:
    action_states = {
        SwingState.TRIAL_ENTRY_CANDIDATE,
        SwingState.ADD_CANDIDATE,
        SwingState.REDUCE_CANDIDATE,
        SwingState.EXIT_CANDIDATE,
    }
    if state not in action_states:
        shares = 0
    return SwingDecision(
        symbol="510300",
        strategy_version="SWING_V1",
        as_of_trading_date=signal_date,
        state=state,
        trend_score=0.0,
        evidence={} if evidence is None else evidence,
        blocked_reasons=(),
        planned_entry_low=(
            entry_low if state is SwingState.TRIAL_ENTRY_CANDIDATE else None
        ),
        planned_entry_high=(
            entry_high if state is SwingState.TRIAL_ENTRY_CANDIDATE else None
        ),
        planned_stop=stop,
        planned_shares=shares,
        planned_risk_rate=0.01,
        first_reduce_price=120.0,
        valid_for_trading_date=(
            execution_date if state is SwingState.TRIAL_ENTRY_CANDIDATE else None
        ),
    )


class PortfolioSwingBacktestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_strategy(ROOT / "data" / "swing" / "strategy.json")
        self.trading = TradingMetadata(
            exchange="SSE",
            asset_type="DOMESTIC_EQUITY_ETF",
            intraday_turnaround=False,
            sellable_delay_days=1,
            lot_size=100,
            price_tick=0.001,
            price_limit_pct=0.20,
            volume_unit_shares=100,
        )
        self.backtester = SwingBacktester(self.config, self.trading)

    @staticmethod
    def histories(count: int = 90) -> dict[str, tuple[object, ...]]:
        return {
            symbol: swing_strategy_bars(
                count, symbol=symbol, pattern="pullback_reclaim",
                raw_scale=0.04 + index * 0.001,
            )
            for index, symbol in enumerate(("510300", "510500"))
        }

    def test_exit_reduce_add_and_trial_priority_is_stable(self) -> None:
        actions = tuple(
            PendingAction(kind, symbol, score, None)
            for kind, symbol, score in (
                ("TRIAL_ENTRY", "510500", 9.0),
                ("ADD", "510300", 2.0),
                ("EXIT", "510500", 0.1),
                ("REDUCE", "510300", 3.0),
                ("TRIAL_ENTRY", "510300", 9.0),
            )
        )
        ranked = self.backtester.rank_actions(actions)
        self.assertEqual(
            [(item.kind, item.symbol) for item in ranked],
            [
                ("EXIT", "510500"),
                ("REDUCE", "510300"),
                ("ADD", "510300"),
                ("TRIAL_ENTRY", "510300"),
                ("TRIAL_ENTRY", "510500"),
            ],
        )

    def test_portfolio_uses_one_cash_budget_and_deterministic_json(self) -> None:
        histories = self.histories()
        first = self.backtester.run_portfolio(histories, 100_000.0)
        second = self.backtester.run_portfolio(histories, 100_000.0)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertLessEqual(first.max_equity_weight, self.config.max_equity_weight + 1e-12)
        self.assertLessEqual(first.max_planned_risk, self.config.max_portfolio_risk + 1e-12)
        self.assertEqual(first.symbols, ("510300", "510500"))
        self.assertEqual(first.event_dates, tuple(sorted(first.event_dates)))

    def test_simultaneous_candidates_compete_for_one_portfolio_risk_budget(self) -> None:
        config = replace(
            self.config,
            risk_per_trade=0.02,
            max_symbol_weight=1.0,
            max_equity_weight=1.0,
            max_portfolio_risk=0.02,
        )
        backtester = SwingBacktester(config, self.trading)
        histories = self.histories(72)

        def evaluate(signal_bars, _config, context, **_kwargs):
            symbol = signal_bars[-1].symbol
            if context.position is None and len(signal_bars) == 70:
                return replace(
                    _decision(
                        SwingState.TRIAL_ENTRY_CANDIDATE,
                        signal_bars[-1].trading_date,
                        context.next_trading_date,
                        shares=100_000,
                        stop=3.0,
                    ),
                    symbol=symbol,
                    trend_score=2.0 if symbol == "510300" else 1.0,
                )
            return replace(
                _decision(
                    SwingState.HOLDING if context.position else SwingState.UPTREND_WATCH,
                    signal_bars[-1].trading_date,
                    context.next_trading_date,
                    shares=0,
                    stop=None,
                ),
                symbol=symbol,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = backtester.run_portfolio(histories, 100_000.0)
        self.assertLessEqual(result.max_planned_risk, config.max_portfolio_risk + 1e-12)
        self.assertGreater(
            result.metrics.rejection_counts.get("PORTFOLIO_RISK_LIMIT", 0), 0,
        )
        buys = [item for item in result.trades if item.side == "BUY"]
        self.assertTrue(buys)
        self.assertEqual(buys[0].symbol, "510300")

    def test_same_day_fills_recheck_shared_budget_at_actual_cost(self) -> None:
        config = replace(
            self.config,
            risk_per_trade=0.50,
            max_symbol_weight=0.40,
            max_equity_weight=0.80,
            max_portfolio_risk=0.50,
        )
        backtester = SwingBacktester(
            config, self.trading, slippage_rate=0.01,
        )
        histories = self.histories(72)
        first = list(histories["510300"])
        execution = first[70]
        scale = execution.close / execution.adjusted_close
        gap_open = execution.previous_close * 1.20
        gap_high = max(gap_open, execution.high)
        first[70] = replace(
            execution,
            open=gap_open,
            high=gap_high,
            adjusted_open=gap_open / scale,
            adjusted_high=gap_high / scale,
        )
        histories["510300"] = tuple(first)
        refreshed_market_values: list[tuple[str, date, float]] = []

        def evaluate(signal_bars, _config, context, **_kwargs):
            symbol = signal_bars[-1].symbol
            if context.position is None and len(signal_bars) == 70:
                refreshed_market_values.append(
                    (
                        symbol, context.next_trading_date,
                        context.current_etf_market_value,
                    ),
                )
                return replace(
                    _decision(
                        SwingState.TRIAL_ENTRY_CANDIDATE,
                        signal_bars[-1].trading_date,
                        context.next_trading_date,
                        shares=1_000_000,
                        stop=0.01,
                    ),
                    symbol=symbol,
                    trend_score=2.0 if symbol == "510300" else 1.0,
                )
            return replace(
                _decision(
                    SwingState.HOLDING if context.position else SwingState.UPTREND_WATCH,
                    signal_bars[-1].trading_date,
                    context.next_trading_date,
                    shares=0,
                    stop=None,
                ),
                symbol=symbol,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = backtester.run_portfolio(histories, 100_000.0)
        buys = [item for item in result.trades if item.side == "BUY"]
        self.assertEqual(len(buys), 2)
        first_buy = buys[0]
        second_refresh = [
            value for symbol, execution_date, value in refreshed_market_values
            if symbol == buys[1].symbol
            and execution_date == buys[1].execution_date
            and value > 0.0
        ][-1]
        self.assertAlmostEqual(
            second_refresh,
            histories[first_buy.symbol][71].open * first_buy.shares,
        )
        shared_cash = 100_000.0 - sum(
            item.fill_price * item.shares + item.fee for item in buys
        )
        current_open_value = sum(
            item.raw_reference_price * item.shares for item in buys
        )
        actual_equity = shared_cash + current_open_value
        self.assertLessEqual(
            current_open_value / actual_equity,
            config.max_equity_weight + 1e-12,
        )

    def test_shared_cash_never_borrows_other_position_value_for_sell_fee(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")

        def seeded(cash: float) -> tuple[BacktestAccount, BacktestAccount]:
            selling = BacktestAccount(
                max(cash, 0.01), self.trading, self.config,
                buy_fee_rate=0.0, sell_fee_rate=0.0,
                minimum_fee=5.0, slippage_rate=0.0,
                half_spread_ticks=0.0,
            )
            selling.cash = cash
            selling.shares = 100
            selling._lots = [(0, 100)]
            selling._entry_date = bars[0].trading_date
            selling._entry_index = 0
            selling._last_mark_price = 0.008
            selling._last_adjusted_close = 0.008
            other = BacktestAccount(0.01, self.trading, self.config)
            other.shares = 100_000
            other._last_mark_price = 100.0
            other._last_adjusted_close = 100.0
            return selling, other

        bar = replace(
            bars[2], open=0.008, high=0.008, low=0.008, close=0.008,
            previous_close=0.008, adjusted_open=0.008,
            adjusted_high=0.008, adjusted_low=0.008, adjusted_close=0.008,
        )
        decision = _decision(
            SwingState.EXIT_CANDIDATE,
            bars[1].trading_date,
            bars[2].trading_date,
            shares=100,
            stop=None,
        )

        blocked, other = seeded(0.0)
        shared, fill = self.backtester._execute_with_shared_cash(
            blocked, {"510300": blocked, "510500": other}, 0.0,
            lambda: blocked.execute(decision, bar, execution_index=2),
        )
        self.assertIsNone(fill)
        self.assertEqual(shared, 0.0)
        self.assertEqual(blocked.shares, 100)
        self.assertEqual(
            blocked.rejections[-1].reason,
            "INSUFFICIENT_CASH_FOR_SELL_FEE",
        )

        allowed, other = seeded(10.0)
        shared, fill = self.backtester._execute_with_shared_cash(
            allowed, {"510300": allowed, "510500": other}, 10.0,
            lambda: allowed.execute(decision, bar, execution_index=2),
        )
        self.assertIsNotNone(fill)
        self.assertAlmostEqual(shared, 5.8)
        self.assertEqual(allowed.shares, 0)

    def test_buy_uses_portfolio_equity_for_limits_but_shared_cash_for_payment(self) -> None:
        config = replace(
            self.config,
            risk_per_trade=0.01,
            max_symbol_weight=0.40,
            max_equity_weight=0.90,
            max_portfolio_risk=0.02,
        )
        account = BacktestAccount(
            44_000.0, self.trading, config,
            buy_fee_rate=0.0, sell_fee_rate=0.0, minimum_fee=0.0,
            slippage_rate=0.0, half_spread_ticks=0.0,
        )
        bars = swing_strategy_bars(2, pattern="rising")
        bar = replace(
            bars[1], open=4.0, high=4.1, low=3.9, close=4.0,
            previous_close=4.0, adjusted_open=4.0, adjusted_high=4.1,
            adjusted_low=3.9, adjusted_close=4.0,
        )
        fill = account.execute(
            _decision(
                SwingState.TRIAL_ENTRY_CANDIDATE,
                bars[0].trading_date,
                bars[1].trading_date,
                shares=7_000,
                stop=3.9,
                entry_low=3.9,
                entry_high=4.1,
            ),
            bar,
            execution_index=1,
            portfolio_equity_override=100_000.0,
            portfolio_market_value_override=56_000.0,
            portfolio_risk_override=0.0,
        )
        self.assertIsNotNone(fill)
        self.assertEqual(fill.shares, 7_000)
        self.assertEqual(account.cash, 16_000.0)

    def test_three_same_day_buys_keep_portfolio_equity_basis(self) -> None:
        config = replace(
            self.config,
            risk_per_trade=0.01,
            max_symbol_weight=0.40,
            max_equity_weight=0.90,
            max_portfolio_risk=0.02,
        )
        backtester = SwingBacktester(
            config, self.trading,
            buy_fee_rate=0.0, sell_fee_rate=0.0, minimum_fee=0.0,
            slippage_rate=0.0, half_spread_ticks=0.0,
        )
        histories = {
            symbol: swing_strategy_bars(
                72, symbol=symbol, pattern="pullback_reclaim", raw_scale=0.04,
            )
            for symbol in ("510300", "510500", "159915")
        }

        def evaluate(signal_bars, _config, context, **_kwargs):
            symbol = signal_bars[-1].symbol
            state = (
                SwingState.TRIAL_ENTRY_CANDIDATE
                if context.position is None and len(signal_bars) == 70
                else SwingState.HOLDING
                if context.position is not None
                else SwingState.UPTREND_WATCH
            )
            return replace(
                _decision(
                    state,
                    signal_bars[-1].trading_date,
                    context.next_trading_date,
                    shares=7_000,
                    stop=4.2,
                ),
                symbol=symbol,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = backtester.run_portfolio(histories, 100_000.0)

        buys = [trade for trade in result.trades if trade.side == "BUY"]
        self.assertEqual(len(buys), 3)
        self.assertEqual([trade.shares for trade in buys], [7_000] * 3)
        self.assertLessEqual(result.max_equity_weight, 0.90 + 1e-12)
        entry_risks = [
            trade.shares * (trade.fill_price - trade.planned_stop)
            for trade in buys
        ]
        self.assertTrue(all(risk <= 1_000.0 + 1e-9 for risk in entry_risks))
        self.assertLessEqual(sum(entry_risks), 2_000.0 + 1e-9)

    def test_common_range_and_equal_weight_baseline_use_actual_shared_cash(self) -> None:
        histories = self.histories(95)
        histories["510500"] = histories["510500"][5:]
        result = self.backtester.run_portfolio(histories, 100_000.0)
        expected = histories["510500"][self.config.minimum_daily_bars].trading_date
        self.assertEqual(result.common_start_date, expected)
        if result.baseline is not None:
            self.assertAlmostEqual(sum(result.baseline_weights.values()), 1.0)
            self.assertGreaterEqual(result.baseline.cash, -1e-9)
            self.assertLessEqual(
                sum(fill.shares * fill.fill_price + fill.fee for fill in result.baseline.trades),
                100_000.0 + 1e-9,
            )

    def test_walk_forward_reports_all_81_variants_and_exact_untouched_windows(self) -> None:
        histories = self.histories(650)
        report = self.backtester.walk_forward(histories, 100_000.0)
        self.assertEqual(len(report.variants), 81)
        self.assertEqual(
            [
                (
                    item.parameters["short_ma_days"],
                    item.parameters["long_ma_days"],
                    item.parameters["initial_stop_atr"],
                    item.parameters["trailing_stop_atr"],
                )
                for item in report.variants
            ],
            sorted(
                (
                    short, long, initial, trailing,
                )
                for short in (18, 20, 22)
                for long in (55, 60, 65)
                for initial in (1.75, 2.0, 2.25)
                for trailing in (2.75, 3.0, 3.25)
            ),
        )
        first = report.variants[0].folds[0]
        self.assertEqual(first.train_bar_count, 504)
        self.assertEqual(first.test_bar_count, 126)
        self.assertLess(first.train_end_date, first.test_start_date)
        self.assertEqual(report.selected_variant, None)

    def test_walk_forward_reuses_equivalent_variant_decisions(self) -> None:
        config = replace(
            self.config,
            walk_forward_train_days=80,
            walk_forward_test_days=3,
            walk_forward_step_days=3,
        )
        backtester = SwingBacktester(config, self.trading)
        histories = self.histories(83)
        with patch(
            "etf_rotation.swing_backtest.evaluate_swing",
            wraps=evaluate_swing,
        ) as evaluate, patch(
            "etf_rotation.swing_strategy._compute_metrics",
            wraps=swing_strategy_module._compute_metrics,
        ) as compute_metrics:
            report = backtester.walk_forward(histories, 100_000.0)
        self.assertEqual(len(report.variants), 81)
        self.assertLess(evaluate.call_count, 1_000)
        self.assertLess(compute_metrics.call_count, 700)


class SingleSymbolSwingBacktestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_strategy(ROOT / "data" / "swing" / "strategy.json")
        self.trading = TradingMetadata(
            exchange="SSE",
            asset_type="DOMESTIC_EQUITY_ETF",
            intraday_turnaround=False,
            sellable_delay_days=1,
            lot_size=100,
            price_tick=0.001,
            price_limit_pct=0.20,
            volume_unit_shares=100,
        )
        self.backtester = SwingBacktester(self.config, self.trading)

    def test_signal_executes_at_next_raw_open_without_lookahead(self) -> None:
        bars = swing_strategy_bars(72, pattern="pullback_reclaim", raw_scale=0.0472)
        first_execution = bars[self.config.minimum_daily_bars]
        seen_lengths: list[int] = []
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            seen_lengths.append(len(signal_bars))
            if calls == 1:
                return _decision(
                    SwingState.TRIAL_ENTRY_CANDIDATE,
                    signal_bars[-1].trading_date,
                    first_execution.trading_date,
                    shares=100, stop=4.0,
                )
            return _decision(
                SwingState.HOLDING,
                signal_bars[-1].trading_date,
                first_execution.trading_date,
                shares=0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = self.backtester.run_symbol(bars, 100_000.0)

        trade = result.trades[0]
        self.assertEqual(trade.signal_date, bars[69].trading_date)
        self.assertEqual(trade.execution_date, bars[70].trading_date)
        self.assertEqual(trade.raw_reference_price, bars[70].open)
        self.assertGreater(trade.fill_price, trade.raw_reference_price)
        self.assertEqual(seen_lengths, [70, 70])
        self.assertEqual(len(bars), 72)

    def test_gap_below_stop_uses_executable_open_not_ideal_stop(self) -> None:
        bars = list(swing_strategy_bars(73, pattern="rising"))
        bars[71] = replace(
            bars[71], open=95.0, high=101.0, low=94.0, close=100.0,
            previous_close=bars[70].close,
            amount=100.0 * bars[71].volume * self.trading.volume_unit_shares,
            adjusted_open=95.0, adjusted_high=101.0,
            adjusted_low=94.0, adjusted_close=100.0,
        )
        bars[72] = replace(bars[72], previous_close=100.0)
        states = [SwingState.TRIAL_ENTRY_CANDIDATE, SwingState.ADD_CANDIDATE]
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            state = states.pop(0) if states else SwingState.TREND_BLOCKED
            return _decision(
                state, signal_bars[-1].trading_date,
                bars[69 + calls].trading_date,
                shares=100, stop=100.0 if calls == 1 else 99.0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = self.backtester.run_symbol(tuple(bars), 100_000.0)
        exit_trade = next(trade for trade in result.trades if trade.side == "SELL")
        self.assertEqual([trade.side for trade in result.trades], ["BUY", "SELL"])
        self.assertLess(exit_trade.fill_price, exit_trade.planned_stop)
        self.assertEqual(exit_trade.reason, "GAP_THROUGH_STOP")

    def test_open_equal_to_stop_executes_as_stop_not_gap(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")
        account = BacktestAccount(100_000.0, self.trading, self.config)
        account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=99.0),
            bars[1], execution_index=1,
        )
        account.mark(bars[1], 1)
        at_stop = replace(
            bars[2], open=99.0, high=100.0, low=98.0, close=99.5,
            adjusted_open=99.0, adjusted_high=100.0,
            adjusted_low=98.0, adjusted_close=99.5,
        )
        self.assertTrue(account.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[1].trading_date,
                      bars[2].trading_date, stop=99.0),
            at_stop, execution_index=2,
        ))
        fill = account.trades[-1]
        self.assertEqual(fill.raw_reference_price, 99.0)
        self.assertLess(fill.fill_price, fill.raw_reference_price)
        self.assertEqual(fill.reason, "STOP_EXIT")
        self.assertEqual(account.context().last_stop_trading_date,
                         bars[2].trading_date)

    def test_intraday_stop_touch_executes_at_stop_not_better_open(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")
        account = BacktestAccount(100_000.0, self.trading, self.config)
        account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=99.0),
            bars[1], execution_index=1,
        )
        account.mark(bars[1], 1)
        touched = replace(
            bars[2], open=105.0, high=106.0, low=98.0, close=104.0,
            adjusted_open=105.0, adjusted_high=106.0,
            adjusted_low=98.0, adjusted_close=104.0,
        )

        self.assertTrue(account.execute_protective_stop(
            _decision(SwingState.HOLDING, bars[1].trading_date,
                      bars[2].trading_date, stop=99.0),
            touched, execution_index=2,
        ))

        fill = account.trades[-1]
        self.assertEqual(fill.raw_reference_price, 99.0)
        self.assertLess(fill.fill_price, 99.0)
        self.assertEqual(fill.reason, "STOP_EXIT")
        self.assertGreater(fill.spread_cost, 0.0)
        self.assertGreater(fill.slippage, 0.0)

    def test_entry_gap_below_stop_and_above_zone_are_rejected(self) -> None:
        bars = swing_strategy_bars(2, pattern="rising")
        invalidated = replace(
            bars[1], open=89.0, high=91.0, low=88.0, close=90.0,
            adjusted_open=89.0, adjusted_high=91.0,
            adjusted_low=88.0, adjusted_close=90.0,
        )
        account = BacktestAccount(100_000.0, self.trading, self.config)
        self.assertIsNone(account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=90.0),
            invalidated, execution_index=1,
        ))
        self.assertEqual(
            account.rejections[-1].reason, "ENTRY_INVALIDATED_BY_GAP",
        )
        self.assertEqual(account.shares, 0)

        chase = BacktestAccount(100_000.0, self.trading, self.config)
        self.assertIsNone(chase.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=90.0,
                      entry_low=95.0, entry_high=99.0),
            bars[1], execution_index=1,
        ))
        self.assertEqual(chase.rejections[-1].reason, "ENTRY_GAP_ABOVE_ZONE")
        self.assertEqual(chase.shares, 0)

    def test_sell_fee_may_use_cash_but_never_make_account_negative(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")
        account = BacktestAccount(
            100_000.0, self.trading, self.config,
            buy_fee_rate=0.0, sell_fee_rate=0.0, minimum_fee=0.0,
        )
        account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100),
            bars[1], execution_index=1,
        )
        account.mark(bars[1], 1)
        account.minimum_fee = 20_000.0

        allowed = account.execute(
            _decision(SwingState.EXIT_CANDIDATE,
                      bars[1].trading_date, bars[2].trading_date,
                      shares=100),
            bars[2], execution_index=2,
        )
        self.assertIsNotNone(allowed)
        self.assertGreater(allowed.fee, allowed.fill_price * allowed.shares)
        self.assertGreaterEqual(account.cash, 0.0)

        blocked = BacktestAccount(
            100_000.0, self.trading, self.config,
            buy_fee_rate=0.0, sell_fee_rate=0.0, minimum_fee=0.0,
        )
        blocked.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100),
            bars[1], execution_index=1,
        )
        blocked.mark(bars[1], 1)
        prior_cash = blocked.cash
        blocked.minimum_fee = 1_000_000.0
        self.assertIsNone(blocked.execute(
            _decision(SwingState.EXIT_CANDIDATE,
                      bars[1].trading_date, bars[2].trading_date,
                      shares=100),
            bars[2], execution_index=2,
        ))
        self.assertEqual(
            blocked.rejections[-1].reason, "INSUFFICIENT_CASH_FOR_SELL_FEE",
        )
        self.assertEqual(blocked.shares, 100)
        self.assertEqual(blocked.cash, prior_cash)
        self.assertGreaterEqual(blocked.cash, 0.0)

    def test_execution_day_phases_add_before_intraday_stop(self) -> None:
        bars = list(swing_strategy_bars(73, pattern="rising"))
        bars[71] = replace(
            bars[71], open=105.0, high=106.0, low=98.0, close=104.0,
            previous_close=bars[70].close,
            amount=104.0 * bars[71].volume * self.trading.volume_unit_shares,
            adjusted_open=105.0, adjusted_high=106.0,
            adjusted_low=98.0, adjusted_close=104.0,
        )
        bars[72] = replace(bars[72], previous_close=104.0)
        config = replace(
            self.config,
            risk_per_trade=0.02,
            max_portfolio_risk=0.02,
            max_symbol_weight=1.0,
            max_equity_weight=1.0,
        )
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            state = {
                1: SwingState.TRIAL_ENTRY_CANDIDATE,
                2: SwingState.ADD_CANDIDATE,
            }.get(calls, SwingState.HOLDING)
            return _decision(
                state,
                signal_bars[-1].trading_date,
                bars[69 + calls].trading_date,
                shares=100,
                stop=99.0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = SwingBacktester(config, self.trading).run_symbol(
                tuple(bars), 100_000.0,
            )

        self.assertEqual(
            [(trade.side, trade.execution_date) for trade in result.trades[:3]],
            [
                ("BUY", bars[70].trading_date),
                ("BUY", bars[71].trading_date),
                ("SELL", bars[71].trading_date),
            ],
        )
        self.assertEqual(result.trades[2].reason, "STOP_EXIT")
        self.assertEqual(result.trades[2].shares, 100)
        self.assertEqual(result.open_position_shares, 100)
        self.assertEqual(result.rejections[-1].reason, "T_PLUS_ONE")

    def test_open_and_intraday_orders_share_prior_day_liquidity_budget(self) -> None:
        bars = list(swing_strategy_bars(72, pattern="rising"))
        bars[70] = replace(
            bars[70], volume=10.0,
            amount=bars[70].close * 10.0 * self.trading.volume_unit_shares,
        )
        bars[71] = replace(
            bars[71], open=105.0, high=106.0, low=98.0, close=104.0,
            previous_close=bars[70].close,
            amount=104.0 * bars[71].volume * self.trading.volume_unit_shares,
            adjusted_open=105.0, adjusted_high=106.0,
            adjusted_low=98.0, adjusted_close=104.0,
        )
        config = replace(
            self.config, risk_per_trade=0.02, max_portfolio_risk=0.02,
            max_symbol_weight=1.0, max_equity_weight=1.0,
        )
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            return _decision(
                SwingState.TRIAL_ENTRY_CANDIDATE
                if calls == 1 else SwingState.ADD_CANDIDATE,
                signal_bars[-1].trading_date,
                bars[69 + calls].trading_date,
                shares=100,
                stop=99.0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = SwingBacktester(config, self.trading).run_symbol(
                tuple(bars), 100_000.0,
            )

        self.assertEqual([trade.side for trade in result.trades], ["BUY", "BUY"])
        self.assertEqual(result.open_position_shares, 200)
        self.assertEqual(result.rejections[-1].reason, "VOLUME_PARTICIPATION")

    def test_pending_gap_stop_retries_unlock_at_adverse_open_price(self) -> None:
        for high in (90.0, 100.0):
            with self.subTest(high=high):
                bars = list(swing_strategy_bars(72, pattern="rising"))
                lower = round(
                    bars[70].close
                    * (1.0 - self.trading.price_limit_pct), 3,
                )
                bars[70] = replace(
                    bars[70], volume=10.0,
                    amount=(
                        bars[70].close * 10.0
                        * self.trading.volume_unit_shares
                    ),
                )
                close = 89.0 if high < 99.0 else 95.0
                bars[71] = replace(
                    bars[71], open=lower, high=high, low=lower, close=close,
                    previous_close=bars[70].close,
                    amount=(
                        close * bars[71].volume
                        * self.trading.volume_unit_shares
                    ),
                    adjusted_open=lower, adjusted_high=high,
                    adjusted_low=lower, adjusted_close=close,
                )
                calls = 0

                def evaluate(signal_bars, config, context):
                    nonlocal calls
                    calls += 1
                    return _decision(
                        SwingState.TRIAL_ENTRY_CANDIDATE
                        if calls == 1 else SwingState.ADD_CANDIDATE,
                        signal_bars[-1].trading_date,
                        bars[69 + calls].trading_date,
                        shares=100,
                        stop=100.0 if calls == 1 else 99.0,
                    )

                with patch(
                    "etf_rotation.swing_backtest.evaluate_swing",
                    side_effect=evaluate,
                ):
                    result = self.backtester.run_symbol(
                        tuple(bars), 100_000.0,
                    )

                self.assertEqual(
                    [trade.side for trade in result.trades], ["BUY", "SELL"],
                )
                self.assertEqual(result.trades[-1].reason, "GAP_THROUGH_STOP")
                self.assertEqual(result.trades[-1].raw_reference_price, lower)
                self.assertEqual(result.open_position_shares, 0)
                self.assertEqual(result.rejections[-1].reason, "LIMIT_LOCKED")

    def test_pending_open_stop_preserves_exact_or_gap_classification(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")
        config = replace(
            self.config, risk_per_trade=0.05, max_portfolio_risk=0.05,
            max_symbol_weight=1.0, max_equity_weight=1.0,
        )
        unlocked = replace(
            bars[2], previous_close=100.0,
            open=80.0, high=90.0, low=80.0, close=85.0,
            adjusted_open=80.0, adjusted_high=90.0,
            adjusted_low=80.0, adjusted_close=85.0,
        )

        for stop, expected_reason in (
            (80.0, "STOP_EXIT"),
            (81.0, "GAP_THROUGH_STOP"),
        ):
            with self.subTest(stop=stop):
                account = BacktestAccount(100_000.0, self.trading, config)
                account.execute(
                    _decision(
                        SwingState.TRIAL_ENTRY_CANDIDATE,
                        bars[0].trading_date,
                        bars[1].trading_date,
                        shares=100,
                        stop=stop,
                    ),
                    bars[1],
                    execution_index=1,
                )
                account.mark(bars[1], 1)
                self.assertTrue(account.execute_protective_stop(
                    _decision(
                        SwingState.HOLDING,
                        bars[1].trading_date,
                        bars[2].trading_date,
                        stop=stop,
                    ),
                    unlocked,
                    execution_index=2,
                ))

                self.assertEqual(account.rejections[-1].reason, "LIMIT_LOCKED")
                self.assertEqual(account.trades[-1].reason, expected_reason)
                self.assertEqual(account.trades[-1].raw_reference_price, 80.0)

    def test_current_physical_volume_only_reduces_shared_capacity(self) -> None:
        bars = list(swing_strategy_bars(72, pattern="rising"))
        bars[71] = replace(
            bars[71], open=105.0, high=106.0, low=98.0, close=104.0,
            previous_close=bars[70].close,
            volume=10.0,
            amount=104.0 * 10.0 * self.trading.volume_unit_shares,
            adjusted_open=105.0, adjusted_high=106.0,
            adjusted_low=98.0, adjusted_close=104.0,
        )
        config = replace(
            self.config, risk_per_trade=0.02, max_portfolio_risk=0.02,
            max_symbol_weight=1.0, max_equity_weight=1.0,
        )
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            return _decision(
                SwingState.TRIAL_ENTRY_CANDIDATE
                if calls == 1 else SwingState.ADD_CANDIDATE,
                signal_bars[-1].trading_date,
                bars[69 + calls].trading_date,
                shares=100,
                stop=99.0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = SwingBacktester(config, self.trading).run_symbol(
                tuple(bars), 100_000.0,
            )

        execution_date = bars[71].trading_date
        day_fills = tuple(
            trade for trade in result.trades
            if trade.execution_date == execution_date
        )
        self.assertEqual(sum(trade.shares for trade in day_fills), 100)
        self.assertEqual(
            self.trading.volume_unit_shares * bars[71].volume, 1_000,
        )
        self.assertEqual(result.rejections[-1].reason, "VOLUME_PARTICIPATION")

        benchmark_bars = list(swing_strategy_bars(72, pattern="falling_ma60"))
        benchmark_bars[70] = replace(
            benchmark_bars[70], volume=10.0,
            amount=(
                benchmark_bars[70].close
                * 10.0 * self.trading.volume_unit_shares
            ),
        )
        benchmark = self.backtester.run_symbol(
            tuple(benchmark_bars), 100_000.0,
        ).benchmark
        self.assertEqual(benchmark.start_date, benchmark_bars[70].trading_date)
        self.assertEqual(benchmark.shares, 100)

        ten_share_lot = replace(self.trading, lot_size=10)
        ten_share_account = BacktestAccount(
            100_000.0, ten_share_lot, self.config,
        )
        self.assertEqual(
            ten_share_account.execution_day_liquidity(
                benchmark_bars[70], prior_completed_volume=5.0,
            ).capacity,
            50,
        )

    def test_same_day_new_entry_stop_obeys_turnaround_metadata(self) -> None:
        bars = list(swing_strategy_bars(71, pattern="rising"))
        bars[70] = replace(
            bars[70], open=105.0, high=106.0, low=98.0, close=104.0,
            previous_close=bars[69].close,
            amount=104.0 * bars[70].volume * self.trading.volume_unit_shares,
            adjusted_open=105.0, adjusted_high=106.0,
            adjusted_low=98.0, adjusted_close=104.0,
        )
        config = replace(
            self.config,
            risk_per_trade=0.02,
            max_portfolio_risk=0.02,
            max_symbol_weight=1.0,
            max_equity_weight=1.0,
        )

        def run(trading):
            def evaluate(signal_bars, config, context):
                return _decision(
                    SwingState.TRIAL_ENTRY_CANDIDATE,
                    signal_bars[-1].trading_date,
                    bars[70].trading_date,
                    shares=100,
                    stop=99.0,
                )

            with patch(
                "etf_rotation.swing_backtest.evaluate_swing",
                side_effect=evaluate,
            ):
                return SwingBacktester(config, trading).run_symbol(
                    tuple(bars), 100_000.0,
                )

        t_plus_one = run(self.trading)
        self.assertEqual([trade.side for trade in t_plus_one.trades], ["BUY"])
        self.assertEqual(t_plus_one.open_position_shares, 100)
        self.assertEqual(t_plus_one.rejections[-1].reason, "T_PLUS_ONE")

        turnaround = replace(
            self.trading, intraday_turnaround=True, sellable_delay_days=0,
        )
        same_day = run(turnaround)
        self.assertEqual(
            [trade.side for trade in same_day.trades], ["BUY", "SELL"],
        )
        self.assertEqual(same_day.trades[-1].reason, "STOP_EXIT")
        self.assertEqual(same_day.open_position_shares, 0)

    def test_actual_open_risk_caps_gap_up_add_quantity(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")
        config = replace(
            self.config,
            risk_per_trade=0.02,
            max_portfolio_risk=0.02,
            max_symbol_weight=1.0,
            max_equity_weight=1.0,
        )
        account = BacktestAccount(100_000.0, self.trading, config)
        account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=99.0),
            bars[1], execution_index=1,
        )
        account.mark(bars[1], 1)
        gap_up = replace(
            bars[2], open=110.0, high=111.0, low=109.0, close=110.5,
            adjusted_open=110.0, adjusted_high=111.0,
            adjusted_low=109.0, adjusted_close=110.5,
        )
        fill = account.execute(
            _decision(SwingState.ADD_CANDIDATE,
                      bars[1].trading_date, bars[2].trading_date,
                      shares=1_000, stop=104.0),
            gap_up, execution_index=2,
        )

        self.assertIsNotNone(fill)
        self.assertLess(fill.shares, 1_000)
        self.assertEqual(account.rejections[-1].reason, "ACTUAL_RISK_LIMIT")
        equity_at_open = account.cash + account.shares * gap_up.open
        actual_risk = (
            100 * (gap_up.open - 104.0)
            + fill.shares * (fill.fill_price - 104.0)
        )
        self.assertLessEqual(
            actual_risk / equity_at_open,
            config.risk_per_trade + 1e-12,
        )

    def test_next_open_fill_does_not_use_execution_day_high(self) -> None:
        bars = swing_strategy_bars(2, pattern="rising")
        narrow = replace(
            bars[1], open=100.0, high=100.0, low=99.5, close=100.0,
            adjusted_open=100.0, adjusted_high=100.0,
            adjusted_low=99.5, adjusted_close=100.0,
        )
        wide = replace(narrow, high=110.0, adjusted_high=110.0)

        def fill(bar):
            account = BacktestAccount(100_000.0, self.trading, self.config)
            return account.execute(
                _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                          bars[0].trading_date, bars[1].trading_date,
                          shares=100, stop=99.0),
                bar,
                execution_index=1,
                known_volume=bars[0].volume,
            )

        self.assertEqual(fill(narrow).fill_price, fill(wide).fill_price)

    def test_protective_gap_respects_t_plus_one_volume_and_limit_lock(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")

        def opened_account():
            account = BacktestAccount(100_000.0, self.trading, self.config)
            account.execute(
                _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                          bars[0].trading_date, bars[1].trading_date,
                          shares=100, stop=99.0),
                bars[1], execution_index=1,
            )
            account.mark(bars[1], 1)
            return account

        same_day = opened_account()
        self.assertTrue(same_day.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[0].trading_date,
                      bars[1].trading_date, stop=108.0),
            bars[1], execution_index=1,
        ))
        self.assertEqual(same_day.rejections[-1].reason, "T_PLUS_ONE")
        self.assertEqual(same_day.shares, 100)

        no_volume = opened_account()
        gap = replace(
            bars[2], open=90.0, high=100.0, low=89.0, close=95.0,
            volume=0.0, amount=0.0, adjusted_open=90.0,
            adjusted_high=100.0, adjusted_low=89.0, adjusted_close=95.0,
        )
        self.assertTrue(no_volume.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[1].trading_date,
                      bars[2].trading_date, stop=99.0),
            gap, execution_index=2,
        ))
        self.assertEqual(
            no_volume.rejections[-1].reason, "SUSPENDED_OR_ZERO_VOLUME",
        )
        self.assertEqual(no_volume.shares, 100)

        locked_account = opened_account()
        lower = 100.0 * (1.0 - self.trading.price_limit_pct)
        locked = replace(
            bars[2], previous_close=100.0, open=lower, high=lower,
            low=lower, close=lower, adjusted_open=lower,
            adjusted_high=lower, adjusted_low=lower, adjusted_close=lower,
        )
        self.assertTrue(locked_account.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[1].trading_date,
                      bars[2].trading_date, stop=90.0),
            locked, execution_index=2,
        ))
        self.assertEqual(locked_account.rejections[-1].reason, "LIMIT_LOCKED")
        self.assertEqual(locked_account.shares, 100)

    def test_stop_exit_classification_controls_cooldown(self) -> None:
        bars = swing_strategy_bars(4, pattern="rising")

        def closed_account(exit_evidence):
            account = BacktestAccount(100_000.0, self.trading, self.config)
            account.execute(
                _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                          bars[0].trading_date, bars[1].trading_date,
                          shares=100, stop=99.0),
                bars[1], execution_index=1,
            )
            account.mark(bars[1], 1)
            fill = account.execute(
                _decision(SwingState.EXIT_CANDIDATE,
                          bars[1].trading_date, bars[2].trading_date,
                          shares=100, stop=90.0, evidence=exit_evidence),
                bars[2], execution_index=2,
            )
            return account, fill

        stop_account, stop_fill = closed_account({
            "exit_any": True, "exit_hard_stop": True,
            "exit_trailing_stop": False,
        })
        self.assertEqual(stop_fill.reason, "STOP_EXIT")
        self.assertEqual(stop_account.context().last_stop_trading_date,
                         bars[2].trading_date)

        signal_account, signal_fill = closed_account({
            "exit_any": True, "exit_hard_stop": False,
            "exit_trailing_stop": False,
            "exit_close_below_ma60": True,
        })
        self.assertEqual(signal_fill.reason, "EXIT_SIGNAL")
        self.assertIsNone(signal_account.context().last_stop_trading_date)

    def test_partial_stop_does_not_precommit_future_cooldown(self) -> None:
        bars = swing_strategy_bars(4, pattern="rising")

        def opened_account():
            account = BacktestAccount(100_000.0, self.trading, self.config)
            account.execute(
                _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                          bars[0].trading_date, bars[1].trading_date,
                          shares=300, stop=99.0),
                bars[1], execution_index=1,
            )
            account.mark(bars[1], 1)
            return account

        partial_bar = replace(
            bars[2], open=95.0, high=100.0, low=94.0, close=98.0,
            volume=10.0, adjusted_open=95.0, adjusted_high=100.0,
            adjusted_low=94.0, adjusted_close=98.0,
        )
        ordinary = opened_account()
        ordinary.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[1].trading_date,
                      bars[2].trading_date, stop=99.0),
            partial_bar, execution_index=2,
        )
        self.assertEqual(ordinary.shares, 200)
        self.assertIsNone(ordinary.context().last_stop_trading_date)
        final_signal = ordinary.execute(
            _decision(SwingState.EXIT_CANDIDATE,
                      bars[2].trading_date, bars[3].trading_date,
                      shares=200, stop=90.0,
                      evidence={"exit_close_below_ma60": True}),
            bars[3], execution_index=3,
        )
        self.assertEqual(final_signal.reason, "EXIT_SIGNAL")
        self.assertEqual(ordinary.shares, 0)
        self.assertIsNone(ordinary.context().last_stop_trading_date)

        final_stop = opened_account()
        final_stop.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[1].trading_date,
                      bars[2].trading_date, stop=99.0),
            partial_bar, execution_index=2,
        )
        last_bar = replace(
            bars[3], open=95.0, high=100.0, low=94.0, close=98.0,
            adjusted_open=95.0, adjusted_high=100.0,
            adjusted_low=94.0, adjusted_close=98.0,
        )
        final_stop.execute_protective_gap(
            _decision(SwingState.HOLDING, bars[2].trading_date,
                      bars[3].trading_date, stop=99.0),
            last_bar, execution_index=3,
        )
        self.assertEqual(final_stop.shares, 0)
        self.assertEqual(final_stop.context().last_stop_trading_date,
                         bars[3].trading_date)

    def test_no_completed_trade_reports_insufficient_sample(self) -> None:
        result = self.backtester.run_symbol(
            swing_strategy_bars(72, pattern="falling_ma60"), 100_000.0,
        )
        self.assertEqual(result.status, "INSUFFICIENT_SAMPLE")
        self.assertIsNone(result.outperformance)
        self.assertIsNone(result.metrics.win_rate)
        self.assertEqual(result.completed_round_trips, 0)
        self.assertLess(result.benchmark.cumulative_return, 0.0)

    def test_benchmark_retries_until_first_actually_executable_open(self) -> None:
        bars = list(swing_strategy_bars(73, pattern="falling_ma60"))
        bars[69] = replace(bars[69], volume=0.0, amount=0.0)
        result = self.backtester.run_symbol(tuple(bars), 100_000.0)
        self.assertIsNotNone(result.benchmark)
        self.assertEqual(result.benchmark.start_date, bars[71].trading_date)

        unavailable = tuple(
            replace(bar, volume=0.0, amount=0.0)
            if index >= 69 else bar
            for index, bar in enumerate(bars)
        )
        missing = self.backtester.run_symbol(unavailable, 100_000.0)
        self.assertIsNone(missing.benchmark)
        self.assertIsNone(missing.outperformance)
        self.assertEqual(missing.status, "INSUFFICIENT_SAMPLE")

    def test_fees_slippage_lots_volume_cash_limits_and_t_plus_one(self) -> None:
        execution_config = replace(
            self.config, max_symbol_weight=1.0, max_equity_weight=1.0,
        )
        account = BacktestAccount(
            20_000.0, self.trading, execution_config,
            buy_fee_rate=0.001, sell_fee_rate=0.001,
            minimum_fee=5.0, slippage_rate=0.001,
        )
        bars = swing_strategy_bars(3, pattern="rising")
        buy = account.execute(
            _decision(
                SwingState.TRIAL_ENTRY_CANDIDATE,
                bars[0].trading_date, bars[1].trading_date, shares=155,
            ),
            bars[1], execution_index=1,
        )
        self.assertIsNotNone(buy)
        self.assertEqual(buy.shares, 100)
        self.assertGreaterEqual(buy.fee, 5.0)
        self.assertGreater(buy.slippage, 0.0)
        same_day_sell = account.execute(
            _decision(
                SwingState.EXIT_CANDIDATE,
                bars[0].trading_date, bars[1].trading_date, shares=100,
            ),
            bars[1], execution_index=1,
        )
        self.assertIsNone(same_day_sell)
        self.assertEqual(account.rejections[-1].reason, "T_PLUS_ONE")
        next_day_sell = account.execute(
            _decision(
                SwingState.EXIT_CANDIDATE,
                bars[1].trading_date, bars[2].trading_date, shares=100,
            ),
            bars[2], execution_index=2,
        )
        self.assertIsNotNone(next_day_sell)
        self.assertLess(next_day_sell.fill_price, next_day_sell.raw_reference_price)

        turnaround = replace(
            self.trading, intraday_turnaround=True, sellable_delay_days=0,
        )
        same_day_account = BacktestAccount(
            20_000.0, turnaround, execution_config,
        )
        same_day_account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=99.0),
            bars[1], execution_index=1,
        )
        allowed = same_day_account.execute(
            _decision(SwingState.EXIT_CANDIDATE,
                      bars[0].trading_date, bars[1].trading_date,
                      shares=100, stop=99.0),
            bars[1], execution_index=1,
        )
        self.assertIsNotNone(allowed)

    def test_zero_volume_participation_and_locked_price_limit_are_rejected(self) -> None:
        bars = swing_strategy_bars(3, pattern="rising")
        account = BacktestAccount(100_000.0, self.trading, self.config)
        zero = replace(bars[1], volume=0.0, amount=0.0)
        self.assertIsNone(account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE, bars[0].trading_date,
                      zero.trading_date, shares=100),
            zero, execution_index=1,
        ))
        self.assertEqual(
            account.rejections[-1].reason, "SUSPENDED_OR_ZERO_VOLUME",
        )
        thin_account = BacktestAccount(100_000.0, self.trading, self.config)
        thin = replace(bars[1], volume=10.0, amount=bars[1].close * 1_000.0)
        partial = thin_account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                      bars[0].trading_date, thin.trading_date,
                      shares=300, stop=99.0),
            thin, execution_index=1,
        )
        self.assertEqual(partial.shares, 100)
        self.assertEqual(thin_account.rejections[-1].reason,
                         "VOLUME_PARTICIPATION")
        upper = bars[0].close * (1.0 + self.trading.price_limit_pct)
        locked = replace(
            bars[1], open=upper, high=upper, low=upper, close=upper,
            previous_close=bars[0].close, adjusted_open=upper,
            adjusted_high=upper, adjusted_low=upper, adjusted_close=upper,
        )
        self.assertIsNone(account.execute(
            _decision(SwingState.TRIAL_ENTRY_CANDIDATE, bars[0].trading_date,
                      locked.trading_date, shares=100),
            locked, execution_index=1,
        ))
        self.assertEqual(account.rejections[-1].reason, "LIMIT_LOCKED")

    def test_current_zero_volume_suspends_strategy_and_benchmark(self) -> None:
        bars = list(swing_strategy_bars(73, pattern="rising"))
        bars[70] = replace(bars[70], volume=0.0, amount=0.0)
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            return _decision(
                SwingState.TRIAL_ENTRY_CANDIDATE
                if calls == 1 else SwingState.TREND_BLOCKED,
                signal_bars[-1].trading_date,
                bars[69 + calls].trading_date,
                shares=100,
                stop=100.0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = self.backtester.run_symbol(tuple(bars), 100_000.0)

        self.assertEqual(result.trades, ())
        self.assertEqual(
            result.rejections[0].reason, "SUSPENDED_OR_ZERO_VOLUME",
        )
        self.assertEqual(result.benchmark.start_date, bars[72].trading_date)

    def test_price_limit_uses_half_up_tick_rounding_and_exact_boundary(self) -> None:
        trading = replace(
            self.trading, price_tick=0.01, price_limit_pct=0.10,
        )
        account = BacktestAccount(100_000.0, trading, self.config)
        prior = swing_strategy_bars(1, pattern="rising")[0]
        account.mark(replace(
            prior, close=100.05, adjusted_close=100.05,
        ), 0)
        template = swing_strategy_bars(2, pattern="rising")[1]

        def flat(price):
            return replace(
                template, previous_close=100.05,
                open=price, high=price, low=price, close=price,
                adjusted_open=price, adjusted_high=price,
                adjusted_low=price, adjusted_close=price,
            )

        self.assertTrue(account._open_limit_blocked(flat(110.06), "BUY"))
        self.assertFalse(account._open_limit_blocked(flat(110.05), "BUY"))
        self.assertTrue(account._open_limit_blocked(flat(90.05), "SELL"))
        self.assertFalse(account._open_limit_blocked(flat(90.06), "SELL"))
        self.assertTrue(account._limit_locked(flat(110.06), "BUY"))
        unlocked = replace(flat(110.06), low=110.05, adjusted_low=110.05)
        self.assertFalse(account._limit_locked(unlocked, "BUY"))

    def test_cash_and_strategy_risk_blocks_are_counted(self) -> None:
        bars = swing_strategy_bars(73, pattern="rising")
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _decision(
                    SwingState.TRIAL_ENTRY_CANDIDATE,
                    signal_bars[-1].trading_date,
                    bars[69 + calls].trading_date,
                    shares=100,
                )
            return SwingDecision(
                symbol="510300",
                strategy_version="SWING_V1",
                as_of_trading_date=signal_bars[-1].trading_date,
                state=SwingState.PULLBACK_WATCH,
                trend_score=0.0,
                evidence={"trial_technical_ok": True},
                blocked_reasons=("portfolio_risk_cap",),
                planned_entry_low=95.0,
                planned_entry_high=105.0,
                planned_stop=90.0,
                planned_shares=0,
                planned_risk_rate=0.0,
                first_reduce_price=120.0,
                valid_for_trading_date=None,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = self.backtester.run_symbol(bars, 5_000.0)
        self.assertEqual(result.metrics.rejection_counts["CASH"], 1)
        self.assertEqual(result.metrics.rejection_counts["RISK"], 2)

    def test_open_position_is_marked_without_fabricating_round_trip(self) -> None:
        bars = swing_strategy_bars(72, pattern="rising")
        calls = 0

        def evaluate(signal_bars, config, context):
            nonlocal calls
            calls += 1
            if calls == 1:
                return _decision(SwingState.TRIAL_ENTRY_CANDIDATE,
                                 signal_bars[-1].trading_date,
                                 bars[70].trading_date, shares=100, stop=100.0)
            return _decision(SwingState.HOLDING, signal_bars[-1].trading_date,
                             bars[71].trading_date, shares=0)

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
            result = self.backtester.run_symbol(bars, 100_000.0)
        self.assertEqual(result.completed_round_trips, 0)
        self.assertEqual(result.open_position_shares, 100)
        self.assertEqual(result.uncompleted_leg_count, 1)
        self.assertGreater(result.ending_equity, result.cash)
        self.assertEqual(result.status, "INSUFFICIENT_SAMPLE")

    def test_metrics_benchmark_and_json_are_deterministic(self) -> None:
        bars = swing_strategy_bars(74, pattern="rising")

        def run_once():
            calls = 0

            def evaluate(signal_bars, config, context):
                nonlocal calls
                calls += 1
                state = {
                    1: SwingState.TRIAL_ENTRY_CANDIDATE,
                    2: SwingState.EXIT_CANDIDATE,
                }.get(calls, SwingState.TREND_BLOCKED)
                return _decision(state, signal_bars[-1].trading_date,
                                 bars[69 + calls].trading_date,
                                 shares=100, stop=105.0)

            with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=evaluate):
                return self.backtester.run_symbol(bars, 100_000.0)

        first = run_once()
        second = run_once()
        self.assertEqual(first.to_json(), second.to_json())
        json.loads(first.to_json())
        self.assertEqual(first.status, "OK")
        self.assertIsNotNone(first.outperformance)
        self.assertEqual(first.completed_round_trips, 1)
        self.assertIsNotNone(first.metrics.cumulative_return)
        self.assertIsNotNone(first.metrics.annualized_return)
        self.assertIsNotNone(first.metrics.maximum_drawdown)
        self.assertIsNotNone(first.metrics.win_rate)
        self.assertIsNotNone(first.metrics.average_holding_days)
        self.assertGreaterEqual(first.benchmark.ending_equity, 0.0)
        self.assertAlmostEqual(
            first.metrics.cumulative_return,
            first.ending_equity / first.initial_cash - 1.0,
        )
        self.assertAlmostEqual(
            first.metrics.fees, sum(trade.fee for trade in first.trades),
        )
        self.assertAlmostEqual(
            first.metrics.slippage,
            sum(trade.slippage for trade in first.trades),
        )
        self.assertGreaterEqual(first.metrics.maximum_drawdown, 0.0)
        self.assertGreaterEqual(first.metrics.utilization, 0.0)
        self.assertLessEqual(first.metrics.utilization, 1.0)
        pnl = first.round_trips[0].net_pnl
        if pnl < 0.0:
            self.assertEqual(first.metrics.win_rate, 0.0)
            self.assertEqual(first.metrics.average_loss, pnl)
            self.assertIsNone(first.metrics.average_profit)
            self.assertIsNone(first.metrics.payoff_ratio)
            self.assertEqual(first.metrics.longest_losing_streak, 1)
        else:
            self.assertEqual(first.metrics.win_rate, 1.0)
            self.assertEqual(first.metrics.average_profit, pnl)
            self.assertIsNone(first.metrics.average_loss)
            self.assertEqual(first.metrics.longest_losing_streak, 0)

    def test_result_serializes_complete_reproducible_parameters(self) -> None:
        bars = swing_strategy_bars(72, pattern="falling_ma60")
        result = self.backtester.run_symbol(bars, 100_000.0)
        payload = result.to_dict()
        expected_parameters = {
            field.name: getattr(self.config, field.name)
            for field in fields(type(self.config))
        }
        self.assertEqual(
            list(payload["strategy_parameters"]),
            sorted(expected_parameters),
        )
        self.assertEqual(payload["strategy_parameters"], dict(
            sorted(expected_parameters.items()),
        ))
        self.assertEqual(payload["execution_assumptions"], {
            "asset_type": "DOMESTIC_EQUITY_ETF",
            "benchmark_liquidated_at_end": False,
            "benchmark_policy": (
                "same_initial_cash_first_executable_open_using_prior_completed_"
                "volume_buy_and_hold_to_end"
            ),
            "buy_fill_price_formula": (
                "ceil_to_tick((reference_price+half_spread_ticks*price_tick)"
                "*(1+slippage_rate))"
            ),
            "buy_fee_rate": self.backtester.costs["buy_fee_rate"],
            "corporate_action_policy": (
                "fail_closed_on_adjusted_raw_scale_change"
            ),
            "default_half_spread_ticks": 1.0,
            "default_half_spread_ticks_rationale": (
                "conservative_one_tick_per_side"
            ),
            "entry_execution_policy": (
                "reject_open_at_or_below_stop_or_above_entry_high_plus_one_tick"
            ),
            "actual_buy_sizing_policy": (
                "recompute_at_actual_fill_and_stop_then_cap_cash_lot_prior_"
                "volume_symbol_weight_total_exposure_risk_per_trade_and_"
                "portfolio_risk"
            ),
            "exchange": "SSE",
            "execution_cost_order": (
                "half_spread_then_percentage_slippage_then_single_adverse_"
                "tick_rounding"
            ),
            "execution_day_phases": (
                "A_open_preexisting_stop;B_next_open_formal_order;C_intraday_"
                "stop_touch_with_metadata_sellability"
            ),
            "execution_timing": "next_trading_day_raw_open_then_intraday_stop",
            "execution_volume_gate": (
                "current_execution_day_volume_must_be_positive_else_"
                "SUSPENDED_OR_ZERO_VOLUME"
            ),
            "fee_formula": (
                "max(shares*fill_price*side_fee_rate,minimum_fee)"
            ),
            "fill_cap_policy": (
                "open_orders_daily_limit_only_without_execution_day_high_low;"
                "intraday_stop_final_ohlc_and_daily_limit"
            ),
            "financing_policy": "cash_only_no_negative_balance",
            "half_spread_ticks": 1.0,
            "intraday_turnaround": False,
            "lot_size": 100,
            "liquidity_budget_policy": (
                "single_shared_A_B_C_budget=floor_to_lot(min(prior_completed_"
                "volume_units,execution_day_volume_units)*volume_unit_shares*"
                "max_volume_participation);execution_volume_only_reduces_"
                "fills_and_never_changes_signal_or_price"
            ),
            "mark_to_market_policy": (
                "final_raw_close_without_forced_liquidation"
            ),
            "max_volume_participation": self.config.max_volume_participation,
            "minimum_fee": self.backtester.costs["minimum_fee"],
            "price_limit_pct": 0.20,
            "price_limit_policy": (
                "round_half_up_theoretical_limit_to_price_tick;ulp_exact_"
                "boundary;open_reject_at_adverse_limit;intraday_reject_only_"
                "when_all_ohlc_equal_limit;cap_fill_to_effective_limit"
            ),
            "price_tick": 0.001,
            "raw_adjusted_policy": (
                "signals_on_adjusted_prices_execution_on_raw_prices"
            ),
            "sell_fee_rate": self.backtester.costs["sell_fee_rate"],
            "sell_fill_price_formula": (
                "max(price_tick,floor_to_tick((reference_price-half_spread_ticks"
                "*price_tick)*(1-slippage_rate)))"
            ),
            "sellability_policy": (
                "metadata_intraday_turnaround_and_sellable_delay_days"
            ),
            "sell_fee_cash_policy": (
                "allow_negative_leg_proceeds_if_total_cash_remains_nonnegative;"
                "otherwise_reject_INSUFFICIENT_CASH_FOR_SELL_FEE"
            ),
            "sellable_delay_days": 1,
            "signal_bar_policy": "completed_daily_bars_through_signal_date",
            "slippage_rate": self.backtester.costs["slippage_rate"],
            "spread_slippage_attribution": (
                "if_half_spread_ticks_zero_spread_cost_zero;otherwise_spread_"
                "is_zero_slippage_effective_fill_cost_capped_by_total_adverse_"
                "cost;slippage_is_residual_adverse_cost"
            ),
            "stop_execution_policy": (
                "preexisting_open_le_stop_first_and_suppress_stale_signal;"
                "rejected_or_partial_open_stop_remains_pending_and_on_unlock_"
                "retries_at_adverse_open_preserving_open_eq_stop_STOP_EXIT_"
                "versus_open_lt_stop_GAP_THROUGH_STOP;otherwise_after_"
                "formal_open_order_intraday_low_le_stop_le_high_at_stop"
            ),
            "volume_policy": (
                "prior_completed_bar_volume_is_audited_liquidity_proxy;"
                "execution_day_volume_is_conservative_ex_post_physical_cap_"
                "only;metadata_units_then_lot_floor"
            ),
            "volume_unit_shares": 100,
        })

        varied_config = replace(
            self.config,
            walk_forward_step_days=self.config.walk_forward_step_days + 1,
        )
        varied = SwingBacktester(varied_config, self.trading).run_symbol(
            bars, 100_000.0,
        )
        self.assertNotEqual(result.to_json(), varied.to_json())
        self.assertNotEqual(
            payload["strategy_parameters"],
            varied.to_dict()["strategy_parameters"],
        )
        varied_cost = SwingBacktester(
            self.config, self.trading, half_spread_ticks=0.5,
        ).run_symbol(bars, 100_000.0)
        self.assertEqual(
            varied_cost.execution_assumptions["half_spread_ticks"], 0.5,
        )
        self.assertNotEqual(result.to_json(), varied_cost.to_json())
        with self.assertRaises(TypeError):
            result.strategy_parameters["risk_per_trade"] = 0.5
        with self.assertRaises(TypeError):
            result.execution_assumptions["half_spread_ticks"] = 0.0

        adjusted = swing_strategy_bars(72, pattern="pullback_reclaim")
        corporate_action = with_raw_scales(
            adjusted, [1.0] * 70 + [0.5, 0.5],
        )
        unavailable = self.backtester.run_symbol(corporate_action, 100_000.0)
        self.assertEqual(
            unavailable.to_dict()["execution_assumptions"],
            payload["execution_assumptions"],
        )
        self.assertEqual(
            unavailable.to_dict()["strategy_parameters"],
            payload["strategy_parameters"],
        )

    def test_strategy_evaluation_uses_bounded_equivalent_window(self) -> None:
        bars = swing_strategy_bars(800, pattern="rising")
        limit = strategy_lookback(self.config)
        seen_lengths: list[int] = []

        def capture(signal_bars, config, context):
            seen_lengths.append(len(signal_bars))
            return _decision(
                SwingState.TREND_BLOCKED,
                signal_bars[-1].trading_date,
                signal_bars[-1].trading_date,
                shares=0,
            )

        with patch("etf_rotation.swing_backtest.evaluate_swing", side_effect=capture):
            self.backtester.run_symbol(bars, 100_000.0)

        self.assertEqual(len(seen_lengths), 800 - self.config.minimum_daily_bars)
        self.assertTrue(seen_lengths)
        self.assertLessEqual(max(seen_lengths), limit)
        self.assertEqual(seen_lengths[-1], limit)

        signal_index = 240
        context = PortfolioContext.empty(
            100_000.0,
            lot_size=self.trading.lot_size,
            next_trading_date=bars[signal_index + 1].trading_date,
        )
        full = evaluate_swing(
            bars[:signal_index + 1], self.config, context,
        )
        bounded = evaluate_swing(
            bars[signal_index + 1 - limit:signal_index + 1],
            self.config,
            context,
        )
        restored = self.backtester._restore_full_history_evidence(
            bounded,
            full_bar_count=signal_index + 1,
            signal_index=signal_index,
            trading_date_indices={
                bar.trading_date: index for index, bar in enumerate(bars)
            },
            last_stop_trading_date=None,
        )
        self.assertEqual(full.to_dict(), restored.to_dict())

    def test_bounded_history_restores_exact_full_cooldown_evidence(self) -> None:
        bars = swing_strategy_bars(242, pattern="rising")
        signal_index = 240
        lookback = strategy_lookback(self.config)
        context = PortfolioContext.empty(
            100_000.0,
            lot_size=self.trading.lot_size,
            next_trading_date=bars[signal_index + 1].trading_date,
            last_stop_trading_date=bars[0].trading_date,
        )
        full = evaluate_swing(
            bars[:signal_index + 1], self.config, context,
        )
        bounded = evaluate_swing(
            bars[signal_index + 1 - lookback:signal_index + 1],
            self.config,
            context,
        )
        restored = self.backtester._restore_full_history_evidence(
            bounded,
            full_bar_count=signal_index + 1,
            signal_index=signal_index,
            trading_date_indices={
                bar.trading_date: index for index, bar in enumerate(bars)
            },
            last_stop_trading_date=context.last_stop_trading_date,
        )

        self.assertNotEqual(full.to_dict(), bounded.to_dict())
        self.assertEqual(restored.to_dict(), full.to_dict())
        self.assertEqual(restored.evidence["bar_count"], 241)
        self.assertEqual(restored.evidence["cooldown_sessions_elapsed"], 240)

    def test_bounded_unavailable_does_not_invent_cooldown_evidence(self) -> None:
        bars = swing_strategy_bars(242, pattern="rising")
        signal_index = 240
        overflowing = replace(
            self.config, pullback_atr_distance=1.7e308,
        )
        backtester = SwingBacktester(overflowing, self.trading)
        context = PortfolioContext.empty(
            100_000.0,
            lot_size=self.trading.lot_size,
            next_trading_date=bars[signal_index + 1].trading_date,
            last_stop_trading_date=bars[0].trading_date,
        )
        full = evaluate_swing(
            bars[:signal_index + 1], overflowing, context,
        )
        lookback = strategy_lookback(overflowing)
        bounded = evaluate_swing(
            bars[signal_index + 1 - lookback:signal_index + 1],
            overflowing,
            context,
        )
        restored = backtester._restore_full_history_evidence(
            bounded,
            full_bar_count=signal_index + 1,
            signal_index=signal_index,
            trading_date_indices={
                bar.trading_date: index for index, bar in enumerate(bars)
            },
            last_stop_trading_date=context.last_stop_trading_date,
        )

        self.assertEqual(full.state, SwingState.DATA_UNAVAILABLE)
        self.assertEqual(restored.to_dict(), full.to_dict())
        self.assertNotIn("cooldown_sessions_elapsed", restored.evidence)

    def test_spread_cost_is_separate_for_strategy_and_benchmark(self) -> None:
        bars = swing_strategy_bars(73, pattern="rising")

        def run(half_spread_ticks):
            calls = 0

            def evaluate(signal_bars, config, context):
                nonlocal calls
                calls += 1
                state = {
                    1: SwingState.TRIAL_ENTRY_CANDIDATE,
                    2: SwingState.EXIT_CANDIDATE,
                }.get(calls, SwingState.TREND_BLOCKED)
                return _decision(
                    state,
                    signal_bars[-1].trading_date,
                    bars[69 + calls].trading_date,
                    shares=100,
                    stop=100.0,
                )

            backtester = SwingBacktester(
                self.config,
                self.trading,
                buy_fee_rate=0.0,
                sell_fee_rate=0.0,
                minimum_fee=0.0,
                slippage_rate=0.0,
                half_spread_ticks=half_spread_ticks,
            )
            with patch(
                "etf_rotation.swing_backtest.evaluate_swing",
                side_effect=evaluate,
            ):
                return backtester.run_symbol(bars, 100_000.0)

        without_spread = run(0.0)
        with_half_tick_spread = run(0.5)
        with_spread = run(1.0)

        self.assertEqual(without_spread.metrics.spread_cost, 0.0)
        self.assertEqual(without_spread.metrics.slippage, 0.0)
        self.assertEqual(without_spread.benchmark.spread_cost, 0.0)
        self.assertGreater(with_half_tick_spread.metrics.spread_cost, 0.0)
        self.assertEqual(with_half_tick_spread.metrics.slippage, 0.0)
        self.assertGreater(with_half_tick_spread.benchmark.spread_cost, 0.0)
        self.assertEqual(with_half_tick_spread.benchmark.slippage, 0.0)
        self.assertGreater(with_spread.metrics.spread_cost, 0.0)
        self.assertEqual(with_spread.metrics.slippage, 0.0)
        self.assertGreater(with_spread.benchmark.spread_cost, 0.0)
        self.assertEqual(with_spread.benchmark.slippage, 0.0)
        self.assertGreaterEqual(without_spread.benchmark.cash, 0.0)
        self.assertGreaterEqual(with_spread.benchmark.cash, 0.0)
        self.assertLess(with_spread.ending_equity, without_spread.ending_equity)
        self.assertIn('"spread_cost":', with_spread.to_json())

        off_tick = replace(
            bars[1], open=100.0055, adjusted_open=100.0055,
        )
        account = BacktestAccount(
            100_000.0,
            self.trading,
            self.config,
            buy_fee_rate=0.0,
            minimum_fee=0.0,
            slippage_rate=0.0,
            half_spread_ticks=0.0,
        )
        fill = account.execute(
            _decision(
                SwingState.TRIAL_ENTRY_CANDIDATE,
                bars[0].trading_date,
                off_tick.trading_date,
                shares=100,
            ),
            off_tick,
            execution_index=1,
        )
        self.assertEqual(fill.spread_cost, 0.0)
        self.assertGreater(fill.slippage, 0.0)

    def test_invalid_inputs_are_rejected_without_mutating_sequence(self) -> None:
        bars = list(swing_strategy_bars(72, pattern="rising"))
        original = tuple(bars)
        cases = (
            (tuple(reversed(bars)), 100_000.0),
            ((replace(bars[0], symbol="510500"), *bars[1:]), 100_000.0),
            ((replace(bars[0], adjusted_close=math.nan), *bars[1:]), 100_000.0),
            (tuple(bars[:69]), 100_000.0),
            (tuple(bars), math.inf),
        )
        for candidate, cash in cases:
            with self.subTest(cash=cash, count=len(candidate)):
                with self.assertRaises(SwingBacktestError):
                    self.backtester.run_symbol(candidate, cash)
        self.assertEqual(tuple(bars), original)

    def test_adjusted_signal_raw_fill_and_corporate_action_scale(self) -> None:
        adjusted = swing_strategy_bars(72, pattern="pullback_reclaim")
        bars = with_raw_scales(adjusted, [1.0] * 70 + [0.5, 0.5])
        with patch("etf_rotation.swing_backtest.evaluate_swing") as evaluate:
            result = self.backtester.run_symbol(bars, 100_000.0)
        evaluate.assert_not_called()
        self.assertEqual(result.status, "DATA_UNAVAILABLE")
        self.assertEqual(result.reason, "CORPORATE_ACTION_UNSUPPORTED")
        self.assertEqual(result.trades, ())
        self.assertIsNone(result.ending_equity)
        self.assertIsNone(result.metrics)
        self.assertIsNone(result.benchmark)
        self.assertIsNone(result.outperformance)
        self.assertEqual(result.to_json(),
                         self.backtester.run_symbol(bars, 100_000.0).to_json())

    def test_early_observation_is_rejected_before_signal_or_mark(self) -> None:
        bars = list(swing_strategy_bars(72, pattern="rising"))
        bars[70] = replace(
            bars[70],
            observed_at=bars[70].observed_at.replace(hour=9, minute=0),
        )
        with patch("etf_rotation.swing_backtest.evaluate_swing") as evaluate:
            with self.assertRaises(SwingBacktestError):
                self.backtester.run_symbol(tuple(bars), 100_000.0)
        evaluate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
