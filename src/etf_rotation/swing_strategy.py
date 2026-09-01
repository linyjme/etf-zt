"""Pure indicators and formal state transitions for the swing monitor."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
import math
from types import MappingProxyType
from typing import TypeAlias

from .swing_config import SwingStrategyConfig
from .swing_data import DailyBar


EvidenceScalar: TypeAlias = float | int | bool | str | None
_LOT_BOUNDARY_ULPS = 16.0


class SwingStrategyError(ValueError):
    """Raised when a strategy context is malformed."""


class SwingState(StrEnum):
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    TREND_BLOCKED = "TREND_BLOCKED"
    UPTREND_WATCH = "UPTREND_WATCH"
    PULLBACK_WATCH = "PULLBACK_WATCH"
    TRIAL_ENTRY_CANDIDATE = "TRIAL_ENTRY_CANDIDATE"
    HOLDING = "HOLDING"
    ADD_CANDIDATE = "ADD_CANDIDATE"
    REDUCE_CANDIDATE = "REDUCE_CANDIDATE"
    EXIT_CANDIDATE = "EXIT_CANDIDATE"
    COOLDOWN = "COOLDOWN"


class IntradayOverlay(StrEnum):
    NONE = "NONE"
    APPROACHING_ENTRY_ZONE = "APPROACHING_ENTRY_ZONE"
    PREDEFINED_STOP_TOUCHED = "PREDEFINED_STOP_TOUCHED"
    INTRADAY_FEED_UNAVAILABLE = "INTRADAY_FEED_UNAVAILABLE"


def _finite_number(value: object, field: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float):
        raise SwingStrategyError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise SwingStrategyError(f"{field} must be a finite number") from error
    if not math.isfinite(number):
        raise SwingStrategyError(f"{field} must be a finite number")
    if positive and number <= 0.0:
        raise SwingStrategyError(f"{field} must be positive")
    if not positive and number < 0.0:
        raise SwingStrategyError(f"{field} must be nonnegative")
    return number


def _optional_number(
    value: object,
    field: str,
    *,
    positive: bool = False,
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, field, positive=positive)


def _strict_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise SwingStrategyError(f"{field} must be a built-in string")
    if not value.strip() and not (allow_empty and value == ""):
        raise SwingStrategyError(f"{field} must not be blank")
    return value


def _immutable_evidence(value: object) -> Mapping[str, EvidenceScalar]:
    if not isinstance(value, Mapping):
        raise SwingStrategyError("evidence must be a scalar mapping")
    try:
        materialized = dict(value)
    except Exception as error:
        raise SwingStrategyError("evidence must be a scalar mapping") from error
    copied: dict[str, EvidenceScalar] = {}
    for key, scalar in materialized.items():
        if type(key) is not str or type(scalar) not in (
            float, int, bool, str, type(None),
        ):
            raise SwingStrategyError("evidence must contain built-in JSON scalars")
        if type(scalar) is float and not math.isfinite(scalar):
            raise SwingStrategyError("evidence floats must be finite")
        copied[key] = scalar
    return MappingProxyType(copied)


def _blocked_reason_tuple(value: object) -> tuple[str, ...]:
    if type(value) not in (tuple, list):
        raise SwingStrategyError("blocked_reasons must be a tuple or list")
    reasons = tuple(value)
    if any(type(reason) is not str or not reason.strip() for reason in reasons):
        raise SwingStrategyError("blocked_reasons must contain built-in strings")
    return reasons


def _strict_date(value: object, field: str, *, optional: bool = False) -> date | None:
    if optional and value is None:
        return None
    if type(value) is not date:
        raise SwingStrategyError(f"{field} must be a date")
    return value


@dataclass(frozen=True)
class PositionContext:
    shares: int
    sellable_shares: int
    average_cost: float
    initial_risk_per_share: float
    entry_trading_date: date
    highest_completed_close: float
    hard_stop: float
    first_reduction_completed: bool

    def __post_init__(self) -> None:
        if type(self.shares) is not int or self.shares <= 0:
            raise SwingStrategyError("shares must be a positive integer")
        if (
            type(self.sellable_shares) is not int
            or self.sellable_shares < 0
            or self.sellable_shares > self.shares
        ):
            raise SwingStrategyError("sellable_shares must be an integer within shares")
        object.__setattr__(
            self, "average_cost",
            _finite_number(self.average_cost, "average_cost", positive=True),
        )
        object.__setattr__(self, "initial_risk_per_share", _finite_number(
            self.initial_risk_per_share, "initial_risk_per_share", positive=True,
        ))
        _strict_date(self.entry_trading_date, "entry_trading_date")
        object.__setattr__(self, "highest_completed_close", _finite_number(
            self.highest_completed_close, "highest_completed_close", positive=True,
        ))
        object.__setattr__(
            self, "hard_stop",
            _finite_number(self.hard_stop, "hard_stop", positive=True),
        )
        if type(self.first_reduction_completed) is not bool:
            raise SwingStrategyError("first_reduction_completed must be bool")


@dataclass(frozen=True)
class PortfolioContext:
    equity: float
    cash: float
    current_etf_market_value: float = 0.0
    current_planned_risk_amount: float = 0.0
    lot_size: int = 100
    data_healthy: bool = True
    metadata_complete: bool = True
    ledger_healthy: bool = True
    tradable: bool = True
    next_trading_date: date | None = None
    last_stop_trading_date: date | None = None
    position: PositionContext | None = None

    def __post_init__(self) -> None:
        for field, positive in (
            ("equity", True),
            ("cash", False),
            ("current_etf_market_value", False),
            ("current_planned_risk_amount", False),
        ):
            object.__setattr__(
                self, field,
                _finite_number(getattr(self, field), field, positive=positive),
            )
        if type(self.lot_size) is not int or self.lot_size <= 0:
            raise SwingStrategyError("lot_size must be a positive integer")
        for field in (
            "data_healthy", "metadata_complete", "ledger_healthy", "tradable",
        ):
            if type(getattr(self, field)) is not bool:
                raise SwingStrategyError(f"{field} must be bool")
        _strict_date(self.next_trading_date, "next_trading_date", optional=True)
        _strict_date(
            self.last_stop_trading_date, "last_stop_trading_date", optional=True,
        )
        if self.position is not None and type(self.position) is not PositionContext:
            raise SwingStrategyError("position must be PositionContext or None")

    @classmethod
    def empty(
        cls,
        equity: float,
        *,
        cash: float | None = None,
        lot_size: int = 100,
        data_healthy: bool = True,
        metadata_complete: bool = True,
        ledger_healthy: bool = True,
        tradable: bool = True,
        next_trading_date: date | None = None,
        last_stop_trading_date: date | None = None,
        current_etf_market_value: float = 0.0,
        current_planned_risk_amount: float = 0.0,
    ) -> PortfolioContext:
        """Build an explicitly positionless context with conservative defaults."""
        resolved_cash = equity if cash is None else cash
        return cls(
            equity=equity,
            cash=resolved_cash,
            current_etf_market_value=current_etf_market_value,
            current_planned_risk_amount=current_planned_risk_amount,
            lot_size=lot_size,
            data_healthy=data_healthy,
            metadata_complete=metadata_complete,
            ledger_healthy=ledger_healthy,
            tradable=tradable,
            next_trading_date=next_trading_date,
            last_stop_trading_date=last_stop_trading_date,
            position=None,
        )


@dataclass(frozen=True)
class SwingDecision:
    symbol: str
    strategy_version: str
    as_of_trading_date: date | None
    state: SwingState
    evidence: Mapping[str, EvidenceScalar]
    blocked_reasons: tuple[str, ...]
    planned_entry_low: float | None
    planned_entry_high: float | None
    planned_stop: float | None
    planned_shares: int
    planned_risk_rate: float
    first_reduce_price: float | None
    valid_for_trading_date: date | None

    def __post_init__(self) -> None:
        if type(self.state) is not SwingState:
            raise SwingStrategyError("state must be SwingState")
        allow_empty_symbol = self.state is SwingState.DATA_UNAVAILABLE
        _strict_text(self.symbol, "symbol", allow_empty=allow_empty_symbol)
        _strict_text(self.strategy_version, "strategy_version")
        _strict_date(
            self.as_of_trading_date, "as_of_trading_date", optional=True,
        )
        _strict_date(
            self.valid_for_trading_date, "valid_for_trading_date", optional=True,
        )
        if (
            self.as_of_trading_date is not None
            and self.valid_for_trading_date is not None
            and self.valid_for_trading_date <= self.as_of_trading_date
        ):
            raise SwingStrategyError(
                "valid_for_trading_date must be after as_of_trading_date",
            )
        entry_low = _optional_number(
            self.planned_entry_low, "planned_entry_low", positive=True,
        )
        entry_high = _optional_number(
            self.planned_entry_high, "planned_entry_high", positive=True,
        )
        if (entry_low is None) != (entry_high is None):
            raise SwingStrategyError("planned entry bounds must both be present")
        if entry_low is not None and entry_high is not None and entry_low > entry_high:
            raise SwingStrategyError("planned_entry_low must not exceed high")
        planned_stop = _optional_number(
            self.planned_stop, "planned_stop", positive=True,
        )
        first_reduce_price = _optional_number(
            self.first_reduce_price, "first_reduce_price", positive=True,
        )
        if type(self.planned_shares) is not int or self.planned_shares < 0:
            raise SwingStrategyError("planned_shares must be a nonnegative integer")
        planned_risk_rate = _finite_number(
            self.planned_risk_rate, "planned_risk_rate",
        )
        object.__setattr__(self, "planned_entry_low", entry_low)
        object.__setattr__(self, "planned_entry_high", entry_high)
        object.__setattr__(self, "planned_stop", planned_stop)
        object.__setattr__(self, "first_reduce_price", first_reduce_price)
        object.__setattr__(self, "planned_risk_rate", planned_risk_rate)
        object.__setattr__(self, "evidence", _immutable_evidence(self.evidence))
        object.__setattr__(
            self, "blocked_reasons", _blocked_reason_tuple(self.blocked_reasons),
        )


@dataclass(frozen=True)
class IntradayDecision:
    formal_state: SwingState
    overlay: IntradayOverlay
    price: float | None
    planned_entry_low: float | None
    planned_entry_high: float | None
    planned_stop: float | None
    evidence: Mapping[str, EvidenceScalar]

    def __post_init__(self) -> None:
        if type(self.formal_state) is not SwingState:
            raise SwingStrategyError("formal_state must be SwingState")
        if type(self.overlay) is not IntradayOverlay:
            raise SwingStrategyError("overlay must be IntradayOverlay")
        normalized = {
            field: _optional_number(getattr(self, field), field, positive=True)
            for field in (
                "price", "planned_entry_low", "planned_entry_high", "planned_stop",
            )
        }
        low = normalized["planned_entry_low"]
        high = normalized["planned_entry_high"]
        if (low is None) != (high is None):
            raise SwingStrategyError("planned entry bounds must both be present")
        if low is not None and high is not None and low > high:
            raise SwingStrategyError("planned_entry_low must not exceed high")
        for field, value in normalized.items():
            object.__setattr__(self, field, value)
        object.__setattr__(self, "evidence", _immutable_evidence(self.evidence))


@dataclass(frozen=True)
class _Metrics:
    ma20: float
    ma60: float
    ma60_prior: float
    previous_ma20: float
    atr: float
    raw_scale: float
    ma20_raw: float
    ma60_raw: float
    ma60_prior_raw: float
    atr_raw: float
    entry_low_adjusted: float
    entry_high_adjusted: float
    entry_low_raw: float
    entry_high_raw: float
    planned_stop: float
    per_share_risk: float
    first_reduce_price: float


def _mean(values: Sequence[float]) -> float:
    result = math.fsum(values) / len(values)
    if not math.isfinite(result):
        raise ArithmeticError("nonfinite mean")
    return result


def _product(left: float, right: float) -> float:
    result = left * right
    if not math.isfinite(result):
        raise ArithmeticError("nonfinite product")
    return result


def _compute_metrics(
    bars: tuple[DailyBar, ...], config: SwingStrategyConfig,
) -> _Metrics:
    closes = tuple(bar.adjusted_close for bar in bars)
    ma20 = _mean(closes[-config.short_ma_days:])
    ma60 = _mean(closes[-config.long_ma_days:])
    prior_end = len(closes) - config.long_ma_slope_lookback
    ma60_prior = _mean(closes[prior_end - config.long_ma_days:prior_end])
    previous_ma20 = _mean(closes[-config.short_ma_days - 1:-1])
    true_ranges: list[float] = []
    for index in range(len(bars) - config.atr_days, len(bars)):
        current = bars[index]
        previous_close = bars[index - 1].adjusted_close
        true_range = max(
            current.adjusted_high - current.adjusted_low,
            abs(current.adjusted_high - previous_close),
            abs(current.adjusted_low - previous_close),
        )
        if not math.isfinite(true_range) or true_range < 0.0:
            raise ArithmeticError("invalid true range")
        true_ranges.append(true_range)
    atr = _mean(true_ranges)
    latest = bars[-1]
    raw_scale = latest.close / latest.adjusted_close
    if not math.isfinite(raw_scale) or raw_scale <= 0.0:
        raise ArithmeticError("invalid raw scale")
    ma20_raw = _product(ma20, raw_scale)
    ma60_raw = _product(ma60, raw_scale)
    ma60_prior_raw = _product(ma60_prior, raw_scale)
    atr_raw = _product(atr, raw_scale)
    half_width = _product(config.entry_zone_atr_half_width, atr)
    entry_low_adjusted = ma20 - half_width
    entry_high_adjusted = ma20 + half_width
    entry_low_raw = _product(entry_low_adjusted, raw_scale)
    entry_high_raw = _product(entry_high_adjusted, raw_scale)
    stop_distance = _product(config.initial_stop_atr, atr_raw)
    planned_stop = entry_high_raw - stop_distance
    per_share_risk = entry_high_raw - planned_stop
    first_reduce_price = entry_high_raw + _product(
        config.reduce_profit_r, per_share_risk,
    )
    values = (
        entry_low_adjusted, entry_high_adjusted, entry_low_raw, entry_high_raw,
        planned_stop, per_share_risk, first_reduce_price,
    )
    if (
        any(not math.isfinite(value) for value in values)
        or planned_stop <= 0.0
        or per_share_risk <= 0.0
    ):
        raise ArithmeticError("unrepresentable strategy arithmetic")
    return _Metrics(
        ma20, ma60, ma60_prior, previous_ma20, atr, raw_scale,
        ma20_raw, ma60_raw, ma60_prior_raw, atr_raw,
        entry_low_adjusted, entry_high_adjusted, entry_low_raw, entry_high_raw,
        planned_stop, per_share_risk, first_reduce_price,
    )


def _candidate_symbol(values: tuple[object, ...]) -> str:
    for value in values:
        try:
            symbol = value.symbol if isinstance(value, DailyBar) else value.get("symbol")
        except Exception:
            continue
        if type(symbol) is str and symbol.strip():
            return symbol
    return ""


def _safe_error_text(error: Exception) -> str:
    try:
        return str(error)
    except Exception:
        return type(error).__name__


def _unavailable(
    config: SwingStrategyConfig,
    symbol: str,
    reason: str,
    *,
    count: int = 0,
    as_of: date | None = None,
    detail: str | None = None,
) -> SwingDecision:
    evidence: dict[str, EvidenceScalar] = {
        "sample_ok": False,
        "bar_count": count,
        "input_error": detail,
    }
    return SwingDecision(
        symbol=symbol,
        strategy_version=config.strategy_version,
        as_of_trading_date=as_of,
        state=SwingState.DATA_UNAVAILABLE,
        evidence=evidence,
        blocked_reasons=(reason,),
        planned_entry_low=None,
        planned_entry_high=None,
        planned_stop=None,
        planned_shares=0,
        planned_risk_rate=0.0,
        first_reduce_price=None,
        valid_for_trading_date=None,
    )


def _normalize_bars(
    bars: Sequence[DailyBar | Mapping[str, object]],
) -> tuple[tuple[DailyBar, ...] | None, tuple[object, ...], str | None, str | None]:
    try:
        materialized = tuple(bars)
    except Exception as error:
        return None, (), "invalid_bar_sequence", _safe_error_text(error)
    normalized: list[DailyBar] = []
    try:
        for item in materialized:
            if type(item) is DailyBar:
                normalized.append(DailyBar.from_mapping(item.to_dict()))
            elif isinstance(item, Mapping):
                normalized.append(DailyBar.from_mapping(item))
            else:
                raise ValueError("bar must be DailyBar or mapping")
    except Exception as error:
        return None, materialized, "invalid_daily_bar", _safe_error_text(error)
    if normalized:
        symbol = normalized[0].symbol
        if any(bar.symbol != symbol for bar in normalized):
            return None, materialized, "mixed_symbols", "one symbol is required"
        if any(
            current.trading_date <= previous.trading_date
            for previous, current in zip(normalized, normalized[1:])
        ):
            return None, materialized, "non_increasing_trading_dates", (
                "trading dates must be strictly increasing"
            )
    return tuple(normalized), materialized, None, None


def _health_reasons(portfolio: PortfolioContext) -> tuple[str, ...]:
    return tuple(
        field
        for field in (
            "data_healthy", "metadata_complete", "ledger_healthy", "tradable",
        )
        if not getattr(portfolio, field)
    )


def _local_ulp_tolerance(left: float, right: float) -> float:
    return max(math.ulp(left), math.ulp(right)) * _LOT_BOUNDARY_ULPS


def _snap_near_integer(value: float) -> float:
    nearest = float(round(value))
    if abs(value - nearest) <= _local_ulp_tolerance(value, nearest):
        return nearest
    return value


def _lot_floor(shares: float, lot_size: int) -> int:
    if not math.isfinite(shares) or shares <= 0.0:
        return 0
    lots = math.floor(_snap_near_integer(shares / lot_size))
    return max(0, lots * lot_size)


def _cap_allows_one_lot(shares: float, lot_size: int) -> bool:
    return _lot_floor(shares, lot_size) >= lot_size


def _entry_sizing(
    metrics: _Metrics,
    config: SwingStrategyConfig,
    portfolio: PortfolioContext,
) -> tuple[int, float, dict[str, EvidenceScalar], tuple[str, ...]]:
    equity = float(portfolio.equity)
    entry = metrics.entry_high_raw
    risk_budget = _product(equity, config.risk_per_trade)
    portfolio_risk_limit = _product(equity, config.max_portfolio_risk)
    remaining_portfolio_risk = max(
        0.0, portfolio_risk_limit - portfolio.current_planned_risk_amount,
    )
    symbol_value_room = max(
        0.0,
        _product(equity, config.max_symbol_weight),
    )
    total_value_room = max(
        0.0,
        _product(equity, config.max_equity_weight)
        - portfolio.current_etf_market_value,
    )
    caps = {
        "cash_cap_shares": portfolio.cash / entry,
        "single_symbol_cap_shares": symbol_value_room / entry,
        "total_exposure_cap_shares": total_value_room / entry,
        "trade_risk_cap_shares": risk_budget / metrics.per_share_risk,
        "portfolio_risk_cap_shares": remaining_portfolio_risk / metrics.per_share_risk,
    }
    if any(not math.isfinite(value) or value < 0.0 for value in caps.values()):
        raise ArithmeticError("unrepresentable entry sizing cap")
    selected = _lot_floor(min(caps.values()), portfolio.lot_size)
    planned_risk_rate = (
        _product(selected, metrics.per_share_risk) / equity if selected else 0.0
    )
    reasons: list[str] = list(_health_reasons(portfolio))
    reason_by_cap = {
        "cash_cap_shares": "cash_cap",
        "single_symbol_cap_shares": "single_symbol_cap",
        "total_exposure_cap_shares": "total_exposure_cap",
        "trade_risk_cap_shares": "trade_risk_cap",
        "portfolio_risk_cap_shares": "portfolio_risk_cap",
    }
    for key, value in caps.items():
        if not _cap_allows_one_lot(value, portfolio.lot_size):
            reasons.append(reason_by_cap[key])
    if selected < portfolio.lot_size:
        reasons.append("minimum_lot")
    evidence: dict[str, EvidenceScalar] = {
        "risk_budget_amount": risk_budget,
        "portfolio_risk_limit_amount": portfolio_risk_limit,
        "remaining_portfolio_risk_amount": remaining_portfolio_risk,
        **caps,
        "selected_shares": selected,
        "planned_risk_rate": planned_risk_rate,
        "cash_cap_ok": _cap_allows_one_lot(
            caps["cash_cap_shares"], portfolio.lot_size,
        ),
        "single_symbol_cap_ok": (
            _cap_allows_one_lot(
                caps["single_symbol_cap_shares"], portfolio.lot_size,
            )
        ),
        "total_exposure_cap_ok": (
            _cap_allows_one_lot(
                caps["total_exposure_cap_shares"], portfolio.lot_size,
            )
        ),
        "trade_risk_cap_ok": (
            _cap_allows_one_lot(
                caps["trade_risk_cap_shares"], portfolio.lot_size,
            )
        ),
        "portfolio_risk_cap_ok": (
            _cap_allows_one_lot(
                caps["portfolio_risk_cap_shares"], portfolio.lot_size,
            )
        ),
        "minimum_lot_ok": selected >= portfolio.lot_size,
        "health_gates_ok": not _health_reasons(portfolio),
        "entry_hard_gates_ok": not reasons,
    }
    return selected, planned_risk_rate, evidence, tuple(dict.fromkeys(reasons))


def _next_weekday(value: date) -> date:
    candidate = value + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _base_evidence(
    bars: tuple[DailyBar, ...],
    metrics: _Metrics,
    config: SwingStrategyConfig,
    portfolio: PortfolioContext,
) -> dict[str, EvidenceScalar]:
    latest = bars[-1]
    previous = bars[-2]
    close_distance = abs(latest.adjusted_close - metrics.ma20)
    trend_close = latest.adjusted_close > metrics.ma60
    trend_short = metrics.ma20 > metrics.ma60
    trend_rising = metrics.ma60 > metrics.ma60_prior
    low_touched = latest.adjusted_low <= metrics.ma20
    distance_ok = close_distance <= config.pullback_atr_distance * metrics.atr
    evidence: dict[str, EvidenceScalar] = {
        "sample_ok": True,
        "bar_count": len(bars),
        "trend_close_above_ma60": trend_close,
        "trend_ma20_above_ma60": trend_short,
        "trend_ma60_rising": trend_rising,
        "pullback_low_touched": low_touched,
        "pullback_distance_ok": distance_ok,
        "reclaim_close_above_ma20": latest.adjusted_close > metrics.ma20,
        "confirmation_above_previous_high": (
            latest.adjusted_close > previous.adjusted_high
        ),
        "anti_chase_ok": False,
        "ma20_adjusted": metrics.ma20,
        "ma60_adjusted": metrics.ma60,
        "ma60_prior_adjusted": metrics.ma60_prior,
        "previous_ma20_adjusted": metrics.previous_ma20,
        "atr14_adjusted": metrics.atr,
        "raw_scale": metrics.raw_scale,
        "ma20_raw": metrics.ma20_raw,
        "ma60_raw": metrics.ma60_raw,
        "ma60_prior_raw": metrics.ma60_prior_raw,
        "atr14_raw": metrics.atr_raw,
        "entry_zone_low_adjusted": metrics.entry_low_adjusted,
        "entry_zone_high_adjusted": metrics.entry_high_adjusted,
        "entry_zone_low_raw": metrics.entry_low_raw,
        "entry_zone_high_raw": metrics.entry_high_raw,
        "close_ma20_distance_adjusted": close_distance,
        "close_ma20_distance_raw": _product(close_distance, metrics.raw_scale),
        "initial_stop_raw": metrics.planned_stop,
        "per_share_risk_raw": metrics.per_share_risk,
        "data_healthy": portfolio.data_healthy,
        "metadata_complete": portfolio.metadata_complete,
        "ledger_healthy": portfolio.ledger_healthy,
        "tradable": portfolio.tradable,
        "health_gates_ok": not _health_reasons(portfolio),
        "entry_hard_gates_ok": False,
        "calendar_fallback_used": False,
        "invalid_next_trading_date": False,
        "cooldown_sessions_elapsed": 0,
        "exit_two_closes_below_ma20": False,
        "exit_close_below_ma60": False,
        "exit_trailing_stop": False,
        "exit_hard_stop": False,
        "exit_any": False,
        "reduce_profit_ok": False,
        "reduce_quantity_ok": False,
        "reduce_candidate_technical_ok": False,
        "add_profit_ok": False,
        "add_breakout_ok": False,
        "add_stop_to_cost_ok": False,
        "add_candidate_technical_ok": False,
        "trend_allowed": trend_close and trend_short and trend_rising,
        "trial_technical_ok": False,
        "cash_cap_shares": None,
        "single_symbol_cap_shares": None,
        "total_exposure_cap_shares": None,
        "trade_risk_cap_shares": None,
        "portfolio_risk_cap_shares": None,
        "cash_cap_ok": False,
        "single_symbol_cap_ok": False,
        "total_exposure_cap_ok": False,
        "trade_risk_cap_ok": False,
        "portfolio_risk_cap_ok": False,
        "minimum_lot_ok": False,
        "selected_shares": 0,
        "trailing_stop_raw": None,
        "protective_stop_raw": metrics.planned_stop,
    }
    return evidence


def _position_decision(
    bars: tuple[DailyBar, ...],
    metrics: _Metrics,
    config: SwingStrategyConfig,
    portfolio: PortfolioContext,
    evidence: dict[str, EvidenceScalar],
) -> SwingDecision:
    position = portfolio.position
    assert position is not None
    latest = bars[-1]
    previous = bars[-2]
    available_since_entry = tuple(
        bar.close for bar in bars if bar.trading_date >= position.entry_trading_date
    )
    highest = max((position.highest_completed_close, *available_since_entry))
    trailing_stop = highest - _product(config.trailing_stop_atr, metrics.atr_raw)
    protective_stop = max(position.hard_stop, trailing_stop)
    position_risk_per_share = max(0.0, latest.close - protective_stop)
    position_risk_amount = _product(position.shares, position_risk_per_share)
    position_risk_rate = position_risk_amount / portfolio.equity
    two_below = (
        previous.adjusted_close < metrics.previous_ma20
        and latest.adjusted_close < metrics.ma20
    )
    below_ma60 = latest.adjusted_close < metrics.ma60
    trailing_exit = latest.close <= trailing_stop
    hard_exit = latest.close <= position.hard_stop
    evidence.update({
        "highest_completed_close_raw": highest,
        "trailing_stop_raw": trailing_stop,
        "hard_stop_raw": position.hard_stop,
        "protective_stop_raw": protective_stop,
        "exit_two_closes_below_ma20": two_below,
        "exit_close_below_ma60": below_ma60,
        "exit_trailing_stop": trailing_exit,
        "exit_hard_stop": hard_exit,
        "exit_any": two_below or below_ma60 or trailing_exit or hard_exit,
        "position_risk_per_share": position_risk_per_share,
        "position_risk_amount": position_risk_amount,
        "position_risk_rate": position_risk_rate,
    })
    common = {
        "symbol": latest.symbol,
        "strategy_version": config.strategy_version,
        "as_of_trading_date": latest.trading_date,
        "evidence": evidence,
        "planned_entry_low": None,
        "planned_entry_high": None,
        "planned_stop": protective_stop,
        "planned_risk_rate": position_risk_rate,
        "first_reduce_price": (
            position.average_cost
            + _product(config.reduce_profit_r, position.initial_risk_per_share)
        ),
        "valid_for_trading_date": None,
    }
    health_reasons = _health_reasons(portfolio)
    if two_below or below_ma60 or trailing_exit or hard_exit:
        return SwingDecision(
            **common,
            state=SwingState.EXIT_CANDIDATE,
            blocked_reasons=(),
            planned_shares=position.sellable_shares,
        )

    reduce_price = position.average_cost + _product(
        config.reduce_profit_r, position.initial_risk_per_share,
    )
    reduce_profit_ok = (
        not position.first_reduction_completed and latest.close >= reduce_price
    )
    reduce_shares = _lot_floor(position.sellable_shares / 2.0, portfolio.lot_size)
    reduce_quantity_ok = reduce_shares >= portfolio.lot_size
    evidence["reduce_profit_ok"] = reduce_profit_ok
    evidence["reduce_quantity_ok"] = reduce_quantity_ok
    evidence["reduce_candidate_technical_ok"] = (
        reduce_profit_ok and reduce_quantity_ok
    )
    if reduce_profit_ok and reduce_quantity_ok and not health_reasons:
        return SwingDecision(
            **common,
            state=SwingState.REDUCE_CANDIDATE,
            blocked_reasons=(),
            planned_shares=reduce_shares,
        )

    add_profit_ok = (
        latest.close - position.average_cost
        >= _product(config.add_profit_r, position.initial_risk_per_share)
    )
    prior_breakout_high = max(
        bar.adjusted_high for bar in bars[-config.breakout_days - 1:-1]
    )
    add_breakout_ok = latest.adjusted_close > prior_breakout_high
    add_stop_ok = protective_stop >= position.average_cost
    evidence.update({
        "add_profit_ok": add_profit_ok,
        "add_breakout_ok": add_breakout_ok,
        "add_stop_to_cost_ok": add_stop_ok,
        "add_candidate_technical_ok": (
            add_profit_ok and add_breakout_ok and add_stop_ok
        ),
        "prior_breakout_high_adjusted": prior_breakout_high,
    })
    blocked: list[str] = list(health_reasons)
    if reduce_profit_ok and not reduce_quantity_ok:
        blocked.extend(("reduce_quantity_below_lot", "minimum_lot"))
    if reduce_profit_ok and health_reasons:
        blocked.append("reduce_health_gate")

    if add_profit_ok and add_breakout_ok and add_stop_ok:
        entry = latest.close
        symbol_value = _product(position.shares, latest.close)
        cash_cap = portfolio.cash / entry
        symbol_cap = max(
            0.0, _product(portfolio.equity, config.max_symbol_weight) - symbol_value,
        ) / entry
        total_cap = max(
            0.0,
            _product(portfolio.equity, config.max_equity_weight)
            - portfolio.current_etf_market_value,
        ) / entry
        per_share_add_risk = max(0.0, entry - protective_stop)
        remaining_risk = max(
            0.0,
            _product(portfolio.equity, config.max_portfolio_risk)
            - portfolio.current_planned_risk_amount,
        )
        trade_risk_room = max(
            0.0,
            _product(portfolio.equity, config.risk_per_trade)
            - position_risk_amount,
        )
        risk_cap = (
            remaining_risk / per_share_add_risk
            if per_share_add_risk > 0.0
            else max(cash_cap, symbol_cap, total_cap)
        )
        trade_risk_cap = (
            trade_risk_room / per_share_add_risk
            if per_share_add_risk > 0.0
            else max(cash_cap, symbol_cap, total_cap)
        )
        add_caps = {
            "cash_cap_shares": cash_cap,
            "single_symbol_cap_shares": symbol_cap,
            "total_exposure_cap_shares": total_cap,
            "trade_risk_cap_shares": trade_risk_cap,
            "portfolio_risk_cap_shares": risk_cap,
        }
        if any(
            not math.isfinite(value) or value < 0.0
            for value in add_caps.values()
        ):
            raise ArithmeticError("unrepresentable add sizing cap")
        add_shares = _lot_floor(min(add_caps.values()), portfolio.lot_size)
        evidence.update(add_caps)
        evidence["remaining_trade_risk_amount"] = trade_risk_room
        evidence["selected_shares"] = add_shares
        evidence["minimum_lot_ok"] = add_shares >= portfolio.lot_size
        reason_names = {
            "cash_cap_shares": "cash_cap",
            "single_symbol_cap_shares": "single_symbol_cap",
            "total_exposure_cap_shares": "total_exposure_cap",
            "trade_risk_cap_shares": "trade_risk_cap",
            "portfolio_risk_cap_shares": "portfolio_risk_cap",
        }
        for key, value in add_caps.items():
            if not _cap_allows_one_lot(value, portfolio.lot_size):
                blocked.append(reason_names[key])
        evidence["cash_cap_ok"] = _cap_allows_one_lot(
            cash_cap, portfolio.lot_size,
        )
        evidence["single_symbol_cap_ok"] = _cap_allows_one_lot(
            symbol_cap, portfolio.lot_size,
        )
        evidence["total_exposure_cap_ok"] = _cap_allows_one_lot(
            total_cap, portfolio.lot_size,
        )
        evidence["trade_risk_cap_ok"] = _cap_allows_one_lot(
            trade_risk_cap, portfolio.lot_size,
        )
        evidence["portfolio_risk_cap_ok"] = _cap_allows_one_lot(
            risk_cap, portfolio.lot_size,
        )
        if add_shares < portfolio.lot_size:
            blocked.append("minimum_lot")
        if not blocked:
            risk_rate = (
                position_risk_amount + add_shares * per_share_add_risk
            ) / portfolio.equity
            evidence["post_add_risk_rate"] = risk_rate
            common["planned_risk_rate"] = risk_rate
            return SwingDecision(
                **common,
                state=SwingState.ADD_CANDIDATE,
                blocked_reasons=(),
                planned_shares=add_shares,
            )
    return SwingDecision(
        **common,
        state=SwingState.HOLDING,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        planned_shares=0,
    )


def evaluate_swing(
    bars: Sequence[DailyBar | Mapping[str, object]],
    config: SwingStrategyConfig,
    portfolio: PortfolioContext,
) -> SwingDecision:
    """Evaluate completed bars without sorting, mutation, I/O, or broker actions."""
    if type(config) is not SwingStrategyConfig:
        raise SwingStrategyError("config must be SwingStrategyConfig")
    if type(portfolio) is not PortfolioContext:
        raise SwingStrategyError("portfolio must be PortfolioContext")
    normalized, materialized, reason, detail = _normalize_bars(bars)
    symbol = _candidate_symbol(materialized)
    if normalized is None:
        return _unavailable(
            config, symbol, reason or "invalid_daily_bars",
            count=len(materialized), detail=detail,
        )
    symbol = normalized[0].symbol if normalized else symbol
    as_of = normalized[-1].trading_date if normalized else None
    if len(normalized) < config.minimum_daily_bars:
        return _unavailable(
            config, symbol, "insufficient_daily_bars",
            count=len(normalized), as_of=as_of,
        )
    try:
        metrics = _compute_metrics(normalized, config)
    except (ArithmeticError, OverflowError, ValueError) as error:
        return _unavailable(
            config, symbol, "indicator_calculation_failed",
            count=len(normalized), as_of=as_of, detail=str(error),
        )
    latest = normalized[-1]
    previous = normalized[-2]
    try:
        evidence = _base_evidence(normalized, metrics, config, portfolio)
        pullback_limit = _product(config.pullback_atr_distance, metrics.atr)
        anti_chase_limit = metrics.ma20 + _product(
            config.anti_chase_atr_distance, metrics.atr,
        )
        if not math.isfinite(anti_chase_limit):
            raise ArithmeticError("nonfinite anti-chase limit")
        evidence.update({
            "pullback_distance_limit_adjusted": pullback_limit,
            "pullback_distance_ok": (
                abs(latest.adjusted_close - metrics.ma20) <= pullback_limit
            ),
            "anti_chase_limit_adjusted": anti_chase_limit,
            "anti_chase_ok": latest.adjusted_close <= anti_chase_limit,
        })
        evidence["trial_technical_ok"] = bool(
            evidence["trend_allowed"]
            and evidence["pullback_low_touched"]
            and evidence["reclaim_close_above_ma20"]
            and evidence["confirmation_above_previous_high"]
            and evidence["anti_chase_ok"]
        )
    except (ArithmeticError, OverflowError, ValueError) as error:
        return _unavailable(
            config, symbol, "indicator_calculation_failed",
            count=len(normalized), as_of=as_of, detail=_safe_error_text(error),
        )

    if portfolio.position is not None:
        try:
            return _position_decision(
                normalized, metrics, config, portfolio, evidence,
            )
        except (ArithmeticError, OverflowError, ValueError) as error:
            return _unavailable(
                config, symbol, "position_calculation_failed",
                count=len(normalized), as_of=as_of, detail=str(error),
            )

    try:
        sizing_shares, risk_rate, sizing_evidence, sizing_reasons = _entry_sizing(
            metrics, config, portfolio,
        )
        evidence.update(sizing_evidence)
    except (ArithmeticError, OverflowError, ValueError) as error:
        return _unavailable(
            config, symbol, "sizing_calculation_failed",
            count=len(normalized), as_of=as_of, detail=_safe_error_text(error),
        )

    if portfolio.last_stop_trading_date is not None:
        elapsed = sum(
            bar.trading_date > portfolio.last_stop_trading_date
            for bar in normalized
        )
        evidence["cooldown_sessions_elapsed"] = elapsed
        if elapsed <= config.cooldown_days:
            return SwingDecision(
                symbol=symbol,
                strategy_version=config.strategy_version,
                as_of_trading_date=as_of,
                state=SwingState.COOLDOWN,
                evidence=evidence,
                blocked_reasons=("cooldown_active",),
                planned_entry_low=metrics.entry_low_raw,
                planned_entry_high=metrics.entry_high_raw,
                planned_stop=metrics.planned_stop,
                planned_shares=0,
                planned_risk_rate=0.0,
                first_reduce_price=metrics.first_reduce_price,
                valid_for_trading_date=None,
            )

    trend = bool(
        evidence["trend_close_above_ma60"]
        and evidence["trend_ma20_above_ma60"]
        and evidence["trend_ma60_rising"]
    )
    common = {
        "symbol": symbol,
        "strategy_version": config.strategy_version,
        "as_of_trading_date": as_of,
        "evidence": evidence,
        "planned_entry_low": metrics.entry_low_raw,
        "planned_entry_high": metrics.entry_high_raw,
        "planned_stop": metrics.planned_stop,
        "first_reduce_price": metrics.first_reduce_price,
    }
    if not trend:
        return SwingDecision(
            **common,
            state=SwingState.TREND_BLOCKED,
            blocked_reasons=("trend_gate",),
            planned_shares=0,
            planned_risk_rate=0.0,
            valid_for_trading_date=None,
        )

    technical_trial = bool(
        evidence["pullback_low_touched"]
        and evidence["reclaim_close_above_ma20"]
        and evidence["confirmation_above_previous_high"]
        and evidence["anti_chase_ok"]
    )
    pullback = bool(
        evidence["pullback_low_touched"] or evidence["pullback_distance_ok"]
    )
    if technical_trial and not sizing_reasons and sizing_shares >= portfolio.lot_size:
        valid_for = portfolio.next_trading_date
        if valid_for is not None and valid_for <= latest.trading_date:
            evidence["invalid_next_trading_date"] = True
            return SwingDecision(
                **common,
                state=(
                    SwingState.PULLBACK_WATCH
                    if pullback else SwingState.UPTREND_WATCH
                ),
                blocked_reasons=("invalid_next_trading_date",),
                planned_shares=0,
                planned_risk_rate=0.0,
                valid_for_trading_date=None,
            )
        if valid_for is None:
            valid_for = _next_weekday(latest.trading_date)
            evidence["calendar_fallback_used"] = True
        return SwingDecision(
            **common,
            state=SwingState.TRIAL_ENTRY_CANDIDATE,
            blocked_reasons=(),
            planned_shares=sizing_shares,
            planned_risk_rate=risk_rate,
            valid_for_trading_date=valid_for,
        )
    blocked_reasons = sizing_reasons if technical_trial else ()
    return SwingDecision(
        **common,
        state=SwingState.PULLBACK_WATCH if pullback else SwingState.UPTREND_WATCH,
        blocked_reasons=blocked_reasons,
        planned_shares=0,
        planned_risk_rate=0.0,
        valid_for_trading_date=None,
    )


def evaluate_intraday_overlay(
    formal: SwingDecision,
    price: object,
    *,
    feed_healthy: bool = True,
    has_position: bool | None = None,
) -> IntradayDecision:
    """Layer ephemeral price information over an unchanged formal decision."""
    if type(formal) is not SwingDecision:
        raise SwingStrategyError("formal must be SwingDecision")
    inferred_position = formal.state in {
        SwingState.HOLDING,
        SwingState.ADD_CANDIDATE,
        SwingState.REDUCE_CANDIDATE,
        SwingState.EXIT_CANDIDATE,
    }
    valid_position_flag = has_position is None or type(has_position) is bool
    if has_position is None:
        actual_position = inferred_position
    elif type(has_position) is bool:
        actual_position = has_position
    else:
        actual_position = False
    valid_price = type(price) in (int, float)
    normalized_price: float | None = None
    if valid_price:
        try:
            candidate = float(price)
        except (OverflowError, ValueError):
            valid_price = False
        else:
            if math.isfinite(candidate) and candidate > 0.0:
                normalized_price = candidate
            else:
                valid_price = False
    healthy = type(feed_healthy) is bool and feed_healthy
    low = formal.planned_entry_low
    high = formal.planned_entry_high
    stop = formal.planned_stop
    local_ulp = max(
        (math.ulp(value) for value in (low, high) if value is not None),
        default=0.0,
    )
    supplied_tick = formal.evidence.get("price_tick")
    tick = 0.0
    if type(supplied_tick) in (int, float):
        try:
            normalized_tick = float(supplied_tick)
        except (OverflowError, ValueError):
            normalized_tick = 0.0
        if math.isfinite(normalized_tick) and normalized_tick > 0.0:
            tick = normalized_tick
    proximity = max(tick, local_ulp)
    overlay = IntradayOverlay.NONE
    stop_touched = False
    entry_near = False
    if not healthy or not valid_price or not valid_position_flag:
        overlay = IntradayOverlay.INTRADAY_FEED_UNAVAILABLE
    elif actual_position and stop is not None and normalized_price <= stop:
        overlay = IntradayOverlay.PREDEFINED_STOP_TOUCHED
        stop_touched = True
    elif (
        not actual_position
        and low is not None
        and high is not None
        and low - proximity <= normalized_price <= high + proximity
    ):
        overlay = IntradayOverlay.APPROACHING_ENTRY_ZONE
        entry_near = True
    evidence: dict[str, EvidenceScalar] = {
        "feed_healthy": healthy,
        "has_position": actual_position,
        "stop_touched": stop_touched,
        "entry_zone_near": entry_near,
        "entry_proximity": proximity,
    }
    return IntradayDecision(
        formal_state=formal.state,
        overlay=overlay,
        price=normalized_price,
        planned_entry_low=low,
        planned_entry_high=high,
        planned_stop=stop,
        evidence=evidence,
    )
