"""Read-only SWING_V11 rules, indicator evidence, and position actions.

The V11 layer is deliberately independent from the formal V1 strategy. It
produces an explainable shadow decision and never emits broker instructions.
Completed daily bars and completed weekly context are required inputs; missing
quality, environment, or position evidence fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import json
import math
from pathlib import Path
from typing import Any

from .swing_data import DailyBar
from .swing_indicators import calculate_indicator_context, calculate_indicator_snapshot


class V11State(StrEnum):
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    OBSERVE = "OBSERVE"
    TECHNICAL_CANDIDATE = "TECHNICAL_CANDIDATE"
    ACTION_CANDIDATE = "ACTION_CANDIDATE"
    POSITION_ACTION = "POSITION_ACTION"
    UNCERTAIN = "UNCERTAIN"


class V11Setup(StrEnum):
    NONE = "NONE"
    A_PULLBACK = "A_PULLBACK"
    B_BREAKOUT = "B_BREAKOUT"


@dataclass(frozen=True)
class V11Config:
    schema_version: int
    strategy_version: str
    target_order_cny: float
    max_order_cny: float
    shadow_risk_rate: float
    formal_risk_rate: float
    minimum_daily_bars: int
    weekly_confirmation_days: int
    pullback_window_min: int
    pullback_window_max: int
    box_days: int
    cooldown_sessions: int
    lot_size: int = 100


@dataclass(frozen=True)
class V11Context:
    """External evidence supplied by the service, not inferred by defaults."""

    data_quality: str = "VERIFIED"
    environment_state: str = "UNKNOWN"
    category: str | None = None
    correlation_group: str | None = None
    relative_strength_20: float | None = None
    snapshot_only: bool = False
    account_known: bool = True
    cash_cny: float = 0.0
    equity_cny: float = 0.0
    has_position: bool = False
    sellable_shares: int = 0
    price: float | None = None
    as_of_kind: str = "COMPLETED_DAILY"
    as_of_trading_date: str | None = None
    quasi_close: Mapping[str, object] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=dict)
    indicator: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class V11Decision:
    strategy_version: str
    state: V11State
    setup: V11Setup
    action: str
    executable: bool
    blocked_reasons: tuple[str, ...]
    evidence: Mapping[str, object]
    planned_shares: int = 0
    planned_notional_cny: float | None = None
    stop_price: float | None = None
    as_of_kind: str = "COMPLETED_DAILY"
    as_of_trading_date: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_version": self.strategy_version,
            "state": self.state.value,
            "setup": self.setup.value,
            "action": self.action,
            "executable": False,
            "blocked_reasons": list(self.blocked_reasons),
            "evidence": dict(self.evidence),
            "planned_shares": self.planned_shares,
            "planned_notional_cny": self.planned_notional_cny,
            "stop_price": self.stop_price,
            "as_of_kind": self.as_of_kind,
            "as_of_trading_date": self.as_of_trading_date,
        }


@dataclass(frozen=True)
class V11Position:
    shares: int
    sellable_shares: int
    entry_price: float
    stop_price: float
    current_price: float
    initial_risk_per_share: float
    profit_r: float = 0.0
    reduced: bool = False
    tracking_price: float | None = None

    def __post_init__(self) -> None:
        if type(self.shares) is not int or self.shares < 0:
            raise ValueError("shares must be a nonnegative integer")
        if type(self.sellable_shares) is not int or not 0 <= self.sellable_shares <= self.shares:
            raise ValueError("sellable_shares must be between zero and shares")
        for name in ("entry_price", "stop_price", "current_price", "initial_risk_per_share", "profit_r"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.entry_price <= 0.0 or self.stop_price <= 0.0 or self.current_price <= 0.0:
            raise ValueError("position prices must be positive")
        if self.initial_risk_per_share <= 0.0:
            raise ValueError("initial_risk_per_share must be positive")


_CONFIG_KEYS = frozenset({
    "schema_version", "strategy_version", "target_order_cny", "max_order_cny",
    "shadow_risk_rate", "formal_risk_rate", "minimum_daily_bars",
    "weekly_confirmation_days", "pullback_window_min", "pullback_window_max",
    "box_days", "cooldown_sessions", "lot_size",
})


def _finite_positive(payload: Mapping[str, object], key: str) -> float:
    value = payload[key]
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        raise ValueError(f"{key} must be finite")
    number = float(value)
    if number <= 0.0:
        raise ValueError(f"{key} must be positive")
    return number


def _positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload[key]
    if type(value) is not int or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def load_v11_config(path: Path) -> V11Config:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("V11 config is not valid JSON") from error
    if not isinstance(payload, dict) or frozenset(payload) != _CONFIG_KEYS:
        raise ValueError("V11 config keys are invalid")
    if payload["schema_version"] != 1 or payload["strategy_version"] != "SWING_V11_SHADOW":
        raise ValueError("V11 config version is invalid")
    target = _finite_positive(payload, "target_order_cny")
    maximum = _finite_positive(payload, "max_order_cny")
    if maximum < target:
        raise ValueError("max_order_cny must not be below target_order_cny")
    shadow = _finite_positive(payload, "shadow_risk_rate")
    formal = _finite_positive(payload, "formal_risk_rate")
    if shadow >= formal or formal > 1.0:
        raise ValueError("risk rates are invalid")
    minimum = _positive_int(payload, "minimum_daily_bars")
    weekly = _positive_int(payload, "weekly_confirmation_days")
    pullback_min = _positive_int(payload, "pullback_window_min")
    pullback_max = _positive_int(payload, "pullback_window_max")
    if pullback_min > pullback_max:
        raise ValueError("pullback window is invalid")
    box = _positive_int(payload, "box_days")
    cooldown = _positive_int(payload, "cooldown_sessions")
    lot_size = _positive_int(payload, "lot_size")
    return V11Config(
        schema_version=1,
        strategy_version="SWING_V11_SHADOW",
        target_order_cny=target,
        max_order_cny=maximum,
        shadow_risk_rate=shadow,
        formal_risk_rate=formal,
        minimum_daily_bars=minimum,
        weekly_confirmation_days=weekly,
        pullback_window_min=pullback_min,
        pullback_window_max=pullback_max,
        box_days=box,
        cooldown_sessions=cooldown,
        lot_size=lot_size,
    )


def _decision(
    state: V11State,
    config: V11Config,
    *,
    setup: V11Setup = V11Setup.NONE,
    action: str = "OBSERVE",
    reasons: Sequence[str] = (),
    evidence: Mapping[str, object] | None = None,
    context: V11Context,
    planned_shares: int = 0,
    planned_notional_cny: float | None = None,
    stop_price: float | None = None,
) -> V11Decision:
    return V11Decision(
        strategy_version=config.strategy_version,
        state=state,
        setup=setup,
        action=action,
        executable=False,
        blocked_reasons=tuple(dict.fromkeys(reasons)),
        evidence=dict(evidence or {}),
        planned_shares=planned_shares,
        planned_notional_cny=planned_notional_cny,
        stop_price=stop_price,
        as_of_kind=context.as_of_kind,
        as_of_trading_date=context.as_of_trading_date,
    )


def evaluate_v11(
    bars: Sequence[object], *, config: V11Config, context: V11Context,
) -> V11Decision:
    """Return a fail-closed, never-executable V11 decision."""
    if type(config) is not V11Config or type(context) is not V11Context:
        raise TypeError("config and context must be V11Config/V11Context")
    try:
        materialized = tuple(bars)
        count = len(materialized)
    except TypeError:
        return _decision(
            V11State.DATA_UNAVAILABLE, config,
            reasons=("INVALID_BARS",), evidence={}, context=context,
        )
    if count == 0:
        return _decision(
            V11State.DATA_UNAVAILABLE, config,
            reasons=("NO_COMPLETED_BARS",), evidence={"bar_count": 0}, context=context,
        )
    try:
        indicator = dict(context.indicator) or calculate_v11_indicators(materialized)
    except (ValueError, TypeError, KeyError):
        return _decision(
            V11State.DATA_UNAVAILABLE, config,
            reasons=("INDICATOR_CONTEXT_UNAVAILABLE",),
            evidence={"bar_count": count}, context=context,
        )
    if int(indicator.get("bar_count", count)) < config.minimum_daily_bars:
        return _decision(
            V11State.OBSERVE, config,
            reasons=("INSUFFICIENT_COMPLETED_BARS",),
            evidence=indicator, context=context,
        )
    reasons: list[str] = []
    if context.data_quality != "VERIFIED":
        reasons.append("DATA_QUALITY_UNVERIFIED")
    if context.snapshot_only:
        reasons.append("SNAPSHOT_ONLY")
    environment = context.environment_state.upper()
    if environment not in {"ATTACK", "NEUTRAL", "DEFENSE"}:
        reasons.append("ENVIRONMENT_UNKNOWN")
    elif environment == "DEFENSE" and (context.category or "UNVERIFIED") not in {
        "CROSS_BORDER", "GOLD",
    }:
        reasons.append("ENVIRONMENT_DEFENSE")

    price = _number_or_none(indicator.get("price"))
    ma20 = _number_or_none(indicator.get("ma20"))
    ma60 = _number_or_none(indicator.get("ma60"))
    weekly_close = _number_or_none(indicator.get("weekly_close"))
    weekly_ma20 = _number_or_none(indicator.get("weekly_ma20"))
    weekly_ma10 = _number_or_none(indicator.get("weekly_ma10"))
    ma20_slope = _number_or_none(indicator.get("ma20_slope_pct_10d"))
    trend_ok = bool(
        price is not None and ma60 is not None and price > ma60
        and ma20 is not None and ma20_slope is not None
        and (ma20_slope > 0.0 or ma20 > ma60)
    )
    if not trend_ok:
        reasons.append("TREND_NOT_CONFIRMED")
    a_week_ok = bool(
        weekly_close is not None and weekly_ma20 is not None
        and weekly_close > weekly_ma20
    )
    a_pullback_ok = all(bool(indicator.get(key)) for key in (
        "pullback_window_ok", "pullback_recovery_ok", "volume_contraction_ok",
    ))
    category = (context.category or "UNVERIFIED").upper()
    bias = _number_or_none(indicator.get("bias20_pct"))
    bias_low, bias_high = ((-2.0, 4.0) if category in {"BROAD", "GOLD"}
                           else (-3.0, 6.0))
    bias_ok = bias is not None and bias_low <= bias <= bias_high
    if not bias_ok:
        reasons.append("BIAS_OUT_OF_RANGE")
    momentum_triggers = (
        bool(indicator.get("macd_trigger")),
        bool(indicator.get("rsi_trigger")),
        bool(indicator.get("volume_recovery_trigger")),
    )
    a_ok = trend_ok and a_week_ok and a_pullback_ok and bias_ok and any(momentum_triggers)

    b_ok = bool(
        trend_ok
        and bool(indicator.get("box_ok"))
        and bool(indicator.get("box_breakout_ok"))
        and _number_or_none(indicator.get("volume_ratio20")) is not None
        and float(indicator.get("volume_ratio20")) >= 1.5
        and bool(indicator.get("macd_dif_nonnegative"))
        and _number_or_none(indicator.get("macd_dif")) is not None
        and _number_or_none(indicator.get("macd_dea")) is not None
        and float(indicator["macd_dif"]) > float(indicator["macd_dea"])
        and bias is not None and bias <= (4.0 if category == "BROAD" else 6.0)
        and weekly_close is not None and weekly_ma10 is not None
        and weekly_close > weekly_ma10
    )
    # A and B are alternative entry setups. Do not let failed A evidence
    # block a valid B breakout; report setup-specific blockers only when both
    # alternatives fail.
    if not a_ok and not b_ok:
        if not a_week_ok:
            reasons.append("WEEKLY_TREND_NOT_CONFIRMED")
        if not a_pullback_ok:
            reasons.append("PULLBACK_NOT_CONFIRMED")
        if not any(momentum_triggers):
            reasons.append("MOMENTUM_NOT_CONFIRMED")
        if not bool(indicator.get("box_ok")):
            reasons.append("BOX_NOT_CONFIRMED")
        if not bool(indicator.get("box_breakout_ok")):
            reasons.append("BREAKOUT_NOT_CONFIRMED")
    rs = context.relative_strength_20
    if rs is not None and rs < -3.0:
        reasons.append("RELATIVE_STRENGTH_TOO_WEAK")
    forced_half = bool(rs is not None and -3.0 <= rs < 0.0)
    if rs is not None and not math.isfinite(float(rs)):
        reasons.append("RELATIVE_STRENGTH_INVALID")
        forced_half = False
    setup = V11Setup.A_PULLBACK if a_ok else V11Setup.B_BREAKOUT if b_ok else V11Setup.NONE
    hard_reasons_list = list(dict.fromkeys(reasons))
    entry_price = _number_or_none(indicator.get("price")) or context.price
    stop_distance_pct = _number_or_none(indicator.get("stop_distance_pct"))
    atr14 = _number_or_none(indicator.get("atr14"))
    if stop_distance_pct is None and atr14 is not None and entry_price is not None:
        stop_distance_pct = max(0.02, (1.5 * atr14) / entry_price)
    stop_price = (
        entry_price * (1.0 - stop_distance_pct)
        if entry_price is not None and stop_distance_pct is not None
        and 0.0 < stop_distance_pct < 1.0 else None
    )
    sizing: dict[str, object] = {"status": "NOT_ATTEMPTED"}
    planned_shares = 0
    planned_notional = None
    if setup is not V11Setup.NONE and not hard_reasons_list:
        if entry_price is None or stop_price is None:
            hard_reasons_list.append("STOP_CONTEXT_UNAVAILABLE")
            sizing = {"status": "UNAVAILABLE"}
        elif context.account_known and context.equity_cny > 0.0:
            planned_shares, sizing = size_v11_order(
                entry_price=entry_price,
                stop_price=stop_price,
                config=config,
                context=context,
                force_half=forced_half,
            )
            planned_notional = planned_shares * entry_price if planned_shares else None
            hard_reasons_list.extend(sizing.get("blocked_reasons", ()))
        else:
            sizing = {"status": "ACCOUNT_CONTEXT_UNAVAILABLE"}
    hard_reasons = tuple(dict.fromkeys(hard_reasons_list))
    if environment not in {"ATTACK", "NEUTRAL", "DEFENSE"}:
        state = V11State.UNCERTAIN
    elif setup is V11Setup.NONE or hard_reasons:
        state = V11State.OBSERVE
    else:
        state = V11State.TECHNICAL_CANDIDATE
    evidence = {
        **indicator,
        "trend_ok": trend_ok,
        "weekly_trend_ok": a_week_ok,
        "a_setup_ok": a_ok,
        "b_setup_ok": b_ok,
        "momentum_trigger_count": sum(momentum_triggers),
        "macd_trigger": momentum_triggers[0],
        "rsi_trigger": momentum_triggers[1],
        "volume_recovery_trigger": momentum_triggers[2],
        "kdj_required": False,
        "forced_half_size": forced_half,
        "environment_state": environment,
        "relative_strength_20": rs,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "sizing": sizing,
    }
    return _decision(
        state, config, setup=setup,
        action=("ENTRY_CANDIDATE" if state in {
            V11State.TECHNICAL_CANDIDATE, V11State.ACTION_CANDIDATE,
        } else "OBSERVE"),
        reasons=hard_reasons, evidence=evidence, context=context,
        planned_shares=planned_shares,
        planned_notional_cny=planned_notional,
        stop_price=stop_price,
    )


def _number_or_none(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def classify_v11_environment(indicator: Mapping[str, object]) -> str:
    """Classify the broad market backdrop from explicit completed-bar evidence."""
    price = _number_or_none(indicator.get("price"))
    ma20 = _number_or_none(indicator.get("ma20"))
    ma60 = _number_or_none(indicator.get("ma60"))
    slope = _number_or_none(indicator.get("ma20_slope_pct_10d"))
    weekly_close = _number_or_none(indicator.get("weekly_close"))
    weekly_ma20 = _number_or_none(indicator.get("weekly_ma20"))
    if None in (price, ma20, ma60, slope):
        return "UNKNOWN"
    if price > ma60 and ma20 > ma60 and slope > 0.0:
        if weekly_close is None or weekly_ma20 is None or weekly_close >= weekly_ma20:
            return "ATTACK"
    if price < ma60 and ma20 < ma60 and slope < 0.0:
        return "DEFENSE"
    if abs(slope) <= 0.8 and abs(price / ma60 - 1.0) <= 0.04:
        return "NEUTRAL"
    return "UNKNOWN"


def _lot_floor(shares: float, lot_size: int) -> int:
    if not math.isfinite(shares) or shares <= 0.0:
        return 0
    return max(0, math.floor(shares / lot_size) * lot_size)


def size_v11_order(
    *, entry_price: float, stop_price: float, config: V11Config,
    context: V11Context, force_half: bool = False,
) -> tuple[int, dict[str, object]]:
    """Size a manual candidate without ever exceeding the configured notional."""
    evidence: dict[str, object] = {"blocked_reasons": []}
    reasons: list[str] = []
    if entry_price <= 0.0 or stop_price <= 0.0 or entry_price <= stop_price:
        reasons.append("INVALID_ENTRY_STOP")
        evidence["blocked_reasons"] = reasons
        return 0, evidence
    distance = entry_price - stop_price
    width = distance / entry_price
    category = (context.category or "UNVERIFIED").upper()
    cap = 0.04 if category in {"BROAD", "GOLD"} else 0.07
    evidence.update({"stop_distance": distance, "stop_width_pct": width, "stop_width_cap_pct": cap})
    if width > cap:
        reasons.append("STOP_WIDTH_OVER_CAP")
    equity = max(0.0, context.equity_cny)
    cash = max(0.0, context.cash_cny)
    risk_budget = equity * config.shadow_risk_rate
    risk_shares = risk_budget / distance if distance > 0.0 else 0.0
    cash_shares = cash / entry_price if entry_price > 0.0 else 0.0
    target_notional = config.target_order_cny * (0.5 if force_half else 1.0)
    target_notional_shares = target_notional / entry_price
    hard_notional_shares = config.max_order_cny / entry_price
    if force_half:
        risk_shares *= 0.5
        evidence["forced_half_size"] = True
    selected = _lot_floor(min(
        risk_shares, cash_shares, target_notional_shares, hard_notional_shares,
    ), config.lot_size)
    if selected < config.lot_size:
        reasons.append("MINIMUM_LOT")
        selected = 0
    if reasons and "STOP_WIDTH_OVER_CAP" in reasons:
        selected = 0
    evidence.update({
        "risk_budget_cny": risk_budget,
        "risk_cap_shares": risk_shares,
        "cash_cap_shares": cash_shares,
        "target_cap_shares": target_notional_shares,
        "max_order_cap_shares": hard_notional_shares,
        "selected_shares": selected,
        "selected_notional_cny": selected * entry_price,
        "target_order_cny": config.target_order_cny,
        "max_order_cny": config.max_order_cny,
        "blocked_reasons": tuple(dict.fromkeys(reasons)),
    })
    return selected, evidence


def evaluate_v11_position(
    position: V11Position, *, config: V11Config, context: V11Context,
) -> V11Decision:
    """Apply v1.1 position priority, bounded by currently sellable shares."""
    reasons: list[str] = []
    action = "HOLD"
    planned = 0
    if position.sellable_shares <= 0:
        reasons.append("NO_SELLABLE_SHARES")
    elif position.current_price <= position.stop_price:
        action = "EXIT"
        planned = position.sellable_shares
        reasons.append("STOP_TRIGGERED")
    elif context.environment_state.upper() == "DEFENSE":
        if position.profit_r < 0.0:
            action = "EXIT"
            planned = position.sellable_shares
            reasons.append("ENVIRONMENT_DEFENSE_LOSS")
        else:
            action = "REDUCE"
            planned = _lot_floor(position.sellable_shares / 2.0, config.lot_size)
            reasons.append("ENVIRONMENT_DEFENSE")
    elif position.tracking_price is not None and position.current_price < position.tracking_price:
        action = "EXIT"
        planned = position.sellable_shares
        reasons.append("TRACKING_LINE_BROKEN")
    elif position.profit_r >= 2.0 or position.reduced:
        action = "REDUCE"
        planned = _lot_floor(position.sellable_shares / 2.0, config.lot_size)
        reasons.append("PROFIT_OR_REDUCED_TRACKING")
    state = V11State.POSITION_ACTION if action != "HOLD" else V11State.OBSERVE
    return V11Decision(
        strategy_version=config.strategy_version,
        state=state,
        setup=V11Setup.NONE,
        action=action,
        executable=False,
        blocked_reasons=tuple(dict.fromkeys(reasons)),
        evidence={
            "priority": "STOP_OR_EXIT > ENVIRONMENT > REDUCE > TOP_UP > ENTRY",
            "shares": position.shares,
            "sellable_shares": position.sellable_shares,
            "profit_r": position.profit_r,
        },
        planned_shares=planned,
        planned_notional_cny=planned * position.current_price,
        stop_price=position.stop_price,
        as_of_kind=context.as_of_kind,
        as_of_trading_date=context.as_of_trading_date,
    )


def _average(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _completed_weekly(bars: Sequence[DailyBar]) -> dict[str, object]:
    grouped: list[tuple[tuple[int, int], DailyBar]] = []
    for bar in bars:
        key = (bar.trading_date.isocalendar().year, bar.trading_date.isocalendar().week)
        if grouped and grouped[-1][0] == key:
            grouped[-1] = (key, bar)
        else:
            grouped.append((key, bar))
    complete = [bar for _, bar in grouped if bar.trading_date.weekday() == 4]
    closes = [bar.adjusted_close for bar in complete]
    return {
        "status": "READY" if len(closes) >= 20 else "WARMUP",
        "bar_count": len(closes),
        "close": closes[-1] if closes else None,
        "ma10": _average(closes, 10),
        "ma20": _average(closes, 20),
        "as_of_trading_date": complete[-1].trading_date.isoformat() if complete else None,
    }


def calculate_v11_indicators(bars: Sequence[DailyBar]) -> dict[str, object]:
    """Calculate V11 evidence from completed daily bars only.

    The function intentionally ignores a trailing partial ISO week.  Volume
    expansion compares the latest completed day with the *previous* 20 days,
    so the candidate day cannot dilute its own denominator.
    """
    materialized = tuple(bars)
    if any(type(bar) is not DailyBar or bar.is_final is not True for bar in materialized):
        raise ValueError("V11 indicators require completed DailyBar values")
    if any(current.trading_date <= previous.trading_date
           for previous, current in zip(materialized, materialized[1:])):
        raise ValueError("V11 bars must be strictly ascending")
    snapshot = calculate_indicator_snapshot(materialized, minimum_bars=1)
    context_snapshot = calculate_indicator_context(materialized, lookback=3)
    latest_context = context_snapshot.get("latest") or {}
    recent_context = context_snapshot.get("recent") or []
    closes = [bar.adjusted_close for bar in materialized]
    highs = [bar.adjusted_high for bar in materialized]
    lows = [bar.adjusted_low for bar in materialized]
    volumes = [float(bar.volume) for bar in materialized]
    moving = dict(snapshot["moving_averages"])
    moving["ma250"] = _average(closes, 250)
    if len(closes) >= 30:
        current_ma20 = sum(closes[-20:]) / 20.0
        prior_ma20 = sum(closes[-30:-10]) / 20.0
        moving["ma20_slope_pct_10d"] = (current_ma20 / prior_ma20 - 1.0) * 100.0
    else:
        moving["ma20_slope_pct_10d"] = None
    atr_values: list[float] = []
    for index in range(max(1, len(materialized) - 14), len(materialized)):
        previous = closes[index - 1]
        atr_values.append(max(
            highs[index] - lows[index],
            abs(highs[index] - previous),
            abs(lows[index] - previous),
        ))
    atr = sum(atr_values) / len(atr_values) if atr_values else None
    prior_volume = volumes[-21:-1] if len(volumes) >= 21 else ()
    prior_volume_average = (
        sum(prior_volume) / len(prior_volume) if prior_volume else None
    )
    current_volume = volumes[-1] if volumes else None
    volume = {
        "prior_ma20": prior_volume_average,
        "ratio20": (
            current_volume / prior_volume_average
            if current_volume is not None and prior_volume_average else None
        ),
        "contraction": (
            current_volume < prior_volume_average
            if current_volume is not None and prior_volume_average else None
        ),
    }
    latest_ma20 = moving.get("ma20")
    prior_closes = closes[-26:-1] if len(closes) >= 26 else ()
    box_high = max(prior_closes) if prior_closes else None
    box_low = min(prior_closes) if prior_closes else None
    box_width_pct = (
        (box_high - box_low) / closes[-1]
        if box_high is not None and box_low is not None and closes[-1] > 0
        else None
    )
    current_volume = volumes[-1] if volumes else None
    recovery_volume_avg = _average(volumes[-6:-1], 5)
    pullback_window_ok = bool(
        latest_ma20 is not None
        and len(lows) >= 5
        and any(low <= latest_ma20 * 1.02 for low in lows[-15:])
    )
    pullback_recovery_ok = bool(
        latest_ma20 is not None
        and closes[-1] >= latest_ma20
        and (len(closes) < 2 or closes[-1] > closes[-2])
    )
    volume_contraction_ok = bool(
        recovery_volume_avg is not None
        and prior_volume_average is not None
        and recovery_volume_avg < prior_volume_average
    )
    macd = snapshot["macd"]
    macd_trigger = bool(
        latest_context.get("macd_cross") == "BULLISH"
        and (latest_context.get("macd_cross_age") or 0) <= 3
    ) or bool(
        latest_context.get("macd_histogram_rising_days", 0) >= 2
        and latest_context.get("macd_dif_above_dea") is True
    )
    rsi = snapshot["rsi"].get("rsi14")
    previous_rsi = latest_context.get("rsi_previous_1")
    rsi_trigger = bool(
        rsi is not None and previous_rsi is not None
        and rsi > previous_rsi and 35.0 <= rsi <= 60.0
    )
    volume_recovery_trigger = bool(
        current_volume is not None and prior_volume_average is not None
        and current_volume >= prior_volume_average * 1.2
        and (len(closes) < 2 or closes[-1] > closes[-2])
    )
    box_ok = bool(
        len(prior_closes) >= 20 and box_width_pct is not None
        and box_width_pct <= 0.15
    )
    box_breakout_ok = bool(
        box_ok and box_high is not None and closes[-1] > box_high
    )
    weekly = _completed_weekly(materialized)
    weekly_above_ma10 = bool(
        weekly.get("close") is not None and weekly.get("ma10") is not None
        and weekly["close"] > weekly["ma10"]
    )
    return {
        "status": "READY" if len(materialized) >= 250 else "WARMUP",
        "bar_count": len(materialized),
        "as_of_trading_date": materialized[-1].trading_date.isoformat() if materialized else None,
        "moving_averages": moving,
        "macd": snapshot["macd"],
        "rsi": snapshot["rsi"],
        "kdj": snapshot["kdj"],
        "bias20": (
            {"value": (closes[-1] / moving["ma20"] - 1.0) * 100.0}
            if moving.get("ma20") else {"value": None}
        ),
        "volume": volume,
        "atr14": atr,
        "weekly": weekly,
        "setups": {
            "pullback_window_ok": pullback_window_ok,
            "pullback_recovery_ok": pullback_recovery_ok,
            "volume_contraction_ok": volume_contraction_ok,
            "macd_trigger": macd_trigger,
            "rsi_trigger": rsi_trigger,
            "volume_recovery_trigger": volume_recovery_trigger,
            "box_ok": box_ok,
            "box_breakout_ok": box_breakout_ok,
            "macd_dif_nonnegative": bool(
                macd.get("dif") is not None and macd["dif"] >= 0.0
            ),
            "weekly_above_ma10": weekly_above_ma10,
            "box_high": box_high,
            "box_low": box_low,
            "box_width_pct": box_width_pct,
            "latest_context": latest_context,
            "recent_context": recent_context,
        },
    }


__all__ = [
    "V11Config", "V11Context", "V11Decision", "V11Position", "V11Setup", "V11State",
    "calculate_v11_indicators", "classify_v11_environment", "evaluate_v11", "evaluate_v11_position",
    "load_v11_config", "size_v11_order",
]
