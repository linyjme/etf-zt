"""Research-only history quality classification for the swing strategy.

The runtime monitor is intentionally kept independent from this module.  The
helpers here classify a completed daily-bar sample and produce a stable hash so
that backtests can record exactly which history they used.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from hashlib import sha256
import json

from .swing_data import DailyBar


class ResearchError(ValueError):
    """Raised when a research sample cannot be interpreted safely."""


class ResearchStatus(StrEnum):
    """Research usability status, deliberately separate from runtime state."""

    VERIFIED = "VERIFIED"
    USABLE_WITH_WARNINGS = "USABLE_WITH_WARNINGS"
    SHORT_SAMPLE = "SHORT_SAMPLE"
    EXCLUDED = "EXCLUDED"


@dataclass(frozen=True)
class ResearchAssessment:
    """Immutable quality assessment for one ETF's completed daily history."""

    status: ResearchStatus
    bar_count: int
    history_start: date | None
    history_end: date | None
    duplicate_dates: tuple[str, ...]
    warnings: tuple[str, ...]
    walk_forward_eligible: bool
    data_version: str


def _data_version(bars: Sequence[DailyBar]) -> str:
    payload = [bar.to_dict() for bar in bars]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(canonical).hexdigest()


def assess_history(
    bars: Sequence[DailyBar],
    *,
    crosscheck_status: str,
    adjustment_status: str,
    amount_quality: str,
    minimum_walk_forward_bars: int = 630,
) -> ResearchAssessment:
    """Classify one ordered sequence of completed daily bars.

    The function does not mutate or de-duplicate input.  A duplicate trading
    date is an exclusion because silently selecting one record would make the
    research result non-reproducible.  Quality warnings are retained in the
    assessment even when the sample is too short for walk-forward testing.
    """

    if not isinstance(bars, Sequence):
        raise ResearchError("bars must be a sequence of DailyBar")
    if type(minimum_walk_forward_bars) is not int or minimum_walk_forward_bars < 1:
        raise ResearchError("minimum_walk_forward_bars must be a positive integer")
    for value_name, value in (
        ("crosscheck_status", crosscheck_status),
        ("adjustment_status", adjustment_status),
        ("amount_quality", amount_quality),
    ):
        if type(value) is not str or not value.strip():
            raise ResearchError(f"{value_name} must be a non-empty string")

    materialized = tuple(bars)
    valid_bars: list[DailyBar] = []
    invalid_bar = False
    for bar in materialized:
        if type(bar) is not DailyBar:
            invalid_bar = True
            continue
        try:
            # Re-parse the serialized form so direct dataclass construction or
            # mutation cannot bypass schema, OHLC, units, and is_final checks.
            valid_bars.append(DailyBar.from_mapping(bar.to_dict()))
        except Exception:
            invalid_bar = True

    dates = tuple(bar.trading_date for bar in valid_bars)
    symbols = {bar.symbol for bar in valid_bars}
    counts = Counter(dates)
    duplicate_dates = tuple(
        item.isoformat() for item, count in sorted(counts.items()) if count > 1
    )
    warnings: list[str] = []

    if crosscheck_status != "PASSED":
        warnings.append(
            {
                "FAILED": "CROSSCHECK_FAILED",
                "PENDING": "CROSSCHECK_PENDING",
            }.get(crosscheck_status, "CROSSCHECK_UNKNOWN")
        )
    if adjustment_status != "VERIFIED":
        warnings.append(
            {
                "REVIEW": "ADJUSTMENT_REVIEW",
                "UNKNOWN": "ADJUSTMENT_UNVERIFIED",
                "PENDING": "ADJUSTMENT_PENDING",
            }.get(adjustment_status, "ADJUSTMENT_UNKNOWN")
        )
    if amount_quality == "ESTIMATED":
        warnings.append("AMOUNT_ESTIMATED")
    elif amount_quality != "PROVIDER_REPORTED":
        warnings.append("AMOUNT_UNKNOWN")
    if duplicate_dates:
        warnings.append("DUPLICATE_TRADING_DATE")
    if len(symbols) > 1:
        warnings.append("MIXED_SYMBOLS")
    if dates and tuple(sorted(set(dates))) != dates:
        warnings.append("NON_INCREASING_TRADING_DATE")
    if not materialized:
        warnings.append("NO_COMPLETED_BARS")
    if invalid_bar:
        warnings.append("INVALID_DAILY_BAR")

    invalid_structure = (
        not materialized
        or invalid_bar
        or bool(duplicate_dates)
        or len(symbols) > 1
        or (bool(dates) and tuple(sorted(set(dates))) != dates)
    )
    if invalid_structure:
        status = ResearchStatus.EXCLUDED
    elif warnings:
        # Quality warnings are useful for research triage even before the
        # sample is long enough for walk-forward evaluation.
        status = ResearchStatus.USABLE_WITH_WARNINGS
    elif len(materialized) < minimum_walk_forward_bars:
        status = ResearchStatus.SHORT_SAMPLE
    else:
        status = ResearchStatus.VERIFIED

    return ResearchAssessment(
        status=status,
        bar_count=len(valid_bars),
        history_start=dates[0] if dates else None,
        history_end=dates[-1] if dates else None,
        duplicate_dates=duplicate_dates,
        warnings=tuple(warnings),
        walk_forward_eligible=status is ResearchStatus.VERIFIED,
        data_version=_data_version(valid_bars),
    )
