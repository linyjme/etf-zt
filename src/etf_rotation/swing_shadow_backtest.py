"""Fixed-window shadow research using the same V1 evaluator and fills."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, asdict, replace
from datetime import date
import math
from pathlib import Path

from .etf_metadata import EtfMetadataStore, TradingMetadata
from .market_data import load_closed_dates
from .swing_backtest import SwingBacktester, SwingBacktestError, strategy_lookback, _curve_metric_values
from .swing_config import load_strategy
from .swing_data import DailyBar
from .swing_indicators import calculate_indicator_context
from .swing_opportunities import opportunity_timeline
from .swing_shadow import (
    ShadowVariant, ShadowState, ShadowContext, infer_shadow_regime,
    evaluate_hybrid_shadow, load_shadow_config, _evaluate_shadow_context,
)
from .swing_strategy import evaluate_swing, SwingState

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ExecutionCosts:
    commission_rate: float = 0.00012
    slippage_rate: float = 0.0002
    minimum_fee: float = 0.0
    half_spread_ticks: float = 1.0
    lot_size: int = 100
    target_notional: float = 2000.0
    maximum_notional: float = 5000.0

    def __post_init__(self):
        for key in ("commission_rate", "slippage_rate", "minimum_fee", "half_spread_ticks", "target_notional", "maximum_notional"):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"invalid execution cost: {key}")
        if not 0 < self.target_notional <= self.maximum_notional <= 5000:
            raise ValueError("notional must be positive and capped at 5000")

    def assumptions(self):
        return tuple(sorted({**asdict(self), "execution": "NEXT_OPEN_APPROXIMATION",
            "marking": "daily_raw_close_including_unsold_inventory",
            "sizing": "target_2000_hard_cap_5000_metadata_lots",
            "sellability": "ETF_metadata",
            "baseline": "same_initial_cash_buy_and_hold_first_test_open",
            "exit": "shared_V1_stop_reduce_exit_or_20_session_time_exit"}.items()))


@dataclass(frozen=True)
class ShadowTrade:
    symbol: str
    side: str
    signal_date: date
    execution_date: date
    shares: int
    execution_price: float
    fee: float
    spread_cost: float = 0.0
    slippage: float = 0.0

    def to_dict(self):
        return {**asdict(self), "signal_date": self.signal_date.isoformat(), "execution_date": self.execution_date.isoformat()}


@dataclass(frozen=True)
class ShadowReplayResult:
    variant: str
    trades: tuple[ShadowTrade, ...]
    execution_assumptions: tuple
    validation_status: str
    performance_claim_allowed: bool
    net_pnl: float
    max_drawdown: float
    rejected_count: int
    folds: tuple[Mapping[str, object], ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)


def _validate_history(history):
    if not isinstance(history, Mapping) or not history:
        raise ValueError("history_by_symbol must be a non-empty mapping")
    result = {}
    for symbol, supplied in history.items():
        bars = tuple(supplied)
        if not bars or any(type(bar) is not DailyBar or bar.symbol != symbol or not bar.is_final for bar in bars):
            raise ValueError("history key must match completed bars")
        if any(a.trading_date >= b.trading_date for a, b in zip(bars, bars[1:])):
            raise ValueError("history dates must be strictly ascending")
        result[symbol] = bars
    return result


class _ResearchRunner(SwingBacktester):
    def __init__(self, config, trading, *, variant, costs, shadow_config, prepared):
        super().__init__(config, trading, buy_fee_rate=costs.commission_rate, sell_fee_rate=costs.commission_rate,
            minimum_fee=costs.minimum_fee, slippage_rate=costs.slippage_rate, half_spread_ticks=costs.half_spread_ticks)
        self.variant, self.research_costs, self.shadow_config, self.prepared = variant, costs, shadow_config, prepared

    def _evaluate_signal(self, bars, context):
        formal = evaluate_swing(bars[-strategy_lookback(self.config):], self.config, context)
        if context.position is not None:
            held = sum(bar.trading_date > context.position.entry_trading_date for bar in bars)
            if held >= 20 and formal.state not in (SwingState.EXIT_CANDIDATE, SwingState.REDUCE_CANDIDATE):
                return replace(formal, state=SwingState.EXIT_CANDIDATE, planned_shares=context.position.sellable_shares,
                    blocked_reasons=(), valid_for_trading_date=None)
        elif self.variant is not ShadowVariant.V1:
            mode, _regime_evidence = infer_shadow_regime(bars)
            shadow_context = ShadowContext(
                data_quality="VERIFIED", data_version="sha256:prefix",
                trend_state=mode, range_confirmed=mode == "RANGE",
                uncertain=mode == "UNCERTAIN",
            )
            if self.variant is ShadowVariant.HYBRID:
                point, _event = self.prepared.get(bars[-1].trading_date, ({}, None))
                shadow = evaluate_hybrid_shadow(
                    bars, context=shadow_context,
                    indicator={
                        "status": "READY" if len(bars) >= 120 else "WARMUP",
                        "bar_count": len(bars), "latest": point,
                        "indicator_version": "INDICATORS_V1",
                        "data_version": "sha256:prefix",
                        "as_of_trading_date": bars[-1].trading_date.isoformat(),
                    },
                )
            else:
                point, event = self.prepared.get(bars[-1].trading_date, ({}, None))
                shadow = _evaluate_shadow_context(
                    bars, variant=self.variant, config=self.shadow_config,
                    context=ShadowContext(
                        data_version="sha256:prefix", trend_state=mode,
                        range_confirmed=mode == "RANGE", uncertain=mode == "UNCERTAIN",
                        opportunity_id=event.opportunity_id if event else None,
                        opportunity_status=event.status.value if event else None,
                    ),
                    indicator={"status": "READY" if len(bars) >= 120 else "WARMUP",
                        "bar_count": len(bars), "latest": point,
                        "indicator_version": "INDICATORS_V1", "data_version": "sha256:prefix",
                        "as_of_trading_date": bars[-1].trading_date.isoformat()},
                )
            if (shadow.state is ShadowState.TECHNICAL_CANDIDATE
                    and formal.evidence.get("health_gates_ok") is True
                    and formal.evidence.get("calendar_validity_ok") is True):
                planned_shares = formal.planned_shares
                planned_risk_rate = formal.planned_risk_rate
                if formal.planned_shares <= 0:
                    entry = formal.planned_entry_high or bars[-1].close
                    lot = self.trading.lot_size
                    planned = int(self.research_costs.target_notional / entry / lot) * lot
                    planned_shares = max(lot, planned)
                    planned_risk_rate = max(formal.planned_risk_rate, self.config.risk_per_trade)
                formal = replace(formal, state=SwingState.TRIAL_ENTRY_CANDIDATE,
                    planned_shares=planned_shares, planned_risk_rate=planned_risk_rate,
                    blocked_reasons=(), valid_for_trading_date=context.next_trading_date)
            else:
                formal = replace(formal, state=SwingState.PULLBACK_WATCH, planned_shares=0, planned_risk_rate=0.0,
                    valid_for_trading_date=None, blocked_reasons=tuple(shadow.blocked_reasons) or ("SHADOW_ENTRY_BLOCKED",))
        if formal.state in (SwingState.TRIAL_ENTRY_CANDIDATE, SwingState.ADD_CANDIDATE):
            entry = formal.planned_entry_high or bars[-1].close
            lot = self.trading.lot_size
            shares = min(formal.planned_shares, int(self.research_costs.target_notional / entry / lot) * lot,
                int(self.research_costs.maximum_notional / (bars[-1].close * (1 + self.trading.price_limit_pct)) / lot) * lot)
            if shares < lot:
                return replace(formal, state=SwingState.PULLBACK_WATCH, planned_shares=0, planned_risk_rate=0.0,
                    valid_for_trading_date=None, blocked_reasons=("ORDER_NOTIONAL_OR_LOT_LIMIT",))
            formal = replace(formal, planned_shares=shares,
                planned_risk_rate=formal.planned_risk_rate * shares / formal.planned_shares)
        return formal


def _summarize(results, initial_equity):
    rounds = [trip for result in results for trip in result.round_trips]
    curves = [result.cache_evidence()["equity_curve"] for result in results]
    curve = [sum(values) for values in zip(*curves)]
    path = _curve_metric_values(initial_equity, curve, [])
    metrics = {"completed_round_trips": len(rounds), "uncompleted_leg_count": sum(r.uncompleted_leg_count for r in results),
        "fees": sum(r.metrics.fees for r in results), "spread_cost": sum(r.metrics.spread_cost for r in results),
        "slippage": sum(r.metrics.slippage for r in results), "win_rate": sum(t.net_pnl > 0 for t in rounds) / len(rounds) if rounds else None,
        "average_holding_days": sum(t.holding_days for t in rounds) / len(rounds) if rounds else None,
        "baseline_policy": "same_initial_cash_buy_and_hold_first_test_open",
        "baseline_net_pnl": sum((r.benchmark.ending_equity - r.initial_cash) for r in results) if all(r.benchmark is not None for r in results) else None,
        "rejection_counts": {}, "equity_curve": curve, "net_pnl": sum(r.ending_equity - r.initial_cash for r in results),
        "max_drawdown": path["maximum_drawdown"]}
    for r in results:
        for key, count in r.metrics.rejection_counts.items():
            metrics["rejection_counts"][key] = metrics["rejection_counts"].get(key, 0) + count
    return metrics


def replay_variant(history_by_symbol: Mapping[str, Sequence[DailyBar]], *, variant: ShadowVariant, costs: ExecutionCosts,
    initial_equity: float = 100000.0, trading_by_symbol: Mapping[str, TradingMetadata] | None = None, _prepared=None) -> ShadowReplayResult:
    variant = ShadowVariant(variant)
    histories = _validate_history(history_by_symbol)
    if trading_by_symbol is None:
        metadata = {s: m.trading for s, m in EtfMetadataStore(ROOT / "data/monitor/etf_metadata.json").load().items()}
    else:
        metadata = trading_by_symbol
    if any(symbol not in metadata for symbol in histories):
        return ShadowReplayResult(variant.value, (), costs.assumptions(), "BLOCKED_METADATA", False, 0.0, 0.0, 0)
    overlap_start = max(bars[0].trading_date for bars in histories.values())
    overlap_end = min(bars[-1].trading_date for bars in histories.values())
    calendars = [{b.trading_date for b in bars if overlap_start <= b.trading_date <= overlap_end} for bars in histories.values()]
    if any(values != calendars[0] for values in calendars[1:]):
        return ShadowReplayResult(variant.value, (), costs.assumptions(), "NON_CONTIGUOUS_COMMON_HISTORY", False, 0.0, 0.0, 0)
    calendar = sorted(calendars[0])
    if len(calendar) < 630:
        return ShadowReplayResult(variant.value, (), costs.assumptions(), "INSUFFICIENT_SAMPLE", False, 0.0, 0.0, 0)
    config = load_strategy(ROOT / "data/swing/strategy.json")
    shadow_config = load_shadow_config(ROOT / "data/swing/shadow_strategy.json")
    prepared = _prepared if _prepared is not None else {}
    if variant is not ShadowVariant.V1:
        holidays = load_closed_dates(ROOT / "data/monitor/market_calendar.json")
        for symbol, bars in histories.items():
            if symbol not in prepared:
                context = calculate_indicator_context(bars, lookback=len(bars))
                points = context["recent"]
                events = opportunity_timeline(bars, points, window_sessions=shadow_config.event_window_sessions,
                    atr_distance_max=shadow_config.atr_distance_max, closed_dates=holidays)
                prepared[symbol] = {b.trading_date: (p, events[b.trading_date]) for b, p in zip(bars, points)}
    folds, all_trades = [], []
    for offset in range(0, len(calendar) - 629, 126):
        train, test = calendar[offset:offset + 504], calendar[offset + 504:offset + 630]
        results = []
        for symbol, bars in histories.items():
            runner = _ResearchRunner(config, metadata[symbol], variant=variant, costs=costs, shadow_config=shadow_config, prepared=prepared.get(symbol, {}))
            prefix = tuple(b for b in bars if b.trading_date <= test[-1])
            result = runner.run_symbol(prefix, initial_equity / len(histories), start_date=test[0])
            if result.metrics is None:
                return ShadowReplayResult(variant.value, (), costs.assumptions(),
                    result.reason or "BLOCKED_INVALID_HISTORY", False, 0.0, 0.0, 0)
            results.append(result)
        metrics = _summarize(results, initial_equity)
        trades = [trade for result in results for trade in result.trades]
        all_trades.extend(trades)
        folds.append({"fold_index": len(folds), "train_start_date": train[0].isoformat(), "train_end_date": train[-1].isoformat(),
            "test_start_date": test[0].isoformat(), "test_end_date": test[-1].isoformat(), "train_bar_count": 504, "test_bar_count": 126,
            "parameters_policy": "fixed_before_test_no_selection", "metrics": metrics,
            "trades": [trade.to_dict() for trade in trades]})
    average_holding = [f["metrics"]["average_holding_days"] for f in folds if f["metrics"]["average_holding_days"] is not None]
    aggregate = {"fold_count": len(folds), "completed_round_trips": sum(f["metrics"]["completed_round_trips"] for f in folds),
        "uncompleted_leg_count": sum(f["metrics"]["uncompleted_leg_count"] for f in folds), "fees": sum(f["metrics"]["fees"] for f in folds),
        "spread_cost": sum(f["metrics"]["spread_cost"] for f in folds), "slippage": sum(f["metrics"]["slippage"] for f in folds),
        "average_holding_days": (sum(average_holding) / len(average_holding) if average_holding else None),
        "baseline_policy": "same_initial_cash_buy_and_hold_first_test_open", "baseline_net_pnl": sum((f["metrics"]["baseline_net_pnl"] or 0) for f in folds)}
    status = "REVIEW_REQUIRED" if len(folds) >= 2 and aggregate["completed_round_trips"] >= 20 else "INSUFFICIENT_SAMPLE"
    return ShadowReplayResult(variant.value, tuple(all_trades), costs.assumptions(), status, False,
        sum(f["metrics"]["net_pnl"] for f in folds), max((f["metrics"]["max_drawdown"] for f in folds), default=0.0),
        0, tuple(folds), aggregate)


def replay_all_variants(history_by_symbol, *, costs, initial_equity=100000.0, trading_by_symbol=None):
    prepared = {}
    return {variant.value: replay_variant(history_by_symbol, variant=variant, costs=costs,
        initial_equity=initial_equity, trading_by_symbol=trading_by_symbol, _prepared=prepared)
        for variant in (
            ShadowVariant.V1, ShadowVariant.V2_A, ShadowVariant.V2_B,
            ShadowVariant.V2_C, ShadowVariant.HYBRID,
        )}


__all__ = ["ExecutionCosts", "ShadowReplayResult", "ShadowTrade", "replay_all_variants", "replay_variant"]
