from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_service import SwingPaths, SwingService
from etf_rotation.valuation import ValuationStore, classify_valuation_stage


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
    def _valuation_path(self, root: Path, **overrides: object) -> Path:
        record: dict[str, object] = {
            "index_code": "000300",
            "index_name": "沪深300",
            "as_of": "2026-09-20",
            "pe_ttm": 20.0,
            "pb": 4.0,
            "dividend_yield": 2.0,
            "pe_percentile_5y": 50.0,
            "pe_percentile_10y": 40.0,
            "pb_percentile_5y": 50.0,
            "pb_percentile_10y": 40.0,
            "roe_ttm": 10.0,
            "roe_period": "TTM",
            "status": "OK",
            "source": "Synthetic",
        }
        record.update(overrides)
        path = root / "valuation.json"
        path.write_text(json.dumps({"schema_version": 1, "items": [record]}), encoding="utf-8")
        return path

    def test_h1_roe_is_annualized_before_canonical_cross_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._valuation_path(Path(directory), roe_ttm=10.0, roe_period="H1", pe_ttm=20.0, pb=4.0)
            snapshot = ValuationStore(path, today=date(2026, 9, 23)).get("000300")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.roe_period, "H1")
        self.assertAlmostEqual(snapshot.roe_annualized or 0.0, 20.0)
        self.assertAlmostEqual(snapshot.pr_pe_pb or 0.0, 1.0)
        self.assertAlmostEqual(snapshot.pr_pe_roe or 0.0, 1.0)
        self.assertTrue(snapshot.roe_consistent)

    def test_unknown_roe_period_never_assumes_ttm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._valuation_path(Path(directory), roe_period="UNKNOWN")
            snapshot = ValuationStore(path, today=date(2026, 9, 23)).get("000300")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertIsNone(snapshot.roe_annualized)
        self.assertIsNone(snapshot.pr_pe_roe)
        self.assertFalse(snapshot.roe_consistent)
        self.assertEqual(snapshot.status, "UNKNOWN")

    def test_roe_cross_check_over_twenty_percent_is_not_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._valuation_path(Path(directory), roe_ttm=6.0, roe_period="TTM")
            snapshot = ValuationStore(path, today=date(2026, 9, 23)).get("000300")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertIsNone(snapshot.pr_pe_roe)
        self.assertFalse(snapshot.roe_consistent)
        self.assertAlmostEqual(snapshot.pr_pe_pb or 0.0, 1.0)

    def test_stale_status_and_percentile_horizon_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = self._valuation_path(
                root,
                as_of="2026-09-10",
                pe_percentile_5y=5.0,
                pb_percentile_5y=5.0,
                pe_percentile_10y=80.0,
                pb_percentile_10y=80.0,
            )
            snapshot = ValuationStore(stale, today=date(2026, 9, 20), stale_days=10).get("000300")
            fallback = self._valuation_path(
                root,
                as_of="2026-09-20",
                pe_percentile_10y=None,
                pb_percentile_10y=None,
                pe_percentile_5y=5.0,
                pb_percentile_5y=5.0,
            )
            fallback_snapshot = ValuationStore(fallback, today=date(2026, 9, 23)).get("000300")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.status, "STALE")
        self.assertEqual(snapshot.percentile_horizon_used, "10y")
        self.assertEqual(snapshot.level, "HIGH")
        self.assertIsNotNone(fallback_snapshot)
        assert fallback_snapshot is not None
        self.assertEqual(fallback_snapshot.percentile_horizon_used, "5y")
        self.assertEqual(fallback_snapshot.level, "LOW")

    def test_missing_status_is_preserved_when_snapshot_is_old(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._valuation_path(
                Path(directory),
                as_of="2020-01-01",
                status="MISSING_VALUATION",
                pe_ttm=None,
                pb=None,
                roe_ttm=None,
                roe_period=None,
                pe_percentile_5y=None,
                pe_percentile_10y=None,
                pb_percentile_5y=None,
                pb_percentile_10y=None,
            )
            snapshot = ValuationStore(path, today=date(2026, 9, 23)).get("000300")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.status, "MISSING_VALUATION")

    def test_valuation_stage_boundaries_and_industry_without_percentiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stages: list[tuple[float, str]] = [
                (10.0, "DEEP_VALUE"), (30.0, "VALUE"),
                (69.99, "FAIR"), (89.99, "RICH"), (90.0, "EXPENSIVE"),
            ]
            for percentile, expected in stages:
                path = self._valuation_path(
                    root,
                    pe_percentile_10y=percentile,
                    pb_percentile_10y=percentile,
                    pe_percentile_5y=None,
                    pb_percentile_5y=None,
                )
                snapshot = ValuationStore(path, today=date(2026, 9, 23)).get("000300")
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(classify_valuation_stage(snapshot).stage, expected)
            path = self._valuation_path(
                root,
                pe_percentile_10y=None,
                pb_percentile_10y=None,
                pe_percentile_5y=None,
                pb_percentile_5y=None,
            )
            snapshot = ValuationStore(path, today=date(2026, 9, 23)).get("000300")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(classify_valuation_stage(snapshot, category="industry").stage, "UNAVAILABLE")

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
            valuation_payload = item["valuation"]
            self.assertEqual(valuation_payload["index_code"], "000300")
            self.assertEqual(valuation_payload["percentile_horizon_used"], "10y")
            self.assertIsNone(valuation_payload["pr_pe_roe"])
            self.assertAlmostEqual(valuation_payload["pr_pe_pb"], 1.2445714285714284)
            self.assertEqual(valuation_payload["status"], "UNKNOWN")

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
