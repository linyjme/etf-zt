"""Read-only SWING_V11 rules, indicator evidence, and position actions.

The V11 layer is deliberately independent from the formal V1 strategy. It
produces an explainable shadow decision and never emits broker instructions.
Completed daily bars and completed weekly context are required inputs; missing
quality, environment, or position evidence fails closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import StrEnum
import json
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .swing_data import DailyBar
from .swing_indicators import (
    _rsi_wilder,
    calculate_indicator_context,
    calculate_indicator_snapshot,
)
from .valuation import ValuationStage


SHANGHAI = ZoneInfo("Asia/Shanghai")


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
    valuation_stage: Mapping[str, object] | ValuationStage | None = None
    valuation_stage_enforcement: bool = False


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
    reduced: bool = False
    tracking_price: float | None = None
    # Position state is optional so callers that only provide the original
    # risk fields remain source compatible.  These values are evidence for
    # the shadow state machine; they never authorize a broker order.
    holding_session: int = 0
    setup: V11Setup | str = V11Setup.NONE
    peak_price: float | None = None
    new_high: bool = False
    initial_shares: int | None = None
    topup_done: bool = False
    tracking_started: bool = False
    cooldown_sessions: int = 0
    standard_shares: int | None = None
    entry_environment: str | None = None

    def __post_init__(self) -> None:
        if type(self.shares) is not int or self.shares < 0:
            raise ValueError("shares must be a nonnegative integer")
        if type(self.sellable_shares) is not int or not 0 <= self.sellable_shares <= self.shares:
            raise ValueError("sellable_shares must be between zero and shares")
        for name in ("entry_price", "stop_price", "current_price", "initial_risk_per_share"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.entry_price <= 0.0 or self.stop_price <= 0.0 or self.current_price <= 0.0:
            raise ValueError("position prices must be positive")
        if self.initial_risk_per_share <= 0.0:
            raise ValueError("initial_risk_per_share must be positive")
        if type(self.holding_session) is not int or self.holding_session < 0:
            raise ValueError("holding_session must be a nonnegative integer")
        if self.initial_shares is not None and (
            type(self.initial_shares) is not int
            or self.initial_shares < 0
        ):
            raise ValueError("initial_shares must be a nonnegative integer")
        if type(self.cooldown_sessions) is not int or self.cooldown_sessions < 0:
            raise ValueError("cooldown_sessions must be a nonnegative integer")
        if self.standard_shares is not None and (
            type(self.standard_shares) is not int
            or self.standard_shares < 0
        ):
            raise ValueError("standard_shares must be a nonnegative integer")
        for name in ("peak_price", "tracking_price"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(float(value))
            ):
                raise ValueError(f"{name} must be finite when supplied")
            if value is not None and value <= 0.0:
                raise ValueError(f"{name} must be positive when supplied")


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
        indicator = normalize_v11_indicators(context.indicator)
        if not indicator:
            indicator = normalize_v11_indicators(calculate_v11_indicators(materialized))
    except (ValueError, TypeError, KeyError):
        return _decision(
            V11State.DATA_UNAVAILABLE, config,
            reasons=("INDICATOR_CONTEXT_UNAVAILABLE",),
            evidence={"bar_count": count}, context=context,
        )
    if int(indicator.get("bar_count", count)) < config.minimum_daily_bars:
        early_reasons = ["INSUFFICIENT_COMPLETED_BARS"]
        if context.data_quality != "VERIFIED":
            early_reasons.append("DATA_QUALITY_UNVERIFIED")
            early_reasons.extend(
                reason for reason in context.metadata.get("data_quality_reasons", ())
                if isinstance(reason, str)
            )
        return _decision(
            V11State.OBSERVE, config,
            reasons=early_reasons,
            evidence=indicator, context=context,
        )
    reasons: list[str] = []
    # A quasi-close decision is only valid when the service supplied a real
    # current-day observation captured during the 14:45--15:00 tail window.
    # Do not infer this from the last completed daily bar: that would turn a
    # stale quote into a live candidate.
    as_of_gate = "VALID"
    if context.as_of_kind == "QUASI_CLOSE_1445":
        quasi = context.quasi_close
        observed_at = quasi.get("observed_at") if isinstance(quasi, Mapping) else None
        observed_time = None
        observed_date = None
        parsed_observed_at = parse_v11_observed_at(observed_at)
        if parsed_observed_at is not None:
            observed_time = parsed_observed_at.timetz().replace(tzinfo=None)
            observed_date = parsed_observed_at.date().isoformat()
        valid_time = (
            observed_time is not None
            and time(14, 45) <= observed_time <= time(15, 0)
        )
        valid_price = _number_or_none(quasi.get("price")) if isinstance(quasi, Mapping) else None
        valid_health = isinstance(quasi, Mapping) and quasi.get("health") == "REALTIME"
        if not (
            valid_price is not None and valid_price > 0.0
            and valid_health and valid_time
            and context.as_of_trading_date is not None
            and observed_date == context.as_of_trading_date
        ):
            as_of_gate = "BLOCKED"
            reasons.append("QUASI_CLOSE_NOT_VERIFIED")
    elif context.as_of_kind != "COMPLETED_DAILY":
        as_of_gate = "BLOCKED"
        reasons.append("AS_OF_KIND_UNSUPPORTED")
    if context.metadata.get("v11_metadata_errors"):
        reasons.append("METADATA_INCOMPLETE")
    if context.data_quality != "VERIFIED":
        reasons.append("DATA_QUALITY_UNVERIFIED")
        for quality_reason in context.metadata.get("data_quality_reasons", ()):
            if isinstance(quality_reason, str):
                reasons.append(quality_reason)
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
    t1_price_above_ma60 = bool(
        price is not None and ma60 is not None and price > ma60
    )
    t2_ma20_trend = bool(
        ma20 is not None and ma60 is not None
        and ((ma20_slope is not None and ma20_slope > 0.0) or ma20 > ma60)
    )
    trend_ok = t1_price_above_ma60 and t2_ma20_trend
    if not trend_ok:
        reasons.append("TREND_NOT_CONFIRMED")
    weekly_ma10_down_3w = indicator.get("weekly_ma10_down_3w")
    t3_week_ok = bool(
        weekly_close is not None and weekly_ma20 is not None
        and weekly_close > weekly_ma20
        and type(weekly_ma10_down_3w) is bool
        and weekly_ma10_down_3w is False
    )
    a_week_ok = t3_week_ok
    category = (context.category or "UNVERIFIED").upper()
    category_unverified = (
        context.category is None
        or not str(context.category).strip()
        or category == "UNVERIFIED"
    )
    if category_unverified:
        reasons.append("CATEGORY_UNVERIFIED")
    # T5 is intentionally explicit and category-aware.  An unverified category
    # already fail-closes the decision, so it is not evaluated again as a
    # false environment permission.  Neutral only permits broad/gold and
    # defense only permits defensive categories.
    t5_reported: bool | None
    if category_unverified:
        t5_allowed = False
        t5_reported = None
    else:
        explicit_t5 = indicator.get("environment_category_allowed")
        if explicit_t5 is None:
            if environment == "ATTACK":
                t5_allowed = True
            elif environment == "NEUTRAL":
                t5_allowed = category in {"BROAD", "GOLD", "CROSS_BORDER"}
            elif environment == "DEFENSE":
                t5_allowed = category in {"CROSS_BORDER", "GOLD"}
            else:
                t5_allowed = False
        else:
            t5_allowed = bool(explicit_t5)
        t5_reported = t5_allowed
        if not t5_allowed:
            reasons.append("T5_ENVIRONMENT_CATEGORY")

    ma10 = _number_or_none(indicator.get("ma10"))
    pullback_sessions = _number_or_none(indicator.get("pullback_window_sessions"))
    pullback_depth = _number_or_none(indicator.get("pullback_depth_pct"))
    pullback_low = _number_or_none(indicator.get("pullback_low"))
    depth_threshold = 3.0 if category in {"UNVERIFIED", "BROAD", "GOLD"} else 5.0
    pullback_depth_evidence = (
        pullback_sessions is not None and pullback_depth is not None
    )
    pullback_touch_evidence = pullback_low is not None and ma10 is not None
    pullback_evidence_available = pullback_depth_evidence or pullback_touch_evidence
    pullback_shape_ok = bool(
        pullback_depth_evidence and 5.0 <= pullback_sessions <= 15.0
        and pullback_depth >= depth_threshold
    ) or bool(
        pullback_touch_evidence and pullback_low <= ma10 * 1.01
    )
    pullback_recovery_within_3d = (
        type(indicator.get("pullback_recovery_within_3d")) is bool
        and bool(indicator.get("pullback_recovery_within_3d"))
    )
    recovery_shadow_evidence = type(indicator.get("recovery_long_upper_shadow")) is bool
    recovery_shadow_ok = recovery_shadow_evidence and not bool(
        indicator.get("recovery_long_upper_shadow")
    )
    volume_contraction_evidence = type(indicator.get("volume_contraction_majority")) is bool
    volume_contraction_majority = volume_contraction_evidence and bool(
        indicator.get("volume_contraction_majority")
    )
    a_pullback_ok = bool(
        pullback_shape_ok and pullback_recovery_within_3d
        and recovery_shadow_ok and volume_contraction_majority
    )
    bias = _number_or_none(indicator.get("bias20_pct"))
    bias_low, bias_high = ((-2.0, 4.0) if category in {"BROAD", "GOLD"}
                           else (-3.0, 6.0))
    bias_ok = bias is not None and bias_low <= bias <= bias_high
    if not bias_ok:
        reasons.append("BIAS_OUT_OF_RANGE")
    macd_dif = _number_or_none(indicator.get("macd_dif"))
    macd_dea = _number_or_none(indicator.get("macd_dea"))
    macd_dif_above_dea = (
        macd_dif is not None and macd_dea is not None and macd_dif > macd_dea
    )
    macd_evidence_available = (
        type(indicator.get("macd_histogram_improving_2d")) is bool
        and macd_dif is not None and macd_dea is not None
    )
    macd_exact = bool(
        macd_evidence_available
        and indicator.get("macd_histogram_improving_2d")
        and macd_dif_above_dea
    )
    rsi_min = _number_or_none(indicator.get("rsi_pullback_min"))
    rsi_current = _number_or_none(indicator.get("rsi_current"))
    rsi_previous = _number_or_none(indicator.get("rsi_previous_1"))
    rsi_evidence_available = rsi_min is not None and rsi_current is not None
    rsi_exact = bool(
        rsi_evidence_available and rsi_min >= 40.0 and rsi_current > 50.0
    )
    rsi_crossed_above_50 = bool(
        rsi_previous is not None and rsi_current is not None
        and rsi_previous <= 50.0 and rsi_current > 50.0
    )
    volume_evidence_available = (
        type(indicator.get("volume_recovery_trigger")) is bool
        and _number_or_none(indicator.get("volume_ratio20")) is not None
    )
    volume_exact = bool(
        volume_evidence_available and indicator.get("volume_recovery_trigger")
        and float(indicator["volume_ratio20"]) >= 1.2
    )
    momentum_triggers = (macd_exact, rsi_exact, volume_exact)
    a_ok = bool(
        trend_ok and t3_week_ok and t5_allowed
        and a_pullback_ok and bias_ok
        and _number_or_none(indicator.get("return_60d_pct")) is not None
        and float(indicator["return_60d_pct"]) > 0.0
        and any(momentum_triggers)
    )
    return_60d = _number_or_none(indicator.get("return_60d_pct"))
    ma250_slope = _number_or_none(indicator.get("ma250_slope_pct_20d"))
    return_250 = _number_or_none(indicator.get("return_250d_pct"))
    ma250 = _number_or_none(indicator.get("ma250"))
    b1_ok = bool(
        (ma250_slope is not None and ma250_slope > 0.0)
        or (return_250 is not None and return_250 > 0.0)
    )
    box_first_low = _number_or_none(indicator.get("box_first_half_low"))
    box_latter_low = _number_or_none(indicator.get("box_latter_half_low"))
    box_days = _number_or_none(indicator.get("box_days"))
    b2_ok = bool(
        box_days is not None and box_days >= 25.0
        and box_first_low is not None and box_latter_low is not None
        and box_latter_low >= box_first_low
    )
    box_high = _number_or_none(indicator.get("box_high"))
    b3_ok = bool(
        indicator.get("box_breakout_ok") and price is not None and ma60 is not None
        and price > ma60
        and box_high is not None and price > box_high
    )
    bollinger_upper = _number_or_none(indicator.get("bollinger_upper"))
    b6_ok = bool(
        bias is not None and bias <= (4.0 if category == "BROAD" else 6.0)
        and bollinger_upper is not None and price is not None
        and price <= bollinger_upper * 1.01
    )
    b_ok = bool(
        trend_ok and t5_allowed and b1_ok and b2_ok and b3_ok
        and _number_or_none(indicator.get("volume_ratio20")) is not None
        and float(indicator.get("volume_ratio20")) >= 1.5
        and macd_dif is not None and macd_dea is not None
        and macd_dif > macd_dea and macd_dif >= 0.0
        and b6_ok and weekly_close is not None and weekly_ma10 is not None
        and weekly_close > weekly_ma10
    )
    stage_name = None
    stage_allow_a = True
    stage_allow_b = True
    stage_force_half = False
    stage_block_entry = False
    if context.valuation_stage_enforcement:
        stage_multiplier: float | None = None
        if isinstance(context.valuation_stage, ValuationStage):
            stage_name = context.valuation_stage.stage
            stage_allow_a = context.valuation_stage.allow_a_pullback
            stage_allow_b = context.valuation_stage.allow_b_breakout
            stage_multiplier = context.valuation_stage.size_multiplier
        elif isinstance(context.valuation_stage, Mapping):
            stage_name = str(context.valuation_stage.get("stage") or "") or None
            stage_allow_a = context.valuation_stage.get("allow_a_pullback", True) is not False
            stage_allow_b = context.valuation_stage.get("allow_b_breakout", True) is not False
            stage_multiplier = _number_or_none(context.valuation_stage.get("size_multiplier"))
        if stage_multiplier is not None and stage_multiplier <= 0.0:
            stage_block_entry = True
        elif stage_multiplier is not None and stage_multiplier <= 0.5:
            stage_force_half = True
        if stage_block_entry:
            reasons.append("VALUATION_STAGE_NO_ENTRY")
        if a_ok and not stage_allow_a:
            reasons.append("VALUATION_EXPENSIVE_NO_PULLBACK")
        if b_ok and not stage_allow_b:
            reasons.append("VALUATION_BREAKOUT_NOT_ALLOWED")
    # A and B are alternative entry setups. Do not let failed A evidence
    # block a valid B breakout; report setup-specific blockers only when both
    # alternatives fail.
    if not a_ok and not b_ok:
        if type(weekly_ma10_down_3w) is not bool:
            reasons.append("T3_EVIDENCE_UNAVAILABLE")
        elif not t3_week_ok:
            reasons.append("T3_WEEKLY_TREND")
        if return_60d is None:
            reasons.append("T4_EVIDENCE_UNAVAILABLE")
        elif return_60d <= 0.0:
            reasons.append("T4_RETURN_60D")
        if not a_pullback_ok:
            reasons.append("PULLBACK_NOT_CONFIRMED")
        if not pullback_evidence_available:
            reasons.append("A1_A2_EVIDENCE_UNAVAILABLE")
        elif not pullback_shape_ok:
            reasons.append("A1_A2_PULLBACK")
        if not volume_contraction_evidence:
            reasons.append("A3_EVIDENCE_UNAVAILABLE")
        elif not volume_contraction_majority:
            reasons.append("A3_VOLUME_CONTRACTION")
        if not type(indicator.get("pullback_recovery_within_3d")) is bool or not recovery_shadow_evidence:
            reasons.append("A4_EVIDENCE_UNAVAILABLE")
        elif not pullback_recovery_within_3d or not recovery_shadow_ok:
            reasons.append("A4_RECOVERY")
        if not (macd_evidence_available or rsi_evidence_available or volume_evidence_available):
            reasons.append("A6_EVIDENCE_UNAVAILABLE")
        elif not any(momentum_triggers):
            reasons.append("A6_MOMENTUM")
        if not b1_ok:
            reasons.append("B1_LONG_TREND")
        if not b2_ok:
            reasons.append("B2_BOX_STRUCTURE")
        if not b3_ok:
            reasons.append("B3_BREAKOUT")
        if not b6_ok:
            reasons.append("B6_OVERHEATED")
        if not bool(indicator.get("box_ok")):
            reasons.append("BOX_NOT_CONFIRMED")
        if not bool(indicator.get("box_breakout_ok")):
            reasons.append("BREAKOUT_NOT_CONFIRMED")
    rs = context.relative_strength_20
    if rs is not None and rs < -3.0:
        reasons.append("RELATIVE_STRENGTH_TOO_WEAK")
    if rs is not None and not math.isfinite(float(rs)):
        reasons.append("RELATIVE_STRENGTH_INVALID")
    setup = V11Setup.A_PULLBACK if a_ok else V11Setup.B_BREAKOUT if b_ok else V11Setup.NONE
    hard_reasons_list = list(dict.fromkeys(reasons))
    entry_price = _number_or_none(indicator.get("price")) or context.price
    atr14 = _number_or_none(indicator.get("atr14"))
    stop_reference = "pullback_low" if setup is V11Setup.A_PULLBACK else (
        "box_high" if setup is V11Setup.B_BREAKOUT else None
    )
    stop_reference_value = (
        _number_or_none(indicator.get(stop_reference))
        if stop_reference is not None else None
    )
    stop_price = (
        max(stop_reference_value * 0.99, entry_price - 2.0 * atr14)
        if entry_price is not None and stop_reference_value is not None
        and atr14 is not None and atr14 > 0.0 else None
    )
    # Keep callers that explicitly supplied a stop distance on the original
    # flat contract readable while requiring P1 evidence for newly calculated
    # indicators.  The deprecated implicit 1.5*ATR fallback is intentionally
    # gone; this branch only honors an explicit caller value.
    explicit_stop_distance = _number_or_none(indicator.get("stop_distance_pct"))
    if stop_price is None and setup is not V11Setup.NONE and explicit_stop_distance is not None:
        if 0.0 < explicit_stop_distance < 1.0 and entry_price is not None:
            stop_price = entry_price * (1.0 - explicit_stop_distance)
            stop_reference = "explicit_stop_distance_pct"
            stop_reference_value = None
    stop_width_pct = (
        (entry_price - stop_price) / entry_price
        if entry_price is not None and stop_price is not None and entry_price > 0.0
        else None
    )
    stop_cap = 0.04 if category in {"BROAD", "GOLD"} else 0.07
    if setup is not V11Setup.NONE and stop_price is None:
        hard_reasons_list = list(dict.fromkeys((*reasons, "STOP_CONTEXT_UNAVAILABLE")))
    elif setup is not V11Setup.NONE and entry_price is not None and stop_price >= entry_price:
        hard_reasons_list = list(dict.fromkeys((*reasons, "INVALID_ENTRY_STOP")))
    elif setup is not V11Setup.NONE and stop_width_pct is not None and stop_width_pct > stop_cap:
        hard_reasons_list = list(dict.fromkeys((*reasons, "STOP_WIDTH_OVER_CAP")))
    else:
        hard_reasons_list = list(dict.fromkeys(reasons))
    sizing: dict[str, object] = {"status": "NOT_ATTEMPTED"}
    planned_shares = 0
    planned_notional = None
    macd_below_zero = bool(
        setup is V11Setup.A_PULLBACK and macd_dif is not None and macd_dif < 0.0
    )
    forced_half = bool(
        setup is V11Setup.B_BREAKOUT or macd_below_zero
        or (rs is not None and -3.0 <= rs < 0.0)
        or stage_force_half
    )
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
    if isinstance(context.valuation_stage, ValuationStage):
        valuation_evidence = context.valuation_stage.to_dict()
    elif isinstance(context.valuation_stage, Mapping):
        valuation_evidence = dict(context.valuation_stage)
    else:
        valuation_evidence = None
    evidence = {
        **indicator,
        "trend_ok": trend_ok,
        "t1_price_above_ma60": t1_price_above_ma60,
        "t2_ma20_trend": t2_ma20_trend,
        "t3_weekly_trend": t3_week_ok,
        "t4_return_60d": bool(
            _number_or_none(indicator.get("return_60d_pct")) is not None
            and float(indicator["return_60d_pct"]) > 0.0
        ),
        "t5_environment_category": t5_reported,
        "weekly_trend_ok": a_week_ok,
        "a_setup_ok": a_ok,
        "b_setup_ok": b_ok,
        "a1_a2_pullback": pullback_shape_ok,
        "a3_volume_contraction_majority": volume_contraction_majority,
        "a4_recovery": pullback_recovery_within_3d and recovery_shadow_ok,
        "a6_momentum": any(momentum_triggers),
        "b1_long_trend": b1_ok,
        "b2_box_structure": b2_ok,
        "b3_breakout": b3_ok,
        "b6_overheated_ok": b6_ok,
        "momentum_trigger_count": sum(momentum_triggers),
        "macd_trigger": momentum_triggers[0],
        "macd_dif_above_dea": macd_dif_above_dea,
        "rsi_trigger": momentum_triggers[1],
        "rsi_crossed_above_50": rsi_crossed_above_50,
        "volume_recovery_trigger": momentum_triggers[2],
        "kdj_required": False,
        "forced_half_size": forced_half,
        "stop_reference": stop_reference,
        "stop_reference_value": stop_reference_value,
        "stop_width_pct": stop_width_pct,
        "stop_width_cap_pct": stop_cap,
        "environment_state": environment,
        "as_of_gate": as_of_gate,
        "relative_strength_20": rs,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "sizing": sizing,
        "valuation_stage": valuation_evidence,
        "valuation_stage_enforcement": context.valuation_stage_enforcement,
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


def parse_v11_observed_at(value: object) -> datetime | None:
    """Parse an observed timestamp only when it is timezone-aware.

    The quasi-close gate is defined in Asia/Shanghai wall time.  A naive
    timestamp has no safe interpretation and therefore fails closed instead
    of being silently treated as local time by the host process.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHANGHAI)


