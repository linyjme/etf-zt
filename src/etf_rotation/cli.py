from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Sequence

from . import constants


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / "var" / "monitor"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="etf-rotation")
    commands = parser.add_subparsers(dest="command", required=True)
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--quotes", type=Path, default=RUNTIME_ROOT / "quotes.json")
    monitor.add_argument("--watchlist", type=Path, default=PROJECT_ROOT / "data" / "monitor" / "watchlist.json")
    monitor.add_argument("--history", type=Path, default=RUNTIME_ROOT / "quotes.jsonl")
    monitor.add_argument("--alert-history", type=Path, default=RUNTIME_ROOT / "alerts.jsonl")
    monitor.add_argument("--metadata", type=Path, default=PROJECT_ROOT / "data" / "monitor" / "etf_metadata.json")
    monitor.add_argument("--valuation", type=Path, default=PROJECT_ROOT / "data" / "monitor" / "valuation.json")
    monitor.add_argument("--calendar", type=Path, default=PROJECT_ROOT / "data" / "monitor" / "market_calendar.json")
    monitor.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    monitor.add_argument("--port", type=int, default=8765)
    monitor.add_argument(
        "--refresh-interval",
        type=float,
        default=constants.DEFAULT_REFRESH_INTERVAL_SECONDS,
    )
    monitor.add_argument("--no-collect", action="store_true")
    rebuild = commands.add_parser("rebuild-history")
    rebuild.add_argument("--input", type=Path, default=PROJECT_ROOT / "data" / "monitor" / "quotes.json")
    rebuild.add_argument("--output", type=Path, default=RUNTIME_ROOT / "quotes.jsonl")
    rebuild.add_argument("--metadata", type=Path, default=PROJECT_ROOT / "data" / "monitor" / "etf_metadata.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "rebuild-history":
        from .history_migration import rebuild_history

        count = rebuild_history(arguments.input, arguments.output, arguments.metadata)
        print(f"{count} finalized minutes rebuilt")
        return 0
    if not 1 <= arguments.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if arguments.refresh_interval <= 0:
        raise ValueError("refresh interval must be positive")
    from .quote_collector import Trends2QuoteCollector
    from .t_web import create_server

    collector = None if arguments.no_collect else Trends2QuoteCollector()
    server = create_server(
        host=arguments.host,
        port=arguments.port,
        quotes_path=arguments.quotes,
        watchlist_path=arguments.watchlist,
        history_path=arguments.history,
        collector=collector,
        refresh_interval=arguments.refresh_interval,
        alert_history_path=arguments.alert_history,
        metadata_path=arguments.metadata,
        valuation_path=arguments.valuation,
        calendar_path=arguments.calendar,
    )
    host, port = server.server_address
    print(f"monitor-only: http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
