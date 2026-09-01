from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request

from etf_rotation.cli import PROJECT_ROOT, RUNTIME_ROOT, _parser
from etf_rotation.etf_metadata import EtfMetadata, IndexMetadata, TradingMetadata
from etf_rotation.history_migration import rebuild_history
from etf_rotation.market_data import (
    MarketDataValidator,
    MarketHealthClassifier,
    MinuteHistoryStore,
    finalized_points,
    load_closed_dates,
    market_session_state,
)
from etf_rotation.quote_collector import SOURCE_NAME, Trends2QuoteCollector
from etf_rotation.t_monitor import JsonQuoteAdapter, MarketDataError, Quote, QuotePoint, WatchItem


CALENDAR_PATH = Path(__file__).resolve().parents[1] / "data" / "monitor" / "market_calendar.json"
EXPECTED_CLOSED_DATES = {
    date(2026, 1, 1),
    date(2026, 1, 2),
    date(2026, 2, 16),
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 23),
    date(2026, 4, 6),
    date(2026, 5, 1),
    date(2026, 5, 4),
    date(2026, 5, 5),
    date(2026, 6, 19),
    date(2026, 9, 25),
    date(2026, 10, 1),
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
}
TRADING = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100)


class CliPathTests(unittest.TestCase):
    def test_runtime_defaults_are_outside_tracked_configuration(self) -> None:
        arguments = _parser().parse_args(["monitor", "--no-collect"])
        self.assertEqual(arguments.quotes, RUNTIME_ROOT / "quotes.json")
        self.assertEqual(arguments.history, RUNTIME_ROOT / "quotes.jsonl")
        self.assertEqual(arguments.alert_history, RUNTIME_ROOT / "alerts.jsonl")
        self.assertEqual(
            arguments.watchlist,
            PROJECT_ROOT / "data" / "monitor" / "watchlist.json",
        )
        self.assertEqual(
            arguments.calendar,
            PROJECT_ROOT / "data" / "monitor" / "market_calendar.json",
        )

    def test_rebuild_history_defaults_to_runtime_root(self) -> None:
        arguments = _parser().parse_args(["rebuild-history"])
        self.assertEqual(
            arguments.input,
            PROJECT_ROOT / "data" / "monitor" / "quotes.json",
        )
        self.assertEqual(arguments.output, RUNTIME_ROOT / "quotes.jsonl")
        self.assertEqual(
            arguments.metadata,
            PROJECT_ROOT / "data" / "monitor" / "etf_metadata.json",
        )


def point(
    minute: str,
    price: float = 10.0,
    volume: float = 100.0,
    amount: float | None = None,
    *,
    average_price: float | None = None,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
) -> QuotePoint:
    if amount is None:
        amount = price * volume * TRADING.volume_unit_shares
    return QuotePoint(
        datetime.fromisoformat(f"2026-08-28T{minute}:00+08:00"),
        price,
        price if average_price is None else average_price,
        price if open_price is None else open_price,
        price if high is None else high,
        price if low is None else low,
        volume,
        amount,
    )


def quote_record(**overrides: object) -> dict[str, object]:
    timestamp = "2026-08-28T10:00:00+08:00"
    record: dict[str, object] = {
        "symbol": "510300",
        "name": "沪深300ETF",
        "price": 10.0,
        "average_price": 10.0,
        "previous_close": 10.0,
        "timestamp": timestamp,
        "points": [[timestamp, 10.0, 10.0]],
    }
    record.update(overrides)
    return record


def metadata_for_test() -> dict[str, EtfMetadata]:
    trading = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100)
    metadata = EtfMetadata("510300", "沪深300ETF", IndexMetadata("000300", "沪深300", "中证指数"), trading)
    return {"510300": metadata}


def history_quote(
    price: float,
    previous_close: float,
    observed_at: str,
    *,
    timestamp: str = "2026-08-28T09:30:00+08:00",
) -> Quote:
    point_timestamp = datetime.fromisoformat(timestamp)
    item = QuotePoint(
        timestamp=point_timestamp,
        price=price,
        average_price=price,
        open=price,
        high=price,
        low=price,
        volume=100.0,
        amount=price * 100.0 * 100.0,
    )
    return Quote(
        symbol="510300",
        name="沪深300ETF",
        price=price,
        average_price=price,
        previous_close=previous_close,
        timestamp=point_timestamp,
        points=(item,),
        observed_at=datetime.fromisoformat(observed_at),
        source="TEST",
    )