def classify_v11_environment(indicator: Mapping[str, object]) -> str:
    """Classify one index as attack or defense.

    Complete inputs never return neutral or unknown.  Neutral is reserved for
    the two-index synthesis in ``calculate_v11_environment``.  A healthy index
    is above MA60 while MA20 is not declining and, when weekly evidence exists,
    the weekly close has not broken weekly MA20.
    """
    price = _number_or_none(indicator.get("price"))
    ma20 = _number_or_none(indicator.get("ma20"))
    ma60 = _number_or_none(indicator.get("ma60"))
    slope = _number_or_none(indicator.get("ma20_slope_pct_10d"))
    if None in (price, ma20, ma60, slope):
        return "UNKNOWN"
    weekly_close = _number_or_none(indicator.get("weekly_close"))
    weekly_ma20 = _number_or_none(indicator.get("weekly_ma20"))
    weekly_broken = (
        weekly_close is not None and weekly_ma20 is not None
        and weekly_close < weekly_ma20
    )
    if price > ma60 and slope >= -0.5 and not weekly_broken:
        return "ATTACK"
    return "DEFENSE"


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
    """Apply the deterministic v1.1 position priority.

    The evaluator is intentionally a pure, read-only projection of the next
    position state.  A caller persists ``reduced``/``tracking_price`` after
    displaying the shadow decision; this function never mutates the supplied
    position and every decision remains non-executable.
    """
    reasons: list[str] = []
    action = "HOLD"
    planned = 0
    decision_stop = position.stop_price
    evidence: dict[str, object] = {
        "priority": "STOP_OR_EXIT > ENVIRONMENT > REDUCE > TOP_UP > ENTRY",
        "shares": position.shares,
        "sellable_shares": position.sellable_shares,
        "holding_session": position.holding_session,
        "reduced": position.reduced,
        "tracking_started": position.tracking_started or position.tracking_price is not None,
        "cooldown_sessions": position.cooldown_sessions,
        "standard_shares": position.standard_shares,
        "market_gate_reasons": (),
    }
    indicator: Mapping[str, object] = context.indicator

    def value(*keys: str) -> object:
        for key in keys:
            if key in indicator:
                return indicator[key]
        return None

    def number(*keys: str) -> float | None:
        raw = value(*keys)
        if isinstance(raw, Mapping):
            raw = raw.get("value", raw.get("rsi14", raw.get("upper")))
        return _number_or_none(raw)

    def flag(*keys: str) -> bool:
        return bool(value(*keys))

    category = (context.category or "UNVERIFIED").upper()
    environment = context.environment_state.upper()
    defense_exempt = category in {"CROSS_BORDER", "GOLD"}
    market_gate_reasons: list[str] = []
    if context.data_quality != "VERIFIED":
        market_gate_reasons.append("DATA_QUALITY_UNVERIFIED")
    if environment not in {"ATTACK", "NEUTRAL", "DEFENSE"}:
        market_gate_reasons.append("ENVIRONMENT_UNKNOWN")
    if context.as_of_kind == "QUASI_CLOSE_1445":
        quasi = context.quasi_close
        observed = parse_v11_observed_at(
            quasi.get("observed_at") if isinstance(quasi, Mapping) else None,
        )
        valid_quasi = bool(
            observed is not None
            and time(14, 45) <= observed.timetz().replace(tzinfo=None) <= time(15, 0)
            and context.as_of_trading_date == observed.date().isoformat()
            and isinstance(quasi, Mapping)
            and quasi.get("health") == "REALTIME"
            and _number_or_none(quasi.get("price")) is not None
            and _number_or_none(quasi.get("price")) > 0.0
        )
        if not valid_quasi:
            market_gate_reasons.append("QUASI_CLOSE_NOT_VERIFIED")
    elif context.as_of_kind != "COMPLETED_DAILY":
        market_gate_reasons.append("AS_OF_KIND_UNSUPPORTED")
    evidence["market_gate_reasons"] = tuple(dict.fromkeys(market_gate_reasons))
    half = _lot_floor(position.sellable_shares / 2.0, config.lot_size)
    close = number("close", "effective_close", "price")
    close = close if close is not None else position.current_price
    upper = number("bollinger_upper", "boll_upper", "bollinger")
    bias = number("bias20_pct", "bias20", "bias")
    rsi = number("rsi_current", "rsi")
    volume_ratio = number("volume_ratio20", "volume_ratio")
    daily_return = number("daily_return_pct", "return_1d_pct")
    stage = context.valuation_stage
    stage_name = None
    stage_allow_topup = True
    stage_s1_limit: float | None = None
    stage_reduce_at_r: float | None = None
    if context.valuation_stage_enforcement:
        if isinstance(stage, ValuationStage):
            stage_name = stage.stage
            stage_allow_topup = stage.allow_topup
            stage_s1_limit = stage.s1_bias_limit
            stage_reduce_at_r = stage.reduce_at_r
        elif isinstance(stage, Mapping):
            stage_name = str(stage.get("stage") or "") or None
            stage_allow_topup = stage.get("allow_topup", True) is not False
            stage_s1_limit = _number_or_none(stage.get("s1_bias_limit"))
            stage_reduce_at_r = _number_or_none(stage.get("reduce_at_r"))
    s1_limit = stage_s1_limit or (8.0 if category in {"BROAD", "GOLD", "UNVERIFIED"} else 12.0)
    s2_limit = 75.0 if category in {"BROAD", "GOLD", "UNVERIFIED"} else 80.0
    s1 = bias is not None and bias > s1_limit
    s2 = not s1 and rsi is not None and rsi > s2_limit and upper is not None and close > upper
    s3_volume = (
        position.holding_session >= 2
        and volume_ratio is not None and volume_ratio >= 2.5
        and daily_return is not None and daily_return > 0.0
    )
    day_move_limit = 4.0 if category in {"BROAD", "GOLD", "UNVERIFIED"} else 6.0
    s3_move = (
        position.holding_session >= 2
        and daily_return is not None and daily_return >= day_move_limit
        and upper is not None and close > upper
    )
    s3_trigger = flag("s3_triggered") or s3_volume or s3_move
    s3_ready = flag("s3_ready") or (
        flag("s3_next_day_10am") and flag("s3_no_new_high")
    ) or flag("s3_intraday_below_vwap")
    s3 = s3_trigger and s3_ready
    s4 = (
        flag("near_ma250_or_prior_high", "at_pressure")
        and flag("long_upper_shadow")
        and volume_ratio is not None and volume_ratio >= 1.0
    )
    premium = number("premium_pct", "premium_rate")
    s6 = category == "CROSS_BORDER" and premium is not None and premium > 5.0
    profit_r = (position.current_price - position.entry_price) / position.initial_risk_per_share
    evidence["profit_r"] = profit_r
    s7 = flag("long_holiday_preclose") and not defense_exempt and profit_r < 1.0
    evidence.update({
        "s1_bias20": s1,
        "s1_bias_limit": s1_limit,
        "s2_rsi": s2,
        "s2_rsi_limit": s2_limit,
        "s2_close_outside_bollinger": bool(upper is not None and close > upper),
        "s3_trigger": s3_trigger,
        "s3_ready": s3_ready,
        "s4_pressure": s4,
        "s6_premium_pct": premium,
        "s7_holiday": s7,
    })

    # All exit rules are evaluated before environmental, reduction and top-up
    # rules.  Missing evidence is false, never an inferred trigger.
    exit_reasons: list[str] = []
    if position.current_price <= position.stop_price:
        exit_reasons.append("STOP_TRIGGERED")
    if position.tracking_price is not None and position.current_price < max(
        position.entry_price, position.tracking_price,
    ):
        exit_reasons.append("TRACKING_LINE_BROKEN")
    ma60 = number("ma60")
    ma60_break_days = number("close_below_ma60_days")
    if flag("ma60_break_confirmed") or (ma60 is not None and close < ma60 * 0.98) or (ma60_break_days is not None and ma60_break_days >= 2):
        exit_reasons.append("C2_MA60_BREAK")
    weekly_close = number("weekly_close")
    weekly_ma20 = number("weekly_ma20")
    if weekly_close is not None and weekly_ma20 is not None and weekly_close < weekly_ma20:
        exit_reasons.append("C3_WEEKLY_MA20_BREAK")
    if flag("csi300_weekly_break") and not defense_exempt:
        exit_reasons.append("C4_CSI300_WEEKLY_BREAK")
    fund_size = number("fund_size_cny")
    turnover = number("avg_turnover_20d_cny")
    if flag("fund_abnormal", "etf_abnormal_size_amount"):
        exit_reasons.append("C5_FUND_LIQUIDITY")
    elif fund_size is not None and fund_size < 500_000_000:
        exit_reasons.append("C5_FUND_SIZE")
    elif turnover is not None and turnover < 100_000_000:
        exit_reasons.append("C5_TURNOVER")
    if category == "CROSS_BORDER" and premium is not None and premium > 8.0:
        exit_reasons.append("C6_PREMIUM_OVER_8")
    if flag("rule_violation", "manual_bad_entry", "emotion_trade"):
        exit_reasons.append("C7_RULE_VIOLATION")
    setup = str(position.setup).upper()
    box_high = number("box_high")
    ma10 = number("ma10")
    ma20 = number("ma20")
    if setup.endswith("B_BREAKOUT") and position.holding_session <= 3 and box_high is not None and close < box_high:
        exit_reasons.append("E1_BOX_BREAK")
    if setup.endswith("A_PULLBACK") and ma20 is not None and close < ma20 and (
        flag("macd_dead_cross", "macd_histogram_turning_down", "macd_red_to_green")
    ):
        exit_reasons.append("E2_PULLBACK_FAILURE")
    stage = context.valuation_stage
    e3_session = 10
    if context.valuation_stage_enforcement and isinstance(stage, ValuationStage):
        e3_session = stage.e3_session
    elif context.valuation_stage_enforcement and isinstance(stage, Mapping):
        raw_e3 = _number_or_none(stage.get("e3_session"))
        if raw_e3 is not None and raw_e3 >= 1:
            e3_session = int(raw_e3)
    if position.holding_session >= e3_session and not position.new_high and profit_r < 1.0:
        exit_reasons.append("E3_NO_PROGRESS")
    if (
        position.entry_environment is not None
        and position.entry_environment.upper() == "ATTACK"
        and environment == "NEUTRAL" and not defense_exempt and profit_r < 0.0
    ):
        exit_reasons.append("E4_NEUTRAL_LOSS")
    if position.holding_session >= 25 and profit_r < 1.0:
        exit_reasons.append("T25_NO_1R")
    if (
        context.valuation_stage_enforcement and stage_name == "EXPENSIVE"
        and position.holding_session >= 15 and profit_r < 1.0
    ):
        exit_reasons.append("VALUATION_EXPENSIVE_TIME_LIMIT")
    if (
        context.valuation_stage_enforcement and stage_name == "EXPENSIVE"
        and ma10 is not None and close < ma10
    ):
        exit_reasons.append("VALUATION_EXPENSIVE_MA10_BREAK")

    safe_exit_reasons = [
        reason for reason in exit_reasons
        if reason in {"STOP_TRIGGERED", "TRACKING_LINE_BROKEN"}
    ]
    if exit_reasons and position.sellable_shares > 0 and (
        not market_gate_reasons or safe_exit_reasons
    ):
        action = "EXIT"
        planned = position.sellable_shares
        selected_exit = safe_exit_reasons[0] if market_gate_reasons else exit_reasons[0]
        reasons.append(selected_exit)
        base_exit = selected_exit.split("_", 1)[0]
        if selected_exit == "STOP_TRIGGERED":
            reasons.append("C1")
        if base_exit in {"C2", "C3", "C4", "C5", "C6", "C7", "E1", "E2", "E3", "T25"}:
            reasons.append(base_exit)
        if base_exit == "E4":
            reasons.append("E4")
    elif position.sellable_shares <= 0:
        reasons.append("NO_SELLABLE_SHARES")
        if exit_reasons:
            reasons.append(exit_reasons[0])
    elif market_gate_reasons:
        reasons.extend(market_gate_reasons)
    elif environment == "DEFENSE" and not defense_exempt and profit_r < 0.0:
        action = "EXIT"
        planned = position.sellable_shares
        reasons.append("ENVIRONMENT_DEFENSE_LOSS")
    elif environment == "DEFENSE" and not defense_exempt and not position.reduced and half > 0:
        action = "REDUCE"
        planned = half
        reasons.append("S5_ENVIRONMENT_DEFENSE")
        evidence["reduce_reason"] = "S5_ENVIRONMENT_DEFENSE"
        tracking_ma = "MA20" if category in {"BROAD", "GOLD", "UNVERIFIED"} else "MA10"
        tracking_value = number("ma20" if tracking_ma == "MA20" else "ma10")
        tracking_price = max(position.entry_price, tracking_value or position.entry_price)
        evidence.update({"tracking_ma": tracking_ma, "tracking_price": tracking_price, "tracking_floor": position.entry_price})
        decision_stop = max(position.stop_price, tracking_price)
    elif not position.reduced and half > 0 and stage_reduce_at_r is not None and profit_r >= stage_reduce_at_r:
        action = "REDUCE"
        planned = half
        reasons.append("S8_VALUATION_STAGE")
        evidence["reduce_reason"] = "S8_VALUATION_STAGE"
        tracking_ma = "MA20" if category in {"BROAD", "GOLD", "UNVERIFIED"} else "MA10"
        tracking_value = number("ma20" if tracking_ma == "MA20" else "ma10")
        tracking_price = max(position.entry_price, tracking_value or position.entry_price)
        evidence.update({"tracking_ma": tracking_ma, "tracking_price": tracking_price, "tracking_floor": position.entry_price})
        decision_stop = max(position.stop_price, tracking_price)
    elif not position.reduced and half > 0 and (
        s1 or s2 or s3 or s4 or s6 or s7
    ):
        action = "REDUCE"
        planned = half
        reduce_reason = (
            "S1_BIAS20" if s1 else "S2_RSI_BOLLINGER" if s2 else
            "S3_EMOTION" if s3 else "S4_PRESSURE" if s4 else
            "S6_PREMIUM" if s6 else "S7_HOLIDAY"
        )
        evidence["reduce_reason"] = reduce_reason
        reasons.append(reduce_reason)
        tracking_ma = "MA20" if category in {"BROAD", "GOLD", "UNVERIFIED"} else "MA10"
        tracking_value = number("ma20" if tracking_ma == "MA20" else "ma10")
        tracking_price = max(position.entry_price, tracking_value or position.entry_price)
        evidence.update({"tracking_ma": tracking_ma, "tracking_price": tracking_price, "tracking_floor": position.entry_price})
        decision_stop = max(position.stop_price, tracking_price)
    elif (
        profit_r >= 2.0
        and position.tracking_price is None
        and not position.tracking_started
        and not position.reduced
    ):
        tracking_ma = "MA20" if category in {"BROAD", "GOLD", "UNVERIFIED"} else "MA10"
        tracking_value = number("ma20" if tracking_ma == "MA20" else "ma10")
        tracking_price = max(position.entry_price, tracking_value or position.entry_price)
        evidence.update({"tracking_ma": tracking_ma, "tracking_price": tracking_price, "tracking_floor": position.entry_price})
        # A tracking transition may only raise the protective stop; it never
        # moves an already tighter stop lower.
        decision_stop = max(position.stop_price, tracking_price)
        action = "TRACK"
        reasons.append("PROFIT_2R_TRACKING")
    elif position.holding_session >= 25 and 1.0 <= profit_r < 2.0:
        tracking_value = number("ma20")
        tracking_price = max(position.entry_price, tracking_value or position.entry_price)
        evidence.update({"tracking_ma": "MA20", "tracking_price": tracking_price, "tracking_floor": position.entry_price})
        decision_stop = max(position.stop_price, tracking_price)
        action = "TRACK"
        reasons.extend(("T25_TRACKING", "T25"))
    elif profit_r >= 1.0 and position.stop_price < position.entry_price and position.tracking_price is None and not position.tracking_started:
        decision_stop = position.entry_price
        evidence.update({"new_stop": position.entry_price, "breakeven": True})
        action = "MOVE_STOP"
        reasons.append("PROFIT_1R_BREAKEVEN")
    else:
        cooldown = number("reentry_cooldown_sessions")
        if cooldown is None:
            cooldown = float(position.cooldown_sessions)
        if cooldown is not None and cooldown > 0:
            reasons.append("REENTRY_COOLDOWN")
        related_group_occupied = flag("related_group_occupied") or bool(context.metadata.get("related_group_occupied"))
        stronger_related_signal = flag("stronger_related_signal") or bool(context.metadata.get("stronger_related_signal"))
        if related_group_occupied and stronger_related_signal:
            reasons.append("NO_SWITCH")
        standard = number("standard_shares", "target_shares")
        if standard is None and position.standard_shares is not None:
            standard = float(position.standard_shares)
        bias_low, bias_high = (-2.0, 4.0) if category in {"BROAD", "GOLD", "UNVERIFIED"} else (-3.0, 6.0)
        trigger = any(flag(key) for key in (
            "topup_pullback_ok", "macd_dif_zero_cross", "half_reason_cleared",
        ))
        topup_ok = (
            action == "HOLD" and environment == "ATTACK" and profit_r >= 0.0
            and position.holding_session <= 15 and not position.topup_done
            and (bias is not None and bias_low <= bias <= bias_high)
            and flag("portfolio_room") and trigger
            and stage_allow_topup
            and not (cooldown is not None and cooldown > 0)
            and not (related_group_occupied and stronger_related_signal)
            and standard is not None and standard > position.shares
        )
        topup_shares = _lot_floor((standard - position.shares) if standard is not None else 0.0, config.lot_size)
        if topup_ok and topup_shares > 0:
            action = "TOP_UP"
            planned = topup_shares
            reasons[:] = ["TOP_UP"]
            evidence["topup_trigger"] = True
            evidence["topup_standard_shares"] = int(standard)
            pullback_low = number("topup_pullback_low")
            if pullback_low is not None:
                decision_stop = max(decision_stop, pullback_low * 0.99)

    state = V11State.POSITION_ACTION if action != "HOLD" else V11State.OBSERVE
    evidence["action"] = action
    evidence["planned_shares"] = planned
    return V11Decision(
        strategy_version=config.strategy_version,
        state=state,
        setup=V11Setup.NONE,
        action=action,
        executable=False,
        blocked_reasons=tuple(dict.fromkeys(reasons)),
        evidence=evidence,
        planned_shares=planned,
        planned_notional_cny=planned * position.current_price,
        stop_price=decision_stop,
        as_of_kind=context.as_of_kind,
        as_of_trading_date=context.as_of_trading_date,
    )


