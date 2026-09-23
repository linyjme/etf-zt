"""Read-only provenance and sample coverage, never a performance certification.

Legacy DailyBar records have no independent verification receipt. Source labels
can identify an estimated field, but cannot prove that a supplier was correct.
These summaries neither change canonical bars nor authorize trading.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math

from .swing_config import SwingStrategyConfig
from .swing_data import DailyBar


def _window_count(count: int, config: SwingStrategyConfig) -> int:
    required = config.walk_forward_train_days + config.walk_forward_test_days
    return 0 if count < required else 1 + (count - required) // config.walk_forward_step_days


def _records(bars: Sequence[DailyBar]) -> tuple[tuple[DailyBar, ...], bool]:
    supplied = tuple(bars)
    if any(type(bar) is not DailyBar for bar in supplied):
        raise ValueError("quality summary requires DailyBar records")
    if len({bar.symbol for bar in supplied}) > 1:
        raise ValueError("quality summary must contain one symbol")
    ordered = tuple(sorted(supplied, key=lambda bar: (bar.trading_date, bar.observed_at)))
    by_date = {bar.trading_date: bar for bar in ordered}
    return tuple(by_date.values()), len(by_date) != len(supplied)


def _amount_quality(source: str) -> str:
    if source.startswith("腾讯 fqkline 原始+前复权 (") and source.endswith(
        "amount=OHLC均价×成交量(手)×100估算"
    ):
        return "ESTIMATED"
    if source.startswith("东方财富 kline (") and source.endswith(")"):
        return "PROVIDER_REPORTED"
    if source.startswith("Wind fund_data.get_fund_kline ") and "TURNOVER=元" in source:
        return "PROVIDER_REPORTED"
    return "UNKNOWN"


def summarize_history_quality(
    bars: Sequence[DailyBar], config: SwingStrategyConfig,
) -> dict[str, object]:
    """Describe what is recorded; sample sufficiency is not data acceptance."""
    ordered, duplicates = _records(bars)
    count = len(ordered)
    sources = sorted({bar.source for bar in ordered})
    kinds = {_amount_quality(source) for source in sources}
    amount_quality = next(iter(kinds)) if len(kinds) == 1 else "MIXED" if kinds else "UNKNOWN"
    warnings = ["INDEPENDENT_CROSSCHECK_NOT_RECORDED", "ADJUSTMENT_BASIS_UNVERIFIED"]
    if "ESTIMATED" in kinds:
        warnings.append("AMOUNT_ESTIMATED")
    if "UNKNOWN" in kinds:
        warnings.append("UNKNOWN_SOURCE_CONTRACT")
    if len(sources) > 1:
        warnings.append("MIXED_SOURCES")
    if duplicates:
        warnings.append("DUPLICATE_TRADING_DATE")
    scales = [bar.close / bar.adjusted_close for bar in ordered]
    changed = any(
        not math.isclose(prior, current, rel_tol=1e-6, abs_tol=1e-12)
        for prior, current in zip(scales, scales[1:])
    )
    adjustment_status = (
        "NO_DATA" if not count else
        "RATIO_CHANGED_REQUIRES_REVIEW" if changed else "RATIO_STABLE_UNVERIFIED"
    )
    if changed:
        warnings.append("ADJUSTMENT_RATIO_CHANGED")
    if not count:
        warnings.append("NO_DAILY_HISTORY")
    return {
        "bar_count": count,
        "start_date": ordered[0].trading_date.isoformat() if count else None,
        "end_date": ordered[-1].trading_date.isoformat() if count else None,
        "last_observed_at": max(bar.observed_at for bar in ordered).isoformat() if count else None,
        "sources": sources,
        "classification_basis": "LEGACY_SOURCE_LABEL",
        "amount_quality": amount_quality,
        "adjustment_status": adjustment_status,
        "crosscheck_status": "NOT_RECORDED",
        "warnings": warnings,
        "minimum_daily_bars": config.minimum_daily_bars,
        "minimum_backtest_bars": config.minimum_daily_bars + 1,
        "indicator_sample_ok": not duplicates and count >= config.minimum_daily_bars,
        "backtest_sample_ok": not duplicates and count >= config.minimum_daily_bars + 1,
        "walk_forward_required_bars": config.walk_forward_train_days + config.walk_forward_test_days,
        "walk_forward_fold_count": 0 if duplicates else _window_count(count, config),
        "performance_validated": False,
    }


def summarize_common_history(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]], config: SwingStrategyConfig,
) -> dict[str, object]:
    """Count shared windows without silently omitting missing symbols/dates."""
    groups = {symbol: _records(bars) for symbol, bars in bars_by_symbol.items()}
    if any(bars and bars[0].symbol != symbol for symbol, (bars, _) in groups.items()):
        raise ValueError("history key must match bar symbol")
    missing = not groups or any(not bars for bars, _ in groups.values())
    warnings: list[str] = []
    start = end = None
    common = set()
    aligned = False
    if missing:
        warnings.append("MISSING_SYMBOL_HISTORY")
    else:
        start = max(bars[0].trading_date for bars, _ in groups.values())
        end = min(bars[-1].trading_date for bars, _ in groups.values())
        date_sets = [
            {bar.trading_date for bar in bars if start <= bar.trading_date <= end}
            for bars, _ in groups.values()
        ]
        common = set.intersection(*date_sets)
        aligned = bool(common) and all(dates == common for dates in date_sets)
        if any(duplicates for _, duplicates in groups.values()):
            aligned = False
            warnings.append("DUPLICATE_TRADING_DATE")
        if not aligned:
            warnings.append("NON_ALIGNED_COMMON_HISTORY")
    if not common:
        start = end = None
    count = len(common)
    required = config.walk_forward_train_days + config.walk_forward_test_days
    if count < required:
        warnings.append("INSUFFICIENT_COMMON_WALK_FORWARD_SAMPLE")
    return {
        "symbols": sorted(groups),
        "common_bar_count": count,
        "start_date": start.isoformat() if start is not None else None,
        "end_date": end.isoformat() if end is not None else None,
        "aligned": aligned,
        "walk_forward_required_bars": required,
        "walk_forward_fold_count": _window_count(count, config) if aligned else 0,
        "performance_validated": False,
        "warnings": warnings,
    }


def _research_quality(items: Sequence[Mapping[str, object]]) -> str:
    """Never label legacy data as verified.

    Legacy DailyBar records carry no independent crosscheck receipt, so
    research_quality is always UNVERIFIED. The guard below makes the rule
    explicit: unknown quality or missing data must never upgrade to verified.
    """
    for item in items:
        data_quality = item.get("data_quality") if isinstance(item, Mapping) else None
        if not isinstance(data_quality, Mapping):
            return "UNVERIFIED"
        if data_quality.get("amount_quality") in ("UNKNOWN", "MIXED"):
            return "UNVERIFIED"
    return "UNVERIFIED"


def summarize_strategy_diagnostics(
    items: Sequence[Mapping[str, object]],
    history_coverage: Mapping[str, object] | None,
    health: Mapping[str, str],
) -> dict[str, object]:
    """Read-only cross-section diagnostics for the current snapshot only.

    This is provenance, not certification. It never changes canonical bars,
    never authorizes trading, and is NOT a historical signal funnel: every
    count here describes the single published snapshot, not the rate at which
    signals historically triggered. Use ``snapshot_scope`` to keep that
    boundary explicit and avoid computing any historical trigger rate.

    Per-symbol de-duplication: a single symbol contributes at most once to any
    warning count and at most once to any blocked-reason count, so repeated
    entries for the same symbol cannot inflate the totals.
    """
    # A published snapshot has one row per symbol. Fail closed on malformed
    # rows, and use the last row when callers supply duplicate symbols.
    by_symbol = {
        item["symbol"]: item for item in items
        if isinstance(item, Mapping)
        and isinstance(item.get("symbol"), str) and item["symbol"]
    }
    items = tuple(by_symbol[symbol] for symbol in sorted(by_symbol))
    item_status = "NO_DATA" if not items else "AVAILABLE"
    warning_counts: dict[str, int] = {}
    warning_symbols: list[str] = []
    blocked_reason_counts: dict[str, int] = {}
    formal_state_counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        symbol = item.get("symbol")
        data_quality = item.get("data_quality")
        symbol_warnings: set[str] = set()
        if isinstance(data_quality, Mapping):
            for warning in data_quality.get("warnings", []) or []:
                if isinstance(warning, str):
                    symbol_warnings.add(warning)
        if symbol_warnings:
            warning_symbols.append(symbol)
            for warning in symbol_warnings:
                warning_counts[warning] = warning_counts.get(warning, 0) + 1
        symbol_reasons: set[str] = set()
        for reason in item.get("blocked_reasons", []) or []:
            if isinstance(reason, str):
                symbol_reasons.add(reason)
        for reason in symbol_reasons:
            blocked_reason_counts[reason] = blocked_reason_counts.get(reason, 0) + 1
        formal_state = item.get("formal_state")
        if isinstance(formal_state, str):
            formal_state_counts[formal_state] = formal_state_counts.get(formal_state, 0) + 1
    if not isinstance(history_coverage, Mapping):
        coverage = {
            "common_bar_count": 0,
            "required": 0,
            "fold": 0,
            "sample_windows_are_not_validation": True,
        }
    else:
        coverage = {
            "common_bar_count": history_coverage.get("common_bar_count", 0),
            "required": history_coverage.get("walk_forward_required_bars", 0),
            "fold": history_coverage.get("walk_forward_fold_count", 0),
            "sample_windows_are_not_validation": True,
        }
    return {
        "snapshot_scope": True,
        "item_status": item_status,
        "scope_note": (
            "current snapshot cross-section only; not a historical signal funnel"
        ),
        "warning_counts": warning_counts,
        "warning_symbols": warning_symbols,
        "blocked_reason_counts": blocked_reason_counts,
        "formal_state_counts": formal_state_counts,
        "coverage": coverage,
        "layers": {
            "daily_load": health.get("daily"),
            "account": health.get("portfolio"),
            "intraday": health.get("intraday"),
            "research_quality": _research_quality(items),
            "performance": "NOT_VALIDATED",
        },
    }