class MarketDataTests(unittest.TestCase):
    def test_closing_snapshot_rebuilds_clean_schema_v3_history(self) -> None:
        metadata = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "closing-snapshot.json"
            output = root / "monitor" / "quotes.jsonl"
            output.parent.mkdir()
            (output.parent / "alerts.jsonl").write_text("keep-alerts\n", encoding="utf-8")
            source.write_text(json.dumps({
                "schema_version": 2,
                "collected_at": "2026-08-28T17:56:09+08:00",
                "observed_at": "2026-08-28T17:56:09+08:00",
                "source": "TEST",
                "quotes": [{
                    "schema_version": 2,
                    "symbol": "510300",
                    "name": "沪深300ETF",
                    "price": 4.685,
                    "average_price": 4.6845,
                    "previous_close": 4.691,
                    "timestamp": "2026-08-28T09:31:00+08:00",
                    "observed_at": "2026-08-28T17:56:09+08:00",
                    "source": "TEST",
                    "points": [
                        {"timestamp": "2026-08-28T09:30:00+08:00", "price": 4.684, "average_price": 4.684, "open": 4.684, "high": 4.684, "low": 4.684, "volume": 100.0, "amount": 46840.0},
                        {"timestamp": "2026-08-28T09:31:00+08:00", "price": 4.685, "average_price": 4.6845, "open": 4.685, "high": 4.685, "low": 4.685, "volume": 100.0, "amount": 46850.0},
                    ],
                }],
            }), encoding="utf-8")

            count = rebuild_history(source, output, metadata)

            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(count, 2)
            self.assertEqual(len({(item["symbol"], item["timestamp"]) for item in records}), 2)
            first = next(
                item for item in records
                if item["symbol"] == "510300" and item["timestamp"].startswith("2026-08-28T09:30")
            )
            self.assertEqual(first["price"], 4.684)
            self.assertEqual(first["previous_close"], 4.691)
            self.assertTrue(all(
                item["schema_version"] == 3
                and item["observed_at"]
                and item["trading_date"]
                and item["is_complete"]
                for item in records
            ))
            self.assertTrue((output.parent / "history" / "2026-08-28" / "quotes.jsonl").exists())
            self.assertEqual(
                (output.parent / "alerts.jsonl").read_text(encoding="utf-8"),
                "keep-alerts\n",
            )

    def test_failed_rebuild_does_not_replace_existing_output(self) -> None:
        metadata = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "bad.json"
            output = root / "monitor" / "quotes.jsonl"
            source.write_text('{"quotes":[{"symbol":"510300"}]}', encoding="utf-8")
            output.parent.mkdir()
            output.write_text("preserve\n", encoding="utf-8")

            with self.assertRaises(MarketDataError):
                rebuild_history(source, output, metadata)

            self.assertEqual(output.read_text(encoding="utf-8"), "preserve\n")

    def test_history_upserts_later_final_observation_and_writes_schema_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            early = history_quote(4.095, 4.095, "2026-08-28T09:31:01+08:00")
            final = history_quote(4.684, 4.691, "2026-08-28T17:56:09+08:00")

            store.upsert({"510300": early}, metadata_for_test())
            store.upsert({"510300": final}, metadata_for_test())

            records = store.query("2026-08-28", "510300")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["price"], 4.684)
            self.assertEqual(records[0]["previous_close"], 4.691)
            self.assertEqual(records[0]["schema_version"], 3)
            self.assertEqual(records[0]["trading_date"], "2026-08-28")
            self.assertEqual(records[0]["observed_at"], "2026-08-28T17:56:09+08:00")
            self.assertTrue(records[0]["is_complete"])

    def test_history_does_not_persist_current_minute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            current = history_quote(4.684, 4.691, "2026-08-28T09:30:30+08:00")

            store.upsert({"510300": current}, metadata_for_test())

            self.assertEqual(store.query("2026-08-28", "510300"), [])

    def test_history_transaction_rolls_back_canonical_and_daily_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            store.upsert({"510300": history_quote(
                4.684, 4.691, "2026-08-28T09:31:01+08:00",
            )}, metadata_for_test())
            daily = store.daily_root / "2026-08-28" / "quotes.jsonl"
            original_canonical = path.read_bytes()
            original_daily = daily.read_bytes()
            real_replace = os.replace
            injected = False

            def fail_daily_replace(source: object, destination: object) -> None:
                nonlocal injected
                if (
                    not injected
                    and Path(source).suffix == ".tmp"
                    and Path(destination).resolve(strict=False) == daily.resolve(strict=False)
                ):
                    injected = True
                    raise OSError("injected daily replace failure")
                real_replace(source, destination)

            later = history_quote(
                4.685,
                4.691,
                "2026-08-28T09:32:01+08:00",
                timestamp="2026-08-28T09:31:00+08:00",
            )
            with patch("etf_rotation.market_data.os.replace", side_effect=fail_daily_replace):
                with self.assertRaisesRegex(OSError, "injected daily replace failure"):
                    store.upsert({"510300": later}, metadata_for_test())

            self.assertTrue(injected)
            self.assertEqual(path.read_bytes(), original_canonical)
            self.assertEqual(daily.read_bytes(), original_daily)
            leftovers = [
                item for item in Path(temporary).rglob("*")
                if item.is_file() and item.suffix in {".tmp", ".bak"}
            ]
            self.assertEqual(leftovers, [])

    def test_cleanup_failure_does_not_mask_the_original_replace_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            quote = history_quote(4.684, 4.691, "2026-08-28T09:31:01+08:00")

            with (
                patch("etf_rotation.market_data.os.replace", side_effect=OSError("replace failed")),
                patch.object(Path, "unlink", side_effect=OSError("cleanup failed")),
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.upsert({"510300": quote}, metadata_for_test())

    def test_readers_hold_shared_lock_during_history_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            writer = MinuteHistoryStore(path)
            quote = history_quote(4.684, 4.691, "2026-08-28T09:31:01+08:00")
            writer.upsert({"510300": quote}, metadata_for_test())
            operations = {
                "query": lambda store: store.query("2026-08-28", "510300"),
                "available_dates": lambda store: store.available_dates(),
                "merge": lambda store: store.merge({"510300": quote}),
            }

            for name, operation in operations.items():
                with self.subTest(operation=name):
                    hidden = path.with_name(f".{path.name}.{name}.hidden")
                    missing = threading.Event()
                    release = threading.Event()
                    finished = threading.Event()
                    errors: list[BaseException] = []

                    def hold_replace_window() -> None:
                        with writer._lock:
                            os.replace(path, hidden)
                            missing.set()
                            release.wait(timeout=1)
                            os.replace(hidden, path)

                    def read() -> None:
                        try:
                            operation(MinuteHistoryStore(path))
                        except BaseException as error:
                            errors.append(error)
                        finally:
                            finished.set()

                    replace_thread = threading.Thread(target=hold_replace_window)
                    replace_thread.start()
                    self.assertTrue(missing.wait(timeout=1))
                    read_thread = threading.Thread(target=read)
                    read_thread.start()
                    finished_while_missing = finished.wait(timeout=0.05)
                    release.set()
                    replace_thread.join(timeout=1)
                    read_thread.join(timeout=1)

                    self.assertFalse(finished_while_missing)
                    self.assertEqual(errors, [])
                    self.assertTrue(path.exists())

    def test_failed_rollback_is_recovered_by_the_next_store_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            store.upsert({"510300": history_quote(
                4.684, 4.691, "2026-08-28T09:31:01+08:00",
            )}, metadata_for_test())
            daily = store.daily_root / "2026-08-28" / "quotes.jsonl"
            original_canonical = path.read_bytes()
            original_daily = daily.read_bytes()
            journal = path.parent / f".{path.name}.transaction.json"
            real_replace = os.replace
            commit_failed = False
            restore_failed = False

            def fail_commit_and_one_restore(source: object, destination: object) -> None:
                nonlocal commit_failed, restore_failed
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not commit_failed
                    and source_path.suffix == ".tmp"
                    and destination_path.resolve(strict=False) == daily.resolve(strict=False)
                    and ".restore." not in source_path.name
                ):
                    commit_failed = True
                    raise OSError("injected commit failure")
                if (
                    commit_failed
                    and not restore_failed
                    and ".restore." in source_path.name
                    and destination_path.resolve(strict=False) == path.resolve(strict=False)
                ):
                    restore_failed = True
                    raise OSError("injected restore failure")
                real_replace(source, destination)

            later = history_quote(
                4.685,
                4.691,
                "2026-08-28T09:32:01+08:00",
                timestamp="2026-08-28T09:31:00+08:00",
            )
            with patch(
                "etf_rotation.market_data.os.replace",
                side_effect=fail_commit_and_one_restore,
            ):
                with self.assertRaisesRegex(MarketDataError, "事务.*恢复"):
                    store.upsert({"510300": later}, metadata_for_test())

            self.assertTrue(commit_failed)
            self.assertTrue(restore_failed)
            self.assertTrue(journal.exists())
            self.assertNotEqual(path.read_bytes(), original_canonical)

            recovered = MinuteHistoryStore(path).query("2026-08-28", "510300")

            self.assertEqual(len(recovered), 1)
            self.assertEqual(path.read_bytes(), original_canonical)
            self.assertEqual(daily.read_bytes(), original_daily)
            self.assertFalse(journal.exists())
            self.assertEqual(list(Path(temporary).rglob("*.bak")), [])
            self.assertEqual(list(Path(temporary).rglob("*.tmp")), [])

    def test_store_recovers_a_manifest_with_partially_replaced_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            store.upsert({"510300": history_quote(
                4.684, 4.691, "2026-08-28T09:31:01+08:00",
            )}, metadata_for_test())
            daily = store.daily_root / "2026-08-28" / "quotes.jsonl"
            original_canonical = path.read_bytes()
            original_daily = daily.read_bytes()
            canonical_backup = path.parent / f".{path.name}.crash.bak"
            daily_backup = daily.parent / f".{daily.name}.crash.bak"
            leftover_stage = path.parent / f".{path.name}.crash.tmp"
            canonical_backup.write_bytes(original_canonical)
            daily_backup.write_bytes(original_daily)
            leftover_stage.write_text("staged\n", encoding="utf-8")
            path.write_text('{"partial":true}\n', encoding="utf-8")
            daily.write_text('{"partial":true}\n', encoding="utf-8")
            journal = path.parent / f".{path.name}.transaction.json"
            journal.write_text(json.dumps({
                "schema_version": 1,
                "targets": [
                    {
                        "target": str(path.resolve()),
                        "backup": str(canonical_backup.resolve()),
                        "existed": True,
                        "staged": str(leftover_stage.resolve()),
                    },
                    {
                        "target": str(daily.resolve()),
                        "backup": str(daily_backup.resolve()),
                        "existed": True,
                        "staged": None,
                    },
                ],
            }, separators=(",", ":")), encoding="utf-8")

            recovered = MinuteHistoryStore(path).query("2026-08-28", "510300")

            self.assertEqual(len(recovered), 1)
            self.assertEqual(path.read_bytes(), original_canonical)
            self.assertEqual(daily.read_bytes(), original_daily)
            for artifact in (journal, canonical_backup, daily_backup, leftover_stage):
                self.assertFalse(artifact.exists())

    def test_incomplete_legacy_record_is_filtered_and_cannot_be_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            daily = Path(temporary) / "history" / "2026-08-28" / "quotes.jsonl"
            daily.parent.mkdir(parents=True)
            legacy = {
                "schema_version": 2,
                "symbol": "510300",
                "name": "沪深300ETF",
                "trading_date": "2026-08-28",
                "timestamp": "2026-08-28T09:30:00+08:00",
                "previous_close": 4.691,
                "open": 4.684,
                "high": 4.684,
                "low": 4.684,
                "price": 4.684,
                "average_price": 4.684,
                "volume": 100.0,
                "amount": 46840.0,
            }
            original = (json.dumps(legacy, ensure_ascii=False) + "\n").encode("utf-8")
            path.write_bytes(original)
            daily.write_bytes(original)
            store = MinuteHistoryStore(path)

            self.assertEqual(store.query("2026-08-28", "510300"), [])
            with self.assertRaisesRegex(MarketDataError, "observed_at|is_complete"):
                store.upsert({}, metadata_for_test())
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(daily.read_bytes(), original)

    def test_append_compatibility_rejects_missing_ohlc_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            valid = history_quote(4.684, 4.691, "2026-08-28T09:31:01+08:00")
            incomplete_point = QuotePoint(
                valid.points[0].timestamp,
                valid.points[0].price,
                valid.points[0].average_price,
                volume=valid.points[0].volume,
                amount=valid.points[0].amount,
            )
            incomplete = Quote(
                valid.symbol,
                valid.name,
                valid.price,
                valid.average_price,
                valid.previous_close,
                valid.timestamp,
                (incomplete_point,),
                valid.observed_at,
                valid.source,
            )

            with self.assertRaisesRegex(MarketDataError, "开盘价|OHLC"):
                MinuteHistoryStore(path).append_legacy({"510300": incomplete})
            self.assertFalse(path.exists())

    def test_canonical_is_authoritative_and_orphan_daily_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            store.upsert({"510300": history_quote(
                4.684, 4.691, "2026-08-28T09:31:01+08:00",
            )}, metadata_for_test())
            orphan = store.daily_root / "2026-08-27" / "quotes.jsonl"
            orphan.parent.mkdir(parents=True)
            orphan.write_bytes(path.read_bytes())

            self.assertEqual(store.query("2026-08-27", "510300"), [])
            self.assertEqual(store.available_dates(), ["2026-08-28"])
            store.upsert({}, metadata_for_test())
            self.assertFalse(orphan.exists())

    def test_two_store_instances_do_not_lose_concurrent_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            stores = (MinuteHistoryStore(path), MinuteHistoryStore(path))
            quotes = (
                history_quote(4.684, 4.691, "2026-08-28T09:31:01+08:00"),
                history_quote(
                    4.685,
                    4.691,
                    "2026-08-28T09:32:01+08:00",
                    timestamp="2026-08-28T09:31:00+08:00",
                ),
            )
            start = threading.Barrier(3)
            errors: list[BaseException] = []
            original_read = MinuteHistoryStore._read_path

            def slow_canonical_read(instance: MinuteHistoryStore, read_path: Path, *args: object, **kwargs: object) -> object:
                records = original_read(instance, read_path, *args, **kwargs)
                if Path(read_path) == instance.path:
                    time.sleep(0.05)
                return records

            def write(index: int) -> None:
                try:
                    start.wait()
                    stores[index].upsert({"510300": quotes[index]}, metadata_for_test())
                except BaseException as error:
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
            with patch.object(MinuteHistoryStore, "_read_path", new=slow_canonical_read):
                for thread in threads:
                    thread.start()
                start.wait()
                for thread in threads:
                    thread.join(timeout=2)

            self.assertEqual(errors, [])
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            records = MinuteHistoryStore(path).query("2026-08-28", "510300")
            self.assertEqual([item["timestamp"] for item in records], [
                "2026-08-28T09:30:00+08:00",
                "2026-08-28T09:31:00+08:00",
            ])

    def test_upsert_requires_metadata_and_revalidates_existing_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            store.upsert({"510300": history_quote(
                4.684, 4.691, "2026-08-28T09:31:01+08:00",
            )}, metadata_for_test())
            daily = store.daily_root / "2026-08-28" / "quotes.jsonl"
            record = json.loads(path.read_text(encoding="utf-8"))
            for field in ("open", "high", "low", "price", "average_price"):
                record[field] = 40.0
            record["amount"] = 400000.0
            invalid = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            path.write_bytes(invalid)
            original_daily = daily.read_bytes()

            with self.assertRaisesRegex(MarketDataError, "交易元数据"):
                store.upsert({}, {})
            self.assertEqual(path.read_bytes(), invalid)
            self.assertEqual(daily.read_bytes(), original_daily)
            with self.assertRaisesRegex(MarketDataError, "涨跌幅"):
                store.upsert({}, metadata_for_test())
            self.assertEqual(path.read_bytes(), invalid)
            self.assertEqual(daily.read_bytes(), original_daily)

    def test_equivalent_offsets_share_one_shanghai_primary_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            store = MinuteHistoryStore(path)
            utc = history_quote(
                4.684,
                4.691,
                "2026-08-28T01:31:01+00:00",
                timestamp="2026-08-28T01:30:00+00:00",
            )
            shanghai = history_quote(
                4.685,
                4.691,
                "2026-08-28T09:31:02+08:00",
            )

            store.upsert({"510300": utc}, metadata_for_test())
            store.upsert({"510300": shanghai}, metadata_for_test())

            records = store.query("2026-08-28", "510300")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["timestamp"], "2026-08-28T09:30:00+08:00")
            self.assertEqual(records[0]["observed_at"], "2026-08-28T09:31:02+08:00")
            self.assertEqual(records[0]["price"], 4.685)