def _average(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _completed_weekly(
    bars: Sequence[DailyBar],
    closed_dates: Iterable[date] | None = None,
) -> dict[str, object]:
    grouped: list[tuple[tuple[int, int], DailyBar]] = []
    for bar in bars:
        key = (bar.trading_date.isocalendar().year, bar.trading_date.isocalendar().week)
        if grouped and grouped[-1][0] == key:
            grouped[-1] = (key, bar)
        else:
            grouped.append((key, bar))
    closures = frozenset(closed_dates or ())
    complete: list[DailyBar] = []
    for index, (_, bar) in enumerate(grouped):
        if index < len(grouped) - 1:
            complete.append(bar)
            continue
        # A trailing week is complete when Friday traded.  On a holiday
        # Friday, the calendar confirms that no later trading day remains.
        if bar.trading_date.weekday() == 4:
            complete.append(bar)
            continue
        candidate = bar.trading_date + timedelta(days=1)
        remaining = False
        while candidate.isocalendar()[:2] == bar.trading_date.isocalendar()[:2]:
            if candidate.weekday() < 5 and candidate not in closures:
                remaining = True
                break
            candidate += timedelta(days=1)
        if not remaining and closures:
            complete.append(bar)
    closes = [bar.adjusted_close for bar in complete]
    current_ma20 = _average(closes, 20)
    previous_ma20 = _average(closes[:-10], 20) if len(closes) >= 30 else None
    ma10_history = [
        _average(closes[:index + 1], 10)
        for index in range(len(closes))
        if len(closes[:index + 1]) >= 10
    ]
    ma10_down_3w = bool(
        len(ma10_history) >= 4
        and ma10_history[-1] < ma10_history[-2]
        and ma10_history[-2] < ma10_history[-3]
        and ma10_history[-3] < ma10_history[-4]
    )
    return {
        "status": "READY" if len(closes) >= 20 else "WARMUP",
        "bar_count": len(closes),
        "close": closes[-1] if closes else None,
        "ma10": _average(closes, 10),
        "ma10_history": tuple(ma10_history),
        "ma10_down_3w": ma10_down_3w,
        "ma10_not_down_3w": not ma10_down_3w,
        "ma20": current_ma20,
        "ma20_slope_pct_10d": (
            (current_ma20 / previous_ma20 - 1.0) * 100.0
            if current_ma20 is not None and previous_ma20 is not None else None
        ),
        "as_of_trading_date": complete[-1].trading_date.isoformat() if complete else None,
    }


def calculate_v11_indicators(
    bars: Sequence[DailyBar],
    *,
    closed_dates: Iterable[date] | None = None,
) -> dict[str, object]:
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

    # Pullback evidence is measured from the latest prior swing high to the
    # lowest close/low in the trailing fifteen completed sessions.  Keep the
    # raw reference values so the evaluator can apply the category threshold.
    search_start = max(0, len(closes) - 15)
    recovery_index = len(closes) - 1
    low_index = min(range(search_start, len(lows)), key=lows.__getitem__) if lows else None
    start_index = None
    if low_index is not None:
        before_low = range(search_start, low_index + 1)
        start_index = max(before_low, key=highs.__getitem__) if before_low else low_index
    pullback_start_high = highs[start_index] if start_index is not None else None
    pullback_low = lows[low_index] if low_index is not None else None
    pullback_sessions = (
        recovery_index - start_index + 1
        if start_index is not None else None
    )
    pullback_depth_pct = (
        (pullback_start_high - pullback_low) / pullback_start_high * 100.0
        if pullback_start_high and pullback_low is not None and pullback_start_high > 0.0
        else None
    )
    ma10_series = [
        _average(closes[:index + 1], 10)
        for index in range(len(closes))
    ]
    ma20_series = [
        _average(closes[:index + 1], 20)
        for index in range(len(closes))
    ]
    recovery_candidates = [
        index for index in range(max(0, len(closes) - 3), len(closes))
        if (low_index is None or (index >= low_index and index - low_index <= 3))
        and (
            (ma10_series[index] is not None and closes[index] > ma10_series[index])
            or (ma20_series[index] is not None and closes[index] > ma20_series[index])
        )
    ]
    recovery_day_index = recovery_candidates[-1] if recovery_candidates else None
    pullback_recovery_ok = recovery_day_index is not None
    recovery_open = (
        materialized[recovery_day_index].adjusted_open
        if recovery_day_index is not None else None
    )
    recovery_high = highs[recovery_day_index] if recovery_day_index is not None else None
    recovery_close = closes[recovery_day_index] if recovery_day_index is not None else None
    recovery_low_price = lows[recovery_day_index] if recovery_day_index is not None else None
    recovery_body = (
        abs(recovery_close - recovery_open)
        if recovery_close is not None and recovery_open is not None else None
    )
    recovery_upper_shadow = (
        recovery_high - max(recovery_open, recovery_close)
        if recovery_high is not None and recovery_open is not None and recovery_close is not None
        else None
    )
    recovery_long_upper_shadow = bool(
        recovery_upper_shadow is not None and recovery_body is not None
        and recovery_low_price is not None
        and recovery_upper_shadow > max(recovery_body * 2.0, (recovery_high - recovery_low_price) * 0.5)
    )
    pullback_days = (
        range(max(search_start, start_index or search_start), (low_index or recovery_index) + 1)
        if start_index is not None and low_index is not None else range(0)
    )
    contraction_flags: list[bool] = []
    for index in pullback_days:
        prior = _average(volumes[:index], 20)
        if prior is not None and prior > 0.0:
            contraction_flags.append(volumes[index] < prior)
    volume_contraction_majority = bool(
        contraction_flags and sum(contraction_flags) > len(contraction_flags) / 2.0
    )
    pullback_window_ok = bool(
        pullback_sessions is not None and 5 <= pullback_sessions <= 15
        and pullback_depth_pct is not None and pullback_depth_pct >= 3.0
    ) or bool(
        pullback_low is not None and moving.get("ma10") is not None
        and pullback_low <= moving["ma10"] * 1.01
    )
    volume_contraction_ok = volume_contraction_majority or bool(
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
    )
    rsi = snapshot["rsi"].get("rsi14")
    previous_rsi = latest_context.get("rsi_previous_1")
    rsi_values = [
        _rsi_wilder(closes[:index + 1])
        for index in range(max(0, (low_index or max(0, len(closes) - 15)) - 1), len(closes))
    ]
    valid_pullback_rsi = [value for value in rsi_values[:-1] if value is not None]
    rsi_pullback_min = min(valid_pullback_rsi) if valid_pullback_rsi else None
    rsi_pullback_never_below_40 = bool(
        valid_pullback_rsi and rsi_pullback_min >= 40.0
    )
    rsi_trigger = bool(
        rsi_pullback_min is not None and rsi_pullback_min >= 40.0
        and rsi is not None and rsi > 50.0
    )
    macd_dif_value = macd.get("dif")
    macd_dea_value = macd.get("dea")
    macd_dif_above_dea = bool(
        type(macd_dif_value) in (int, float)
        and type(macd_dea_value) in (int, float)
        and not isinstance(macd_dif_value, bool)
        and not isinstance(macd_dea_value, bool)
        and macd_dif_value > macd_dea_value
    )
    volume_recovery_trigger = bool(
        current_volume is not None and prior_volume_average is not None
        and current_volume >= prior_volume_average * 1.2
        and (len(closes) < 2 or closes[-1] > closes[-2])
    )
    box_size = len(prior_closes)
    box_half = box_size // 2
    box_first_half = prior_closes[:box_half]
    box_latter_half = prior_closes[box_half:]
    box_first_half_low = min(box_first_half) if box_first_half else None
    box_latter_half_low = min(box_latter_half) if box_latter_half else None
    box_ok = bool(
        len(prior_closes) >= 25
        and box_first_half_low is not None and box_latter_half_low is not None
        and box_latter_half_low >= box_first_half_low
    )
    box_breakout_ok = bool(
        box_ok and box_high is not None and closes[-1] > box_high
    )
    weekly = _completed_weekly(materialized, closed_dates)
    weekly_above_ma10 = bool(
        weekly.get("close") is not None and weekly.get("ma10") is not None
        and weekly["close"] > weekly["ma10"]
    )
    return_60d_pct = (
        (closes[-1] / closes[-61] - 1.0) * 100.0
        if len(closes) >= 61 else None
    )
    return_250d_pct = (
        (closes[-1] / closes[-251] - 1.0) * 100.0
        if len(closes) >= 251 else None
    )
    ma250_prior = _average(closes[:-20], 250) if len(closes) >= 270 else None
    ma250 = moving.get("ma250")
    ma250_slope_pct_20d = (
        (ma250 / ma250_prior - 1.0) * 100.0
        if ma250 is not None and ma250_prior not in (None, 0.0) else None
    )
    bollinger = snapshot.get("bollinger") or {}
    histogram = macd.get("histogram")
    previous_histogram = latest_context.get("macd_histogram_previous_1")
    previous_previous_histogram = latest_context.get("macd_histogram_previous_2")
    macd_histogram_improving_2d = bool(
        type(histogram) in (int, float)
        and type(previous_histogram) in (int, float)
        and type(previous_previous_histogram) in (int, float)
        and histogram > previous_histogram > previous_previous_histogram
    )
    return {
        "status": "READY" if len(materialized) >= 250 else "WARMUP",
        "bar_count": len(materialized),
        "as_of_trading_date": materialized[-1].trading_date.isoformat() if materialized else None,
        "price": closes[-1] if closes else None,
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
        "return_60d_pct": return_60d_pct,
        "return_250d_pct": return_250d_pct,
        "ma250_slope_pct_20d": ma250_slope_pct_20d,
        "bollinger_upper": bollinger.get("upper"),
        "pullback_start_high": pullback_start_high,
        "pullback_start_date": (
            materialized[start_index].trading_date.isoformat()
            if start_index is not None else None
        ),
        "pullback_low": pullback_low,
        "pullback_low_date": (
            materialized[low_index].trading_date.isoformat()
            if low_index is not None else None
        ),
        "pullback_window_sessions": pullback_sessions,
        "pullback_depth_pct": pullback_depth_pct,
        "pullback_recovery_within_3d": pullback_recovery_ok,
        "recovery_long_upper_shadow": recovery_long_upper_shadow,
        "recovery_day_date": (
            materialized[recovery_day_index].trading_date.isoformat()
            if recovery_day_index is not None else None
        ),
        "volume_contraction_majority": volume_contraction_majority,
        "rsi_pullback_min": rsi_pullback_min,
        "rsi_pullback_never_below_40": rsi_pullback_never_below_40,
        "rsi_current": rsi,
        "rsi_previous_1": previous_rsi,
        "rsi_crossed_above_50": bool(
            rsi is not None and previous_rsi is not None
            and previous_rsi <= 50.0 and rsi > 50.0
        ),
        "macd_histogram_improving_2d": macd_histogram_improving_2d,
        "macd_dif_above_dea": macd_dif_above_dea,
        "weekly": weekly,
        "setups": {
            "pullback_window_ok": pullback_window_ok,
            "pullback_recovery_ok": pullback_recovery_ok,
            "volume_contraction_ok": volume_contraction_ok,
            "macd_trigger": macd_trigger,
            "macd_dif_above_dea": macd_dif_above_dea,
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
            "box_first_half_low": box_first_half_low,
            "box_latter_half_low": box_latter_half_low,
            "box_days": box_size,
            "latest_context": latest_context,
            "recent_context": recent_context,
        },
    }


def normalize_v11_indicators(indicators: Mapping[str, object]) -> dict[str, object]:
    """Normalize nested indicator output to the evaluator's flat contract."""
    if not isinstance(indicators, Mapping):
        raise TypeError("indicator context must be a mapping")
    if not indicators:
        return {}
    raw = dict(indicators)
    moving = raw.get("moving_averages")
    moving = moving if isinstance(moving, Mapping) else {}
    weekly = raw.get("weekly")
    weekly = weekly if isinstance(weekly, Mapping) else {}
    volume = raw.get("volume")
    volume = volume if isinstance(volume, Mapping) else {}
    setups = raw.get("setups")
    setups = setups if isinstance(setups, Mapping) else {}
    macd = raw.get("macd")
    macd = macd if isinstance(macd, Mapping) else {}
    bias = raw.get("bias20")
    bias = bias if isinstance(bias, Mapping) else {}
    latest = raw.get("latest")
    latest = latest if isinstance(latest, Mapping) else {}
    flat = dict(raw)
    flat.update({
        "bar_count": raw.get("bar_count"),
        "price": raw.get("price", latest.get("price", raw.get("as_of_price"))),
        "ma10": raw.get("ma10", moving.get("ma10")),
        "ma20": raw.get("ma20", moving.get("ma20")),
        "ma60": raw.get("ma60", moving.get("ma60")),
        "ma250": raw.get("ma250", moving.get("ma250")),
        "atr14": raw.get("atr14"),
        "ma20_slope_pct_10d": raw.get("ma20_slope_pct_10d", moving.get("ma20_slope_pct_10d")),
        "weekly_close": raw.get("weekly_close", weekly.get("close")),
        "weekly_ma10": raw.get("weekly_ma10", weekly.get("ma10")),
        "weekly_ma20": raw.get("weekly_ma20", weekly.get("ma20")),
        "weekly_ma20_slope_pct_10d": raw.get(
            "weekly_ma20_slope_pct_10d", weekly.get("ma20_slope_pct_10d")
        ),
        "weekly_ma10_down_3w": raw.get(
            "weekly_ma10_down_3w",
            raw.get("weekly_ma10_continuously_down_3w", weekly.get("ma10_down_3w")),
        ),
        "weekly_ma10_not_down_3w": raw.get(
            "weekly_ma10_not_down_3w", weekly.get("ma10_not_down_3w")
        ),
        "bias20_pct": raw.get("bias20_pct", bias.get("value")),
        "volume_ratio20": raw.get("volume_ratio20", volume.get("ratio20")),
        "pullback_window_ok": bool(raw.get("pullback_window_ok", setups.get("pullback_window_ok"))),
        "pullback_recovery_ok": bool(raw.get("pullback_recovery_ok", setups.get("pullback_recovery_ok"))),
        "volume_contraction_ok": bool(raw.get("volume_contraction_ok", setups.get("volume_contraction_ok"))),
        "macd_trigger": bool(raw.get("macd_trigger", setups.get("macd_trigger"))),
        "rsi_trigger": bool(raw.get("rsi_trigger", setups.get("rsi_trigger"))),
        "volume_recovery_trigger": bool(raw.get("volume_recovery_trigger", setups.get("volume_recovery_trigger"))),
        "box_ok": bool(raw.get("box_ok", setups.get("box_ok"))),
        "box_breakout_ok": bool(raw.get("box_breakout_ok", setups.get("box_breakout_ok"))),
        "macd_dif": raw.get("macd_dif", macd.get("dif")),
        "macd_dea": raw.get("macd_dea", macd.get("dea")),
        "macd_dif_above_dea": bool(raw.get(
            "macd_dif_above_dea", setups.get("macd_dif_above_dea"),
        )),
        "rsi_crossed_above_50": bool(raw.get(
            "rsi_crossed_above_50", setups.get("rsi_crossed_above_50"),
        )),
        "macd_dif_nonnegative": bool(raw.get("macd_dif_nonnegative", setups.get("macd_dif_nonnegative"))),
        "weekly_above_ma10": bool(raw.get("weekly_above_ma10", setups.get("weekly_above_ma10"))),
        "return_60d_pct": raw.get(
            "return_60d_pct", raw.get("return60d_pct", raw.get("return_60d"))
        ),
        "return_250d_pct": raw.get(
            "return_250d_pct", raw.get("return250d_pct", raw.get("return_250d"))
        ),
        "ma250_slope_pct_20d": raw.get(
            "ma250_slope_pct_20d", raw.get("ma250_rising_pct", moving.get("ma250_slope_pct_20d"))
        ),
        "bollinger_upper": raw.get("bollinger_upper", (raw.get("bollinger") or {}).get("upper") if isinstance(raw.get("bollinger"), Mapping) else None),
        "pullback_start_high": raw.get("pullback_start_high", setups.get("pullback_start_high")),
        "pullback_start_date": raw.get("pullback_start_date", setups.get("pullback_start_date")),
        "pullback_low": raw.get("pullback_low", setups.get("pullback_low")),
        "pullback_low_date": raw.get("pullback_low_date", setups.get("pullback_low_date")),
        "pullback_window_sessions": raw.get(
            "pullback_window_sessions", raw.get("pullback_sessions", setups.get("pullback_window_sessions"))
        ),
        "pullback_depth_pct": raw.get(
            "pullback_depth_pct", raw.get("pullback_pct", setups.get("pullback_depth_pct"))
        ),
        "pullback_recovery_within_3d": bool(raw.get("pullback_recovery_within_3d", setups.get("pullback_recovery_within_3d"))),
        "recovery_long_upper_shadow": bool(raw.get("recovery_long_upper_shadow", setups.get("recovery_long_upper_shadow"))),
        "volume_contraction_majority": bool(raw.get("volume_contraction_majority", setups.get("volume_contraction_majority"))),
        "rsi_pullback_min": raw.get("rsi_pullback_min", setups.get("rsi_pullback_min")),
        "rsi_pullback_never_below_40": bool(raw.get("rsi_pullback_never_below_40", setups.get("rsi_pullback_never_below_40"))),
        "rsi_current": raw.get("rsi_current", (raw.get("rsi") or {}).get("rsi14") if isinstance(raw.get("rsi"), Mapping) else None),
        "rsi_previous_1": raw.get("rsi_previous_1", setups.get("rsi_previous_1")),
        "macd_histogram_improving_2d": bool(raw.get("macd_histogram_improving_2d", setups.get("macd_histogram_improving_2d"))),
        "box_first_half_low": raw.get(
            "box_first_half_low", raw.get("box_first_half_close_low", setups.get("box_first_half_low"))
        ),
        "box_latter_half_low": raw.get(
            "box_latter_half_low", raw.get("box_second_half_low", setups.get("box_latter_half_low"))
        ),
        "box_days": raw.get("box_days", setups.get("box_days")),
    })
    normalized = {key: value for key, value in flat.items() if value is not None}
    # Preserve absence for required P1 evidence.  The evaluator uses absence
    # to distinguish an unverified contract from an explicit false result.
    for key in (
        "weekly_ma10_down_3w", "weekly_ma10_not_down_3w",
        "pullback_recovery_within_3d", "volume_contraction_majority",
        "recovery_long_upper_shadow", "rsi_pullback_never_below_40",
        "macd_histogram_improving_2d",
    ):
        if key not in raw and key not in setups:
            normalized.pop(key, None)
    return normalized


def calculate_relative_strength_20(
    symbol_bars: Sequence[DailyBar],
    environment_bars: Sequence[DailyBar],
) -> float | None:
    """Return the 20-session return difference in percentage points."""
    symbol = {bar.trading_date: bar for bar in symbol_bars}
    environment = {bar.trading_date: bar for bar in environment_bars}
    dates = sorted(set(symbol) & set(environment))
    if len(dates) < 21:
        return None
    start, end = dates[-21], dates[-1]
    symbol_return = symbol[end].adjusted_close / symbol[start].adjusted_close - 1.0
    environment_return = environment[end].adjusted_close / environment[start].adjusted_close - 1.0
    value = (symbol_return - environment_return) * 100.0
    return value if math.isfinite(value) else None


def calculate_v11_environment(
    index_indicators: Mapping[str, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    """Combine CSI 300/1000 evidence with two-day confirmation."""
    required = ("000300", "000852")
    if any(code not in index_indicators for code in required):
        csi300 = index_indicators.get("000300", ())
        if csi300:
            latest = csi300[-1]
            weekly_close = _number_or_none(latest.get("weekly_close"))
            weekly_ma20 = _number_or_none(latest.get("weekly_ma20"))
            weekly_slope = _number_or_none(latest.get("weekly_ma20_slope_pct_10d"))
            if weekly_slope is None:
                previous = _number_or_none(latest.get("weekly_ma20_prev"))
                if weekly_ma20 is not None and previous:
                    weekly_slope = (weekly_ma20 / previous - 1.0) * 100.0
            if (
                weekly_close is not None and weekly_ma20 is not None
                and weekly_slope is not None and weekly_close < weekly_ma20
                and weekly_slope < 0.0
            ):
                return {
                    "state": "DEFENSE", "health": "PARTIAL",
                    "hard_defense": True,
                    "hard_defense_reason": "CSI300_WEEKLY_BREAK",
                }
        return {"state": "UNKNOWN", "health": "UNAVAILABLE", "hard_defense": False}
    states: dict[str, tuple[str, ...]] = {}
    for code in required:
        values = tuple(index_indicators[code])
        if len(values) < 2:
            return {"state": "UNKNOWN", "health": "STALE", "hard_defense": False}
        states[code] = tuple(classify_v11_environment(value) for value in values[-2:])
    csi300_latest = index_indicators["000300"][-1]
    weekly_ma20_prev = _number_or_none(csi300_latest.get("weekly_ma20_prev"))
    weekly_slope = _number_or_none(csi300_latest.get("weekly_ma20_slope_pct_10d"))
    if weekly_slope is None and weekly_ma20_prev:
        current_weekly_ma20 = _number_or_none(csi300_latest.get("weekly_ma20"))
        if current_weekly_ma20 is not None:
            weekly_slope = (current_weekly_ma20 / weekly_ma20_prev - 1.0) * 100.0
    hard_defense = (
        _number_or_none(csi300_latest.get("weekly_close")) is not None
        and _number_or_none(csi300_latest.get("weekly_ma20")) is not None
        and float(csi300_latest["weekly_close"]) < float(csi300_latest["weekly_ma20"])
        and weekly_slope is not None and weekly_slope < 0.0
    )
    if hard_defense:
        state = "DEFENSE"
    elif any(value == "UNKNOWN" for values in states.values() for value in values):
        state = "UNKNOWN"
    elif all(value == "ATTACK" for values in states.values() for value in values):
        state = "ATTACK"
    elif all(value == "DEFENSE" for values in states.values() for value in values):
        state = "DEFENSE"
    else:
        state = "NEUTRAL"
    return {
        "state": state,
        "health": "OK",
        "hard_defense": hard_defense,
        "hard_defense_reason": "CSI300_WEEKLY_BREAK" if hard_defense else None,
        "states": states,
    }


_V11_CATEGORIES = frozenset({
    "BROAD", "SECTOR", "CROSS_BORDER", "GOLD",
})
_V11_ENVIRONMENT_INDICES = frozenset({"000300", "000852"})


def validate_v11_metadata(
    metadata: Mapping[str, object], enabled_symbols: Iterable[str],
) -> dict[str, tuple[str, ...]]:
    """Return field-level errors for enabled ETF V11 metadata."""
    errors: dict[str, tuple[str, ...]] = {}
    for symbol in enabled_symbols:
        item = metadata.get(symbol)
        reasons: list[str] = []
        if item is None:
            reasons.append("metadata")
        else:
            category = getattr(item, "category", None)
            if category not in _V11_CATEGORIES:
                reasons.append("category")
            environment_index = getattr(item, "environment_index", None)
            if category in {"CROSS_BORDER", "GOLD"}:
                if (
                    environment_index not in (None, "NONE")
                    and environment_index not in _V11_ENVIRONMENT_INDICES
                ):
                    reasons.append("environment_index")
            elif environment_index not in _V11_ENVIRONMENT_INDICES:
                reasons.append("environment_index")
            if not getattr(item, "correlation_group", None):
                reasons.append("correlation_group")
        if reasons:
            errors[symbol] = tuple(reasons)
    return errors


__all__ = [
    "V11Config", "V11Context", "V11Decision", "V11Position", "V11Setup", "V11State",
    "calculate_v11_indicators", "calculate_v11_environment", "calculate_relative_strength_20",
    "classify_v11_environment", "evaluate_v11", "evaluate_v11_position",
    "normalize_v11_indicators", "validate_v11_metadata",
    "load_v11_config", "size_v11_order",
]
