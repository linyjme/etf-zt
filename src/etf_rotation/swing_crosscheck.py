"""Independent-source crosscheck receipts for the swing daily history.

The verified-data gate (``swing_quality.assess_verified_quality``) requires a
receipt whose ``crosscheck_status`` is ``PASSED`` and whose
``adjustment_status`` is ``VERIFIED``.  Until now nothing produced either
value: the research manifest hard-coded ``PENDING`` / ``REVIEW``.  This module
compares the canonical (Eastmoney) bars with a second provider's raw and
front-adjusted series and records the outcome as evidence.  It never mutates
canonical bars and never upgrades data on its own; the manifest builder only
consumes a receipt whose ``data_version`` matches the history it describes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .swing_data import DailyBar
from .swing_research import _data_version


SHANGHAI = ZoneInfo("Asia/Shanghai")
CROSSCHECK_VERSION = "CROSSCHECK_V1"
# Label recorded on receipts produced from Tencent's kline feed.  The leading
# provider token must differ from the canonical bar's label for the receipt
# to count as independent.
INDEPENDENT_SOURCE_LABEL = "腾讯 fqkline 独立交叉核验 (web.ifzq.gtimg.cn)"
# Relative volume tolerance between providers (both report exchange lots).
DEFAULT_VOLUME_TOLERANCE = 0.005
# A front-adjustment event is a day where both the additive offset and the
# multiplicative factor move.  Providers round the adjusted close to the
# price tick, so the offset can drift by up to one tick and the ratio by up to
# one tick over the close between consecutive sessions without any event;
# 1.5 ticks separates a real distribution from that rounding noise.
_EVENT_OFFSET_TICKS = 1.5
_EVENT_RATIO_TICKS = 1.5


class CrosscheckError(ValueError):
    """Raised when the inputs cannot be compared safely."""


@dataclass(frozen=True)
class IndependentBar:
    """One completed daily bar from the independent provider.

    ``adjusted_close`` is ``None`` when the provider has not yet published the
    front-adjusted value for that session.
    """

    trading_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    adjusted_close: float | None = None

    def __post_init__(self) -> None:
        if type(self.trading_date) is not date:
            raise CrosscheckError("trading_date must be a date")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0.0:
                raise CrosscheckError(f"{name} must be a finite positive number")
        if type(self.volume) not in (int, float) or not math.isfinite(self.volume) or self.volume < 0.0:
            raise CrosscheckError("volume must be a finite nonnegative number")
        if self.adjusted_close is not None and (
            type(self.adjusted_close) not in (int, float)
            or not math.isfinite(self.adjusted_close)
            or self.adjusted_close <= 0.0
        ):
            raise CrosscheckError("adjusted_close must be a finite positive number")


@dataclass(frozen=True)
class AdjustmentEvent:
    """A front-adjustment step implied by close versus adjusted close."""

    trading_date: date
    # Change of the additive offset (raw close - adjusted close) in price
    # units; for a cash distribution this equals the payout per share.
    payout: float

    def to_dict(self) -> dict[str, object]:
        return {"trading_date": self.trading_date.isoformat(), "payout": self.payout}


@dataclass(frozen=True)
class CrosscheckReceipt:
    symbol: str
    source: str
    checked_at: str
    calculation_version: str
    sample_start: str | None
    sample_end: str | None
    bar_count: int
    compared_count: int
    data_version: str
    crosscheck_status: str
    adjustment_status: str
    mismatches: tuple[Mapping[str, object], ...] = ()
    adjustment_events: tuple[AdjustmentEvent, ...] = ()
    independent_adjustment_events: tuple[AdjustmentEvent, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "source": self.source,
            "checked_at": self.checked_at,
            "calculation_version": self.calculation_version,
            "sample_start": self.sample_start,
            "sample_end": self.sample_end,
            "bar_count": self.bar_count,
            "compared_count": self.compared_count,
            "data_version": self.data_version,
            "crosscheck_status": self.crosscheck_status,
            "adjustment_status": self.adjustment_status,
            "mismatches": [dict(item) for item in self.mismatches],
            "adjustment_events": [item.to_dict() for item in self.adjustment_events],
            "independent_adjustment_events": [
                item.to_dict() for item in self.independent_adjustment_events
            ],
            "warnings": list(self.warnings),
        }


def canonical_source_is_independent(source: str, independent_source: str) -> bool:
    """Return whether the canonical bars did not come from the crosscheck provider."""
    if not isinstance(source, str) or not isinstance(independent_source, str):
        return False
    canonical_provider = source.split(" ", 1)[0]
    independent_provider = independent_source.split(" ", 1)[0]
    return bool(canonical_provider) and canonical_provider != independent_provider


def adjustment_events(
    rows: Sequence[tuple[date, float, float]], *, price_tick: float,
) -> tuple[AdjustmentEvent, ...]:
    """Detect front-adjustment steps from ``(date, close, adjusted_close)`` rows.

    A subtractive series keeps the offset constant between events while its
    ratio drifts daily; a multiplicative series keeps the ratio constant while
    its offset drifts.  Requiring both to move isolates real events under
    either convention.
    """
    events: list[AdjustmentEvent] = []
    previous: tuple[float, float, float] | None = None
    offset_threshold = _EVENT_OFFSET_TICKS * price_tick
    for trading_date, close, adjusted_close in rows:
        offset = close - adjusted_close
        ratio = adjusted_close / close
        if previous is not None:
            previous_offset, previous_ratio, previous_close = previous
            ratio_threshold = _EVENT_RATIO_TICKS * price_tick / min(close, previous_close)
            if (
                abs(offset - previous_offset) > offset_threshold
                and abs(ratio - previous_ratio) > ratio_threshold
            ):
                events.append(AdjustmentEvent(
                    trading_date=trading_date,
                    payout=round(previous_offset - offset, 6),
                ))
        previous = (offset, ratio, close)
    return tuple(events)


def _validate_bars(bars: Sequence[DailyBar]) -> tuple[DailyBar, ...]:
    ordered = tuple(bars)
    if not ordered:
        raise CrosscheckError("crosscheck requires at least one canonical bar")
    if any(type(bar) is not DailyBar for bar in ordered):
        raise CrosscheckError("crosscheck requires DailyBar records")
    if len({bar.symbol for bar in ordered}) != 1:
        raise CrosscheckError("crosscheck requires one symbol")
    dates = [bar.trading_date for bar in ordered]
    if any(left >= right for left, right in zip(dates, dates[1:])):
        raise CrosscheckError("canonical bars must be strictly increasing")
    return ordered


def crosscheck_history(
    bars: Sequence[DailyBar],
    independent: Sequence[IndependentBar],
    *,
    source: str,
    checked_at: datetime,
    price_tick: float,
    volume_tolerance: float = DEFAULT_VOLUME_TOLERANCE,
) -> CrosscheckReceipt:
    """Compare canonical bars with an independent provider and return a receipt.

    ``crosscheck_status`` is ``PASSED`` only when every canonical session has
    an independent bar whose raw OHLC agree within one price tick and whose
    volume agrees within ``volume_tolerance``.  ``adjustment_status`` is
    ``VERIFIED`` only when both providers imply the same front-adjustment
    events (same dates, payouts within 1.5 ticks).  Anything else fails
    closed to ``FAILED`` / ``REVIEW``.
    """
    ordered = _validate_bars(bars)
    if type(price_tick) not in (int, float) or not math.isfinite(price_tick) or price_tick <= 0.0:
        raise CrosscheckError("price_tick must be a finite positive number")
    if (
        type(volume_tolerance) not in (int, float)
        or not math.isfinite(volume_tolerance) or volume_tolerance < 0.0
    ):
        raise CrosscheckError("volume_tolerance must be a finite nonnegative number")
    if not isinstance(source, str) or not source.strip():
        raise CrosscheckError("source must be a non-empty string")
    if type(checked_at) is not datetime or checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise CrosscheckError("checked_at must be a timezone-aware datetime")
    materialized = tuple(independent)
    if any(type(item) is not IndependentBar for item in materialized):
        raise CrosscheckError("independent bars must be IndependentBar records")
    by_date: dict[date, IndependentBar] = {}
    for item in materialized:
        if item.trading_date in by_date:
            raise CrosscheckError("independent bars contain a duplicate trading date")
        by_date[item.trading_date] = item

    symbol = ordered[0].symbol
    mismatches: list[dict[str, object]] = []
    warnings: list[str] = []
    compared = 0
    price_tolerance = float(price_tick)
    for bar in ordered:
        other = by_date.get(bar.trading_date)
        if other is None:
            mismatches.append({
                "trading_date": bar.trading_date.isoformat(),
                "field": "bar", "reason": "MISSING_INDEPENDENT_BAR",
            })
            continue
        compared += 1
        for name in ("open", "high", "low", "close"):
            canonical_value = float(getattr(bar, name))
            independent_value = float(getattr(other, name))
            tolerance = price_tolerance + 8.0 * max(
                math.ulp(canonical_value), math.ulp(independent_value),
            )
            if abs(canonical_value - independent_value) > tolerance:
                mismatches.append({
                    "trading_date": bar.trading_date.isoformat(),
                    "field": name, "reason": "PRICE_MISMATCH",
                    "canonical": canonical_value, "independent": independent_value,
                })
        reference = max(float(bar.volume), float(other.volume))
        if abs(float(bar.volume) - float(other.volume)) > volume_tolerance * reference:
            mismatches.append({
                "trading_date": bar.trading_date.isoformat(),
                "field": "volume", "reason": "VOLUME_MISMATCH",
                "canonical": float(bar.volume), "independent": float(other.volume),
            })
    if any(not canonical_source_is_independent(bar.source, source) for bar in ordered):
        mismatches.append({
            "trading_date": None, "field": "source", "reason": "SAME_PROVIDER",
        })
        warnings.append("CROSSCHECK_SOURCE_NOT_INDEPENDENT")

    canonical_events = adjustment_events(
        tuple((bar.trading_date, float(bar.close), float(bar.adjusted_close)) for bar in ordered),
        price_tick=price_tick,
    )
    independent_rows: list[tuple[date, float, float]] = []
    adjusted_missing = False
    for bar in ordered:
        other = by_date.get(bar.trading_date)
        if other is None or other.adjusted_close is None:
            adjusted_missing = True
            continue
        independent_rows.append((bar.trading_date, float(other.close), float(other.adjusted_close)))
    independent_events = adjustment_events(independent_rows, price_tick=price_tick)
    if adjusted_missing:
        warnings.append("INDEPENDENT_ADJUSTED_INCOMPLETE")
    events_agree = (
        not adjusted_missing
        and len(canonical_events) == len(independent_events)
        and all(
            left.trading_date == right.trading_date
            and abs(left.payout - right.payout) <= _EVENT_OFFSET_TICKS * price_tick
            for left, right in zip(canonical_events, independent_events)
        )
    )
    crosscheck_status = "PASSED" if not mismatches else "FAILED"
    adjustment_status = (
        "VERIFIED" if events_agree and crosscheck_status == "PASSED" else "REVIEW"
    )
    return CrosscheckReceipt(
        symbol=symbol,
        source=source,
        checked_at=checked_at.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        calculation_version=CROSSCHECK_VERSION,
        sample_start=ordered[0].trading_date.isoformat(),
        sample_end=ordered[-1].trading_date.isoformat(),
        bar_count=len(ordered),
        compared_count=compared,
        data_version=_data_version(ordered),
        crosscheck_status=crosscheck_status,
        adjustment_status=adjustment_status,
        mismatches=tuple(mismatches),
        adjustment_events=canonical_events,
        independent_adjustment_events=independent_events,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def write_receipts(
    path: Path,
    receipts: Sequence[CrosscheckReceipt],
    *,
    generated_at: datetime,
) -> dict[str, object]:
    """Atomically persist receipts keyed by symbol and return the payload."""
    if type(generated_at) is not datetime or generated_at.tzinfo is None:
        raise CrosscheckError("generated_at must be a timezone-aware datetime")
    items: dict[str, object] = {}
    for receipt in receipts:
        if type(receipt) is not CrosscheckReceipt:
            raise CrosscheckError("receipts must be CrosscheckReceipt records")
        if receipt.symbol in items:
            raise CrosscheckError("duplicate receipt symbol")
        items[receipt.symbol] = receipt.to_dict()
    payload: dict[str, object] = {
        "schema_version": 1,
        "generated_at": generated_at.astimezone(SHANGHAI).isoformat(timespec="seconds"),
        "calculation_version": CROSSCHECK_VERSION,
        "research_only": True,
        "items": dict(sorted(items.items())),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def load_receipts(path: Path | None) -> dict[str, Mapping[str, object]]:
    """Read a receipts file; a missing or malformed file yields no receipts."""
    if path is None:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        return {}
    items = payload.get("items")
    if not isinstance(items, Mapping):
        return {}
    return {
        symbol: item for symbol, item in items.items()
        if isinstance(symbol, str) and isinstance(item, Mapping)
    }


def receipt_applies(receipt: Mapping[str, object], bars: Sequence[DailyBar]) -> bool:
    """Return whether a stored receipt describes exactly these canonical bars."""
    if not isinstance(receipt, Mapping) or not bars:
        return False
    ordered = tuple(bars)
    if any(type(bar) is not DailyBar for bar in ordered):
        return False
    return (
        receipt.get("symbol") == ordered[0].symbol
        and receipt.get("calculation_version") == CROSSCHECK_VERSION
        and receipt.get("sample_start") == ordered[0].trading_date.isoformat()
        and receipt.get("sample_end") == ordered[-1].trading_date.isoformat()
        and receipt.get("data_version") == _data_version(ordered)
    )