class FinalizedPointTests(unittest.TestCase):
    def test_only_minutes_observed_at_least_one_minute_later_are_final(self) -> None:
        points = (point("09:30"), point("09:31"), point("09:32"))
        observed = datetime.fromisoformat("2026-08-28T09:32:00+08:00")

        result = finalized_points(points, observed)

        self.assertIsInstance(result, tuple)
        self.assertEqual([item.timestamp.minute for item in result], [30, 31])

    def test_requires_aware_observation_and_point_timestamps(self) -> None:
        aware_observed = datetime.fromisoformat("2026-08-28T09:32:00+08:00")
        naive_point = QuotePoint(
            datetime.fromisoformat("2026-08-28T09:30:00"),
            10.0, 10.0, 10.0, 10.0, 10.0, 100.0, 100_000.0,
        )

        with self.assertRaisesRegex(MarketDataError, "观测时间.*时区"):
            finalized_points((point("09:30"),), datetime.fromisoformat("2026-08-28T09:32:00"))
        with self.assertRaisesRegex(MarketDataError, "分钟时间.*时区"):
            finalized_points((point("09:29"), naive_point), aware_observed)


class MarketSessionStateTests(unittest.TestCase):
    def test_utc_time_is_converted_to_shanghai_session_boundary(self) -> None:
        state = market_session_state(datetime.fromisoformat("2026-08-28T01:30:00+00:00"))

        self.assertEqual(
            (state.phase, state.health_status, state.active, state.catch_up_allowed),
            ("MORNING", "REALTIME", True, False),
        )

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaisesRegex(MarketDataError, "当前时间必须带时区"):
            market_session_state(datetime.fromisoformat("2026-08-28T09:30:00"))

    def test_trading_day_boundaries_control_collection(self) -> None:
        expected = {
            "2026-08-28T09:29:59+08:00": ("PRE_OPEN", "CLOSED", False, False),
            "2026-08-28T09:30:00+08:00": ("MORNING", "REALTIME", True, False),
            "2026-08-28T11:30:00+08:00": ("MORNING", "REALTIME", True, False),
            "2026-08-28T11:30:01+08:00": ("LUNCH_BREAK", "LUNCH_BREAK", False, True),
            "2026-08-28T13:00:00+08:00": ("AFTERNOON", "REALTIME", True, False),
            "2026-08-28T15:00:00+08:00": ("AFTERNOON", "REALTIME", True, False),
            "2026-08-28T15:00:01+08:00": ("CLOSED", "CLOSED", False, True),
        }

        for value, wanted in expected.items():
            with self.subTest(now=value):
                state = market_session_state(datetime.fromisoformat(value))
                self.assertEqual(
                    (state.phase, state.health_status, state.active, state.catch_up_allowed),
                    wanted,
                )

    def test_weekend_and_calendar_closure_never_collect(self) -> None:
        closed_dates = {date(2026, 10, 1)}

        for value in ("2026-08-29T10:00:00+08:00", "2026-10-01T10:00:00+08:00"):
            with self.subTest(now=value):
                state = market_session_state(
                    datetime.fromisoformat(value), closed_dates=closed_dates,
                )
                self.assertEqual(
                    (state.phase, state.health_status, state.active, state.catch_up_allowed),
                    ("CLOSED", "CLOSED", False, False),
                )


