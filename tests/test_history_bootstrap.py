from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_config import SwingWatchItem
from etf_rotation.swing_data import DailyHistoryStore, SHANGHAI
from tests.swing_helpers import metadata_fixture, retime_daily_bars, swing_strategy_bars
from tests.test_swing_config import SWING_V1_DEFAULTS


class StaticCollector:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def collect(self, watchlist, last_completed_date, count):
        self.calls.append((tuple(watchlist), last_completed_date, count))
        return self.bars


class HistoryBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("etf_rotation.history_bootstrap"),
            "Independent history bootstrap module is not implemented",
        )
        self.api = importlib.import_module("etf_rotation.history_bootstrap")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.paths = self.api.BootstrapPaths(root=self.root)
        self.write_json(self.paths.metadata, metadata_fixture(("515180", "510300")))
        self.write_json(self.paths.calendar, {"schema_version": 1, "closed_dates": []})
        self.write_json(self.paths.strategy, SWING_V1_DEFAULTS)
        self.watchlist = self.root / "data/swing/watchlist.json"
        self.write_json(self.watchlist, {
            "schema_version": 1, "items": [{"symbol": "515180", "enabled": False}],
        })
        self.end = date(2026, 9, 3)
        self.now = lambda: datetime(2026, 9, 4, 12, tzinfo=SHANGHAI)
        self.bars = retime_daily_bars(
            swing_strategy_bars(70, symbol="515180", pattern="flat"), ending_on=self.end,
        )
        self.collector = StaticCollector(self.bars)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def stage(self, **kwargs):
        arguments = dict(symbols=("515180",), end_date=self.end, count=70,
                         paths=self.paths, collector=self.collector, now=self.now)
        arguments.update(kwargs)
        return self.api.stage_history(**arguments)

    def apply(self, stage):
        return self.api.apply_history(stage, self.paths, now=self.now)

    def store(self):
        return DailyHistoryStore(self.paths.history, EtfMetadataStore(self.paths.metadata).load(), ())

    def test_disabled_verified_symbol_stages_without_touching_history_or_watchlist(self):
        before = self.watchlist.read_bytes()
        stage = self.stage()
        self.assertTrue(stage.is_dir())
        self.assertFalse(self.paths.history.exists())
        self.assertEqual(self.watchlist.read_bytes(), before)
        self.assertEqual(self.collector.calls, [((SwingWatchItem("515180", True),), self.end, 70)])
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["symbols"], ["515180"])
        self.assertEqual(manifest["summary"]["515180"]["count"], 70)
        self.assertEqual(manifest["summary"]["515180"]["latest"], "2026-09-03")
        self.assertEqual(manifest["summary"]["515180"]["source"], ["TEST_DAILY"])
        content = (stage / "daily_quotes.jsonl").read_bytes()
        self.assertEqual(manifest["sha256"], hashlib.sha256(content).hexdigest())
        staged = DailyHistoryStore(stage / "daily_quotes.jsonl", EtfMetadataStore(self.paths.metadata).load(), ()).load()
        self.assertEqual(staged, self.bars)

    def test_staging_uses_unique_directories(self):
        self.assertNotEqual(self.stage(), self.stage())

    def test_unknown_pending_duplicate_or_malformed_selection_rejected_before_collect(self):
        self.write_json(self.root / "data/monitor/pending_etfs.json", {
            "schema_version": 1, "items": [{"symbol": "159792", "status": "OBSERVATION_ONLY"}],
        })
        for symbols in (("159792",), ("999999",), ("515180", "515180"), (), "515180", ("bad",)):
            with self.subTest(symbols=symbols), self.assertRaises(self.api.HistoryBootstrapError):
                self.stage(symbols=symbols)
        self.assertEqual(self.collector.calls, [])

    def test_count_requires_at_least_70_and_current_strategy_minimum(self):
        for count in (69, True, "70", 10001):
            with self.subTest(count=count), self.assertRaises(self.api.HistoryBootstrapError):
                self.stage(count=count)
        self.write_json(self.paths.strategy, {**SWING_V1_DEFAULTS, "minimum_daily_bars": 80})
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.stage(count=70)
        self.assertEqual(self.collector.calls, [])

    def test_default_count_accepts_100_valid_bars_above_strategy_minimum(self):
        bars = retime_daily_bars(
            swing_strategy_bars(100, symbol="515180", pattern="flat"), ending_on=self.end,
        )
        collector = StaticCollector(bars)
        try:
            stage = self.api.stage_history(
                ("515180",), self.end, paths=self.paths, collector=collector, now=self.now,
            )
        except self.api.HistoryBootstrapError as error:
            self.fail(f"100 completed bars must satisfy the 70-bar minimum: {error}")
        manifest = json.loads((stage / "manifest.json").read_bytes())
        self.assertEqual(manifest["requested_count"], 240)
        self.assertEqual(manifest["summary"]["515180"]["count"], 100)
        self.assertEqual(collector.calls[0][2], 240)

    def test_default_count_still_rejects_69_bars(self):
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.stage(count=240, collector=StaticCollector(self.bars[1:]))

    def test_requested_count_is_an_upper_bound(self):
        bars = retime_daily_bars(
            swing_strategy_bars(71, symbol="515180", pattern="flat"), ending_on=self.end,
        )
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.stage(count=70, collector=StaticCollector(bars))

    def test_provider_iterator_is_not_consumed_beyond_requested_batch_bound(self):
        consumed = []

        def excess_records():
            for index in range(72):
                consumed.append(index)
                yield self.bars[-1]

        with self.assertRaises(self.api.HistoryBootstrapError):
            self.stage(count=70, collector=StaticCollector(excess_records()))
        self.assertLessEqual(len(consumed), 71)

    def test_apply_uses_current_strategy_minimum_not_requested_count(self):
        bars = retime_daily_bars(
            swing_strategy_bars(100, symbol="515180", pattern="flat"), ending_on=self.end,
        )
        stage = self.stage(count=100, collector=StaticCollector(bars))
        manifest = json.loads((stage / "manifest.json").read_bytes())
        manifest["requested_count"] = 240
        encoded = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        (stage / "manifest.json").write_bytes(encoded)
        (stage / "manifest.sha256").write_bytes((hashlib.sha256(encoded).hexdigest() + "\n").encode("ascii"))
        self.write_json(self.paths.strategy, {**SWING_V1_DEFAULTS, "minimum_daily_bars": 80})
        try:
            self.apply(stage)
        except self.api.HistoryBootstrapError as error:
            self.fail(f"100 completed bars must satisfy the current 80-bar minimum: {error}")
        self.assertEqual(len(self.store().query("515180")), 100)
        self.write_json(self.paths.strategy, {**SWING_V1_DEFAULTS, "minimum_daily_bars": 110})
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.apply(stage)

    def test_nontrading_future_and_midday_today_rejected(self):
        for end in (date(2026, 9, 5), date(2026, 9, 7), date(2026, 9, 4), "20260903"):
            with self.subTest(end=end), self.assertRaises(self.api.HistoryBootstrapError):
                self.stage(end_date=end)
        self.write_json(self.paths.calendar, {"schema_version": 1, "closed_dates": [self.end.isoformat()]})
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.stage()
        self.assertEqual(self.collector.calls, [])

    def test_insufficient_stale_incomplete_invalid_and_wrong_symbol_batches_rejected(self):
        bad_batches = {
            "insufficient": self.bars[1:],
            "stale": retime_daily_bars(self.bars, ending_on=date(2026, 9, 2)),
            "incomplete": (*self.bars[:10], *self.bars[11:]),
            "duplicate": (*self.bars, self.bars[-1]),
            "ohlc": (*self.bars[:-1], replace(self.bars[-1], high=1.0)),
            "not_final": (*self.bars[:-1], replace(self.bars[-1], is_final=False)),
            "units": (*self.bars[:-1], replace(self.bars[-1], volume=self.bars[-1].volume * 100)),
            "other": (*self.bars, replace(self.bars[-1], symbol="510300")),
            "missing_symbol": (),
        }
        for label, bars in bad_batches.items():
            with self.subTest(label=label), self.assertRaises(self.api.HistoryBootstrapError):
                self.stage(collector=StaticCollector(bars))
        self.assertFalse(self.paths.history.exists())

    def test_sequence_gap_is_rejected_even_when_requested_count_is_met(self):
        bars = retime_daily_bars(swing_strategy_bars(71, symbol="515180", pattern="flat"), ending_on=self.end)
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.stage(collector=StaticCollector((*bars[:10], *bars[11:])))

    def test_estimated_turnover_warning_is_explicit(self):
        collector = StaticCollector(tuple(replace(bar, source="腾讯; amount=OHLC均价×成交量(手)×100估算") for bar in self.bars))
        stage = self.stage(collector=collector)
        manifest = json.loads((stage / "manifest.json").read_text(encoding="utf-8"))
        warning = " ".join(manifest["summary"]["515180"]["warnings"])
        self.assertIn("估算", warning)
        self.assertIn("单位", warning)

    def test_apply_preserves_other_symbols_watchlist_and_exact_recoverable_backup(self):
        others = retime_daily_bars(swing_strategy_bars(3, symbol="510300", pattern="flat"), ending_on=self.end)
        self.store().upsert(others)
        before_history = self.paths.history.read_bytes()
        before_watchlist = self.watchlist.read_bytes()
        result = self.apply(self.stage())
        self.assertEqual(self.store().query("510300"), others)
        self.assertEqual(self.store().query("515180"), self.bars)
        self.assertEqual(Path(result["backup_path"]).read_bytes(), before_history)
        self.assertEqual(self.watchlist.read_bytes(), before_watchlist)
        self.assertEqual(result["total_count"], 73)
        second = self.apply(self.stage())
        self.assertNotEqual(second["backup_path"], result["backup_path"])

    def test_apply_to_missing_history_records_empty_recoverable_backup(self):
        result = self.apply(self.stage())
        self.assertFalse(result["history_existed"])
        self.assertEqual(Path(result["backup_path"]).read_bytes(), b"")

    def test_backup_write_failure_preserves_exact_formal_history(self):
        existing = retime_daily_bars(
            swing_strategy_bars(3, symbol="510300", pattern="flat"), ending_on=self.end,
        )
        self.store().upsert(existing)
        original = self.paths.history.read_bytes()
        stage = self.stage()
        with patch.object(self.api, "_write_new", side_effect=OSError("backup unavailable")):
            with self.assertRaises(self.api.HistoryBootstrapError):
                self.apply(stage)
        self.assertEqual(self.paths.history.read_bytes(), original)
        self.assertEqual(self.store().load(), existing)
        self.assertEqual(list((self.paths.history.parent / "backups").glob("*.jsonl")), [])

    def test_atomic_replace_failure_preserves_history_and_exact_backup(self):
        existing = retime_daily_bars(
            swing_strategy_bars(3, symbol="510300", pattern="flat"), ending_on=self.end,
        )
        self.store().upsert(existing)
        original = self.paths.history.read_bytes()
        stage = self.stage()
        with patch.object(DailyHistoryStore, "_replace_file", side_effect=OSError("replace unavailable")):
            with self.assertRaises(self.api.HistoryBootstrapError):
                self.apply(stage)
        self.assertEqual(self.paths.history.read_bytes(), original)
        self.assertEqual(self.store().load(), existing)
        backups = list((self.paths.history.parent / "backups").glob("*.jsonl"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(list(self.paths.history.parent.glob(".daily_quotes.jsonl.*.tmp")), [])

    def test_merge_gap_rejection_preserves_history_and_exact_backup(self):
        complete = retime_daily_bars(
            swing_strategy_bars(73, symbol="515180", pattern="flat"), ending_on=self.end,
        )
        existing = complete[:2]
        self.assertLess(existing[-1].trading_date, complete[2].trading_date)
        self.assertLess(complete[2].trading_date, self.bars[0].trading_date)
        self.store().upsert(existing)
        original = self.paths.history.read_bytes()
        stage = self.stage()
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.apply(stage)
        self.assertEqual(self.paths.history.read_bytes(), original)
        self.assertEqual(self.store().load(), existing)
        backups = list((self.paths.history.parent / "backups").glob("*.jsonl"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_changed_data_or_manifest_is_rejected_before_formal_writes(self):
        for filename in ("daily_quotes.jsonl", "manifest.json", "manifest.sha256"):
            with self.subTest(filename=filename):
                stage = self.stage()
                path = stage / filename
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaises(self.api.HistoryBootstrapError):
                    self.apply(stage)
                self.assertFalse(self.paths.history.exists())

    def test_apply_rechecks_current_metadata_calendar_strategy(self):
        for label in ("metadata", "calendar", "strategy"):
            with self.subTest(label=label):
                stage = self.stage()
                path = getattr(self.paths, label)
                old = path.read_bytes()
                if label == "metadata":
                    self.write_json(path, metadata_fixture(("510300",)))
                elif label == "calendar":
                    self.write_json(path, {"schema_version": 1, "closed_dates": [self.bars[10].trading_date.isoformat()]})
                else:
                    self.write_json(path, {**SWING_V1_DEFAULTS, "minimum_daily_bars": 80})
                try:
                    with self.assertRaises(self.api.HistoryBootstrapError):
                        self.apply(stage)
                    self.assertFalse(self.paths.history.exists())
                finally:
                    path.write_bytes(old)

    def test_apply_rejects_corrupt_existing_history_without_overwriting_it(self):
        stage = self.stage()
        self.paths.history.parent.mkdir(parents=True, exist_ok=True)
        self.paths.history.write_bytes(b"corrupt history\n")
        with self.assertRaises(self.api.HistoryBootstrapError):
            self.apply(stage)
        self.assertEqual(self.paths.history.read_bytes(), b"corrupt history\n")

    def test_collector_errors_and_cli_failures_do_not_leak_secrets(self):
        with patch.object(self.collector, "collect", side_effect=RuntimeError("https://private/?token=SECRET")):
            with self.assertRaises(self.api.HistoryBootstrapError) as caught:
                self.stage()
        self.assertNotIn("SECRET", str(caught.exception))
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(self.api, "stage_history", side_effect=RuntimeError("SECRET")):
            status = self.api.main(["stage", "--symbols", "515180", "--end-date", "2026-09-03"])
        self.assertNotEqual(status, 0)
        self.assertNotIn("SECRET", stderr.getvalue())

    def test_cli_apply_warns_operator_to_stop_service(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), patch.object(self.api, "apply_history", return_value={"ok": True}):
            self.assertEqual(self.api.main(["apply", "--stage-dir", "example"]), 0)
        self.assertIn("停止", output.getvalue())


if __name__ == "__main__":
    unittest.main()
