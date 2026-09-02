"""Deterministic next-open backtesting for one swing-monitor ETF."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from etf_rotation import constants
from etf_rotation.etf_metadata import (
    EtfMetadata,
    IndexMetadata,
    TradingMetadata,
)
from etf_rotation.swing_config import SwingStrategyConfig
from etf_rotation.swing_data import (
    DailyBar,
    DailyBarValidator,
    SwingDataError,
)
from etf_rotation.swing_strategy import (
    PortfolioContext,
    PositionContext,
    SwingDecision,
    SwingState,
    evaluate_swing,
)


class SwingBacktestError(ValueError):
    """Raised when a backtest input or execution setting is invalid."""


class _CorporateActionUnsupported(Exception):
    def __init__(self, bars: tuple[DailyBar, ...]) -> None:
        super().__init__("CORPORATE_ACTION_UNSUPPORTED")
        self.bars = bars


_BUY_STATES = frozenset((
    SwingState.TRIAL_ENTRY_CANDIDATE,
    SwingState.ADD_CANDIDATE,
))
_SELL_STATES = frozenset((
    SwingState.REDUCE_CANDIDATE,
    SwingState.EXIT_CANDIDATE,
))


def _finite(value: object, field: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise SwingBacktestError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (number <= 0.0 if positive else number < 0.0):
        qualifier = "positive " if positive else "nonnegative "
        raise SwingBacktestError(f"{field} must be a finite {qualifier}number")
    return number


def _clean(value: float) -> float:
    rounded = round(float(value), 12)
    return 0.0 if rounded == 0.0 else rounded


def _freeze_audit_mapping(
    values: Mapping[str, object], field: str,
) -> Mapping[str, object]:
    normalized: dict[str, object] = {}
    for key in sorted(values):
        if type(key) is not str or not key:
            raise SwingBacktestError(f"{field} keys must be nonempty strings")
        value = values[key]
        if type(value) is float:
            if not math.isfinite(value):
                raise SwingBacktestError(f"{field} values must be JSON-safe")
            normalized[key] = float(value)
        elif type(value) in (str, int, bool):
            normalized[key] = value
        else:
            raise SwingBacktestError(f"{field} values must be JSON-safe scalars")
    return MappingProxyType(normalized)


def _lot_floor(shares: int | float, lot_size: int) -> int:
    if not math.isfinite(float(shares)) or shares <= 0:
        return 0
    return max(0, int(math.floor(float(shares) / lot_size)) * lot_size)


def _adverse_tick_price(reference: float, rate: float, tick: float, side: str) -> float:
    unrounded = reference * (1.0 + rate if side == "BUY" else 1.0 - rate)
    quotient = unrounded / tick
    nearest = round(quotient)
    if math.isclose(quotient, nearest, rel_tol=0.0, abs_tol=1e-9):
        ticks = nearest
    else:
        ticks = math.ceil(quotient) if side == "BUY" else math.floor(quotient)
    return max(tick, ticks * tick)


def _execution_price(
    reference: float,
    side: str,
    *,
    tick: float,
    half_spread_ticks: float,
    slippage_rate: float,
) -> float:
    spread_price = reference + (
        half_spread_ticks * tick if side == "BUY"
        else -half_spread_ticks * tick
    )
    return _adverse_tick_price(spread_price, slippage_rate, tick, side)


def _execution_cost_parts(
    reference: float,
    fill_price: float,
    shares: int,
    side: str,
    *,
    tick: float,
    half_spread_ticks: float,
) -> tuple[float, float]:
    adverse_per_share = max(
        0.0,
        fill_price - reference if side == "BUY" else reference - fill_price,
    )
    spread_per_share = 0.0
    if half_spread_ticks > 0.0:
        quoted_spread_price = reference + (
            half_spread_ticks * tick if side == "BUY"
            else -half_spread_ticks * tick
        )
        spread_fill = _adverse_tick_price(
            quoted_spread_price, 0.0, tick, side,
        )
        effective_spread = max(
            0.0,
            spread_fill - reference
            if side == "BUY" else reference - spread_fill,
        )
        spread_per_share = min(adverse_per_share, effective_spread)
    return (
        _clean(spread_per_share * shares),
        _clean(max(0.0, adverse_per_share - spread_per_share) * shares),
    )


def _effective_limit_price(
    previous_close: float,
    price_limit_pct: float,
    price_tick: float,
    side: str,
) -> float:
    factor = (
        Decimal("1") + Decimal(str(price_limit_pct))
        if side == "BUY"
        else Decimal("1") - Decimal(str(price_limit_pct))
    )
    tick = Decimal(str(price_tick))
    ticks = (
        Decimal(str(previous_close)) * factor / tick
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(ticks * tick)


def _limit_equality_tolerance(*values: float) -> float:
    return max(math.ulp(value) for value in values) * 8


def strategy_lookback(config: SwingStrategyConfig) -> int:
    """Return the bounded completed-bar window sufficient for SWING_V1."""
    if type(config) is not SwingStrategyConfig:
        raise SwingBacktestError("config must be SwingStrategyConfig")
    return max(
        config.minimum_daily_bars,
        config.long_ma_days + config.long_ma_slope_lookback,
        config.atr_days + 1,
        config.breakout_days + 1,
        config.cooldown_days + 1,
        2,
    )


def _validate_trading(trading: TradingMetadata) -> None:
    if type(trading) is not TradingMetadata:
        raise SwingBacktestError("trading must be TradingMetadata")
    if type(trading.intraday_turnaround) is not bool:
        raise SwingBacktestError("intraday_turnaround must be bool")
    for value, field, allow_zero in (
        (trading.sellable_delay_days, "sellable_delay_days", True),
        (trading.lot_size, "lot_size", False),
        (trading.volume_unit_shares, "volume_unit_shares", False),
    ):
        if type(value) is not int or value < (0 if allow_zero else 1):
            raise SwingBacktestError(f"{field} is invalid")
    _finite(trading.price_tick, "price_tick", positive=True)
    limit = _finite(trading.price_limit_pct, "price_limit_pct", positive=True)
    if limit > 1.0:
        raise SwingBacktestError("price_limit_pct must not exceed 1")


@dataclass(frozen=True)
class SwingFill:
    symbol: str
    side: str
    requested_shares: int
    shares: int
    signal_date: date
    execution_date: date
    raw_reference_price: float
    fill_price: float
    fee: float
    spread_cost: float
    slippage: float
    planned_stop: float | None
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "requested_shares": self.requested_shares,
            "shares": self.shares,
            "signal_date": self.signal_date.isoformat(),
            "execution_date": self.execution_date.isoformat(),
            "raw_reference_price": _clean(self.raw_reference_price),
            "fill_price": _clean(self.fill_price),
            "fee": _clean(self.fee),
            "spread_cost": _clean(self.spread_cost),
            "slippage": _clean(self.slippage),
            "planned_stop": (
                None if self.planned_stop is None else _clean(self.planned_stop)
            ),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SwingRejection:
    symbol: str
    side: str
    signal_date: date
    execution_date: date
    requested_shares: int
    rejected_shares: int
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "signal_date": self.signal_date.isoformat(),
            "execution_date": self.execution_date.isoformat(),
            "requested_shares": self.requested_shares,
            "rejected_shares": self.rejected_shares,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CompletedRoundTrip:
    entry_date: date
    exit_date: date
    net_pnl: float
    holding_days: int

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "net_pnl": _clean(self.net_pnl),
            "holding_days": self.holding_days,
        }


@dataclass(frozen=True)
class PortfolioCompletedRoundTrip:
    symbol: str
    entry_date: date
    exit_date: date
    net_pnl: float
    holding_days: int

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "entry_date": self.entry_date.isoformat(),
            "exit_date": self.exit_date.isoformat(),
            "net_pnl": _clean(self.net_pnl),
            "holding_days": self.holding_days,
        }


@dataclass(frozen=True)
class SwingBacktestMetrics:
    cumulative_return: float
    annualized_return: float | None
    maximum_drawdown: float
    calmar: float | None
    sharpe: float | None
    win_rate: float | None
    average_profit: float | None
    average_loss: float | None
    payoff_ratio: float | None
    average_holding_days: float | None
    utilization: float
    longest_losing_streak: int | None
    fees: float
    spread_cost: float
    slippage: float
    rejection_counts: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "rejection_counts",
            MappingProxyType(dict(sorted(self.rejection_counts.items()))),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cumulative_return": _clean(self.cumulative_return),
            "annualized_return": (
                None if self.annualized_return is None
                else _clean(self.annualized_return)
            ),
            "maximum_drawdown": _clean(self.maximum_drawdown),
            "calmar": None if self.calmar is None else _clean(self.calmar),
            "sharpe": None if self.sharpe is None else _clean(self.sharpe),
            "win_rate": None if self.win_rate is None else _clean(self.win_rate),
            "average_profit": (
                None if self.average_profit is None else _clean(self.average_profit)
            ),
            "average_loss": (
                None if self.average_loss is None else _clean(self.average_loss)
            ),
            "payoff_ratio": (
                None if self.payoff_ratio is None else _clean(self.payoff_ratio)
            ),
            "average_holding_days": (
                None if self.average_holding_days is None
                else _clean(self.average_holding_days)
            ),
            "utilization": _clean(self.utilization),
            "longest_losing_streak": self.longest_losing_streak,
            "fees": _clean(self.fees),
            "spread_cost": _clean(self.spread_cost),
            "slippage": _clean(self.slippage),
            "rejection_counts": dict(self.rejection_counts),
        }


@dataclass(frozen=True)
class SwingBenchmarkResult:
    start_date: date
    shares: int
    cash: float
    ending_equity: float
    cumulative_return: float
    fee: float
    spread_cost: float
    slippage: float

    def to_dict(self) -> dict[str, object]:
        return {
            "start_date": self.start_date.isoformat(),
            "shares": self.shares,
            "cash": _clean(self.cash),
            "ending_equity": _clean(self.ending_equity),
            "cumulative_return": _clean(self.cumulative_return),
            "fee": _clean(self.fee),
            "spread_cost": _clean(self.spread_cost),
            "slippage": _clean(self.slippage),
        }


@dataclass(frozen=True)
class SwingBacktestResult:
    schema_version: int
    strategy_version: str
    strategy_parameters: Mapping[str, object]
    execution_assumptions: Mapping[str, object]
    symbol: str
    status: str
    reason: str | None
    initial_cash: float
    cash: float
    ending_equity: float | None
    start_date: date
    end_date: date
    trades: tuple[SwingFill, ...]
    rejections: tuple[SwingRejection, ...]
    round_trips: tuple[PortfolioCompletedRoundTrip, ...]
    open_position_shares: int
    uncompleted_leg_count: int
    benchmark: SwingBenchmarkResult | None
    outperformance: float | None
    metrics: SwingBacktestMetrics | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "strategy_parameters",
            _freeze_audit_mapping(
                self.strategy_parameters, "strategy_parameters",
            ),
        )
        object.__setattr__(
            self,
            "execution_assumptions",
            _freeze_audit_mapping(
                self.execution_assumptions, "execution_assumptions",
            ),
        )

    @property
    def completed_round_trips(self) -> int:
        return len(self.round_trips)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_version": self.strategy_version,
            "strategy_parameters": dict(self.strategy_parameters),
            "execution_assumptions": dict(self.execution_assumptions),
            "symbol": self.symbol,
            "status": self.status,
            "reason": self.reason,
            "initial_cash": _clean(self.initial_cash),
            "cash": _clean(self.cash),
            "ending_equity": (
                None if self.ending_equity is None else _clean(self.ending_equity)
            ),
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "trades": [trade.to_dict() for trade in self.trades],
            "rejections": [item.to_dict() for item in self.rejections],
            "completed_round_trips": self.completed_round_trips,
            "round_trips": [item.to_dict() for item in self.round_trips],
            "open_position_shares": self.open_position_shares,
            "uncompleted_leg_count": self.uncompleted_leg_count,
            "benchmark": (
                None if self.benchmark is None else self.benchmark.to_dict()
            ),
            "outperformance": (
                None if self.outperformance is None else _clean(self.outperformance)
            ),
            "metrics": None if self.metrics is None else self.metrics.to_dict(),
            "metric_conventions": {
                "annualization_sessions": 252,
                "sharpe_frequency": "DAILY_252",
                "sharpe_risk_free_rate": 0.0,
                "sharpe_zero_variance": None,
                "drawdown_sign": "POSITIVE_MAGNITUDE",
                "holding_days": "TRADING_SESSIONS",
            },
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )


@dataclass(frozen=True)
class PendingAction:
    """One completed-close action awaiting the next executable open."""

    kind: str
    symbol: str
    trend_score: float
    decision: SwingDecision | None

    def __post_init__(self) -> None:
        if self.kind not in {"EXIT", "REDUCE", "ADD", "TRIAL_ENTRY"}:
            raise SwingBacktestError("pending action kind is invalid")
        if (
            type(self.symbol) is not str
            or len(self.symbol) != 6
            or not self.symbol.isascii()
            or not self.symbol.isdigit()
        ):
            raise SwingBacktestError("pending action symbol is invalid")
        object.__setattr__(
            self, "trend_score", _finite(self.trend_score, "trend_score"),
        )
        if self.decision is not None and type(self.decision) is not SwingDecision:
            raise SwingBacktestError("pending action decision is invalid")


@dataclass(frozen=True)
class PortfolioBenchmarkResult:
    status: str
    reason: str | None
    initial_cash: float
    cash: float
    ending_equity: float | None
    trades: tuple[SwingFill, ...]
    shares_by_symbol: Mapping[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "initial_cash": _clean(self.initial_cash),
            "cash": _clean(self.cash),
            "ending_equity": (
                None if self.ending_equity is None else _clean(self.ending_equity)
            ),
            "trades": [item.to_dict() for item in self.trades],
            "fees": _clean(sum(item.fee for item in self.trades)),
            "spread_cost": _clean(sum(item.spread_cost for item in self.trades)),
            "slippage": _clean(sum(item.slippage for item in self.trades)),
            "shares_by_symbol": dict(sorted(self.shares_by_symbol.items())),
            "cumulative_return": (
                None if self.ending_equity is None
                else _clean(self.ending_equity / self.initial_cash - 1.0)
            ),
        }


class PortfolioRejections(tuple[SwingRejection, ...]):
    """Immutable audit events with convenient deterministic reason counts."""

    def __new__(cls, values: Sequence[SwingRejection]) -> PortfolioRejections:
        return tuple.__new__(cls, tuple(values))

    @property
    def counts(self) -> Mapping[str, int]:
        result: dict[str, int] = {}
        for item in self:
            result[item.reason] = result.get(item.reason, 0) + 1
        return MappingProxyType(dict(sorted(result.items())))

    def __getitem__(self, key: int | slice | str):
        if type(key) is str:
            return self.counts.get(key, 0)
        return super().__getitem__(key)


@dataclass(frozen=True)
class PortfolioBacktestResult:
    schema_version: int
    scope: str
    strategy_version: str
    status: str
    reason: str | None
    symbols: tuple[str, ...]
    initial_cash: float
    cash: float
    ending_equity: float | None
    common_start_date: date | None
    common_end_date: date | None
    event_dates: tuple[date, ...]
    trades: tuple[SwingFill, ...]
    rejections: tuple[SwingRejection, ...]
    round_trips: tuple[CompletedRoundTrip, ...]
    open_position_shares: Mapping[str, int]
    uncompleted_leg_count: int
    max_equity_weight: float
    max_planned_risk: float
    metrics: SwingBacktestMetrics | None
    baseline: PortfolioBenchmarkResult | None
    baseline_weights: Mapping[str, float]
    outperformance: float | None
    strategy_parameters: Mapping[str, object]
    execution_assumptions: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rejections", PortfolioRejections(self.rejections))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "strategy_version": self.strategy_version,
            "status": self.status,
            "reason": self.reason,
            "symbols": list(self.symbols),
            "initial_cash": _clean(self.initial_cash),
            "cash": _clean(self.cash),
            "ending_equity": (
                None if self.ending_equity is None else _clean(self.ending_equity)
            ),
            "common_start_date": (
                None if self.common_start_date is None
                else self.common_start_date.isoformat()
            ),
            "common_end_date": (
                None if self.common_end_date is None
                else self.common_end_date.isoformat()
            ),
            "event_dates": [item.isoformat() for item in self.event_dates],
            "trades": [item.to_dict() for item in self.trades],
            "rejections": [item.to_dict() for item in self.rejections],
            "rejection_counts": dict(self.rejections.counts),
            "completed_round_trips": len(self.round_trips),
            "round_trips": [item.to_dict() for item in self.round_trips],
            "open_position_shares": dict(sorted(self.open_position_shares.items())),
            "uncompleted_leg_count": self.uncompleted_leg_count,
            "max_equity_weight": _clean(self.max_equity_weight),
            "max_planned_risk": _clean(self.max_planned_risk),
            "metrics": None if self.metrics is None else self.metrics.to_dict(),
            "baseline": None if self.baseline is None else self.baseline.to_dict(),
            "baseline_weights": dict(sorted(self.baseline_weights.items())),
            "outperformance": (
                None if self.outperformance is None else _clean(self.outperformance)
            ),
            "strategy_parameters": dict(self.strategy_parameters),
            "execution_assumptions": dict(self.execution_assumptions),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=True, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )


@dataclass(frozen=True)
class WalkForwardFoldResult:
    fold_index: int
    train_start_date: date
    train_end_date: date
    test_start_date: date
    test_end_date: date
    train_bar_count: int
    test_bar_count: int
    train: Mapping[str, object]
    test: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "fold_index": self.fold_index,
            "train_start_date": self.train_start_date.isoformat(),
            "train_end_date": self.train_end_date.isoformat(),
            "test_start_date": self.test_start_date.isoformat(),
            "test_end_date": self.test_end_date.isoformat(),
            "train_bar_count": self.train_bar_count,
            "test_bar_count": self.test_bar_count,
            "train": dict(self.train),
            "test": dict(self.test),
        }


@dataclass(frozen=True)
class WalkForwardVariantResult:
    parameters: Mapping[str, object]
    folds: tuple[WalkForwardFoldResult, ...]
    stability: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "parameters": dict(self.parameters),
            "folds": [item.to_dict() for item in self.folds],
            "stability": dict(self.stability),
        }


@dataclass(frozen=True)
class WalkForwardReport:
    status: str
    reason: str | None
    train_days: int
    test_days: int
    step_days: int
    selected_variant: None
    variants: tuple[WalkForwardVariantResult, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "train_days": self.train_days,
            "test_days": self.test_days,
            "step_days": self.step_days,
            "selected_variant": None,
            "variants": [item.to_dict() for item in self.variants],
        }


@dataclass
class _ExecutionDayLiquidity:
    execution_date: date
    capacity: int
    remaining: int
    pending_gap_stop: bool = False
    pending_gap_reference: float | None = None
    pending_stop_reason: str | None = None

    def consume(self, shares: int) -> None:
        if shares < 0 or shares > self.remaining:
            raise SwingBacktestError("execution liquidity budget is invalid")
        self.remaining -= shares


class BacktestAccount:
    """Mutable simulation account; public snapshots are immutable contexts."""

    def __init__(
        self,
        initial_cash: float,
        trading: TradingMetadata,
        config: SwingStrategyConfig,
        *,
        buy_fee_rate: float = constants.BUY_COMMISSION_RATE,
        sell_fee_rate: float = constants.SELL_COMMISSION_RATE,
        minimum_fee: float = constants.MINIMUM_COMMISSION_CNY,
        slippage_rate: float = constants.SLIPPAGE_RATE,
        half_spread_ticks: float = constants.DEFAULT_HALF_SPREAD_TICKS,
    ) -> None:
        self.initial_cash = _finite(initial_cash, "initial_cash", positive=True)
        _validate_trading(trading)
        if type(config) is not SwingStrategyConfig:
            raise SwingBacktestError("config must be SwingStrategyConfig")
        self.trading = trading
        self.config = config
        self.buy_fee_rate = _finite(buy_fee_rate, "buy_fee_rate")
        self.sell_fee_rate = _finite(sell_fee_rate, "sell_fee_rate")
        self.minimum_fee = _finite(minimum_fee, "minimum_fee")
        self.slippage_rate = _finite(slippage_rate, "slippage_rate")
        self.half_spread_ticks = _finite(
            half_spread_ticks, "half_spread_ticks",
        )
        if self.buy_fee_rate > 1 or self.sell_fee_rate > 1 or self.slippage_rate > 1:
            raise SwingBacktestError("rates must not exceed 1")
        self.cash = self.initial_cash
        self.shares = 0
        self.trades: list[SwingFill] = []
        self.rejections: list[SwingRejection] = []
        self.round_trips: list[CompletedRoundTrip] = []
        self._lots: list[tuple[int, int]] = []
        self._average_cost_adjusted = 0.0
        self._initial_risk_adjusted = 0.0
        self._entry_date: date | None = None
        self._entry_index: int | None = None
        self._highest_adjusted_close = 0.0
        self._hard_stop_adjusted = 0.0
        self._first_reduction_completed = False
        self._cycle_cash_flow = 0.0
        self._last_mark_price = 0.0
        self._last_adjusted_close = 0.0
        self._last_index = -1
        self._last_stop_date: date | None = None
        self._equity_curve: list[float] = []
        self._utilization: list[float] = []
        self._blocked_counts: dict[str, int] = {}

    def _sellable(self, execution_index: int) -> int:
        delay = 0 if self.trading.intraday_turnaround else self.trading.sellable_delay_days
        return sum(shares for acquired, shares in self._lots if acquired + delay <= execution_index)

    def context(
        self,
        *,
        next_trading_date: date | None = None,
        execution_index: int | None = None,
    ) -> PortfolioContext:
        index = self._last_index if execution_index is None else execution_index
        market_value = self.shares * self._last_mark_price
        equity = self.cash + market_value
        if equity <= 0.0:
            equity = max(self.cash, self.initial_cash * 1e-12)
        position = None
        if self.shares:
            position = PositionContext(
                shares=self.shares,
                sellable_shares=min(self.shares, self._sellable(index)),
                average_cost_adjusted=self._average_cost_adjusted,
                initial_risk_per_share_adjusted=self._initial_risk_adjusted,
                entry_trading_date=self._entry_date,
                highest_completed_adjusted_close=self._highest_adjusted_close,
                hard_stop_adjusted=self._hard_stop_adjusted,
                first_reduction_completed=self._first_reduction_completed,
            )
        planned_risk = 0.0
        if self.shares:
            scale = (
                self._last_mark_price / self._last_adjusted_close
                if self._last_adjusted_close > 0.0 else 1.0
            )
            stop_raw = self._hard_stop_adjusted * scale
            planned_risk = self.shares * max(0.0, self._last_mark_price - stop_raw)
        return PortfolioContext(
            equity=equity,
            cash=self.cash,
            current_etf_market_value=market_value,
            current_planned_risk_amount=planned_risk,
            lot_size=self.trading.lot_size,
            data_healthy=True,
            metadata_complete=True,
            ledger_healthy=True,
            tradable=True,
            next_trading_date=next_trading_date,
            last_stop_trading_date=self._last_stop_date,
            position=position,
        )

    def mark(self, bar: DailyBar, execution_index: int) -> None:
        self._last_mark_price = bar.close
        self._last_adjusted_close = bar.adjusted_close
        self._last_index = execution_index
        if self.shares:
            self._highest_adjusted_close = max(
                self._highest_adjusted_close, bar.adjusted_close,
            )
        equity = self.cash + self.shares * bar.close
        self._equity_curve.append(equity)
        self._utilization.append(
            0.0 if equity <= 0.0 else self.shares * bar.close / equity
        )

    def execution_day_liquidity(
        self,
        bar: DailyBar,
        prior_completed_volume: float,
    ) -> _ExecutionDayLiquidity:
        volume = _finite(prior_completed_volume, "prior_completed_volume")
        capacity = self._shared_volume_capacity(volume, bar.volume)
        return _ExecutionDayLiquidity(
            execution_date=bar.trading_date,
            capacity=capacity,
            remaining=capacity,
        )

    def _shared_volume_capacity(
        self,
        prior_completed_volume: float,
        execution_day_volume: float,
    ) -> int:
        return _lot_floor(
            min(prior_completed_volume, execution_day_volume)
            * self.trading.volume_unit_shares
            * self.config.max_volume_participation,
            self.trading.lot_size,
        )

    def execute(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
        raw_reference_price: float | None = None,
        forced_reason: str | None = None,
        known_volume: float | None = None,
        execution_phase: str = "NEXT_OPEN",
        liquidity: _ExecutionDayLiquidity | None = None,
        portfolio_equity_override: float | None = None,
        portfolio_market_value_override: float | None = None,
        portfolio_risk_override: float | None = None,
    ) -> SwingFill | None:
        if type(decision) is not SwingDecision or type(bar) is not DailyBar:
            raise SwingBacktestError("execute requires SwingDecision and DailyBar")
        if forced_reason is not None and forced_reason not in {
            "GAP_THROUGH_STOP", "STOP_EXIT",
        }:
            raise SwingBacktestError("forced_reason is invalid")
        if execution_phase not in {
            "NEXT_OPEN", "OPEN_GAP_STOP", "INTRADAY_STOP",
        }:
            raise SwingBacktestError("execution_phase is invalid")
        overrides = (
            portfolio_equity_override,
            portfolio_market_value_override,
            portfolio_risk_override,
        )
        if any(value is not None for value in overrides):
            if any(value is None for value in overrides):
                raise SwingBacktestError(
                    "portfolio execution overrides must be supplied together",
                )
            portfolio_equity_override = _finite(
                portfolio_equity_override,
                "portfolio_equity_override",
                positive=True,
            )
            portfolio_market_value_override = _finite(
                portfolio_market_value_override,
                "portfolio_market_value_override",
            )
            portfolio_risk_override = _finite(
                portfolio_risk_override,
                "portfolio_risk_override",
            )
        if decision.state not in _BUY_STATES | _SELL_STATES:
            return None
        side = "BUY" if decision.state in _BUY_STATES else "SELL"
        reference_price = (
            bar.open if raw_reference_price is None
            else _finite(raw_reference_price, "raw_reference_price", positive=True)
        )
        if reference_price < bar.low or reference_price > bar.high:
            raise SwingBacktestError("raw_reference_price must be inside bar range")
        if decision.symbol != bar.symbol:
            self._reject(decision, bar, side, max(decision.planned_shares, 0), "SYMBOL")
            return None
        if (
            decision.as_of_trading_date is None
            or decision.as_of_trading_date >= bar.trading_date
            or (
                decision.valid_for_trading_date is not None
                and decision.valid_for_trading_date != bar.trading_date
            )
        ):
            self._reject(
                decision, bar, side, max(decision.planned_shares, 0), "SIGNAL_DATE",
            )
            return None
        requested = decision.planned_shares
        if type(requested) is not int or requested <= 0:
            self._reject(decision, bar, side, max(requested, 0), "INVALID_QUANTITY")
            return None
        rounded = _lot_floor(requested, self.trading.lot_size)
        if rounded <= 0:
            self._reject(decision, bar, side, requested, "LOT_SIZE")
            return None
        if bar.volume <= 0.0:
            self._reject(
                decision, bar, side, requested, "SUSPENDED_OR_ZERO_VOLUME",
            )
            return None
        if liquidity is not None:
            if liquidity.execution_date != bar.trading_date:
                raise SwingBacktestError(
                    "execution liquidity date does not match the bar",
                )
            capacity = liquidity.remaining
        else:
            available_volume = (
                bar.volume if known_volume is None
                else _finite(known_volume, "known_volume")
            )
            capacity = self._shared_volume_capacity(
                available_volume, bar.volume,
            )
        limit_blocked = (
            self._limit_locked(bar, side)
            if execution_phase == "INTRADAY_STOP"
            else self._open_limit_blocked(bar, side)
        )
        if limit_blocked:
            self._reject(decision, bar, side, requested, "LIMIT_LOCKED")
            return None
        if capacity <= 0:
            self._reject(decision, bar, side, requested, "VOLUME_PARTICIPATION")
            return None
        executable = min(rounded, capacity)
        limit_reason = "VOLUME_PARTICIPATION" if executable < rounded else None
        fill_price = _execution_price(
            reference_price,
            side,
            tick=self.trading.price_tick,
            half_spread_ticks=self.half_spread_ticks,
            slippage_rate=self.slippage_rate,
        )
        if not self._is_scale_transition(bar):
            lower_limit = _effective_limit_price(
                bar.previous_close,
                self.trading.price_limit_pct,
                self.trading.price_tick,
                "SELL",
            )
            upper_limit = _effective_limit_price(
                bar.previous_close,
                self.trading.price_limit_pct,
                self.trading.price_tick,
                "BUY",
            )
            if side == "BUY":
                fill_price = min(
                    fill_price,
                    bar.high if execution_phase == "INTRADAY_STOP" else upper_limit,
                    upper_limit,
                )
            else:
                fill_price = max(
                    fill_price,
                    bar.low if execution_phase == "INTRADAY_STOP" else lower_limit,
                    lower_limit,
                )
        elif execution_phase == "INTRADAY_STOP":
            if side == "BUY":
                fill_price = min(fill_price, bar.high)
            else:
                fill_price = max(fill_price, bar.low)
        execution_stop = self._execution_stop(decision, bar)
        if side == "BUY":
            if execution_stop is not None and (
                reference_price <= execution_stop or fill_price <= execution_stop
            ):
                self._reject(
                    decision, bar, side, requested,
                    "ENTRY_INVALIDATED_BY_GAP",
                )
                return None
            entry_high = self._execution_entry_high(decision, bar)
            if (
                entry_high is not None
                and reference_price > entry_high + self.trading.price_tick
            ):
                self._reject(
                    decision, bar, side, requested,
                    "ENTRY_GAP_ABOVE_ZONE",
                )
                return None
        if side == "BUY":
            affordable = self._affordable(fill_price)
            if affordable <= 0:
                self._reject(decision, bar, side, requested, "CASH")
                return None
            if affordable < executable:
                executable = affordable
                limit_reason = "CASH"
            risk_capacity = self._actual_buy_capacity(
                fill_price,
                reference_price,
                execution_stop,
                executable,
                portfolio_equity_override=portfolio_equity_override,
                portfolio_market_value_override=(
                    portfolio_market_value_override
                ),
                portfolio_risk_override=portfolio_risk_override,
            )
            if risk_capacity <= 0:
                self._reject(
                    decision, bar, side, requested, "ACTUAL_RISK_LIMIT",
                )
                return None
            if risk_capacity < executable:
                executable = risk_capacity
                limit_reason = "ACTUAL_RISK_LIMIT"
        else:
            if self.shares <= 0:
                self._reject(decision, bar, side, requested, "INVENTORY")
                return None
            sellable = _lot_floor(
                self._sellable(execution_index), self.trading.lot_size,
            )
            if sellable <= 0:
                self._reject(decision, bar, side, requested, "T_PLUS_ONE")
                return None
            if sellable < executable:
                executable = sellable
                limit_reason = "T_PLUS_ONE"
        executable = _lot_floor(executable, self.trading.lot_size)
        if executable <= 0:
            self._reject(decision, bar, side, requested, limit_reason or "LOT_SIZE")
            return None
        fee_rate = self.buy_fee_rate if side == "BUY" else self.sell_fee_rate
        notional = executable * fill_price
        fee = max(notional * fee_rate, self.minimum_fee)
        if side == "SELL" and self.cash + notional - fee < -1e-9:
            self._reject(
                decision, bar, side, requested,
                "INSUFFICIENT_CASH_FOR_SELL_FEE",
            )
            return None
        spread_cost, slippage = _execution_cost_parts(
            reference_price,
            fill_price,
            executable,
            side,
            tick=self.trading.price_tick,
            half_spread_ticks=self.half_spread_ticks,
        )
        reason = (
            forced_reason
            if forced_reason is not None
            else self._trade_reason(decision, bar, execution_stop)
        )
        fill = SwingFill(
            symbol=bar.symbol,
            side=side,
            requested_shares=requested,
            shares=executable,
            signal_date=decision.as_of_trading_date,
            execution_date=bar.trading_date,
            raw_reference_price=reference_price,
            fill_price=fill_price,
            fee=fee,
            spread_cost=spread_cost,
            slippage=slippage,
            planned_stop=execution_stop,
            reason=reason,
        )
        if side == "BUY":
            self._book_buy(fill, bar, execution_index)
        else:
            self._book_sell(fill, bar, execution_index)
        if liquidity is not None:
            liquidity.consume(executable)
        self.trades.append(fill)
        if executable < requested:
            self._reject(
                decision, bar, side, requested - executable,
                limit_reason or ("LOT_SIZE" if rounded < requested else "PARTIAL"),
                requested_total=requested,
            )
        self._last_index = max(self._last_index, execution_index)
        return fill

    def execute_open_gap_stop(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
        known_volume: float | None = None,
        liquidity: _ExecutionDayLiquidity | None = None,
    ) -> bool:
        """Phase A: execute a pre-existing stop known to be crossed at open."""
        if self.shares <= 0:
            return False
        execution_stop = self._effective_protective_stop(decision, bar)
        if execution_stop is None or bar.open > execution_stop:
            return False
        if liquidity is not None:
            liquidity.pending_gap_stop = True
            liquidity.pending_gap_reference = bar.open
            liquidity.pending_stop_reason = (
                "GAP_THROUGH_STOP"
                if bar.open < execution_stop else "STOP_EXIT"
            )
        protective = self._protective_decision(decision, execution_stop)
        gap = bar.open < execution_stop
        self.execute(
            protective,
            bar,
            execution_index=execution_index,
            raw_reference_price=bar.open,
            forced_reason="GAP_THROUGH_STOP" if gap else "STOP_EXIT",
            known_volume=known_volume,
            execution_phase="OPEN_GAP_STOP",
            liquidity=liquidity,
        )
        return True

    def execute_intraday_stop(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
        liquidity: _ExecutionDayLiquidity | None = None,
    ) -> bool:
        """Phase C: after open orders, sell inventory available at a touched stop."""
        if self.shares <= 0:
            return False
        execution_stop = self._effective_protective_stop(decision, bar)
        if liquidity is not None and liquidity.pending_gap_stop:
            if self._limit_locked(bar, "SELL"):
                return True
            reference = liquidity.pending_gap_reference
            if reference is None:
                raise SwingBacktestError(
                    "pending gap stop is missing its reference price",
                )
            reason = liquidity.pending_stop_reason
            if reason not in {"GAP_THROUGH_STOP", "STOP_EXIT"}:
                raise SwingBacktestError(
                    "pending gap stop is missing its classification",
                )
            protective = self._protective_decision(
                decision,
                execution_stop if execution_stop is not None else reference,
            )
            self.execute(
                protective,
                bar,
                execution_index=execution_index,
                raw_reference_price=reference,
                forced_reason=reason,
                execution_phase="INTRADAY_STOP",
                liquidity=liquidity,
            )
            return True
        if (
            execution_stop is None
            or bar.low > execution_stop
            or bar.high < execution_stop
        ):
            return False
        protective = self._protective_decision(decision, execution_stop)
        self.execute(
            protective,
            bar,
            execution_index=execution_index,
            raw_reference_price=execution_stop,
            forced_reason="STOP_EXIT",
            execution_phase="INTRADAY_STOP",
            liquidity=liquidity,
        )
        return True

    def execute_protective_stop(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
    ) -> bool:
        """Compatibility helper applying phase A, then phase C if needed."""
        liquidity = self.execution_day_liquidity(bar, bar.volume)
        opened = self.execute_open_gap_stop(
            decision, bar, execution_index=execution_index,
            liquidity=liquidity,
        )
        intraday = self.execute_intraday_stop(
            decision, bar, execution_index=execution_index,
            liquidity=liquidity,
        )
        return opened or intraday

    def _protective_decision(
        self,
        decision: SwingDecision,
        execution_stop: float,
    ) -> SwingDecision:
        evidence = dict(decision.evidence)
        evidence.update({
            "exit_any": True,
            "exit_hard_stop": True,
        })
        return SwingDecision(
            symbol=decision.symbol,
            strategy_version=decision.strategy_version,
            as_of_trading_date=decision.as_of_trading_date,
            state=SwingState.EXIT_CANDIDATE,
            trend_score=decision.trend_score,
            evidence=evidence,
            blocked_reasons=(),
            planned_entry_low=None,
            planned_entry_high=None,
            planned_stop=execution_stop,
            planned_shares=self.shares,
            planned_risk_rate=decision.planned_risk_rate,
            first_reduce_price=None,
            valid_for_trading_date=None,
        )

    def execute_protective_gap(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
    ) -> bool:
        """Backward-compatible name for protective open/intraday stop handling."""
        return self.execute_protective_stop(
            decision, bar, execution_index=execution_index,
        )

    def record_blocked_decision(self, decision: SwingDecision) -> None:
        """Count a technically actionable signal blocked before order creation."""
        if decision.state in _BUY_STATES | _SELL_STATES:
            return
        evidence = decision.evidence
        technically_actionable = any(
            evidence.get(key) is True
            for key in (
                "trial_technical_ok",
                "add_candidate_technical_ok",
                "reduce_candidate_technical_ok",
            )
        )
        if not technically_actionable:
            return
        categories: set[str] = set()
        for reason in decision.blocked_reasons:
            if "cash" in reason:
                categories.add("CASH")
            elif "risk" in reason:
                categories.add("RISK")
            elif "lot" in reason or "quantity" in reason:
                categories.add("LOT_SIZE")
            elif any(token in reason for token in (
                "data", "metadata", "ledger", "tradable", "calendar", "health",
            )):
                categories.add("UNAVAILABLE")
        for category in sorted(categories):
            self._blocked_counts[category] = (
                self._blocked_counts.get(category, 0) + 1
            )

    def _limit_locked(self, bar: DailyBar, side: str) -> bool:
        if self._is_scale_transition(bar):
            return False
        bound = _effective_limit_price(
            bar.previous_close,
            self.trading.price_limit_pct,
            self.trading.price_tick,
            side,
        )
        tolerance = _limit_equality_tolerance(
            bound, bar.open, bar.high, bar.low, bar.close,
        )
        return all(
            abs(value - bound) <= tolerance
            for value in (bar.open, bar.high, bar.low, bar.close)
        )

    def _open_limit_blocked(self, bar: DailyBar, side: str) -> bool:
        """Conservatively reject an order opened at its adverse price limit."""
        if self._is_scale_transition(bar):
            return False
        bound = _effective_limit_price(
            bar.previous_close,
            self.trading.price_limit_pct,
            self.trading.price_tick,
            side,
        )
        tolerance = _limit_equality_tolerance(bound, bar.open)
        return (
            bar.open >= bound - tolerance if side == "BUY"
            else bar.open <= bound + tolerance
        )

    def _is_scale_transition(self, bar: DailyBar) -> bool:
        if self._last_mark_price <= 0.0 or self._last_adjusted_close <= 0.0:
            return False
        prior_scale = self._last_mark_price / self._last_adjusted_close
        current_scale = bar.open / bar.adjusted_open
        return not math.isclose(
            current_scale, prior_scale, rel_tol=1e-9, abs_tol=1e-12,
        )

    def _affordable(self, fill_price: float) -> int:
        shares = _lot_floor(self.cash / fill_price, self.trading.lot_size)
        while shares > 0:
            notional = shares * fill_price
            fee = max(notional * self.buy_fee_rate, self.minimum_fee)
            if notional + fee <= self.cash + 1e-9:
                return shares
            shares -= self.trading.lot_size
        return 0

    def _actual_buy_capacity(
        self,
        fill_price: float,
        reference_price: float,
        execution_stop: float | None,
        maximum: int,
        *,
        portfolio_equity_override: float | None = None,
        portfolio_market_value_override: float | None = None,
        portfolio_risk_override: float | None = None,
    ) -> int:
        """Cap an opening order from actual fill, stop, equity and exposure."""
        if execution_stop is None or execution_stop <= 0.0:
            return 0
        lot = self.trading.lot_size
        candidate = _lot_floor(maximum, lot)
        existing_risk = self.shares * max(
            0.0, reference_price - execution_stop,
        )
        local_equity = self.cash + self.shares * reference_price
        equity_before = (
            local_equity
            if portfolio_equity_override is None
            else portfolio_equity_override
        )
        market_before = (
            self.shares * reference_price
            if portfolio_market_value_override is None
            else portfolio_market_value_override
        )
        portfolio_risk_before = (
            existing_risk
            if portfolio_risk_override is None
            else portfolio_risk_override
        )
        while candidate > 0:
            notional = candidate * fill_price
            fee = max(notional * self.buy_fee_rate, self.minimum_fee)
            cash_after = self.cash - notional - fee
            adverse_cost = candidate * max(0.0, fill_price - reference_price)
            equity_after = equity_before - fee - adverse_cost
            new_risk = candidate * max(0.0, fill_price - execution_stop)
            symbol_risk = existing_risk + new_risk
            total_risk = portfolio_risk_before + new_risk
            symbol_market_value = (
                self.shares + candidate
            ) * reference_price
            total_market_value = market_before + candidate * reference_price
            if (
                cash_after >= -1e-9
                and equity_after > 0.0
                and symbol_market_value <= (
                    equity_after * self.config.max_symbol_weight + 1e-9
                )
                and total_market_value <= (
                    equity_after * self.config.max_equity_weight + 1e-9
                )
                and symbol_risk <= (
                    equity_after * self.config.risk_per_trade + 1e-9
                )
                and total_risk <= (
                    equity_after * self.config.max_portfolio_risk + 1e-9
                )
            ):
                return candidate
            candidate -= lot
        return 0

    def _current_stop_raw(self, bar: DailyBar) -> float | None:
        if self._hard_stop_adjusted <= 0.0:
            return None
        return self._hard_stop_adjusted * (bar.open / bar.adjusted_open)

    def _effective_protective_stop(
        self,
        decision: SwingDecision,
        bar: DailyBar,
    ) -> float | None:
        candidates = tuple(
            value for value in (
                self._current_stop_raw(bar),
                self._execution_stop(decision, bar),
            )
            if value is not None and value > 0.0
        )
        return max(candidates) if candidates else None

    def _book_buy(self, fill: SwingFill, bar: DailyBar, index: int) -> None:
        cash_delta = -(fill.fill_price * fill.shares + fill.fee)
        adjusted_scale = bar.adjusted_open / bar.open
        adjusted_cost = fill.fill_price * adjusted_scale
        prior = self.shares
        if prior == 0:
            self._entry_date = fill.execution_date
            self._entry_index = index
            self._cycle_cash_flow = 0.0
            self._first_reduction_completed = False
            stop_raw = fill.planned_stop
            if stop_raw is None or stop_raw >= fill.fill_price:
                stop_raw = max(self.trading.price_tick, fill.fill_price * 0.99)
            self._initial_risk_adjusted = max(
                self.trading.price_tick * adjusted_scale,
                (fill.fill_price - stop_raw) * adjusted_scale,
            )
            self._hard_stop_adjusted = stop_raw * adjusted_scale
            self._highest_adjusted_close = bar.adjusted_close
        self._average_cost_adjusted = (
            self._average_cost_adjusted * prior + adjusted_cost * fill.shares
        ) / (prior + fill.shares)
        if fill.planned_stop is not None:
            self._hard_stop_adjusted = max(
                self._hard_stop_adjusted, fill.planned_stop * adjusted_scale,
            )
        self.shares += fill.shares
        self.cash += cash_delta
        self._normalize_cash()
        self._cycle_cash_flow += cash_delta
        self._lots.append((index, fill.shares))

    def _book_sell(self, fill: SwingFill, bar: DailyBar, index: int) -> None:
        proceeds = fill.fill_price * fill.shares - fill.fee
        self.cash += proceeds
        self._normalize_cash()
        self._cycle_cash_flow += proceeds
        self.shares -= fill.shares
        remaining = fill.shares
        new_lots: list[tuple[int, int]] = []
        for acquired, shares in self._lots:
            taken = min(shares, remaining)
            shares -= taken
            remaining -= taken
            if shares:
                new_lots.append((acquired, shares))
        self._lots = new_lots
        if self.shares == 0:
            holding = max(0, index - self._entry_index)
            self.round_trips.append(CompletedRoundTrip(
                entry_date=self._entry_date,
                exit_date=fill.execution_date,
                net_pnl=self._cycle_cash_flow,
                holding_days=holding,
            ))
            if fill.reason in {"GAP_THROUGH_STOP", "STOP_EXIT"}:
                self._last_stop_date = fill.execution_date
            self._average_cost_adjusted = 0.0
            self._initial_risk_adjusted = 0.0
            self._entry_date = None
            self._entry_index = None
            self._highest_adjusted_close = 0.0
            self._hard_stop_adjusted = 0.0
            self._first_reduction_completed = False
            self._cycle_cash_flow = 0.0
        elif fill.reason == "REDUCE":
            self._first_reduction_completed = True

    def _normalize_cash(self) -> None:
        tolerance = max(1e-9, math.ulp(max(1.0, abs(self.cash))) * 8)
        if -tolerance <= self.cash < 0.0:
            self.cash = 0.0
        if self.cash < 0.0:
            raise SwingBacktestError("cash became negative")

    @staticmethod
    def _execution_stop(decision: SwingDecision, bar: DailyBar) -> float | None:
        if decision.planned_stop is None:
            return None
        current_raw_scale = bar.open / bar.adjusted_open
        adjusted = decision.evidence.get("protective_stop_adjusted")
        if type(adjusted) in (int, float) and math.isfinite(float(adjusted)):
            return float(adjusted) * current_raw_scale
        signal_scale = decision.evidence.get("raw_scale")
        if (
            type(signal_scale) in (int, float)
            and math.isfinite(float(signal_scale))
            and float(signal_scale) > 0.0
        ):
            return decision.planned_stop / float(signal_scale) * current_raw_scale
        return decision.planned_stop

    @staticmethod
    def _execution_entry_high(
        decision: SwingDecision,
        bar: DailyBar,
    ) -> float | None:
        if decision.planned_entry_high is None:
            return None
        current_raw_scale = bar.open / bar.adjusted_open
        signal_scale = decision.evidence.get("raw_scale")
        if (
            type(signal_scale) in (int, float)
            and math.isfinite(float(signal_scale))
            and float(signal_scale) > 0.0
        ):
            return (
                decision.planned_entry_high / float(signal_scale)
                * current_raw_scale
            )
        return decision.planned_entry_high

    @staticmethod
    def _trade_reason(
        decision: SwingDecision,
        bar: DailyBar,
        execution_stop: float | None,
    ) -> str:
        if decision.state is SwingState.TRIAL_ENTRY_CANDIDATE:
            return "TRIAL_ENTRY"
        if decision.state is SwingState.ADD_CANDIDATE:
            return "ADD"
        if decision.state is SwingState.REDUCE_CANDIDATE:
            return "REDUCE"
        if (
            execution_stop is not None
            and bar.open < execution_stop
        ):
            return "GAP_THROUGH_STOP"
        if (
            decision.evidence.get("exit_hard_stop") is True
            or decision.evidence.get("exit_trailing_stop") is True
        ):
            return "STOP_EXIT"
        return "EXIT_SIGNAL"

    def _reject(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        side: str,
        rejected: int,
        reason: str,
        *,
        requested_total: int | None = None,
    ) -> None:
        self.rejections.append(SwingRejection(
            symbol=bar.symbol,
            side=side,
            signal_date=decision.as_of_trading_date,
            execution_date=bar.trading_date,
            requested_shares=(
                decision.planned_shares if requested_total is None else requested_total
            ),
            rejected_shares=max(0, rejected),
            reason=reason,
        ))


class SwingBacktester:
    def __init__(
        self,
        config: SwingStrategyConfig,
        trading: TradingMetadata,
        *,
        buy_fee_rate: float = constants.BUY_COMMISSION_RATE,
        sell_fee_rate: float = constants.SELL_COMMISSION_RATE,
        minimum_fee: float = constants.MINIMUM_COMMISSION_CNY,
        slippage_rate: float = constants.SLIPPAGE_RATE,
        half_spread_ticks: float = constants.DEFAULT_HALF_SPREAD_TICKS,
    ) -> None:
        if type(config) is not SwingStrategyConfig:
            raise SwingBacktestError("config must be SwingStrategyConfig")
        _validate_trading(trading)
        self.config = config
        self.trading = trading
        self.costs = {
            "buy_fee_rate": _finite(buy_fee_rate, "buy_fee_rate"),
            "sell_fee_rate": _finite(sell_fee_rate, "sell_fee_rate"),
            "minimum_fee": _finite(minimum_fee, "minimum_fee"),
            "slippage_rate": _finite(slippage_rate, "slippage_rate"),
            "half_spread_ticks": _finite(
                half_spread_ticks, "half_spread_ticks",
            ),
        }
        if any(self.costs[key] > 1.0 for key in (
            "buy_fee_rate", "sell_fee_rate", "slippage_rate",
        )):
            raise SwingBacktestError("rates must not exceed 1")
        all_config_fields = tuple(fields(SwingStrategyConfig))
        self._decision_config_key = tuple(
            (field.name, getattr(config, field.name))
            for field in all_config_fields
        )
        self._positionless_decision_config_key = tuple(
            item for item in self._decision_config_key
            if item[0] != "trailing_stop_atr"
        )

    def _strategy_parameters(self) -> dict[str, object]:
        return {
            field.name: getattr(self.config, field.name)
            for field in sorted(
                fields(SwingStrategyConfig), key=lambda item: item.name,
            )
        }

    def _execution_assumptions(self) -> dict[str, object]:
        return {
            "asset_type": self.trading.asset_type,
            "benchmark_liquidated_at_end": False,
            "benchmark_policy": (
                "same_initial_cash_first_executable_open_using_prior_completed_"
                "volume_buy_and_hold_to_end"
            ),
            "buy_fill_price_formula": (
                "ceil_to_tick((reference_price+half_spread_ticks*price_tick)"
                "*(1+slippage_rate))"
            ),
            "buy_fee_rate": self.costs["buy_fee_rate"],
            "corporate_action_policy": (
                "fail_closed_on_adjusted_raw_scale_change"
            ),
            "default_half_spread_ticks": constants.DEFAULT_HALF_SPREAD_TICKS,
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
            "exchange": self.trading.exchange,
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
            "half_spread_ticks": self.costs["half_spread_ticks"],
            "intraday_turnaround": self.trading.intraday_turnaround,
            "lot_size": self.trading.lot_size,
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
            "minimum_fee": self.costs["minimum_fee"],
            "price_limit_pct": self.trading.price_limit_pct,
            "price_limit_policy": (
                "round_half_up_theoretical_limit_to_price_tick;ulp_exact_"
                "boundary;open_reject_at_adverse_limit;intraday_reject_only_"
                "when_all_ohlc_equal_limit;cap_fill_to_effective_limit"
            ),
            "price_tick": self.trading.price_tick,
            "raw_adjusted_policy": (
                "signals_on_adjusted_prices_execution_on_raw_prices"
            ),
            "sell_fee_rate": self.costs["sell_fee_rate"],
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
            "sellable_delay_days": self.trading.sellable_delay_days,
            "signal_bar_policy": "completed_daily_bars_through_signal_date",
            "slippage_rate": self.costs["slippage_rate"],
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
            "volume_unit_shares": self.trading.volume_unit_shares,
        }

    def _restore_full_history_evidence(
        self,
        decision: SwingDecision,
        *,
        full_bar_count: int,
        signal_index: int,
        trading_date_indices: Mapping[date, int],
        last_stop_trading_date: date | None,
        has_position: bool = False,
    ) -> SwingDecision:
        evidence = dict(decision.evidence)
        evidence["bar_count"] = full_bar_count
        if (
            last_stop_trading_date is not None
            and not has_position
            and "cooldown_sessions_elapsed" in evidence
            and "cooldown_ok" in evidence
        ):
            stop_index = trading_date_indices.get(last_stop_trading_date)
            if stop_index is None:
                raise SwingBacktestError(
                    "last stop date is absent from validated backtest history",
                )
            elapsed = max(0, signal_index - stop_index)
            cooldown_ok = elapsed > self.config.cooldown_days
            evidence["cooldown_sessions_elapsed"] = elapsed
            evidence["cooldown_ok"] = cooldown_ok
            evidence["entry_hard_gates_ok"] = bool(
                evidence.get("entry_sizing_gates_ok")
                and cooldown_ok
                and evidence.get("calendar_validity_ok")
            )
        return replace(decision, evidence=evidence)

    def run_symbol(
        self,
        bars: Sequence[DailyBar],
        initial_cash: float,
    ) -> SwingBacktestResult:
        cash = _finite(initial_cash, "initial_cash", positive=True)
        try:
            normalized = self._validate_bars(bars)
        except _CorporateActionUnsupported as error:
            return self._unavailable_result(error.bars, cash)
        account = BacktestAccount(cash, self.trading, self.config, **self.costs)
        first_execution_index = self.config.minimum_daily_bars
        first_execution = normalized[first_execution_index]
        account._last_mark_price = normalized[first_execution_index - 1].close
        account._last_adjusted_close = normalized[
            first_execution_index - 1
        ].adjusted_close
        account._last_index = first_execution_index - 1
        lookback = strategy_lookback(self.config)
        trading_date_indices = {
            bar.trading_date: index for index, bar in enumerate(normalized)
        }
        for index in range(self.config.minimum_daily_bars - 1, len(normalized) - 1):
            execution_index = index + 1
            execution_bar = normalized[execution_index]
            context = account.context(
                next_trading_date=execution_bar.trading_date,
                execution_index=execution_index,
            )
            signal_start = max(0, index + 1 - lookback)
            decision = evaluate_swing(
                normalized[signal_start: index + 1], self.config, context,
            )
            decision = self._restore_full_history_evidence(
                decision,
                full_bar_count=index + 1,
                signal_index=index,
                trading_date_indices=trading_date_indices,
                last_stop_trading_date=context.last_stop_trading_date,
                has_position=context.position is not None,
            )
            prior_completed_volume = normalized[index].volume
            liquidity = account.execution_day_liquidity(
                execution_bar, prior_completed_volume,
            )
            open_stop_triggered = account.execute_open_gap_stop(
                decision,
                execution_bar,
                execution_index=execution_index,
                known_volume=prior_completed_volume,
                liquidity=liquidity,
            )
            if not open_stop_triggered:
                account.record_blocked_decision(decision)
                account.execute(
                    decision,
                    execution_bar,
                    execution_index=execution_index,
                    known_volume=prior_completed_volume,
                    execution_phase="NEXT_OPEN",
                    liquidity=liquidity,
                )
            account.execute_intraday_stop(
                decision,
                execution_bar,
                execution_index=execution_index,
                liquidity=liquidity,
            )
            account.mark(execution_bar, execution_index)
        ending_equity = account.cash + account.shares * normalized[-1].close
        benchmark = self._benchmark(normalized, cash, first_execution_index)
        metrics = self._metrics(account, cash, ending_equity)
        completed = len(account.round_trips)
        status = (
            "OK" if completed and benchmark is not None
            else "INSUFFICIENT_SAMPLE"
        )
        reason = (
            None if status == "OK"
            else (
                "BENCHMARK_UNAVAILABLE" if benchmark is None
                else "NO_COMPLETED_ROUND_TRIP"
            )
        )
        outperformance = (
            metrics.cumulative_return - benchmark.cumulative_return
            if completed and benchmark is not None else None
        )
        return SwingBacktestResult(
            schema_version=1,
            strategy_version=self.config.strategy_version,
            strategy_parameters=self._strategy_parameters(),
            execution_assumptions=self._execution_assumptions(),
            symbol=normalized[0].symbol,
            status=status,
            reason=reason,
            initial_cash=cash,
            cash=account.cash,
            ending_equity=ending_equity,
            start_date=first_execution.trading_date,
            end_date=normalized[-1].trading_date,
            trades=tuple(account.trades),
            rejections=tuple(account.rejections),
            round_trips=tuple(account.round_trips),
            open_position_shares=account.shares,
            uncompleted_leg_count=len(account._lots),
            benchmark=benchmark,
            outperformance=outperformance,
            metrics=metrics,
        )

    def _unavailable_result(
        self,
        bars: tuple[DailyBar, ...],
        initial_cash: float,
    ) -> SwingBacktestResult:
        first_execution_index = self.config.minimum_daily_bars
        return SwingBacktestResult(
            schema_version=1,
            strategy_version=self.config.strategy_version,
            strategy_parameters=self._strategy_parameters(),
            execution_assumptions=self._execution_assumptions(),
            symbol=bars[0].symbol,
            status="DATA_UNAVAILABLE",
            reason="CORPORATE_ACTION_UNSUPPORTED",
            initial_cash=initial_cash,
            cash=initial_cash,
            ending_equity=None,
            start_date=bars[first_execution_index].trading_date,
            end_date=bars[-1].trading_date,
            trades=(),
            rejections=(),
            round_trips=(),
            open_position_shares=0,
            uncompleted_leg_count=0,
            benchmark=None,
            outperformance=None,
            metrics=None,
        )

    def _validate_bars(self, bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
        if isinstance(bars, (str, bytes)):
            raise SwingBacktestError("bars must be a sequence of DailyBar")
        try:
            supplied = tuple(bars)
        except Exception as error:
            raise SwingBacktestError("bars must be materializable") from error
        minimum = self.config.minimum_daily_bars + 1
        if len(supplied) < minimum:
            raise SwingBacktestError(f"at least {minimum} daily bars are required")
        if any(type(bar) is not DailyBar for bar in supplied):
            raise SwingBacktestError("bars must contain only DailyBar")
        try:
            result = tuple(
                DailyBar.from_mapping(bar.to_dict()) for bar in supplied
            )
        except SwingDataError as error:
            raise SwingBacktestError(f"invalid daily bar: {error}") from error

        scales = tuple(bar.close / bar.adjusted_close for bar in result)
        uncertainties = tuple(
            self.trading.price_tick / bar.adjusted_close
            + (
                bar.close * self.trading.price_tick
                / (bar.adjusted_close * bar.adjusted_close)
            )
            for bar in result
        )
        for scale_index, (prior_scale, current_scale) in enumerate(
            zip(scales, scales[1:]), start=1,
        ):
            tolerance = max(
                1e-15,
                math.ulp(prior_scale) * 8,
                math.ulp(current_scale) * 8,
                uncertainties[scale_index - 1] + uncertainties[scale_index],
            )
            if abs(current_scale - prior_scale) > tolerance:
                raise _CorporateActionUnsupported(result)

        symbol = result[0].symbol
        validator = DailyBarValidator(set())
        metadata = EtfMetadata(
            symbol=symbol,
            name=symbol,
            index=IndexMetadata("000000", "BACKTEST", "LOCAL"),
            trading=self.trading,
        )
        previous_date: date | None = None
        for bar in result:
            if (
                type(bar.schema_version) is not int
                or bar.schema_version != 1
                or type(bar.symbol) is not str
                or len(bar.symbol) != 6
                or not bar.symbol.isascii()
                or not bar.symbol.isdigit()
                or type(bar.trading_date) is not date
                or type(bar.observed_at) is not datetime
                or bar.observed_at.tzinfo is None
                or bar.observed_at.utcoffset() is None
                or type(bar.source) is not str
                or not bar.source.strip()
                or type(bar.is_final) is not bool
            ):
                raise SwingBacktestError("daily bar scalar fields are invalid")
            if bar.symbol != symbol:
                raise SwingBacktestError("all bars must have the same symbol")
            if not bar.is_final:
                raise SwingBacktestError("all bars must be final")
            if previous_date is not None and bar.trading_date <= previous_date:
                raise SwingBacktestError("bar dates must be strictly increasing")
            previous_date = bar.trading_date
            values = (
                bar.open, bar.high, bar.low, bar.close, bar.previous_close,
                bar.adjusted_open, bar.adjusted_high, bar.adjusted_low,
                bar.adjusted_close,
            )
            if any(type(value) not in (int, float) or not math.isfinite(value)
                   or value <= 0.0 for value in values):
                raise SwingBacktestError("raw and adjusted prices must be positive finite")
            if not (bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high):
                raise SwingBacktestError("raw OHLC is invalid")
            if not (
                bar.adjusted_low <= min(bar.adjusted_open, bar.adjusted_close)
                <= max(bar.adjusted_open, bar.adjusted_close) <= bar.adjusted_high
            ):
                raise SwingBacktestError("adjusted OHLC is invalid")
            if type(bar.volume) not in (int, float) or not math.isfinite(bar.volume) or bar.volume < 0:
                raise SwingBacktestError("volume must be finite and nonnegative")
            if type(bar.amount) not in (int, float) or not math.isfinite(bar.amount) or bar.amount < 0:
                raise SwingBacktestError("amount must be finite and nonnegative")
            if (bar.volume == 0.0) != (bar.amount == 0.0):
                raise SwingBacktestError("volume and amount must both be zero or nonzero")
            scales = (
                bar.adjusted_open / bar.open,
                bar.adjusted_high / bar.high,
                bar.adjusted_low / bar.low,
                bar.adjusted_close / bar.close,
            )
            if max(scales) - min(scales) > max(1e-10, abs(scales[0]) * 1e-9):
                raise SwingBacktestError("adjusted/raw scale is inconsistent")
            try:
                validator.validate(bar, metadata)
            except SwingDataError as error:
                raise SwingBacktestError(
                    f"invalid daily bar: {error}",
                ) from error
        for prior, current in zip(result, result[1:]):
            tolerance = max(
                self.trading.price_tick,
                math.ulp(prior.close) * 4,
                math.ulp(current.previous_close) * 4,
            )
            if abs(current.previous_close - prior.close) > tolerance:
                raise SwingBacktestError("previous_close does not match prior raw close")
            lower = current.previous_close * (
                1.0 - self.trading.price_limit_pct
            )
            upper = current.previous_close * (
                1.0 + self.trading.price_limit_pct
            )
            if any(
                value < lower - tolerance or value > upper + tolerance
                for value in (
                    current.open, current.high, current.low, current.close,
                )
            ):
                raise SwingBacktestError(
                    "raw price exceeds configured price limit",
                )
        return result

    def _benchmark(
        self,
        bars: tuple[DailyBar, ...],
        initial_cash: float,
        index: int,
    ) -> SwingBenchmarkResult | None:
        for candidate_index in range(index, len(bars)):
            bar = bars[candidate_index]
            known_volume = bars[candidate_index - 1].volume
            limit_account = BacktestAccount(
                initial_cash, self.trading, self.config, **self.costs,
            )
            if candidate_index > 0:
                limit_account._last_mark_price = bars[candidate_index - 1].close
                limit_account._last_adjusted_close = (
                    bars[candidate_index - 1].adjusted_close
                )
            if (
                bar.volume <= 0.0
                or known_volume <= 0.0
                or limit_account._open_limit_blocked(bar, "BUY")
            ):
                continue
            price = _execution_price(
                bar.open,
                "BUY",
                tick=self.trading.price_tick,
                half_spread_ticks=self.costs["half_spread_ticks"],
                slippage_rate=self.costs["slippage_rate"],
            )
            price = min(
                price,
                _effective_limit_price(
                    bar.previous_close,
                    self.trading.price_limit_pct,
                    self.trading.price_tick,
                    "BUY",
                ),
            )
            maximum = _lot_floor(initial_cash / price, self.trading.lot_size)
            while maximum > 0:
                notional = maximum * price
                fee = max(
                    notional * self.costs["buy_fee_rate"],
                    self.costs["minimum_fee"],
                )
                if notional + fee <= initial_cash + 1e-9:
                    break
                maximum -= self.trading.lot_size
            capacity = limit_account._shared_volume_capacity(
                known_volume, bar.volume,
            )
            maximum = min(maximum, capacity)
            if maximum <= 0:
                continue
            fee = max(
                maximum * price * self.costs["buy_fee_rate"],
                self.costs["minimum_fee"],
            )
            spread_cost, slippage = _execution_cost_parts(
                bar.open,
                price,
                maximum,
                "BUY",
                tick=self.trading.price_tick,
                half_spread_ticks=self.costs["half_spread_ticks"],
            )
            cash = initial_cash - maximum * price - fee
            if cash < -1e-9:
                continue
            cash = max(0.0, cash)
            ending = cash + maximum * bars[-1].close
            return SwingBenchmarkResult(
                start_date=bar.trading_date,
                shares=maximum,
                cash=cash,
                ending_equity=ending,
                cumulative_return=ending / initial_cash - 1.0,
                fee=fee,
                spread_cost=spread_cost,
                slippage=slippage,
            )
        return None

    @staticmethod
    def _metrics(
        account: BacktestAccount,
        initial_cash: float,
        ending_equity: float,
    ) -> SwingBacktestMetrics:
        curve = [initial_cash, *account._equity_curve]
        cumulative = ending_equity / initial_cash - 1.0
        sessions = len(curve) - 1
        annualized = (
            None if sessions <= 0 or ending_equity <= 0.0
            else (ending_equity / initial_cash) ** (252.0 / sessions) - 1.0
        )
        peak = curve[0]
        drawdown = 0.0
        for equity in curve:
            peak = max(peak, equity)
            if peak > 0.0:
                drawdown = max(drawdown, (peak - equity) / peak)
        calmar = (
            annualized / drawdown
            if annualized is not None and drawdown > 0.0 else None
        )
        daily_returns = [
            curve[index] / curve[index - 1] - 1.0
            for index in range(1, len(curve))
            if curve[index - 1] > 0.0
        ]
        sharpe = None
        if len(daily_returns) >= 2:
            mean = sum(daily_returns) / len(daily_returns)
            variance = sum((item - mean) ** 2 for item in daily_returns) / (
                len(daily_returns) - 1
            )
            if variance > 0.0:
                sharpe = mean / math.sqrt(variance) * math.sqrt(252.0)
        pnls = [trip.net_pnl for trip in account.round_trips]
        profits = [pnl for pnl in pnls if pnl > 0.0]
        losses = [pnl for pnl in pnls if pnl < 0.0]
        win_rate = len(profits) / len(pnls) if pnls else None
        average_profit = sum(profits) / len(profits) if profits else None
        average_loss = sum(losses) / len(losses) if losses else None
        payoff = (
            average_profit / abs(average_loss)
            if average_profit is not None and average_loss is not None else None
        )
        average_holding = (
            sum(item.holding_days for item in account.round_trips)
            / len(account.round_trips)
            if account.round_trips else None
        )
        streak = None
        if pnls:
            longest = current = 0
            for pnl in pnls:
                current = current + 1 if pnl < 0.0 else 0
                longest = max(longest, current)
            streak = longest
        counts: dict[str, int] = {}
        for rejection in account.rejections:
            counts[rejection.reason] = counts.get(rejection.reason, 0) + 1
        for reason, count in account._blocked_counts.items():
            counts[reason] = counts.get(reason, 0) + count
        return SwingBacktestMetrics(
            cumulative_return=cumulative,
            annualized_return=annualized,
            maximum_drawdown=drawdown,
            calmar=calmar,
            sharpe=sharpe,
            win_rate=win_rate,
            average_profit=average_profit,
            average_loss=average_loss,
            payoff_ratio=payoff,
            average_holding_days=average_holding,
            utilization=(
                sum(account._utilization) / len(account._utilization)
                if account._utilization else 0.0
            ),
            longest_losing_streak=streak,
            fees=sum(item.fee for item in account.trades),
            spread_cost=sum(item.spread_cost for item in account.trades),
            slippage=sum(item.slippage for item in account.trades),
            rejection_counts=counts,
        )

    def rank_actions(
        self, actions: Sequence[PendingAction],
    ) -> tuple[PendingAction, ...]:
        """Apply the documented portfolio allocation order deterministically."""
        priority = {"EXIT": 0, "REDUCE": 1, "ADD": 2, "TRIAL_ENTRY": 3}
        materialized = tuple(actions)
        if any(type(item) is not PendingAction for item in materialized):
            raise SwingBacktestError("actions must contain PendingAction")
        return tuple(sorted(
            materialized,
            key=lambda action: (
                priority[action.kind], -action.trend_score, action.symbol,
            ),
        ))

    def _portfolio_trading(
        self,
        symbols: tuple[str, ...],
        trading_by_symbol: Mapping[str, TradingMetadata] | None,
    ) -> dict[str, TradingMetadata]:
        if trading_by_symbol is None:
            return {symbol: self.trading for symbol in symbols}
        if set(trading_by_symbol) != set(symbols):
            raise SwingBacktestError(
                "trading metadata must exactly cover portfolio symbols",
            )
        result: dict[str, TradingMetadata] = {}
        for symbol in symbols:
            trading = trading_by_symbol[symbol]
            _validate_trading(trading)
            result[symbol] = trading
        return result

    def _portfolio_unavailable(
        self,
        symbols: tuple[str, ...],
        initial_cash: float,
        reason: str,
        *,
        start: date | None = None,
        end: date | None = None,
        status: str = "DATA_UNAVAILABLE",
    ) -> PortfolioBacktestResult:
        return PortfolioBacktestResult(
            schema_version=1,
            scope="portfolio",
            strategy_version=self.config.strategy_version,
            status=status,
            reason=reason,
            symbols=symbols,
            initial_cash=initial_cash,
            cash=initial_cash,
            ending_equity=None,
            common_start_date=start,
            common_end_date=end,
            event_dates=(),
            trades=(),
            rejections=(),
            round_trips=(),
            open_position_shares={symbol: 0 for symbol in symbols},
            uncompleted_leg_count=0,
            max_equity_weight=0.0,
            max_planned_risk=0.0,
            metrics=None,
            baseline=None,
            baseline_weights={},
            outperformance=None,
            strategy_parameters=self._strategy_parameters(),
            execution_assumptions={
                **self._execution_assumptions(),
                "portfolio_cash_model": "ONE_SHARED_CASH_BALANCE",
            },
        )

    @staticmethod
    def _portfolio_position_risk(account: BacktestAccount) -> float:
        if account.shares <= 0 or account._last_mark_price <= 0.0:
            return 0.0
        scale = (
            account._last_mark_price / account._last_adjusted_close
            if account._last_adjusted_close > 0.0 else 1.0
        )
        stop = account._hard_stop_adjusted * scale
        return account.shares * max(0.0, account._last_mark_price - stop)

    @staticmethod
    def _portfolio_values(
        accounts: Mapping[str, BacktestAccount], cash: float,
        mark_prices: Mapping[str, float] | None = None,
    ) -> tuple[float, float, float]:
        market_value = sum(
            account.shares * (
                account._last_mark_price
                if mark_prices is None else mark_prices[symbol]
            )
            for symbol, account in accounts.items()
        )
        risk = 0.0
        for symbol, account in accounts.items():
            if account.shares <= 0:
                continue
            price = (
                account._last_mark_price
                if mark_prices is None else mark_prices[symbol]
            )
            scale = (
                account._last_mark_price / account._last_adjusted_close
                if account._last_adjusted_close > 0.0 else 1.0
            )
            stop = account._hard_stop_adjusted * scale
            risk += account.shares * max(0.0, price - stop)
        return cash + market_value, market_value, risk

    def _portfolio_context(
        self,
        account: BacktestAccount,
        accounts: Mapping[str, BacktestAccount],
        cash: float,
        execution_date: date,
        execution_index: int,
        mark_prices: Mapping[str, float] | None = None,
    ) -> PortfolioContext:
        equity, market_value, risk = self._portfolio_values(
            accounts, cash, mark_prices,
        )
        local = account.context(
            next_trading_date=execution_date,
            execution_index=execution_index,
        )
        return PortfolioContext(
            equity=max(equity, max(cash, 1e-9)),
            cash=cash,
            current_etf_market_value=market_value,
            current_planned_risk_amount=risk,
            lot_size=account.trading.lot_size,
            data_healthy=True,
            metadata_complete=True,
            ledger_healthy=True,
            tradable=True,
            next_trading_date=execution_date,
            last_stop_trading_date=local.last_stop_trading_date,
            position=local.position,
        )

    @staticmethod
    def _action_kind(decision: SwingDecision) -> str | None:
        return {
            SwingState.EXIT_CANDIDATE: "EXIT",
            SwingState.REDUCE_CANDIDATE: "REDUCE",
            SwingState.ADD_CANDIDATE: "ADD",
            SwingState.TRIAL_ENTRY_CANDIDATE: "TRIAL_ENTRY",
        }.get(decision.state)

    @staticmethod
    def _blocked_buy_kind(decision: SwingDecision) -> str | None:
        if decision.evidence.get("trial_technical_ok") is True:
            return "TRIAL_ENTRY"
        if decision.evidence.get("add_candidate_technical_ok") is True:
            return "ADD"
        return None

    @staticmethod
    def _portfolio_block_reason(decision: SwingDecision) -> str:
        reasons = decision.blocked_reasons
        if any("portfolio_risk" in reason for reason in reasons):
            return "PORTFOLIO_RISK_LIMIT"
        if any("total_exposure" in reason for reason in reasons):
            return "EQUITY_WEIGHT_LIMIT"
        if any("single_symbol" in reason for reason in reasons):
            return "SYMBOL_WEIGHT_LIMIT"
        if any("cash" in reason for reason in reasons):
            return "CASH"
        if any("lot" in reason or "quantity" in reason for reason in reasons):
            return "LOT_SIZE"
        return "CANDIDATE_GATE"

    def _portfolio_buy_cap(
        self,
        decision: SwingDecision,
        account: BacktestAccount,
        accounts: Mapping[str, BacktestAccount],
        cash: float,
        bar: DailyBar,
        mark_prices: Mapping[str, float],
    ) -> tuple[int, str | None]:
        requested = _lot_floor(decision.planned_shares, account.trading.lot_size)
        if requested <= 0:
            return 0, "LOT_SIZE"
        fill_price = _execution_price(
            bar.open,
            "BUY",
            tick=account.trading.price_tick,
            half_spread_ticks=account.half_spread_ticks,
            slippage_rate=account.slippage_rate,
        )
        if not account._is_scale_transition(bar):
            fill_price = min(fill_price, _effective_limit_price(
                bar.previous_close,
                account.trading.price_limit_pct,
                account.trading.price_tick,
                "BUY",
            ))
        stop = account._execution_stop(decision, bar)
        if stop is None or stop >= fill_price:
            return 0, "ACTUAL_RISK_LIMIT"
        equity, total_market, current_risk = self._portfolio_values(
            accounts, cash, mark_prices,
        )
        symbol_market = account.shares * mark_prices[bar.symbol]
        lot = account.trading.lot_size
        candidate = requested
        last_reason = "PORTFOLIO_RISK_LIMIT"
        while candidate > 0:
            fee = max(
                candidate * fill_price * account.buy_fee_rate,
                account.minimum_fee,
            )
            cash_after = cash - candidate * fill_price - fee
            adverse_cost = candidate * max(0.0, fill_price - bar.open)
            equity_after = equity - fee - adverse_cost
            symbol_after = symbol_market + candidate * bar.open
            total_after = total_market + candidate * bar.open
            risk_after = current_risk + candidate * max(0.0, fill_price - stop)
            if cash_after < -1e-9:
                last_reason = "CASH"
            elif symbol_after > equity_after * self.config.max_symbol_weight + 1e-9:
                last_reason = "SYMBOL_WEIGHT_LIMIT"
            elif total_after > equity_after * self.config.max_equity_weight + 1e-9:
                last_reason = "EQUITY_WEIGHT_LIMIT"
            elif risk_after > equity_after * self.config.max_portfolio_risk + 1e-9:
                last_reason = "PORTFOLIO_RISK_LIMIT"
            else:
                return candidate, None
            candidate -= lot
        return 0, last_reason

    @staticmethod
    def _execute_with_shared_cash(
        account: BacktestAccount,
        accounts: Mapping[str, BacktestAccount],
        shared_cash: float,
        operation: Any,
        mark_prices: Mapping[str, float] | None = None,
    ) -> tuple[float, object]:
        del accounts, mark_prices
        account.cash = shared_cash
        result = operation()
        if account.cash < -1e-9:
            raise SwingBacktestError("shared cash became negative")
        return max(0.0, account.cash), result

    @staticmethod
    def _cached_portfolio_decision(
        bars: tuple[DailyBar, ...],
        config: SwingStrategyConfig,
        context: PortfolioContext,
        cache: dict[tuple[object, ...], SwingDecision] | None,
        worker: SwingBacktester,
        *,
        full_bar_count: int,
        signal_index: int,
        trading_date_indices: Mapping[date, int],
    ) -> SwingDecision:
        if cache is None:
            decision = evaluate_swing(
                bars, config, context, _trusted_completed_bars=True,
            )
            return worker._restore_full_history_evidence(
                decision,
                full_bar_count=full_bar_count,
                signal_index=signal_index,
                trading_date_indices=trading_date_indices,
                last_stop_trading_date=context.last_stop_trading_date,
                has_position=context.position is not None,
            )
        config_key = (
            worker._positionless_decision_config_key
            if context.position is None else worker._decision_config_key
        )
        key = (
            bars[0].symbol, bars[0].trading_date, bars[-1].trading_date,
            len(bars), config_key, context, full_bar_count, signal_index,
        )
        decision = cache.get(key)
        if decision is None:
            decision = evaluate_swing(
                bars, config, context, _trusted_completed_bars=True,
            )
            decision = worker._restore_full_history_evidence(
                decision,
                full_bar_count=full_bar_count,
                signal_index=signal_index,
                trading_date_indices=trading_date_indices,
                last_stop_trading_date=context.last_stop_trading_date,
                has_position=context.position is not None,
            )
            cache[key] = decision
        return decision

    def run_portfolio(
        self,
        bars_by_symbol: Mapping[str, Sequence[DailyBar]],
        initial_cash: float,
        *,
        trading_by_symbol: Mapping[str, TradingMetadata] | None = None,
        _assume_validated: bool = False,
        _include_baseline: bool = True,
        _decision_cache: dict[tuple[object, ...], SwingDecision] | None = None,
    ) -> PortfolioBacktestResult:
        """Run all symbols on one chronological event stream and cash balance."""
        cash = _finite(initial_cash, "initial_cash", positive=True)
        if not isinstance(bars_by_symbol, Mapping) or not bars_by_symbol:
            raise SwingBacktestError("bars_by_symbol must be a nonempty mapping")
        symbols = tuple(sorted(bars_by_symbol))
        if any(
            type(symbol) is not str or len(symbol) != 6
            or not symbol.isascii() or not symbol.isdigit()
            for symbol in symbols
        ):
            raise SwingBacktestError("portfolio symbols must be six ASCII digits")
        trading_map = self._portfolio_trading(symbols, trading_by_symbol)
        normalized: dict[str, tuple[DailyBar, ...]] = {}
        try:
            for symbol in symbols:
                try:
                    supplied_count = len(bars_by_symbol[symbol])
                except Exception as error:
                    raise SwingBacktestError(
                        "portfolio history must be a sized sequence",
                    ) from error
                if supplied_count < self.config.minimum_daily_bars + 1:
                    return self._portfolio_unavailable(
                        symbols,
                        cash,
                        "INSUFFICIENT_COMPLETED_DAILY_BARS",
                        status="INSUFFICIENT_SAMPLE",
                    )
                if _assume_validated:
                    history = tuple(bars_by_symbol[symbol])
                    if not history or any(type(bar) is not DailyBar for bar in history):
                        raise SwingBacktestError("validated histories are invalid")
                else:
                    worker = SwingBacktester(
                        self.config, trading_map[symbol], **self.costs,
                    )
                    history = worker._validate_bars(bars_by_symbol[symbol])
                if history[0].symbol != symbol:
                    raise SwingBacktestError("portfolio history key mismatches symbol")
                normalized[symbol] = history
        except _CorporateActionUnsupported:
            return self._portfolio_unavailable(
                symbols, cash, "CORPORATE_ACTION_UNSUPPORTED",
            )
        except SwingBacktestError as error:
            return self._portfolio_unavailable(symbols, cash, str(error))

        common_start_candidate = max(
            normalized[symbol][self.config.minimum_daily_bars].trading_date
            for symbol in symbols
        )
        common_end_candidate = min(
            normalized[symbol][-1].trading_date for symbol in symbols
        )
        if common_start_candidate > common_end_candidate:
            return self._portfolio_unavailable(
                symbols, cash, "INSUFFICIENT_COMMON_HISTORY",
            )
        comparable_sets = tuple(
            {
                bar.trading_date for bar in normalized[symbol]
                if common_start_candidate <= bar.trading_date <= common_end_candidate
            }
            for symbol in symbols
        )
        if not comparable_sets[0] or any(
            dates != comparable_sets[0] for dates in comparable_sets[1:]
        ):
            return self._portfolio_unavailable(
                symbols,
                cash,
                "NON_CONTIGUOUS_COMMON_HISTORY",
                start=common_start_candidate,
                end=common_end_candidate,
            )
        common_dates = tuple(sorted(comparable_sets[0]))
        common_start, common_end = common_dates[0], common_dates[-1]
        indices = {
            symbol: {bar.trading_date: index for index, bar in enumerate(history)}
            for symbol, history in normalized.items()
        }
        accounts = {
            symbol: BacktestAccount(
                cash, trading_map[symbol], self.config, **self.costs,
            )
            for symbol in symbols
        }
        for symbol, account in accounts.items():
            first_index = indices[symbol][common_start]
            prior = normalized[symbol][first_index - 1]
            account._last_mark_price = prior.close
            account._last_adjusted_close = prior.adjusted_close
            account._last_index = first_index - 1

        event_dates: list[date] = []
        equity_curve: list[float] = []
        utilization: list[float] = []
        max_weight = 0.0
        max_risk_rate = 0.0
        workers = {
            symbol: SwingBacktester(
                self.config, trading_map[symbol], **self.costs,
            )
            for symbol in symbols
        }
        lookback = strategy_lookback(self.config)

        for execution_date in common_dates:
            decisions: dict[str, SwingDecision] = {}
            liquidities: dict[str, _ExecutionDayLiquidity] = {}
            signal_inputs: dict[str, tuple[int, int, tuple[DailyBar, ...]]] = {}
            for symbol in symbols:
                history = normalized[symbol]
                execution_index = indices[symbol][execution_date]
                signal_index = execution_index - 1
                signal_start = max(0, signal_index + 1 - lookback)
                context = self._portfolio_context(
                    accounts[symbol], accounts, cash,
                    execution_date, execution_index,
                )
                decision = self._cached_portfolio_decision(
                    history[signal_start:signal_index + 1],
                    self.config,
                    context,
                    _decision_cache,
                    workers[symbol],
                    full_bar_count=signal_index + 1,
                    signal_index=signal_index,
                    trading_date_indices=indices[symbol],
                )
                decisions[symbol] = decision
                signal_inputs[symbol] = (
                    signal_index,
                    execution_index,
                    history[signal_start:signal_index + 1],
                )
                liquidities[symbol] = accounts[symbol].execution_day_liquidity(
                    history[execution_index], history[signal_index].volume,
                )

            open_prices = {
                symbol: normalized[symbol][indices[symbol][execution_date]].open
                for symbol in symbols
            }

            gap_symbols: set[str] = set()
            for symbol in symbols:
                account = accounts[symbol]
                history = normalized[symbol]
                index = indices[symbol][execution_date]
                bar = history[index]
                cash, triggered = self._execute_with_shared_cash(
                    account, accounts, cash,
                    lambda account=account, decision=decisions[symbol], bar=bar,
                    index=index, liquidity=liquidities[symbol]:
                    account.execute_open_gap_stop(
                        decision, bar, execution_index=index,
                        known_volume=history[index - 1].volume,
                        liquidity=liquidity,
                    ),
                    mark_prices=open_prices,
                )
                if triggered:
                    gap_symbols.add(symbol)

            actions = []
            for symbol, decision in decisions.items():
                kind = self._action_kind(decision)
                if kind is None:
                    kind = self._blocked_buy_kind(decision)
                if kind is not None and symbol not in gap_symbols:
                    actions.append(PendingAction(
                        kind, symbol, decision.trend_score, decision,
                    ))
                else:
                    accounts[symbol].record_blocked_decision(decision)
            for action in self.rank_actions(actions):
                decision = action.decision
                assert decision is not None
                symbol = action.symbol
                account = accounts[symbol]
                history = normalized[symbol]
                index = indices[symbol][execution_date]
                bar = history[index]
                execution_overrides: dict[str, float] = {}
                if action.kind in {"ADD", "TRIAL_ENTRY"}:
                    signal_index, _, signal_bars = signal_inputs[symbol]
                    refreshed_context = self._portfolio_context(
                        account, accounts, cash, execution_date, index,
                        open_prices,
                    )
                    refreshed = self._cached_portfolio_decision(
                        signal_bars,
                        self.config,
                        refreshed_context,
                        _decision_cache,
                        workers[symbol],
                        full_bar_count=signal_index + 1,
                        signal_index=signal_index,
                        trading_date_indices=indices[symbol],
                    )
                    expected_state = (
                        SwingState.ADD_CANDIDATE
                        if action.kind == "ADD"
                        else SwingState.TRIAL_ENTRY_CANDIDATE
                    )
                    if refreshed.state is not expected_state:
                        account._reject(
                            decision,
                            bar,
                            "BUY",
                            max(decision.planned_shares, account.trading.lot_size),
                            self._portfolio_block_reason(refreshed),
                        )
                        continue
                    decision = refreshed
                    capped, reason = self._portfolio_buy_cap(
                        decision, account, accounts, cash, bar, open_prices,
                    )
                    if capped <= 0:
                        account._reject(
                            decision, bar, "BUY", decision.planned_shares,
                            reason or "PORTFOLIO_RISK_LIMIT",
                        )
                        continue
                    decision = replace(decision, planned_shares=capped)
                    portfolio_equity, portfolio_market, portfolio_risk = (
                        self._portfolio_values(accounts, cash, open_prices)
                    )
                    execution_overrides = {
                        "portfolio_equity_override": portfolio_equity,
                        "portfolio_market_value_override": portfolio_market,
                        "portfolio_risk_override": portfolio_risk,
                    }
                cash, _ = self._execute_with_shared_cash(
                    account, accounts, cash,
                    lambda account=account, decision=decision, bar=bar,
                    index=index, liquidity=liquidities[symbol],
                    execution_overrides=execution_overrides: account.execute(
                        decision,
                        bar,
                        execution_index=index,
                        known_volume=history[index - 1].volume,
                        execution_phase="NEXT_OPEN",
                        liquidity=liquidity,
                        **execution_overrides,
                    ),
                    mark_prices=open_prices,
                )

            for symbol in symbols:
                account = accounts[symbol]
                history = normalized[symbol]
                index = indices[symbol][execution_date]
                bar = history[index]
                cash, _ = self._execute_with_shared_cash(
                    account, accounts, cash,
                    lambda account=account, decision=decisions[symbol], bar=bar,
                    index=index, liquidity=liquidities[symbol]:
                    account.execute_intraday_stop(
                        decision, bar, execution_index=index, liquidity=liquidity,
                    ),
                    mark_prices=open_prices,
                )
            for symbol in symbols:
                index = indices[symbol][execution_date]
                accounts[symbol].mark(normalized[symbol][index], index)
            equity, market_value, risk = self._portfolio_values(accounts, cash)
            event_dates.append(execution_date)
            equity_curve.append(equity)
            weight = 0.0 if equity <= 0.0 else market_value / equity
            risk_rate = 0.0 if equity <= 0.0 else risk / equity
            utilization.append(weight)
            max_weight = max(max_weight, weight)
            max_risk_rate = max(max_risk_rate, risk_rate)

        ending_equity = equity_curve[-1]
        all_trades = tuple(sorted(
            (trade for account in accounts.values() for trade in account.trades),
            key=lambda item: (
                item.execution_date, 0 if item.side == "SELL" else 1,
                item.symbol, item.reason,
            ),
        ))
        all_rejections = tuple(sorted(
            (
                rejection
                for account in accounts.values()
                for rejection in account.rejections
            ),
            key=lambda item: (
                item.execution_date, item.symbol, item.side, item.reason,
                item.requested_shares, item.rejected_shares,
            ),
        ))
        round_trips = tuple(sorted(
            (
                PortfolioCompletedRoundTrip(
                    symbol=symbol,
                    entry_date=trip.entry_date,
                    exit_date=trip.exit_date,
                    net_pnl=trip.net_pnl,
                    holding_days=trip.holding_days,
                )
                for symbol in symbols for trip in accounts[symbol].round_trips
            ),
            key=lambda item: (
                item.exit_date, item.entry_date, item.net_pnl, item.holding_days,
            ),
        ))
        metrics = self._portfolio_metrics(
            cash_start=initial_cash,
            equity_curve=tuple(equity_curve),
            utilization=tuple(utilization),
            trades=all_trades,
            rejections=all_rejections,
            round_trips=round_trips,
        )
        if _include_baseline:
            baseline, weights = self._portfolio_baseline(
                normalized, trading_map, common_dates, initial_cash,
            )
        else:
            baseline, weights = None, {}
        completed = len(round_trips)
        status = (
            "OK"
            if completed > 0 and (
                not _include_baseline
                or (baseline is not None and baseline.status == "OK")
            )
            else "INSUFFICIENT_SAMPLE"
        )
        reason = None if status == "OK" else (
            "BASELINE_UNAVAILABLE"
            if _include_baseline and (baseline is None or baseline.status != "OK")
            else "NO_COMPLETED_ROUND_TRIP"
        )
        outperformance = None
        if status == "OK" and baseline is not None and baseline.ending_equity is not None:
            outperformance = (
                metrics.cumulative_return
                - (baseline.ending_equity / initial_cash - 1.0)
            )
        return PortfolioBacktestResult(
            schema_version=1,
            scope="portfolio",
            strategy_version=self.config.strategy_version,
            status=status,
            reason=reason,
            symbols=symbols,
            initial_cash=initial_cash,
            cash=max(0.0, cash),
            ending_equity=ending_equity,
            common_start_date=common_start,
            common_end_date=common_end,
            event_dates=tuple(event_dates),
            trades=all_trades,
            rejections=all_rejections,
            round_trips=round_trips,
            open_position_shares={
                symbol: accounts[symbol].shares for symbol in symbols
            },
            uncompleted_leg_count=sum(
                len(accounts[symbol]._lots) for symbol in symbols
            ),
            max_equity_weight=max_weight,
            max_planned_risk=max_risk_rate,
            metrics=metrics,
            baseline=baseline,
            baseline_weights=weights,
            outperformance=outperformance,
            strategy_parameters=self._strategy_parameters(),
            execution_assumptions={
                **self._execution_assumptions(),
                "portfolio_cash_model": "ONE_SHARED_CASH_BALANCE",
                "action_priority": "EXIT,REDUCE,ADD,TRIAL_ENTRY",
                "common_range_policy": "INTERSECTION_AFTER_WARMUP",
                "trading_metadata_by_symbol": {
                    symbol: trading_map[symbol].to_dict()
                    for symbol in symbols
                },
            },
        )

    def _portfolio_baseline(
        self,
        histories: Mapping[str, tuple[DailyBar, ...]],
        trading_map: Mapping[str, TradingMetadata],
        common_dates: tuple[date, ...],
        initial_cash: float,
    ) -> tuple[PortfolioBenchmarkResult | None, Mapping[str, float]]:
        symbols = tuple(sorted(histories))
        indices = {
            symbol: {
                bar.trading_date: index
                for index, bar in enumerate(histories[symbol])
            }
            for symbol in symbols
        }
        cash = initial_cash
        target = initial_cash / len(symbols)
        shares_by_symbol: dict[str, int] = {}
        trades: list[SwingFill] = []
        allocated: dict[str, float] = {}
        for symbol in symbols:
            trading = trading_map[symbol]
            checker = BacktestAccount(
                initial_cash, trading, self.config, **self.costs,
            )
            filled = False
            for trading_date in common_dates:
                index = indices[symbol][trading_date]
                bar = histories[symbol][index]
                prior = histories[symbol][index - 1]
                if (
                    bar.volume <= 0.0 or prior.volume <= 0.0
                    or checker._open_limit_blocked(bar, "BUY")
                ):
                    continue
                price = _execution_price(
                    bar.open,
                    "BUY",
                    tick=trading.price_tick,
                    half_spread_ticks=self.costs["half_spread_ticks"],
                    slippage_rate=self.costs["slippage_rate"],
                )
                price = min(price, _effective_limit_price(
                    bar.previous_close, trading.price_limit_pct,
                    trading.price_tick, "BUY",
                ))
                capacity = checker._shared_volume_capacity(
                    prior.volume, bar.volume,
                )
                requested = _lot_floor(target / price, trading.lot_size)
                shares = min(requested, capacity)
                shares = _lot_floor(shares, trading.lot_size)
                while shares > 0:
                    fee = max(
                        shares * price * self.costs["buy_fee_rate"],
                        self.costs["minimum_fee"],
                    )
                    if shares * price + fee <= cash + 1e-9:
                        break
                    shares -= trading.lot_size
                if shares <= 0:
                    continue
                fee = max(
                    shares * price * self.costs["buy_fee_rate"],
                    self.costs["minimum_fee"],
                )
                spread, slippage = _execution_cost_parts(
                    bar.open, price, shares, "BUY",
                    tick=trading.price_tick,
                    half_spread_ticks=self.costs["half_spread_ticks"],
                )
                cash -= shares * price + fee
                shares_by_symbol[symbol] = shares
                allocated[symbol] = shares * price + fee
                trades.append(SwingFill(
                    symbol=symbol,
                    side="BUY",
                    requested_shares=requested,
                    shares=shares,
                    signal_date=prior.trading_date,
                    execution_date=trading_date,
                    raw_reference_price=bar.open,
                    fill_price=price,
                    fee=fee,
                    spread_cost=spread,
                    slippage=slippage,
                    planned_stop=None,
                    reason="EQUAL_WEIGHT_BASELINE",
                ))
                filled = True
                break
            if not filled:
                return None, {}
        ending = cash + sum(
            shares_by_symbol[symbol] * histories[symbol][
                indices[symbol][common_dates[-1]]
            ].close
            for symbol in symbols
        )
        total_allocated = sum(allocated.values())
        weights = {
            symbol: allocated[symbol] / total_allocated for symbol in symbols
        }
        return PortfolioBenchmarkResult(
            status="OK",
            reason=None,
            initial_cash=initial_cash,
            cash=max(0.0, cash),
            ending_equity=ending,
            trades=tuple(trades),
            shares_by_symbol=shares_by_symbol,
        ), weights

    @staticmethod
    def _portfolio_metrics(
        *,
        cash_start: float,
        equity_curve: tuple[float, ...],
        utilization: tuple[float, ...],
        trades: tuple[SwingFill, ...],
        rejections: tuple[SwingRejection, ...],
        round_trips: tuple[PortfolioCompletedRoundTrip, ...],
    ) -> SwingBacktestMetrics:
        curve = (cash_start, *equity_curve)
        ending = curve[-1]
        cumulative = ending / cash_start - 1.0
        sessions = len(equity_curve)
        annualized = (
            None if sessions <= 0 or ending <= 0.0
            else (ending / cash_start) ** (252.0 / sessions) - 1.0
        )
        peak = curve[0]
        drawdown = 0.0
        for value in curve:
            peak = max(peak, value)
            if peak > 0.0:
                drawdown = max(drawdown, (peak - value) / peak)
        daily = [
            curve[index] / curve[index - 1] - 1.0
            for index in range(1, len(curve)) if curve[index - 1] > 0.0
        ]
        sharpe = None
        if len(daily) >= 2:
            mean = sum(daily) / len(daily)
            variance = sum((value - mean) ** 2 for value in daily) / (len(daily) - 1)
            if variance > 0.0:
                sharpe = mean / math.sqrt(variance) * math.sqrt(252.0)
        pnls = [item.net_pnl for item in round_trips]
        profits = [value for value in pnls if value > 0.0]
        losses = [value for value in pnls if value < 0.0]
        average_profit = sum(profits) / len(profits) if profits else None
        average_loss = sum(losses) / len(losses) if losses else None
        counts: dict[str, int] = {}
        for item in rejections:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        longest = None
        if pnls:
            running = best = 0
            for pnl in pnls:
                running = running + 1 if pnl < 0.0 else 0
                best = max(best, running)
            longest = best
        return SwingBacktestMetrics(
            cumulative_return=cumulative,
            annualized_return=annualized,
            maximum_drawdown=drawdown,
            calmar=(
                annualized / drawdown
                if annualized is not None and drawdown > 0.0 else None
            ),
            sharpe=sharpe,
            win_rate=len(profits) / len(pnls) if pnls else None,
            average_profit=average_profit,
            average_loss=average_loss,
            payoff_ratio=(
                average_profit / abs(average_loss)
                if average_profit is not None and average_loss is not None
                else None
            ),
            average_holding_days=(
                sum(item.holding_days for item in round_trips) / len(round_trips)
                if round_trips else None
            ),
            utilization=sum(utilization) / len(utilization) if utilization else 0.0,
            longest_losing_streak=longest,
            fees=sum(item.fee for item in trades),
            spread_cost=sum(item.spread_cost for item in trades),
            slippage=sum(item.slippage for item in trades),
            rejection_counts=counts,
        )

    @staticmethod
    def _fold_summary(result: PortfolioBacktestResult) -> dict[str, object]:
        metrics = result.metrics
        return {
            "status": result.status,
            "reason": result.reason,
            "metrics": None if metrics is None else metrics.to_dict(),
            "cumulative_return": (
                None if metrics is None else _clean(metrics.cumulative_return)
            ),
            "maximum_drawdown": (
                None if metrics is None else _clean(metrics.maximum_drawdown)
            ),
            "completed_round_trips": (
                0 if metrics is None else len(result.round_trips)
            ),
            "outperformance": result.outperformance,
        }

    def walk_forward(
        self,
        bars_by_symbol: Mapping[str, Sequence[DailyBar]],
        initial_cash: float,
        *,
        trading_by_symbol: Mapping[str, TradingMetadata] | None = None,
    ) -> WalkForwardReport:
        """Report every fixed neighborhood variant on untouched rolling tests."""
        cash = _finite(initial_cash, "initial_cash", positive=True)
        if not isinstance(bars_by_symbol, Mapping) or not bars_by_symbol:
            raise SwingBacktestError("bars_by_symbol must be a nonempty mapping")
        symbols = tuple(sorted(bars_by_symbol))
        trading_map = self._portfolio_trading(symbols, trading_by_symbol)
        normalized: dict[str, tuple[DailyBar, ...]] = {}
        try:
            for symbol in symbols:
                worker = SwingBacktester(
                    self.config, trading_map[symbol], **self.costs,
                )
                normalized[symbol] = worker._validate_bars(bars_by_symbol[symbol])
        except (_CorporateActionUnsupported, SwingBacktestError):
            return WalkForwardReport(
                "DATA_UNAVAILABLE", "INVALID_HISTORY",
                self.config.walk_forward_train_days,
                self.config.walk_forward_test_days,
                self.config.walk_forward_step_days,
                None, (),
            )
        overlap_start = max(normalized[symbol][0].trading_date for symbol in symbols)
        overlap_end = min(normalized[symbol][-1].trading_date for symbol in symbols)
        calendar_sets = tuple(
            {
                bar.trading_date for bar in normalized[symbol]
                if overlap_start <= bar.trading_date <= overlap_end
            }
            for symbol in symbols
        )
        if not calendar_sets[0] or any(
            values != calendar_sets[0] for values in calendar_sets[1:]
        ):
            return WalkForwardReport(
                "DATA_UNAVAILABLE", "NON_CONTIGUOUS_COMMON_HISTORY",
                self.config.walk_forward_train_days,
                self.config.walk_forward_test_days,
                self.config.walk_forward_step_days,
                None, (),
            )
        common_calendar = tuple(sorted(calendar_sets[0]))
        train_days = self.config.walk_forward_train_days
        test_days = self.config.walk_forward_test_days
        step_days = self.config.walk_forward_step_days
        fold_windows: list[tuple[tuple[date, ...], tuple[date, ...]]] = []
        offset = 0
        while offset + train_days + test_days <= len(common_calendar):
            train = common_calendar[offset:offset + train_days]
            test = common_calendar[
                offset + train_days:offset + train_days + test_days
            ]
            fold_windows.append((train, test))
            offset += step_days
        if not fold_windows:
            return WalkForwardReport(
                "INSUFFICIENT_SAMPLE", "NO_COMPLETE_WALK_FORWARD_FOLD",
                train_days, test_days, step_days, None, (),
            )
        indices = {
            symbol: {
                bar.trading_date: index
                for index, bar in enumerate(normalized[symbol])
            }
            for symbol in symbols
        }
        variants: list[WalkForwardVariantResult] = []
        decision_cache: dict[tuple[object, ...], SwingDecision] = {}
        for short in (18, 20, 22):
            for long in (55, 60, 65):
                for initial in (1.75, 2.0, 2.25):
                    for trailing in (2.75, 3.0, 3.25):
                        config = replace(
                            self.config,
                            short_ma_days=short,
                            long_ma_days=long,
                            minimum_daily_bars=max(
                                self.config.minimum_daily_bars,
                                long + self.config.long_ma_slope_lookback,
                            ),
                            initial_stop_atr=initial,
                            trailing_stop_atr=trailing,
                        )
                        worker = SwingBacktester(
                            config, self.trading, **self.costs,
                        )
                        folds: list[WalkForwardFoldResult] = []
                        test_returns: list[float] = []
                        for fold_index, (train_dates, test_dates) in enumerate(fold_windows):
                            train_set = set(train_dates)
                            train_histories = {
                                symbol: tuple(
                                    bar for bar in normalized[symbol]
                                    if bar.trading_date in train_set
                                )
                                for symbol in symbols
                            }
                            test_histories: dict[str, tuple[DailyBar, ...]] = {}
                            for symbol in symbols:
                                first_test_index = indices[symbol][test_dates[0]]
                                warmup = max(0, first_test_index - config.minimum_daily_bars)
                                last_test_index = indices[symbol][test_dates[-1]]
                                test_histories[symbol] = normalized[symbol][
                                    warmup:last_test_index + 1
                                ]
                            train_result = worker.run_portfolio(
                                train_histories, cash,
                                trading_by_symbol=trading_map,
                                _assume_validated=True,
                                _decision_cache=decision_cache,
                            )
                            test_result = worker.run_portfolio(
                                test_histories, cash,
                                trading_by_symbol=trading_map,
                                _assume_validated=True,
                                _decision_cache=decision_cache,
                            )
                            test_summary = self._fold_summary(test_result)
                            value = test_summary["cumulative_return"]
                            if type(value) in (int, float):
                                test_returns.append(float(value))
                            folds.append(WalkForwardFoldResult(
                                fold_index=fold_index,
                                train_start_date=train_dates[0],
                                train_end_date=train_dates[-1],
                                test_start_date=test_dates[0],
                                test_end_date=test_dates[-1],
                                train_bar_count=len(train_dates),
                                test_bar_count=len(test_dates),
                                train=self._fold_summary(train_result),
                                test=test_summary,
                            ))
                        stability = {
                            "fold_count": len(folds),
                            "test_ok_count": sum(
                                item.test.get("status") == "OK" for item in folds
                            ),
                            "mean_test_return": (
                                None if not test_returns
                                else _clean(sum(test_returns) / len(test_returns))
                            ),
                            "positive_test_fold_count": sum(
                                value > 0.0 for value in test_returns
                            ),
                        }
                        variants.append(WalkForwardVariantResult(
                            parameters={
                                "short_ma_days": short,
                                "long_ma_days": long,
                                "initial_stop_atr": initial,
                                "trailing_stop_atr": trailing,
                            },
                            folds=tuple(folds),
                            stability=stability,
                        ))
        return WalkForwardReport(
            "OK", None, train_days, test_days, step_days, None,
            tuple(variants),
        )


__all__ = [
    "BacktestAccount",
    "CompletedRoundTrip",
    "PendingAction",
    "PortfolioBacktestResult",
    "PortfolioBenchmarkResult",
    "PortfolioCompletedRoundTrip",
    "SwingBacktestError",
    "SwingBacktestMetrics",
    "SwingBacktestResult",
    "SwingBacktester",
    "SwingBenchmarkResult",
    "SwingFill",
    "SwingRejection",
    "WalkForwardFoldResult",
    "WalkForwardReport",
    "WalkForwardVariantResult",
    "strategy_lookback",
]