class MarketHealthTests(unittest.TestCase):
    def test_distinguishes_realtime_delay_outage_lunch_and_close(self) -> None:
        classifier = MarketHealthClassifier(closed_dates={date(2026, 10, 1)})
        last = datetime.fromisoformat("2026-08-28T10:00:00+08:00")

        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T10:01:00+08:00"), last, None).status, "REALTIME")
        delayed = classifier.classify(datetime.fromisoformat("2026-08-28T10:02:00+08:00"), last, None)
        self.assertEqual(delayed.status, "DELAYED")
        self.assertEqual(delayed.quote_age_seconds, 120.0)
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T10:04:00+08:00"), last, None).status, "OUTAGE")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T12:00:00+08:00"), last, None).status, "LUNCH_BREAK")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-28T15:10:00+08:00"), last, None).status, "CLOSED")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-08-29T10:00:00+08:00"), last, None).status, "CLOSED")
        self.assertEqual(classifier.classify(datetime.fromisoformat("2026-10-01T10:00:00+08:00"), None, None).status, "CLOSED")

    def test_explicit_error_or_missing_quote_is_an_outage_during_session(self) -> None:
        classifier = MarketHealthClassifier()
        now = datetime.fromisoformat("2026-08-28T10:00:00+08:00")

        self.assertEqual(classifier.classify(now, now, "upstream failed").status, "OUTAGE")
        self.assertEqual(classifier.classify(now, None, None).status, "OUTAGE")

        empty_error = classifier.classify(now, now, "")
        self.assertEqual(empty_error.status, "OUTAGE")
        self.assertEqual(empty_error.reason, "行情采集失败")


