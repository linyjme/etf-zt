"""Record independent-source crosscheck receipts for the swing daily history.

For every enabled watchlist symbol the canonical bars in ``--history`` are
compared with Tencent's raw and front-adjusted series.  The receipts are
written to ``--output`` (``data/swing/crosscheck_receipts.json`` by default)
and the research manifest can be rebuilt in the same run so the verified-data
gate sees a matching digest.  Nothing here changes canonical bars.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import argparse
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_collector import EastmoneyDailyCollector
from etf_rotation.swing_crosscheck import (
    INDEPENDENT_SOURCE_LABEL,
    CrosscheckReceipt,
    crosscheck_history,
    write_receipts,
)
from etf_rotation.swing_data import DailyBar


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _load_history(path: Path) -> dict[str, tuple[DailyBar, ...]]:
    grouped: dict[str, list[DailyBar]] = defaultdict(list)
    if not path.exists():
        return {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        bar = DailyBar.from_mapping(json.loads(line))
        grouped[bar.symbol].append(bar)
    return {
        symbol: tuple(sorted(bars, key=lambda bar: bar.trading_date))
        for symbol, bars in grouped.items()
    }


def _enabled_symbols(path: Path) -> tuple[str, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        item["symbol"] for item in payload.get("items", ())
        if isinstance(item, dict) and item.get("enabled") is True
    )


def crosscheck_symbols(
    history: dict[str, tuple[DailyBar, ...]],
    symbols: tuple[str, ...],
    metadata: dict[str, object],
    collector: EastmoneyDailyCollector,
    *,
    checked_at: datetime,
    count: int,
) -> tuple[list[CrosscheckReceipt], dict[str, str]]:
    receipts: list[CrosscheckReceipt] = []
    failures: dict[str, str] = {}
    for symbol in symbols:
        bars = history.get(symbol, ())
        item = metadata.get(symbol)
        if not bars or item is None:
            failures[symbol] = "NO_HISTORY" if not bars else "NO_METADATA"
            continue
        try:
            independent = collector.collect_independent(
                symbol, bars[-1].trading_date, count,
            )
            receipts.append(crosscheck_history(
                bars, independent,
                source=INDEPENDENT_SOURCE_LABEL,
                checked_at=checked_at,
                price_tick=float(item.trading.price_tick),
            ))
        except Exception as error:  # noqa: BLE001 - recorded, never raised
            failures[symbol] = type(error).__name__
    return receipts, failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, default=Path("var/swing/daily_quotes.jsonl"))
    parser.add_argument("--watchlist", type=Path, default=Path("data/swing/watchlist.json"))
    parser.add_argument("--metadata", type=Path, default=Path("data/monitor/etf_metadata.json"))
    parser.add_argument("--output", type=Path, default=Path("data/swing/crosscheck_receipts.json"))
    parser.add_argument("--count", type=int, default=800)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--rebuild-manifest", type=Path, metavar="MANIFEST",
        help="rebuild this research manifest after writing receipts",
    )
    parser.add_argument("--calendar", type=Path, default=Path("data/monitor/market_calendar.json"))
    args = parser.parse_args()

    history = _load_history(args.history)
    symbols = _enabled_symbols(args.watchlist)
    metadata = EtfMetadataStore(args.metadata).load()
    checked_at = datetime.now(SHANGHAI)
    receipts, failures = crosscheck_symbols(
        history, symbols, metadata, EastmoneyDailyCollector(timeout=args.timeout),
        checked_at=checked_at, count=args.count,
    )
    write_receipts(args.output, receipts, generated_at=checked_at)
    for receipt in receipts:
        print(
            f"{receipt.symbol} crosscheck={receipt.crosscheck_status} "
            f"adjustment={receipt.adjustment_status} compared={receipt.compared_count} "
            f"mismatches={len(receipt.mismatches)} events={len(receipt.adjustment_events)}"
        )
    for symbol, reason in failures.items():
        print(f"{symbol} crosscheck=UNAVAILABLE reason={reason}")
    if args.rebuild_manifest is not None:
        from build_swing_research_manifest import build_manifest

        build_manifest(
            args.history, args.watchlist, metadata_path=args.metadata,
            output_path=args.rebuild_manifest, calendar_path=args.calendar,
            receipts_path=args.output,
        )
        print(f"manifest rebuilt: {args.rebuild_manifest}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
