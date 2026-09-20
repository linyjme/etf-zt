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
from .swing_indicators import calculate_indicator_context


class ShadowVariant(StrEnum):
    V1 = "V1"
    V2_A = "V2_A"
    V2_B = "V2_B"
    V2_C = "V2_C"


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
    data_version: str = "sha256:unknown"
    indicator_version: str = "INDICATORS_V1"


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
) -> ShadowDecision:
    return ShadowDecision(
        strategy_version="SWING_V2_SHADOW",
        variant=variant,
        state=state,
        executable=False,
        blocked_reasons=tuple(dict.fromkeys(reasons)),
        evidence=dict(evidence),
        data_version=data_version,
        indicator_version=indicator_version,
        opportunity_id=_context_value(context, "opportunity_id", None),
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
    score = 0.0
    if trend_ok:
        score += 40.0
    if pullback_ok:
        score += 25.0
    if any(triggers):
        score += 25.0
    valuation = str(_context_value(context, "valuation_status", "UNKNOWN"))
    if valuation in {"ATTRACTIVE", "NEUTRAL"}:
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
    "ShadowVariant", "evaluate_shadow", "load_shadow_config",
]