class CalendarTests(unittest.TestCase):
    def test_2026_calendar_has_exact_schema_source_and_weekday_closures(self) -> None:
        payload = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["source"], "https://www.sse.com.cn/disclosure/dealinstruc/closed/")
        self.assertEqual(load_closed_dates(CALENDAR_PATH), EXPECTED_CLOSED_DATES)

    def test_calendar_loader_requires_schema_one_and_iso_dates(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "calendar.json"
            for payload in (
                {"schema_version": 2, "closed_dates": []},
                {"schema_version": 1, "closed_dates": ["2026-02-30"]},
                {"schema_version": 1, "closed_dates": "2026-10-01"},
            ):
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(MarketDataError):
                        load_closed_dates(path)


class MarketDataValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = MarketDataValidator(TRADING)

    def test_accepts_session_boundaries_and_price_limit_plus_one_tick(self) -> None:
        self.validator.validate_point(point("09:30"), previous_close=10.0)
        self.validator.validate_point(point("11:30"), previous_close=10.0)
        self.validator.validate_point(point("13:00"), previous_close=10.0)
        self.validator.validate_point(point("15:00"), previous_close=10.0)
        self.validator.validate_point(point("09:31", 11.001), previous_close=10.0)

    def test_rejects_points_outside_continuous_sessions(self) -> None:
        for minute in ("09:29", "11:31", "12:59", "15:01"):
            with self.subTest(minute=minute), self.assertRaisesRegex(MarketDataError, "交易时段"):
                self.validator.validate_point(point(minute), previous_close=10.0)

    def test_rejects_non_finite_or_non_positive_prices(self) -> None:
        invalid_points = (
            point("09:30", price=0.0),
            point("09:30", average_price=math.nan),
            point("09:30", open_price=-1.0),
            point("09:30", high=math.inf),
        )
        for item in invalid_points:
            with self.subTest(item=item), self.assertRaisesRegex(MarketDataError, "有限正数"):
                self.validator.validate_point(item, previous_close=10.0)

    def test_rejects_invalid_ohlc_and_price_limit(self) -> None:
        with self.assertRaisesRegex(MarketDataError, "OHLC"):
            self.validator.validate_point(point("09:31", price=10.1, open_price=10.0, high=10.0, low=10.0), 10.0)
        with self.assertRaisesRegex(MarketDataError, "涨跌幅"):
            self.validator.validate_point(point("09:31", price=11.002), 10.0)

    def test_rejects_invalid_volume_amount_and_zero_pair(self) -> None:
        for item in (
            point("09:31", volume=-1.0, amount=1.0),
            point("09:31", volume=1.0, amount=math.inf),
            point("09:31", volume=0.0, amount=1.0),
            point("09:31", volume=1.0, amount=0.0),
        ):
            with self.subTest(item=item), self.assertRaisesRegex(MarketDataError, "成交量|成交额|零"):
                self.validator.validate_point(item, 10.0)

    def test_rejects_amount_implied_price_outside_ohlc_with_tick_tolerance(self) -> None:
        self.validator.validate_point(
            point("09:31", price=10.0, open_price=10.0, high=10.1, low=9.9, amount=9.899 * 100 * 100),
            10.0,
        )
        with self.assertRaisesRegex(MarketDataError, "量价"):
            self.validator.validate_point(
                point("09:31", price=10.0, open_price=10.0, high=10.1, low=9.9, amount=9.898 * 100 * 100),
                10.0,
            )

    def test_amount_check_allows_one_truncated_volume_unit_for_odd_lots(self) -> None:
        self.validator.validate_point(
            point(
                "09:31",
                price=7.948,
                open_price=7.943,
                high=7.948,
                low=7.943,
                volume=1135.0,
                amount=902238.0,
            ),
            7.95,
        )
        with self.assertRaisesRegex(MarketDataError, "量价"):
            self.validator.validate_point(
                point(
                    "09:31",
                    price=7.948,
                    open_price=7.943,
                    high=7.948,
                    low=7.943,
                    volume=1135.0,
                    amount=(1136.1 * 100 * 7.949),
                ),
                7.95,
            )


class QuoteObservationTests(unittest.TestCase):
    def test_adapter_parses_observed_at_collected_at_and_source(self) -> None:
        adapter = JsonQuoteAdapter()
        observed = adapter.parse([quote_record(
            schema_version=2,
            observed_at="2026-08-28T10:00:05+08:00",
            collected_at="2026-08-28T10:00:06+08:00",
            source="TEST SOURCE",
        )])["510300"]
        collected = adapter.parse([quote_record(
            schema_version=2,
            collected_at="2026-08-28T10:00:07+08:00",
            source="TEST SOURCE",
        )])["510300"]

        self.assertEqual(observed.observed_at.isoformat(), "2026-08-28T10:00:05+08:00")
        self.assertEqual(collected.observed_at.isoformat(), "2026-08-28T10:00:07+08:00")
        self.assertEqual(observed.source, "TEST SOURCE")

    def test_only_schema_less_fixture_defaults_observation_metadata(self) -> None:
        with self.assertRaisesRegex(MarketDataError, "observed_at|collected_at"):
            JsonQuoteAdapter().parse([quote_record()])

        fixture_quote = JsonQuoteAdapter(allow_fixture_defaults=True).parse([quote_record()])["510300"]
        self.assertEqual(fixture_quote.observed_at, fixture_quote.timestamp + timedelta(minutes=1))
        self.assertEqual(fixture_quote.source, "TEST_FIXTURE")

        with self.assertRaisesRegex(MarketDataError, "observed_at|collected_at"):
            JsonQuoteAdapter().parse([quote_record(schema_version=2, source="TEST SOURCE")])
        with self.assertRaisesRegex(MarketDataError, "source"):
            JsonQuoteAdapter().parse([quote_record(schema_version=2, collected_at="2026-08-28T10:01:00+08:00")])

    def test_collector_uses_one_observation_time_and_retains_schema_and_source(self) -> None:
        calls = 0

        def now() -> datetime:
            nonlocal calls
            calls += 1
            return datetime.fromisoformat("2026-08-28T10:00:05+08:00")

        def transport(request: Request, timeout: float) -> bytes:
            symbol = "510300" if "1.510300" in request.full_url else "159915"
            market = 1 if symbol == "510300" else 0
            return json.dumps({
                "rc": 0,
                "data": {
                    "code": symbol,
                    "market": market,
                    "name": symbol,
                    "preClose": 10.0,
                    "trends": ["2026-08-28 10:00,10.0,10.0,10.0,10.0,100,100000,10.0"],
                },
            }).encode("utf-8")

        payload = Trends2QuoteCollector(transport=transport, now=now).collect((
            WatchItem("510300", "沪深300ETF", 0.002),
            WatchItem("159915", "创业板ETF", 0.002),
        ))

        self.assertEqual(calls, 1)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["observed_at"], "2026-08-28T10:00:05+08:00")
        self.assertEqual(payload["collected_at"], payload["observed_at"])
        for record in payload["quotes"]:
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(record["observed_at"], payload["observed_at"])
            self.assertEqual(record["collected_at"], payload["observed_at"])
            self.assertEqual(record["source"], SOURCE_NAME)

    def test_collector_preserves_batch_start_boundary_across_minute_requests(self) -> None:
        clock = {"now": datetime.fromisoformat("2026-08-28T10:00:30+08:00")}
        request_count = 0

        def transport(request: Request, timeout: float) -> bytes:
            nonlocal request_count
            request_count += 1
            symbol = "510300" if "1.510300" in request.full_url else "159915"
            market = 1 if symbol == "510300" else 0
            trends = ["2026-08-28 10:00,10.0,10.0,10.0,10.0,100,100000,10.0"]
            if request_count == 2:
                trends.append("2026-08-28 10:01,10.0,10.0,10.0,10.0,100,100000,10.0")
                clock["now"] = datetime.fromisoformat("2026-08-28T10:01:05+08:00")
            return json.dumps({
                "rc": 0,
                "data": {
                    "code": symbol,
                    "market": market,
                    "name": symbol,
                    "preClose": 10.0,
                    "trends": trends,
                },
            }).encode("utf-8")

        payload = Trends2QuoteCollector(
            transport=transport,
            now=lambda: clock["now"],
        ).collect((
            WatchItem("510300", "沪深300ETF", 0.002),
            WatchItem("159915", "创业板ETF", 0.002),
        ))

        observed_at = datetime.fromisoformat(payload["observed_at"])
        self.assertEqual(observed_at, datetime.fromisoformat("2026-08-28T10:00:30+08:00"))
        self.assertEqual({record["observed_at"] for record in payload["quotes"]}, {payload["observed_at"]})
        self.assertTrue(all(
            [point_value["timestamp"] for point_value in record["points"]]
            == ["2026-08-28T10:00:00+08:00"]
            for record in payload["quotes"]
        ))
        quotes = JsonQuoteAdapter().parse(payload)
        self.assertTrue(all(not finalized_points(quote.points, quote.observed_at) for quote in quotes.values()))

    def test_collector_rejects_a_minute_later_than_batch_observation(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            return json.dumps({
                "rc": 0,
                "data": {
                    "code": "510300",
                    "market": 1,
                    "name": "510300",
                    "preClose": 10.0,
                    "trends": ["2026-08-28 10:01,10.0,10.0,10.0,10.0,100,100000,10.0"],
                },
            }).encode("utf-8")

        collector = Trends2QuoteCollector(
            transport=transport,
            now=lambda: datetime.fromisoformat("2026-08-28T10:00:30+08:00"),
        )
        with self.assertRaisesRegex(MarketDataError, "晚于观测时间"):
            collector.collect((WatchItem("510300", "沪深300ETF", 0.002),))


if __name__ == "__main__":
    unittest.main()
