"""Deterministic next-open backtesting for one swing-monitor ETF."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
    round_trips: tuple[CompletedRoundTrip, ...]
    open_position_shares: int
    uncompleted_leg_count: int
    benchmark: SwingBenchmarkResult | None
    outperformance: float | None
    metrics: SwingBacktestMetrics | None

    @property
    def completed_round_trips(self) -> int:
        return len(self.round_trips)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "strategy_version": self.strategy_version,
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

    def execute(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
        raw_reference_price: float | None = None,
        forced_reason: str | None = None,
    ) -> SwingFill | None:
        if type(decision) is not SwingDecision or type(bar) is not DailyBar:
            raise SwingBacktestError("execute requires SwingDecision and DailyBar")
        if forced_reason is not None and forced_reason not in {
            "GAP_THROUGH_STOP", "STOP_EXIT",
        }:
            raise SwingBacktestError("forced_reason is invalid")
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
            self._reject(decision, bar, side, requested, "ZERO_VOLUME")
            return None
        if self._limit_locked(bar, side):
            self._reject(decision, bar, side, requested, "LIMIT_LOCKED")
            return None
        capacity = _lot_floor(
            bar.volume * self.trading.volume_unit_shares
            * self.config.max_volume_participation,
            self.trading.lot_size,
        )
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
            lower_limit = bar.previous_close * (1.0 - self.trading.price_limit_pct)
            upper_limit = bar.previous_close * (1.0 + self.trading.price_limit_pct)
            if side == "BUY":
                fill_price = min(fill_price, bar.high, upper_limit)
            else:
                fill_price = max(fill_price, bar.low, lower_limit)
        elif side == "BUY":
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
        if side == "SELL" and notional <= fee:
            self._reject(
                decision, bar, side, requested, "NET_PROCEEDS_NONPOSITIVE",
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
        self.trades.append(fill)
        if executable < requested:
            self._reject(
                decision, bar, side, requested - executable,
                limit_reason or ("LOT_SIZE" if rounded < requested else "PARTIAL"),
                requested_total=requested,
            )
        self._last_index = max(self._last_index, execution_index)
        return fill

    def execute_protective_stop(
        self,
        decision: SwingDecision,
        bar: DailyBar,
        *,
        execution_index: int,
    ) -> bool:
        """Exit before lower-priority actions when the completed bar touches stop."""
        if self.shares <= 0 or decision.planned_stop is None:
            return False
        execution_stop = self._execution_stop(decision, bar)
        if execution_stop is None or bar.low > execution_stop:
            return False
        evidence = dict(decision.evidence)
        evidence.update({
            "exit_any": True,
            "exit_hard_stop": True,
            "protective_gap_at_open": True,
        })
        protective = SwingDecision(
            symbol=decision.symbol,
            strategy_version=decision.strategy_version,
            as_of_trading_date=decision.as_of_trading_date,
            state=SwingState.EXIT_CANDIDATE,
            evidence=evidence,
            blocked_reasons=(),
            planned_entry_low=None,
            planned_entry_high=None,
            planned_stop=decision.planned_stop,
            planned_shares=self.shares,
            planned_risk_rate=decision.planned_risk_rate,
            first_reduce_price=None,
            valid_for_trading_date=None,
        )
        gap = bar.open < execution_stop
        self.execute(
            protective,
            bar,
            execution_index=execution_index,
            raw_reference_price=bar.open if gap else execution_stop,
            forced_reason="GAP_THROUGH_STOP" if gap else "STOP_EXIT",
        )
        return True

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
        pct = self.trading.price_limit_pct
        tick = self.trading.price_tick
        bound = bar.previous_close * (1.0 + pct if side == "BUY" else 1.0 - pct)
        locked = max(bar.open, bar.high, bar.low, bar.close) - min(
            bar.open, bar.high, bar.low, bar.close,
        ) <= tick + math.ulp(bound) * 4
        at_bound = (
            bar.open >= bound - tick if side == "BUY"
            else bar.open <= bound + tick
        )
        return locked and at_bound

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
            protected = account.execute_protective_stop(
                decision, execution_bar, execution_index=execution_index,
            )
            if not protected:
                account.record_blocked_decision(decision)
                account.execute(
                    decision, execution_bar, execution_index=execution_index,
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
            limit_account = BacktestAccount(
                initial_cash, self.trading, self.config, **self.costs,
            )
            if candidate_index > 0:
                limit_account._last_mark_price = bars[candidate_index - 1].close
                limit_account._last_adjusted_close = (
                    bars[candidate_index - 1].adjusted_close
                )
            if bar.volume <= 0.0 or limit_account._limit_locked(bar, "BUY"):
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
                bar.high,
                bar.previous_close * (1.0 + self.trading.price_limit_pct),
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
            capacity = _lot_floor(
                bar.volume * self.trading.volume_unit_shares
                * self.config.max_volume_participation,
                self.trading.lot_size,
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


__all__ = [
    "BacktestAccount",
    "CompletedRoundTrip",
    "SwingBacktestError",
    "SwingBacktestMetrics",
    "SwingBacktestResult",
    "SwingBacktester",
    "SwingBenchmarkResult",
    "SwingFill",
    "SwingRejection",
    "strategy_lookback",
]
