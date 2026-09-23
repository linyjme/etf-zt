"""Read-only provenance and sample coverage, never a performance certification.

Legacy DailyBar records have no independent verification receipt. Source labels
can identify an estimated field, but cannot prove that a supplier was correct.
These summaries neither change canonical bars nor authorize trading.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from .swing_config import SwingStrategyConfig
from .swing_data import DailyBar


SHANGHAI = ZoneInfo("Asia/Shanghai")
# The cash session ends at 15:00, but the daily collector usually finishes
# later.  Requiring today's bar at the bell makes a verified snapshot flash
# unverified until that collection lands.
_DAILY_READY_AFTER = time(16, 0)


def _local_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("today must be timezone-aware")
        return value.astimezone(SHANGHAI).date()
    if type(value) is date:
        return value
    raise ValueError("today must be a date or timezone-aware datetime")


def _expected_last_completed_date(
    today: date | datetime, closed_dates: Sequence[date] | set[date] | frozenset[date],
) -> date:
    """Return the latest date that should have a completed daily bar.

    Before 16:00 Asia/Shanghai, today's bar is deliberately excluded so the
    post-close collection window does not flip a verified snapshot.  After
    that ready time it is eligible, while weekends and configured exchange
    holidays are skipped.
    """
    if isinstance(today, datetime):
        if today.tzinfo is None or today.utcoffset() is None:
            raise ValueError("today must be timezone-aware")
        local = today.astimezone(SHANGHAI)
        candidate = local.date()
        if local.time().replace(tzinfo=None) < _DAILY_READY_AFTER:
            candidate -= timedelta(days=1)
    else:
        candidate = _local_date(today) - timedelta(days=1)
    closures = frozenset(closed_dates)
    while candidate.weekday() >= 5 or candidate in closures:
        candidate -= timedelta(days=1)
    return candidate


def _receipt_text(value: object) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        return value.astimezone(SHANGHAI).isoformat()
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
        except ValueError:
            return None
        return parsed.astimezone(SHANGHAI).isoformat()
    return None


def assess_verified_quality(
    bars: Sequence[DailyBar], *, today: date | datetime,
    closed_dates: Sequence[date] | set[date] | frozenset[date] = (),
    metadata_status: str | Mapping[str, object] | None = None,
    environment_histories: Mapping[str, Sequence[DailyBar]] | None = None,
    receipt: Mapping[str, object] | None = None,
    minimum_daily_bars: int = 250,
) -> dict[str, object]:
    """Apply the fail-closed V11 release gate to a completed daily snapshot.

    ``summarize_history_quality`` remains a descriptive legacy report.  This
    function is the stronger publication contract: enough rows alone never
    become VERIFIED without a source receipt, provider-reported amounts,
    metadata validation and both broad-market environment histories.
    """
    ordered, duplicates = _records(bars)
    reasons: list[str] = []
    latest = ordered[-1].trading_date if ordered else None
    expected = _expected_last_completed_date(today, closed_dates)
    kinds = {_amount_quality(bar.source) for bar in ordered}
    amount_quality = (
        next(iter(kinds)) if len(kinds) == 1 else
        "MIXED" if kinds else "UNKNOWN"
    )
    receipt_amount_quality = (
        receipt.get("amount_quality") if isinstance(receipt, Mapping) else None
    )
    if amount_quality == "UNKNOWN" and receipt_amount_quality == "PROVIDER_REPORTED":
        amount_quality = "PROVIDER_REPORTED"
    if len(ordered) < minimum_daily_bars or duplicates:
        reasons.append("DATA_QUALITY_INSUFFICIENT_BARS")
    if latest != expected:
        reasons.append("DATA_QUALITY_LATEST_DATE_NOT_CURRENT")
    if amount_quality != "PROVIDER_REPORTED":
        reasons.append("DATA_QUALITY_AMOUNT_NOT_PROVIDER_REPORTED")

    metadata_ok = metadata_status in {"PASSED", "VERIFIED"}
    if isinstance(metadata_status, Mapping):
        metadata_ok = metadata_status.get("status") in {"PASSED", "VERIFIED"}
    if not metadata_ok:
        reasons.append("DATA_QUALITY_METADATA_INVALID")

    environments_ok = True
    environment_latest: dict[str, str | None] = {}
    for symbol in ("000300", "000852"):
        history = tuple((environment_histories or {}).get(symbol, ()))
        if not history:
            environments_ok = False
            continue
        if any(type(item) is not DailyBar for item in history):
            environments_ok = False
            continue
        dates = tuple(item.trading_date for item in history)
        environment_latest[symbol] = dates[-1].isoformat()
        if len(history) < minimum_daily_bars:
            reasons.append("DATA_QUALITY_ENVIRONMENT_INSUFFICIENT_BARS")
            environments_ok = False
        if len(set(dates)) != len(dates) or any(
            current <= previous for previous, current in zip(dates, dates[1:])
        ):
            reasons.append("DATA_QUALITY_ENVIRONMENT_INVALID_SEQUENCE")
            environments_ok = False
        if dates[-1] != expected:
            reasons.append("DATA_QUALITY_ENVIRONMENT_LATEST_DATE_NOT_CURRENT")
            environments_ok = False
    if not environments_ok:
        reasons.append("DATA_QUALITY_ENVIRONMENT_MISSING")

    receipt_status = "MISSING"
    receipt_payload: dict[str, object] | None = None
    if not isinstance(receipt, Mapping):
        reasons.append("DATA_QUALITY_RECEIPT_MISSING")
    else:
        source = receipt.get("source")
        checked_at = _receipt_text(receipt.get("checked_at"))
        sample_start = receipt.get("sample_start")
        sample_end = receipt.get("sample_end")
        crosscheck = receipt.get("crosscheck_status", receipt.get("amount_crosscheck"))
        adjustment = receipt.get("adjustment_status", receipt.get("adjustment_basis"))
        receipt_amount_quality = receipt.get("amount_quality")
        version = receipt.get("calculation_version")
        warnings = receipt.get("warnings", ())
        if not isinstance(source, str) or not source.strip() or checked_at is None:
            reasons.append("DATA_QUALITY_RECEIPT_INVALID")
        sample_ok = _receipt_sample_matches(ordered, sample_start, sample_end, version)
        if not sample_ok:
            reasons.append("DATA_QUALITY_RECEIPT_SAMPLE_MISMATCH")
        if crosscheck != "PASSED":
            reasons.append("DATA_QUALITY_RECEIPT_CROSSCHECK_PENDING")
        if receipt_amount_quality is not None and receipt_amount_quality != "PROVIDER_REPORTED":
            reasons.append("DATA_QUALITY_AMOUNT_NOT_PROVIDER_REPORTED")
        if adjustment != "VERIFIED":
            reasons.append("DATA_QUALITY_RECEIPT_ADJUSTMENT_UNVERIFIED")
        if not isinstance(version, str) or not version.strip():
            reasons.append("DATA_QUALITY_RECEIPT_VERSION_MISSING")
        if isinstance(warnings, Sequence) and not isinstance(warnings, (str, bytes)) and warnings:
            reasons.append("DATA_QUALITY_RECEIPT_WARNINGS")
        elif warnings not in ((), [], None):
            reasons.append("DATA_QUALITY_RECEIPT_WARNINGS")
        if not any(reason.startswith("DATA_QUALITY_RECEIPT_") for reason in reasons):
            receipt_status = "PASSED"
        receipt_payload = dict(receipt)
        receipt_payload["checked_at"] = checked_at

    unique_reasons = list(dict.fromkeys(reasons))
    return {
        "status": "VERIFIED" if not unique_reasons else "UNVERIFIED",
        "reasons": unique_reasons,
        "checked_at": _receipt_text(today) if isinstance(today, datetime) else None,
        "bar_count": len(ordered),
        "last_completed_date": latest.isoformat() if latest is not None else None,
        "expected_last_completed_date": expected.isoformat(),
        "amount_quality": amount_quality,
        "metadata_status": "PASSED" if metadata_ok else "FAILED",
        "environment_history_status": "PASSED" if environments_ok else "MISSING",
        "environment_latest": environment_latest,
        "receipt_status": receipt_status,
        "receipt": receipt_payload,
        "minimum_daily_bars": minimum_daily_bars,
    }


# Descriptive alias for callers that use the release-gate vocabulary.
verify_data_quality = assess_verified_quality


def _history_digest(bars: Sequence[DailyBar]) -> str:
    payload = [bar.to_dict() for bar in bars]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _version_matches(version: object, digest: str) -> bool:
    return (
        isinstance(version, str)
        and version.startswith("sha256:")
        and len(version) >= len("sha256:") + 8
        and digest.startswith(version)
    )


def _receipt_sample_matches(
    ordered: Sequence[DailyBar],
    sample_start: object,
    sample_end: object,
    version: object,
) -> bool:
    """Accept a receipt whose window sits inside the bars and whose digest matches.

    ``sample_end`` no longer has to equal the newest bar.  It must be at least
    the sample start and no later than the newest bar, and ``calculation_version``
    must be a prefix of the current history digest.  A new bar therefore fails
    closed until the manifest is rebuilt against that history.
    """
    if not ordered or not isinstance(sample_start, str) or not isinstance(sample_end, str):
        return False
    first = ordered[0].trading_date.isoformat()
    latest = ordered[-1].trading_date.isoformat()
    if sample_start != first or not first <= sample_end <= latest:
        return False
    return _version_matches(version, _history_digest(ordered))


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
