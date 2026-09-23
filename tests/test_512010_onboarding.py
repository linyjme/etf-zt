from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MedicalEtf512010OnboardingTests(unittest.TestCase):
    def test_512010_is_registered_with_its_real_index_and_trading_rules(self) -> None:
        metadata = json.loads(
            (ROOT / "data/monitor/etf_metadata.json").read_text(encoding="utf-8")
        )
        item = next(row for row in metadata["items"] if row["symbol"] == "512010")
        self.assertEqual(item["name"], "医药ETF")
        self.assertEqual(item["index"], {
            "code": "000913",
            "name": "沪深300医药卫生",
            "provider": "中证指数",
        })
        self.assertEqual(item["trading"], {
            "exchange": "SSE",
            "asset_type": "DOMESTIC_EQUITY_ETF",
            "intraday_turnaround": False,
            "sellable_delay_days": 1,
            "lot_size": 100,
            "price_tick": 0.001,
            "price_limit_pct": 0.1,
            "volume_unit_shares": 100,
        })

    def test_512010_is_in_monitor_and_swing_watchlists(self) -> None:
        monitor = json.loads(
            (ROOT / "data/monitor/watchlist.json").read_text(encoding="utf-8")
        )
        monitor_item = next(row for row in monitor["watchlist"] if row["symbol"] == "512010")
        self.assertEqual(monitor_item["name"], "医药ETF")
        self.assertEqual(monitor_item["grid_width_pct"], 0.002)
        self.assertTrue(monitor_item["enabled"])

        swing = json.loads(
            (ROOT / "data/swing/watchlist.json").read_text(encoding="utf-8")
        )
        self.assertIn({"symbol": "512010", "enabled": True}, swing["items"])

    def test_512010_has_index_valuation_snapshot(self) -> None:
        valuation = json.loads(
            (ROOT / "data/monitor/valuation.json").read_text(encoding="utf-8")
        )
        item = next(row for row in valuation["items"] if row["index_code"] == "000913")
        self.assertEqual(item["index_name"], "沪深300医药卫生")
        self.assertEqual(item["as_of"], "2026-09-18")
        self.assertAlmostEqual(item["pe_ttm"], 31.9417)
        self.assertAlmostEqual(item["pb"], 3.2712)
        self.assertAlmostEqual(item["pe_percentile_5y"], 62.314)
        self.assertAlmostEqual(item["pb_percentile_5y"], 12.314)

    def test_512010_has_completed_daily_history_through_2026_09_18(self) -> None:
        history = ROOT / "var/swing/daily_quotes.jsonl"
        rows = [
            json.loads(line)
            for line in history.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        selected = [row for row in rows if row["symbol"] == "512010"]
        self.assertGreaterEqual(len(selected), 240)
        self.assertEqual(max(row["trading_date"] for row in selected), "2026-09-18")
        self.assertTrue(all(row["is_final"] for row in selected))


if __name__ == "__main__":
    unittest.main()
