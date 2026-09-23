"""ETF/index identifier compatibility; fixtures contain no private holdings."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.etf_metadata import EtfMetadataStore, MetadataError
import etf_rotation.etf_metadata as metadata_module
from etf_rotation.valuation import ValuationStore
from tests.swing_helpers import metadata_fixture


class IndexIdentifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def metadata(self, *, symbol: str = "513050", index_code: object = "H30533"):
        payload = metadata_fixture((symbol,))
        payload["items"][0]["index"]["code"] = index_code
        path = self.root / "metadata.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return EtfMetadataStore(path).load()

    def valuations(self, index_code: object):
        path = self.root / "valuation.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "items": [{
                "index_code": index_code,
                "index_name": "Synthetic index",
                "as_of": None,
                "status": "MISSING_VALUATION",
                "source": None,
            }],
        }), encoding="utf-8")
        return ValuationStore(path).load()

    def test_metadata_accepts_real_alphanumeric_index_without_losing_prefix(self) -> None:
        try:
            records = self.metadata()
        except MetadataError as error:
            self.fail(f"Real index identifier H30533 must load unchanged: {error}")
        self.assertEqual(records["513050"].index.code, "H30533")
        self.assertEqual(records["513050"].to_dict()["index"]["code"], "H30533")

    def test_valuation_accepts_same_canonical_index_identifier(self) -> None:
        records = self.valuations("H30533")
        self.assertIn("H30533", records)
        self.assertEqual(records["H30533"].to_dict()["index_code"], "H30533")
        self.assertEqual(records["H30533"].status, "MISSING_VALUATION")
        self.assertIsNone(records["H30533"].pe_ttm)

    def test_existing_numeric_index_identifiers_remain_supported(self) -> None:
        self.assertEqual(self.metadata(index_code="000300")["513050"].index.code, "000300")
        self.assertIn("000300", self.valuations("000300"))

    def test_security_identifiers_remain_six_ascii_digits(self) -> None:
        for code in ("H30533", "51305", "5130500", "５１３０５０", "51305 ", "513050.SH"):
            with self.subTest(code=code), self.assertRaises(MetadataError):
                self.metadata(symbol=code, index_code="000300")

    def test_index_identifiers_reject_noncanonical_or_unsafe_values(self) -> None:
        for code in (None, 30533, True, "", "H3053", "H305330", "h30533", "H30533.CSI", "H3053/", " H30533", "Ｈ30533", "０００３００"):
            with self.subTest(code=code):
                with self.assertRaises(MetadataError):
                    self.metadata(index_code=code)
                self.assertEqual(self.valuations(code), {})


class CashflowValueMetadataTests(unittest.TestCase):
    def test_repository_cashflow_and_value_etfs_are_domestic_t1_with_ten_percent_limit(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        records = EtfMetadataStore(repository / "data/monitor/etf_metadata.json").load()
        for symbol, index_code in (("159201", "980092"), ("159263", "980081"), ("159259", "980080")):
            with self.subTest(symbol=symbol):
                item = records[symbol]
                self.assertEqual(item.index.code, index_code)
                self.assertEqual(item.trading.exchange, "SZSE")
                self.assertEqual(item.trading.asset_type, "DOMESTIC_EQUITY_ETF")
                self.assertFalse(item.trading.intraday_turnaround)
                self.assertEqual(item.trading.sellable_delay_days, 1)
                self.assertEqual(item.trading.price_limit_pct, 0.10)
                self.assertEqual(item.trading.price_tick, 0.001)
                self.assertEqual(item.trading.lot_size, 100)
                self.assertEqual(item.trading.volume_unit_shares, 100)


class PendingEtfIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pending_etfs.json"
        self.record = {
            "symbol": "513050",
            "name": "Public test ETF identity",
            "index": {"code": "H30533", "name": "Public test index", "provider": "CSI"},
            "exchange": "SSE",
            "asset_type": "QDII_EQUITY_ETF",
            "intraday_turnaround": True,
            "sellable_delay_days": 0,
            "lot_size": 100,
            "price_tick": 0.001,
            "price_limit_pct": 0.10,
            "volume_unit_shares": None,
            "status": "OBSERVATION_ONLY",
            "sources": ["https://www.efunds.com.cn/fund/513050.shtml"],
        }

    def load(self, payload: object | None = None):
        loader = getattr(metadata_module, "load_pending_etfs", None)
        self.assertTrue(callable(loader), "Pending identity loader must exist independently of trading metadata")
        if payload is not None:
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        return loader(self.path)

    def payload(self, record: dict | None = None):
        return {"schema_version": 1, "items": [deepcopy(self.record if record is None else record)]}

    def test_missing_pending_catalog_is_empty_without_creating_file(self) -> None:
        self.assertEqual(self.load(), {})
        self.assertFalse(self.path.exists())

    def test_pending_identity_keeps_unknown_units_without_trading_metadata(self) -> None:
        result = self.load(self.payload())
        self.assertEqual(result, {"513050": self.record})
        self.assertIs(type(result["513050"]), dict)
        self.assertNotIn("trading", result["513050"])
        self.assertIsNone(result["513050"]["volume_unit_shares"])
        result["513050"]["index"]["code"] = "000300"
        result["513050"]["sources"].append("https://example.com/changed")
        self.assertEqual(self.load()["513050"], self.record)

    def test_pending_catalog_rejects_malformed_top_level_and_duplicate_symbols(self) -> None:
        invalid = (
            [], {"schema_version": True, "items": []}, {"schema_version": 2, "items": []},
            {"schema_version": 1, "items": {}}, {"schema_version": 1, "items": [], "extra": True},
            {"schema_version": 1, "items": [self.record, self.record]},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(MetadataError):
                self.load(payload)

    def test_pending_catalog_rejects_invalid_or_strategy_ready_fields(self) -> None:
        invalid = (
            ("symbol", "H30533"), ("symbol", "５１３０５０"), ("name", " "),
            ("exchange", "NYSE"), ("asset_type", "STOCK"), ("intraday_turnaround", 1),
            ("sellable_delay_days", True), ("sellable_delay_days", 1),
            ("lot_size", 0), ("lot_size", True), ("price_tick", 0), ("price_tick", float("nan")),
            ("price_limit_pct", 0), ("price_limit_pct", 1.1), ("volume_unit_shares", 100),
            ("status", "VERIFIED"), ("sources", []), ("sources", ["file:///secret"]),
            ("sources", ["https://user:password@example.com/"]), ("extra", True),
            ("index", {"code": "H30533.CSI", "name": "Name", "provider": "CSI"}),
            ("index", {"code": "H30533", "name": "Name", "provider": "CSI", "extra": True}),
        )
        for field, value in invalid:
            record = deepcopy(self.record)
            record[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(MetadataError):
                self.load(self.payload(record))
        record = deepcopy(self.record)
        record.pop("sources")
        with self.assertRaises(MetadataError):
            self.load(self.payload(record))

    def test_pending_catalog_wraps_invalid_json_and_utf8(self) -> None:
        for content in (b"{invalid", b"\xff"):
            self.path.write_bytes(content)
            with self.subTest(content=content), self.assertRaises(MetadataError):
                self.load()

    def test_pending_catalog_rejects_unencodable_escaped_text(self) -> None:
        records = []
        for field in ("name", "index", "sources"):
            record = deepcopy(self.record)
            if field == "index":
                record[field]["name"] = "synthetic-\ud800"
            elif field == "sources":
                record[field] = ["https://example.com/synthetic-\ud800"]
            else:
                record[field] = "synthetic-\ud800"
            records.append(record)
        for record in records:
            with self.assertRaises(MetadataError):
                self.load(self.payload(record))

    def test_repository_onboarded_etfs_have_no_duplicate_pending_identity(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        loader = getattr(metadata_module, "load_pending_etfs", None)
        self.assertTrue(callable(loader), "Pending identity loader must exist")
        pending = loader(repository / "data/monitor/pending_etfs.json")
        active = EtfMetadataStore(repository / "data/monitor/etf_metadata.json").load()
        self.assertFalse(set(pending) & set(active))
        for symbol, index, exchange in (("159792", "931637", "SZSE"), ("513050", "H30533", "SSE")):
            self.assertIn(symbol, active)
            self.assertNotIn(symbol, pending)
            self.assertEqual(active[symbol].index.code, index)
            self.assertEqual(active[symbol].trading.exchange, exchange)
            self.assertEqual(active[symbol].trading.volume_unit_shares, 100)
            self.assertTrue(active[symbol].trading.intraday_turnaround)
            self.assertEqual(active[symbol].trading.sellable_delay_days, 0)
        self.assertNotIn("600036", active)
        self.assertFalse(active["515180"].trading.intraday_turnaround)


if __name__ == "__main__":
    unittest.main()
