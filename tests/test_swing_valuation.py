from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_service import SwingPaths, SwingService


SHANGHAI = timezone.utc


def _paths(root: Path) -> SwingPaths:
    return SwingPaths(
        watchlist=root / "watchlist.json",
        strategy=root / "strategy.json",
        daily_history=root / "daily.jsonl",
        portfolio_snapshot=root / "portfolio.json",
        trades=root / "trades.jsonl",
        alerts=root / "alerts.jsonl",
        metadata=root / "metadata.json",
        calendar=root / "calendar.json",
        backtests=root / "backtests",
    )


class SwingValuationTests(unittest.TestCase):
    def test_swing_snapshot_publishes_associated_index_valuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            paths = _paths(tmp_path)
            paths.metadata.write_text(json.dumps({
                "schema_version": 2,
                "items": [{
                    "symbol": "510300", "name": "沪深300ETF",
                    "index": {"code": "000300", "name": "沪深300", "provider": "中证指数"},
                    "trading": {
                        "exchange": "SSE", "asset_type": "DOMESTIC_EQUITY_ETF",
                        "intraday_turnaround": False, "sellable_delay_days": 1,
                        "lot_size": 100, "price_tick": 0.001,
                        "price_limit_pct": 0.1, "volume_unit_shares": 100,
                    },
                }],
            }, ensure_ascii=False), encoding="utf-8")
            paths.watchlist.write_text(json.dumps({
                "schema_version": 1,
                "items": [{"symbol": "510300", "enabled": True}],
            }), encoding="utf-8")
            paths.strategy.write_text(
                (Path(__file__).parents[1] / "data/swing/strategy.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            paths.calendar.write_text(
                json.dumps({"schema_version": 1, "closed_dates": []}),
                encoding="utf-8",
            )
            valuation = tmp_path / "valuation.json"
            valuation.write_text(json.dumps({
                "schema_version": 1,
                "items": [{
                    "index_code": "000300", "index_name": "沪深300", "as_of": "2026-09-18",
                    "pe_ttm": 13.2, "pb": 1.4, "dividend_yield": 3.1,
                    "pe_percentile_5y": 22.0, "pe_percentile_10y": 18.0,
                    "pb_percentile_5y": 20.0, "pb_percentile_10y": 16.0,
                    "roe_ttm": 10.0, "status": "OK", "source": "Wind",
                }],
            }), encoding="utf-8")
            service = SwingService(
                paths,
                collector=None,
                valuation_path=valuation,
                intraday_provider=lambda: {"items": []},
                intraday_points_provider=lambda _symbol: {"upserts": []},
                clock=lambda: datetime(2026, 9, 20, 12, tzinfo=SHANGHAI),
            )
            item = service.snapshot()["items"][0]
            self.assertEqual(item["valuation"], {
                "index_code": "000300",
                "index_name": "沪深300",
                "as_of": "2026-09-18",
                "pe_ttm": 13.2,
                "pb": 1.4,
                "dividend_yield": 3.1,
                "pe_percentile_5y": 22.0,
                "pe_percentile_10y": 18.0,
                "pb_percentile_5y": 20.0,
                "pb_percentile_10y": 16.0,
                "roe_ttm": 10.0,
                "pr_pe_roe": 1.3199999999999998,
                "pr_pe_pb": 1.2445714285714284,
                "level": "LOW",
                "status": "OK",
                "source": "Wind",
            })

    def test_repository_valuation_file_covers_every_swing_metadata_index(self) -> None:
        root = Path(__file__).parents[1]
        metadata = EtfMetadataStore(root / "data/monitor/etf_metadata.json").load()
        payload = json.loads(
            (root / "data/monitor/valuation.json").read_text(encoding="utf-8"),
        )
        codes = {item["index_code"] for item in payload["items"]}
        watchlist = json.loads(
            (root / "data/swing/watchlist.json").read_text(encoding="utf-8"),
        )
        enabled = {item["symbol"] for item in watchlist["items"] if item["enabled"]}
        missing = sorted(
            metadata[symbol].index.code
            for symbol in enabled
            if metadata[symbol].index.code not in codes
        )
        self.assertEqual(missing, [])
