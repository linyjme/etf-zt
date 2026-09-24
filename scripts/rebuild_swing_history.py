"""Rebuild the swing daily history from the primary provider in one pass.

``DailyHistoryStore.upsert`` keeps every stored row forever, so a history
that was first filled from a fallback provider (Tencent, estimated turnover)
keeps those rows outside the daily collection window after the primary
Eastmoney feed recovers.  The verified-data gate then reports MIXED sources
and the independent crosscheck flags SAME_PROVIDER for the old rows.

This script collects every enabled watchlist symbol through the real
collector, refuses to write anything unless every symbol came back from the
primary provider with a provider-reported turnover, and only then replaces
``--history`` atomically.  Run ``crosscheck_swing_history.py`` afterwards so
the receipts and research manifest describe the new digest.
"""

from __future__ import annotations

from datetime import datetime
import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from etf_rotation.constants import DEFAULT_SWING_HISTORY_COUNT
from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.market_data import load_closed_dates
from etf_rotation.swing_collector import EastmoneyDailyCollector
from etf_rotation.swing_config import SwingWatchItem
from etf_rotation.swing_data import DailyBar, DailyHistoryStore, SwingDataError
from etf_rotation.swing_quality import _amount_quality, _expected_last_completed_date


SHANGHAI = ZoneInfo("Asia/Shanghai")
PRIMARY_PROVIDER = "东方财富"


def _enabled_items(path: Path) -> tuple[SwingWatchItem, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        SwingWatchItem(item["symbol"], True)
        for item in payload.get("items", ())
        if isinstance(item, dict) and item.get("enabled") is True
    )


def collect_primary_history(
    items: tuple[SwingWatchItem, ...],
    collector: EastmoneyDailyCollector,
    *,
    last_completed_date,
    count: int,
) -> tuple[dict[str, tuple[DailyBar, ...]], dict[str, str]]:
    """Collect each symbol separately so one failure does not hide the others."""
    collected: dict[str, tuple[DailyBar, ...]] = {}
    failures: dict[str, str] = {}
    for item in items:
        try:
            bars = collector.collect((item,), last_completed_date, count)
        except Exception as error:  # noqa: BLE001 - reported per symbol
            failures[item.symbol] = type(error).__name__
            continue
        if not bars:
            failures[item.symbol] = "NO_BARS"
            continue
        providers = {bar.source.split(" ", 1)[0] for bar in bars}
        kinds = {_amount_quality(bar.source) for bar in bars}
        if providers != {PRIMARY_PROVIDER}:
            failures[item.symbol] = "FALLBACK_PROVIDER:" + ",".join(sorted(providers))
        elif kinds != {"PROVIDER_REPORTED"}:
            failures[item.symbol] = "AMOUNT_NOT_PROVIDER_REPORTED:" + ",".join(sorted(kinds))
        else:
            collected[item.symbol] = bars
    return collected, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=Path("var/swing/daily_quotes.jsonl"))
    parser.add_argument("--watchlist", type=Path, default=Path("data/swing/watchlist.json"))
    parser.add_argument("--metadata", type=Path, default=Path("data/monitor/etf_metadata.json"))
    parser.add_argument("--calendar", type=Path, default=Path("data/monitor/market_calendar.json"))
    parser.add_argument("--count", type=int, default=DEFAULT_SWING_HISTORY_COUNT)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--allow-partial", action="store_true",
        help="write the symbols that succeeded even if others failed (the "
             "failed symbols keep no history and stay OBSERVE until refreshed)",
    )
    args = parser.parse_args()

    metadata = EtfMetadataStore(args.metadata).load()
    closed = load_closed_dates(args.calendar)
    now = datetime.now(SHANGHAI)
    last_completed = _expected_last_completed_date(now, closed)
    items = _enabled_items(args.watchlist)
    collected, failures = collect_primary_history(
        items, EastmoneyDailyCollector(timeout=args.timeout),
        last_completed_date=last_completed, count=args.count,
    )
    for symbol, bars in collected.items():
        print(f"{symbol} bars={len(bars)} {bars[0].trading_date}..{bars[-1].trading_date} source={bars[0].source}")
    for symbol, reason in failures.items():
        print(f"{symbol} FAILED reason={reason}", file=sys.stderr)
    if failures and not args.allow_partial:
        print("history not replaced: every symbol must come from the primary provider", file=sys.stderr)
        return 1
    if not collected:
        print("history not replaced: nothing collected", file=sys.stderr)
        return 1

    staging = args.history.with_name(f"{args.history.name}.rebuild")
    if staging.exists():
        staging.unlink()
    try:
        store = DailyHistoryStore(staging, metadata, closed)
        persisted = store.upsert(tuple(bar for bars in collected.values() for bar in bars))
    except SwingDataError as error:
        print(f"history not replaced: {error}", file=sys.stderr)
        if staging.exists():
            staging.unlink()
        return 1
    staging.replace(args.history)
    print(f"replaced {args.history} with {len(persisted)} bars for {len(collected)} symbols as of {last_completed}")
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
