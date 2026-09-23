"""Read-only SWING_V2 shadow evaluators.

The shadow layer deliberately cannot produce executable shares or alerts for the
formal strategy.  It exists to compare explanations and event quality first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import json
import math
from pathlib import Path
from typing import Any

from .swing_data import DailyBar
from .swing_indicators import IndicatorInputError, calculate_indicator_context


class ShadowVariant(StrEnum):
    V1 = "V1"
    V2_A = "V2_A"
    V2_B = "V2_B"
    V2_C = "V2_C"
    HYBRID = "HYBRID"


class ShadowState(StrEnum):
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    OBSERVE = "OBSERVE"
    TECHNICAL_CANDIDATE = "TECHNICAL_CANDIDATE"
    RANGE_BLOCKED = "RANGE_BLOCKED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class ShadowConfig:
    schema_version: int
    strategy_version: str
    event_window_sessions: int
    rsi_lower: float
    rsi_upper: float
    kdj_j_max: float
    atr_distance_max: float
    macd_histogram_rising_days: int
    min_walk_forward_bars: int
    variants: tuple[ShadowVariant, ...]


@dataclass(frozen=True)
class ShadowContext:
    snapshot_only: bool = False
    data_quality: str = "VERIFIED"
    data_healthy: bool = True
    account_known: bool = True
    cost_ok: bool = True
    risk_ok: bool = True
    valuation_status: str = "UNKNOWN"
    trend_state: str | None = None
    range_confirmed: bool = False
    uncertain: bool = False
    opportunity_id: str | None = None
    opportunity_status: str | None = None
    data_version: str = "sha256:unknown"
    indicator_version: str = "INDICATORS_V1"


def infer_shadow_regime(
    bars: Sequence[DailyBar],
) -> tuple[str, Mapping[str, object]]:
    """Classify the daily shadow mode without defaulting missing evidence to TREND.

    This is deliberately conservative: a RANGE requires low path efficiency,
    both sides of the 20-day mean, and a small mean slope; a TREND requires
    aligned price/MA direction, slope, and path efficiency.  Everything else
    remains UNCERTAIN so V2-C cannot silently use a trend rule on ambiguous data.
    """
    try:
        materialized = tuple(bars)
        if any(type(bar) is not DailyBar for bar in materialized):
            raise ValueError("invalid daily bars")
        closes = [float(bar.adjusted_close) for bar in materialized]
        if any(not math.isfinite(value) or value <= 0.0 for value in closes):
            raise ValueError("invalid daily closes")
    except (IndicatorInputError, ValueError, TypeError, OverflowError):
        return "UNCERTAIN", {"reason": "INDICATOR_CONTEXT_UNAVAILABLE"}
    if len(materialized) < 60:
        return "UNCERTAIN", {
            "reason": "INSUFFICIENT_DAILY_REGIME_SAMPLE",
            "bar_count": len(materialized),
        }
    ma20 = sum(closes[-20:]) / 20.0
    ma60 = sum(closes[-60:]) / 60.0
    prior_ma20 = sum(closes[-25:-5]) / 20.0
    prior_ma60 = sum(closes[-70:-10]) / 60.0 if len(closes) >= 70 else None
    if prior_ma60 in (None, 0.0):
        return "UNCERTAIN", {"reason": "REGIME_MA_SLOPE_UNAVAILABLE"}
    slope20 = (ma20 / prior_ma20 - 1.0) * 100.0
    slope60 = (ma60 / prior_ma60 - 1.0) * 100.0
    closes = closes[-20:]
    path = sum(abs(current - previous) for previous, current in zip(closes, closes[1:]))
    efficiency = abs(closes[-1] - closes[0]) / path if path > 0.0 else 0.0
    ma20_values = [
        sum(float(bar.adjusted_close) for bar in materialized[index - 19:index + 1]) / 20.0
        for index in range(len(materialized) - 20, len(materialized))
    ]
    above = sum(close >= float(mean) for close, mean in zip(closes, ma20_values))
    below = len(closes) - above
    one_side_ratio = max(above, below) / len(closes)
    evidence = {
        "path_efficiency_20d": efficiency,
        "ma20_slope_pct_5d": float(slope20),
        "ma60_slope_pct_10d": float(slope60),
        "ma20_above_count": above,
        "ma20_below_count": below,
        "one_side_ratio": one_side_ratio,
        "as_of_trading_date": materialized[-1].trading_date.isoformat(),
    }
    range_ok = (
        efficiency <= 0.30
        and above >= 4
        and below >= 4
        and one_side_ratio <= 0.70
        and abs(float(slope20)) <= 0.20
        and abs(float(slope60)) <= 0.20
    )
    close = closes[-1]
    trend_up = (
        close > float(ma60) and float(ma20) > float(ma60)
        and float(slope60) >= 0.10 and efficiency >= 0.35
    )
    trend_down = (
        close < float(ma60) and float(ma20) < float(ma60)
        and float(slope60) <= -0.10 and efficiency >= 0.35
    )
    if range_ok:
        return "RANGE", evidence
    if trend_up or trend_down:
        evidence["direction"] = "UP" if trend_up else "DOWN"
        return "TREND", evidence
    return "UNCERTAIN", evidence


@dataclass(frozen=True)
class ShadowDecision:
    strategy_version: str
    variant: ShadowVariant
    state: ShadowState
    executable: bool
    blocked_reasons: tuple[str, ...]
    evidence: Mapping[str, object]
    data_version: str
    indicator_version: str
    opportunity_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_version": self.strategy_version,
            "variant": self.variant.value,
            "state": self.state.value,
            "executable": self.executable,
            "blocked_reasons": list(self.blocked_reasons),
            "evidence": dict(self.evidence),
            "data_version": self.data_version,
            "indicator_version": self.indicator_version,
            "opportunity_id": self.opportunity_id,
        }


_CONFIG_KEYS = frozenset({
    "schema_version", "strategy_version", "event_window_sessions", "rsi_lower",
    "rsi_upper", "kdj_j_max", "atr_distance_max",
    "macd_histogram_rising_days", "min_walk_forward_bars", "variants",
})


def load_shadow_config(path: Path) -> ShadowConfig:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("shadow config is not valid JSON") from error
    if not isinstance(payload, dict) or frozenset(payload) != _CONFIG_KEYS:
        raise ValueError("shadow config keys are invalid")
    if payload["schema_version"] != 1 or payload["strategy_version"] != "SWING_V2_SHADOW":
        raise ValueError("shadow config version is invalid")
    if type(payload["event_window_sessions"]) is not int or payload["event_window_sessions"] <= 0:
        raise ValueError("event_window_sessions must be positive")
    if type(payload["macd_histogram_rising_days"]) is not int or payload["macd_histogram_rising_days"] <= 0:
        raise ValueError("macd_histogram_rising_days must be positive")
    if type(payload["min_walk_forward_bars"]) is not int or payload["min_walk_forward_bars"] <= 0:
        raise ValueError("min_walk_forward_bars must be positive")
    numeric = (
        "rsi_lower", "rsi_upper", "kdj_j_max", "atr_distance_max",
    )
    values: dict[str, float] = {}
    for key in numeric:
        value = payload[key]
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f"{key} must be finite")
        values[key] = float(value)
    if not 0.0 <= values["rsi_lower"] < values["rsi_upper"] <= 100.0:
        raise ValueError("RSI bounds are invalid")
    if not 0.0 < values["kdj_j_max"] <= 100.0 or values["atr_distance_max"] <= 0.0:
        raise ValueError("shadow thresholds are invalid")
    variants = payload["variants"]
    if not isinstance(variants, list):
        raise ValueError("variants must be a list")
    try:
        parsed_variants = tuple(ShadowVariant(item) for item in variants)
    except (TypeError, ValueError) as error:
        raise ValueError("unknown shadow variant") from error
    if set(parsed_variants) != {ShadowVariant.V2_A, ShadowVariant.V2_B, ShadowVariant.V2_C}:
        raise ValueError("shadow config must enable V2-A/B/C")
    return ShadowConfig(
        schema_version=1,
        strategy_version="SWING_V2_SHADOW",
        event_window_sessions=payload["event_window_sessions"],
        rsi_lower=values["rsi_lower"],
        rsi_upper=values["rsi_upper"],
        kdj_j_max=values["kdj_j_max"],
        atr_distance_max=values["atr_distance_max"],
        macd_histogram_rising_days=payload["macd_histogram_rising_days"],
        min_walk_forward_bars=payload["min_walk_forward_bars"],
        variants=parsed_variants,
    )


def _context_value(context: ShadowContext | Mapping[str, object], key: str, default: object) -> object:
    if isinstance(context, ShadowContext):
        return getattr(context, key)
    if isinstance(context, Mapping):
        return context.get(key, default)
    raise ValueError("context must be ShadowContext or mapping")


def _atr(bars: Sequence[DailyBar], period: int = 14) -> float | None:
    if len(bars) < 2:
        return None
    ranges = []
    for index in range(max(1, len(bars) - period), len(bars)):
        current = bars[index]
        previous = bars[index - 1].adjusted_close
        ranges.append(max(
            current.adjusted_high - current.adjusted_low,
            abs(current.adjusted_high - previous),
            abs(current.adjusted_low - previous),
        ))
    result = sum(ranges) / len(ranges) if ranges else None
    return result if result is not None and math.isfinite(result) and result > 0 else None


def _decision(
    variant: ShadowVariant,
    state: ShadowState,
    reasons: Sequence[str],
    evidence: Mapping[str, object],
    context: ShadowContext | Mapping[str, object],
    indicator_version: str,
    data_version: str,
    strategy_version: str = "SWING_V2_SHADOW",
) -> ShadowDecision:
    return ShadowDecision(
        strategy_version=strategy_version,
        variant=variant,
        state=state,
        executable=False,
        blocked_reasons=tuple(dict.fromkeys(reasons)),
        evidence=dict(evidence),
        data_version=data_version,
        indicator_version=indicator_version,
        opportunity_id=_context_value(context, "opportunity_id", None),
    )


def evaluate_hybrid_shadow(
    bars: Sequence[DailyBar], *,
    context: ShadowContext | Mapping[str, object],
    indicator: Mapping[str, object] | None = None,
) -> ShadowDecision:
    """Evaluate the approved multi-factor swing shadow without changing V1.

    The hybrid deliberately scores trend, pullback, momentum, volume and
    weekly context instead of requiring a simultaneous MACD/KDJ/RSI signal.
    It remains research-only: even a clean candidate is never executable.
    """
    strategy_version = "SWING_HYBRID_SHADOW"
    try:
        indicator = indicator or calculate_indicator_context(bars, lookback=3)
    except ValueError as error:
        return _decision(
            ShadowVariant.HYBRID, ShadowState.DATA_UNAVAILABLE, ("DATA_ERROR",),
            {"error": str(error)}, context, "INDICATORS_V1", "sha256:unknown",
            strategy_version,
        )
    data_version = str(_context_value(context, "data_version", "sha256:unknown"))
    if data_version == "sha256:unknown":
        data_version = str(indicator["data_version"])
    indicator_version = str(
        _context_value(context, "indicator_version", indicator["indicator_version"])
    )
    latest = indicator.get("latest")
    if not isinstance(latest, Mapping):
        return _decision(
            ShadowVariant.HYBRID, ShadowState.DATA_UNAVAILABLE,
            ("NO_COMPLETED_BARS",), {}, context, indicator_version, data_version,
            strategy_version,
        )
    if indicator["status"] != "READY":
        return _decision(
            ShadowVariant.HYBRID, ShadowState.OBSERVE,
            ("INSUFFICIENT_COMPLETED_BARS",),
            {"bar_count": indicator["bar_count"], "status": indicator["status"]},
            context, indicator_version, data_version, strategy_version,
        )

    materialized = tuple(bars)
    latest_bar = materialized[-1]
    closes = [float(bar.adjusted_close) for bar in materialized]
    moving = latest.get("moving_averages", {})
    macd = latest.get("macd", {})
    rsi = latest.get("rsi", {})
    kdj = latest.get("kdj", {})
    bias = latest.get("bias20", {})
    bollinger = latest.get("bollinger", {})
    volume = latest.get("volume", {})
    weekly = latest.get("weekly", {})
    ma20 = moving.get("ma20") if isinstance(moving, Mapping) else None
    ma60 = moving.get("ma60") if isinstance(moving, Mapping) else None
    close = float(latest_bar.adjusted_close)
    atr = _atr(materialized)
    weekly_close = weekly.get("close") if isinstance(weekly, Mapping) else None
    weekly_ma20 = weekly.get("ma20") if isinstance(weekly, Mapping) else None
    trend_votes = (
        ma60 is not None and close > float(ma60),
        ma20 is not None and ma60 is not None and float(ma20) >= float(ma60),
        (latest.get("ma60_slope_pct_10d") or 0.0) >= -0.10,
        weekly_close is not None and weekly_ma20 is not None
        and float(weekly_close) >= float(weekly_ma20),
    )
    trend_score = sum(bool(value) for value in trend_votes)
    trend_ok = trend_score >= 3

    recent_peak = max(closes[-15:]) if closes else close
    drawdown_pct = (recent_peak / close - 1.0) * 100.0 if close > 0 else None
    distance = latest.get("close_ma20_atr_distance")
    near_ma20 = isinstance(distance, (int, float)) and float(distance) <= 1.5
    orderly_pullback = (
        isinstance(drawdown_pct, (int, float)) and 1.0 <= float(drawdown_pct) <= 8.0
    )
    bias_value = bias.get("value") if isinstance(bias, Mapping) else None
    not_overextended = (
        not isinstance(bias_value, (int, float)) or float(bias_value) <= 5.0
    )
    pullback_ok = (near_ma20 or orderly_pullback) and not_overextended

    dif = macd.get("dif") if isinstance(macd, Mapping) else None
    dea = macd.get("dea") if isinstance(macd, Mapping) else None
    histogram_ok = bool(
        isinstance(dif, (int, float)) and isinstance(dea, (int, float))
        and float(dif) >= float(dea)
        and int(latest.get("macd_histogram_rising_days") or 0) >= 2
    )
    rsi_value = rsi.get("rsi14") if isinstance(rsi, Mapping) else None
    rsi_ok = bool(
        isinstance(rsi_value, (int, float)) and 45.0 <= float(rsi_value) <= 68.0
        and int(latest.get("rsi_rising_days") or 0) >= 1
    )
    kdj_k = kdj.get("k") if isinstance(kdj, Mapping) else None
    kdj_d = kdj.get("d") if isinstance(kdj, Mapping) else None
    kdj_j = kdj.get("j") if isinstance(kdj, Mapping) else None
    kdj_ok = bool(
        isinstance(kdj_k, (int, float)) and isinstance(kdj_d, (int, float))
        and isinstance(kdj_j, (int, float)) and float(kdj_k) > float(kdj_d)
        and float(kdj_j) <= 90.0
    )
    momentum_score = int(histogram_ok) + int(rsi_ok) + int(kdj_ok)
    volume_ratio = volume.get("ratio20") if isinstance(volume, Mapping) else None
    volume_ok = bool(
        isinstance(volume_ratio, (int, float)) and float(volume_ratio) >= 1.05
    )
    middle = bollinger.get("middle") if isinstance(bollinger, Mapping) else None
    upper = bollinger.get("upper") if isinstance(bollinger, Mapping) else None
    band_ok = bool(
        isinstance(middle, (int, float)) and isinstance(upper, (int, float))
        and close >= float(middle) and close <= float(upper)
    )

    reasons: list[str] = []
    for key, reason in (
        ("data_healthy", "DATA_UNHEALTHY"),
        ("account_known", "ACCOUNT_UNKNOWN"),
        ("cost_ok", "COST_GATE"),
        ("risk_ok", "RISK_GATE"),
    ):
        if _context_value(context, key, True) is not True:
            reasons.append(reason)
    if _context_value(context, "snapshot_only", False) is True:
        reasons.append("SNAPSHOT_ONLY")
    quality = str(_context_value(context, "data_quality", "UNKNOWN"))
    if quality != "VERIFIED":
        reasons.append(
            "DATA_QUALITY_UNKNOWN"
            if quality in {"UNKNOWN", "MIXED"} else "DATA_QUALITY_UNVERIFIED"
        )
    mode = str(_context_value(context, "trend_state", "TREND"))
    if mode == "RANGE" or _context_value(context, "range_confirmed", False) is True:
        reasons.append("RANGE_MODE")
    if mode == "UNCERTAIN" or _context_value(context, "uncertain", False) is True:
        reasons.append("UNCERTAIN_MODE")
    if not trend_ok:
        reasons.append("TREND_SCORE_BELOW_THRESHOLD")
    if not pullback_ok:
        reasons.append("PULLBACK_NOT_CONFIRMED")
    if momentum_score < 2:
        reasons.append("MOMENTUM_SCORE_BELOW_THRESHOLD")
    if not (volume_ok or band_ok):
        reasons.append("VOLUME_OR_BAND_CONFIRMATION_MISSING")
    candidate = trend_ok and pullback_ok and momentum_score >= 2 and (volume_ok or band_ok)
    evidence = {
        "trend_score": trend_score,
        "trend_votes": list(trend_votes),
        "trend_ok": trend_ok,
        "pullback_ok": pullback_ok,
        "near_ma20": near_ma20,
        "orderly_pullback": orderly_pullback,
        "drawdown_pct_15d": drawdown_pct,
        "bias20_pct": bias_value,
        "not_overextended": not_overextended,
        "macd_trigger": histogram_ok,
        "rsi_trigger": rsi_ok,
        "kdj_trigger": kdj_ok,
        "momentum_score": momentum_score,
        "volume_ratio20": volume_ratio,
        "volume_confirmation": volume_ok,
        "bollinger_band_confirmation": band_ok,
        "candidate": candidate,
        "weekly_context": dict(weekly) if isinstance(weekly, Mapping) else {},
        "valuation_status": str(_context_value(context, "valuation_status", "UNKNOWN")),
        "mode": mode,
        "as_of_trading_date": indicator["as_of_trading_date"],
        "atr14_adjusted": atr,
    }
    if candidate and not reasons:
        state = ShadowState.TECHNICAL_CANDIDATE
    elif "RANGE_MODE" in reasons:
        state = ShadowState.RANGE_BLOCKED
    elif "UNCERTAIN_MODE" in reasons:
        state = ShadowState.UNCERTAIN
    else:
        state = ShadowState.OBSERVE
    return _decision(
        ShadowVariant.HYBRID, state, reasons, evidence, context,
        indicator_version, data_version, strategy_version,
    )


def evaluate_shadow(
    bars: Sequence[DailyBar], *, variant: ShadowVariant,
    config: ShadowConfig, context: ShadowContext | Mapping[str, object],
) -> ShadowDecision:
    """Evaluate a shadow variant with fail-closed research gates."""
    if not isinstance(variant, ShadowVariant):
        variant = ShadowVariant(variant)
    if type(config) is not ShadowConfig:
        raise ValueError("config must be ShadowConfig")
    if variant is ShadowVariant.V1:
        raise ValueError("V1 is the formal strategy, not a shadow evaluator")
    if variant not in config.variants:
        raise ValueError("variant is disabled by shadow config")
    try:
        indicator = calculate_indicator_context(bars, lookback=3)
    except ValueError as error:
        return _decision(
            variant, ShadowState.DATA_UNAVAILABLE, ("DATA_ERROR",),
            {"error": str(error)}, context, "INDICATORS_V1", "sha256:unknown",
        )
    return _evaluate_shadow_context(bars, variant=variant, config=config,
        context=context, indicator=indicator)


def _evaluate_shadow_context(bars: Sequence[DailyBar], *, variant: ShadowVariant,
    config: ShadowConfig, context: ShadowContext | Mapping[str, object],
    indicator: Mapping[str, object]) -> ShadowDecision:
    """Shared formulas; replay supplies only a verified prefix's indicator point."""
    data_version = str(_context_value(context, "data_version", "sha256:unknown"))
    if data_version == "sha256:unknown":
        data_version = str(indicator["data_version"])
    indicator_version = str(
        _context_value(context, "indicator_version", indicator["indicator_version"])
    )
    latest = indicator["latest"]
    if not isinstance(latest, Mapping):
        return _decision(
            variant, ShadowState.DATA_UNAVAILABLE, ("NO_COMPLETED_BARS",),
            {}, context, indicator_version, data_version,
        )
    if indicator["status"] != "READY":
        return _decision(
            variant, ShadowState.OBSERVE,
            ("INSUFFICIENT_COMPLETED_BARS",),
            {"bar_count": indicator["bar_count"], "status": indicator["status"]},
            context, indicator_version, data_version,
        )
    materialized = tuple(bars)
    latest_bar = materialized[-1]
    macd = latest["macd"]
    kdj = latest["kdj"]
    rsi = latest["rsi"]
    moving = latest["moving_averages"]
    atr = _atr(materialized)
    ma20 = moving["ma20"]
    ma60 = moving["ma60"]
    close = latest_bar.adjusted_close
    trend_ok = bool(
        ma20 is not None and ma60 is not None
        and close > ma60 and ma20 > ma60
        and (latest.get("ma60_slope_pct_10d") or 0.0) >= 0.0
    )
    pullback_ok = bool(
        ma20 is not None and atr is not None
        and abs(close - ma20) <= atr
        and latest_bar.adjusted_low <= ma20 + atr * 0.25
    )
    macd_ok = bool(
        latest.get("macd_histogram_rising_days", 0) >= config.macd_histogram_rising_days
        and macd["dif"] >= macd["dea"]
    )
    rsi_ok = bool(
        rsi["rsi14"] is not None
        and config.rsi_lower <= rsi["rsi14"] <= config.rsi_upper
        and latest.get("rsi_rising_days", 0) >= 1
    )
    kdj_ok = bool(
        kdj["k"] is not None and kdj["d"] is not None and kdj["j"] is not None
        and kdj["k"] > kdj["d"] and kdj["j"] <= config.kdj_j_max
    )
    triggers = (macd_ok, rsi_ok, kdj_ok)
    primary_trigger = next((name for name, ok in zip(("MACD", "RSI", "KDJ"), triggers) if ok), None)
    confirmation = None
    if ma20 is not None and atr is not None and close >= ma20 and abs(close - ma20) / atr <= config.atr_distance_max:
        confirmation = "MA20_RECLAIM"
    reasons: list[str] = []
    for key, reason in (
        ("data_healthy", "DATA_UNHEALTHY"),
        ("account_known", "ACCOUNT_UNKNOWN"),
        ("cost_ok", "COST_GATE"),
        ("risk_ok", "RISK_GATE"),
    ):
        if _context_value(context, key, True) is not True:
            reasons.append(reason)
    if _context_value(context, "snapshot_only", False) is True:
        reasons.append("SNAPSHOT_ONLY")
    quality = str(_context_value(context, "data_quality", "UNKNOWN"))
    if quality != "VERIFIED":
        reasons.append("DATA_QUALITY_UNKNOWN" if quality in {"UNKNOWN", "MIXED"} else "DATA_QUALITY_UNVERIFIED")
    mode = str(_context_value(context, "trend_state", "TREND"))
    range_confirmed = bool(_context_value(context, "range_confirmed", False))
    uncertain = bool(_context_value(context, "uncertain", False))
    if variant is ShadowVariant.V2_C and (range_confirmed or mode == "RANGE"):
        reasons.append("RANGE_MODE")
    if variant is ShadowVariant.V2_C and (uncertain or mode == "UNCERTAIN"):
        reasons.append("UNCERTAIN_MODE")
    opportunity_id = _context_value(context, "opportunity_id", None)
    opportunity_status = str(_context_value(context, "opportunity_status", ""))
    if variant is ShadowVariant.V2_A and (
        not opportunity_id or opportunity_status != "TECHNICAL_CANDIDATE"
    ):
        reasons.append("OPPORTUNITY_NOT_CONFIRMED")
    score = 0.0
    if trend_ok:
        score += 40.0
    if pullback_ok:
        score += 25.0
    if any(triggers):
        score += 25.0
    valuation = str(_context_value(context, "valuation_status", "UNKNOWN"))
    if valuation in {"LOW", "NORMAL", "VALUE", "FAIR", "DEEP_VALUE"}:
        score += 10.0
    evidence: dict[str, object] = {
        "trend_ok": trend_ok,
        "pullback_ok": pullback_ok,
        "macd_trigger": macd_ok,
        "rsi_trigger": rsi_ok,
        "kdj_trigger": kdj_ok,
        "available_momentum_trigger_count": sum(triggers),
        "momentum_trigger_count": int(primary_trigger is not None),
        "primary_momentum_trigger": primary_trigger,
        "confirmation_count": int(confirmation is not None),
        "confirmation": confirmation,
        "all_three_indicators_required": False,
        "score": score,
        "atr14_adjusted": atr,
        "valuation_status": valuation,
        "mode": mode,
        "as_of_trading_date": indicator["as_of_trading_date"],
    }
    if variant is ShadowVariant.V2_B:
        candidate = score >= 70.0
    else:
        candidate = trend_ok and pullback_ok and primary_trigger is not None and confirmation is not None
    if not trend_ok:
        reasons.append("TREND_NOT_CONFIRMED")
    if not pullback_ok:
        reasons.append("PULLBACK_NOT_CONFIRMED")
    if primary_trigger is None:
        reasons.append("MOMENTUM_NOT_CONFIRMED")
    if confirmation is None:
        reasons.append("CONFIRMATION_NOT_CONFIRMED")
    if candidate and not reasons:
        state = ShadowState.TECHNICAL_CANDIDATE
    elif variant is ShadowVariant.V2_C and (range_confirmed or mode == "RANGE"):
        state = ShadowState.RANGE_BLOCKED
    elif variant is ShadowVariant.V2_C and (uncertain or mode == "UNCERTAIN"):
        state = ShadowState.UNCERTAIN
    else:
        state = ShadowState.OBSERVE
    return _decision(
        variant, state, reasons, evidence, context, indicator_version, data_version,
    )


__all__ = [
    "ShadowConfig", "ShadowContext", "ShadowDecision", "ShadowState",
    "ShadowVariant", "evaluate_hybrid_shadow", "evaluate_shadow",
    "infer_shadow_regime", "load_shadow_config",
]
