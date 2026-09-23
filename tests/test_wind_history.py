"""Offline tests for quarantined Wind history; no network or credentials."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from etf_rotation.wind_history import WindHistoryClient, WindHistoryError, main, stage_history
from tests.swing_helpers import metadata_fixture


SHANGHAI = timezone(timedelta(hours=8))
OBSERVED = datetime(2026, 9, 2, 15, 20, tzinfo=SHANGHAI)
SYMBOLS = ("510300", "510500", "563360", "512100", "159915", "588000")
COLUMNS = ("TIME", "OPEN", "MATCH", "HIGH", "LOW", "TURNOVER", "VOLUME", "CHANGEHANDRATE", "AVPRICE")


def response(*, dates: tuple[str, ...] = ("2026-08-31", "2026-09-01", "2026-09-02")) -> dict:
    payload = {
        "data": {
            "columns": [{"name": name, "type": "string"} for name in COLUMNS],
            "rows": [
                [day + "T00:00:00+08:00", "4.100", "4.200", "4.300", "4.000", "112233445.6789", "123456.7890", "0.54", "4.150"]
                for day in dates
            ],
            "unit": {"TURNOVER 单位：": "元"},
        },
        "error": None,
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": False,
        "cli_meta": {"source": "fund_data", "elapsed_ms": 12},
    }


def mutate(envelope: dict, change) -> dict:
    result = deepcopy(envelope)
    payload = json.loads(result["content"][0]["text"])
    change(payload)
    result["content"][0]["text"] = json.dumps(payload, ensure_ascii=False)
    return result


class RecordedClient:
    def __init__(self, responses: list[dict] | None = None):
        self.calls: list[dict] = []
        self.responses = responses

    def fetch(self, params: dict) -> dict:
        self.calls.append(deepcopy(params))
        if self.responses is None:
            return response()
        return deepcopy(self.responses[len(self.calls) - 1])


class StageHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.watchlist = self.root / "watchlist.json"
        self.metadata = self.root / "etf_metadata.json"
        self.calendar = self.root / "market_calendar.json"
        self.calendar.write_text(json.dumps({
            "schema_version": 1, "closed_dates": ["2026-05-01"],
        }), encoding="utf-8")
        self.output = self.root / "var" / "swing" / "wind"
        self.write_configs()

    def write_configs(self, symbols: tuple[str, ...] = SYMBOLS) -> None:
        self.watchlist.write_text(json.dumps({
            "schema_version": 1,
            "items": [{"symbol": symbol, "enabled": True} for symbol in symbols],
        }), encoding="utf-8")
        self.metadata.write_text(json.dumps(metadata_fixture(symbols)), encoding="utf-8")

    def stage(self, client: RecordedClient | None = None, **kwargs) -> Path:
        return stage_history(
            client=client or RecordedClient(), output=self.output,
            watchlist_path=self.watchlist, metadata_path=self.metadata,
            calendar_path=self.calendar,
            **{"end_date": "2026-09-02", "count": 2, "now": OBSERVED, **kwargs},
        )

    def test_preserves_complete_responses_and_original_numeric_strings(self) -> None:
        raw = response()
        adjusted = mutate(raw, lambda payload: payload["data"]["rows"][0].__setitem__(1, "4.123"))
        self.write_configs(("510300",))
        manifest_path = self.stage(RecordedClient([raw, adjusted]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        item = manifest["items"][0]
        for label, original in (("raw", raw), ("adjusted", adjusted)):
            archive = json.loads((manifest_path.parent / item["files"][label]).read_text(encoding="utf-8"))
            self.assertEqual(archive["response"], original)
            self.assertEqual(archive["observed_at"], OBSERVED.isoformat())
            self.assertEqual(archive["params"]["aftype"], "2" if label == "raw" else "0")
            actual_data = json.loads(archive["response"]["content"][0]["text"])["data"]
            self.assertEqual(actual_data["rows"][0][5:7], ["112233445.6789", "123456.7890"])
            self.assertEqual(actual_data["unit"], {"TURNOVER 单位：": "元"})

    def test_seed_count_serial_pairs_and_suffixes_are_explicit(self) -> None:
        client = RecordedClient()
        manifest_path = self.stage(client)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(client.calls), 12)
        for index, symbol in enumerate(SYMBOLS):
            windcode = symbol + (".SZ" if symbol == "159915" else ".SH")
            for offset, aftype in enumerate(("2", "0")):
                self.assertEqual(client.calls[index * 2 + offset], {
                    "windcode": windcode, "begin_date": "2023-01-01",
                    "end_date": "2026-09-02", "period": "1d", "count": -3,
                    "aftype": aftype, "issusp": "0", "afdate": "2026-09-02",
                })
        self.assertEqual(manifest["requested_usable_rows"], 2)
        for item in manifest["items"]:
            self.assertEqual(item["rows"], 3)
            self.assertEqual(item["usable_rows"], 2)
            self.assertEqual(item["first_date"], "2026-08-31")
            self.assertEqual(item["last_date"], "2026-09-02")
            self.assertEqual(item["source"], "WIND_FUND_KLINE")

    def test_unknown_volume_units_block_promotion_without_touching_canonical(self) -> None:
        canonical = self.output.parent / "daily_quotes.jsonl"
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(b"existing canonical data\n")
        manifest_path = self.stage()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["staging_only"])
        self.assertFalse(manifest["production_import_allowed"])
        self.assertEqual(manifest["status"], "BLOCKED_VOLUME_UNIT")
        self.assertIn("BLOCKED_VOLUME_UNIT", manifest["blocked_reasons"])
        for item in manifest["items"]:
            self.assertIn("BLOCKED_VOLUME_UNIT", item["blocked_reasons"])
            self.assertIsNone(item["units"]["raw"]["volume"])
            self.assertEqual(item["units"]["raw"]["metadata"], {"TURNOVER 单位：": "元"})
        self.assertEqual(canonical.read_bytes(), b"existing canonical data\n")
        self.assertEqual(list(self.output.rglob("daily_quotes.jsonl")), [])

    def test_declared_units_are_preserved_without_claiming_production_ready(self) -> None:
        supplied = mutate(response(), lambda payload: payload["data"]["columns"][6].update(unit="手"))
        self.write_configs(("510300",))
        manifest_path = self.stage(RecordedClient([supplied, supplied]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"][0]["units"]["raw"]["volume"], "手")
        self.assertTrue(manifest["staging_only"])
        self.assertFalse(manifest["production_import_allowed"])
        self.assertIn("BLOCKED_PRODUCTION_ADAPTER", manifest["blocked_reasons"])

    def test_short_real_history_keeps_one_seed_and_does_not_fabricate_rows(self) -> None:
        supplied = response(dates=("2026-09-02",))
        self.write_configs(("563360",))
        manifest_path = self.stage(RecordedClient([supplied, supplied]), count=756)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"][0]["rows"], 1)
        self.assertEqual(manifest["items"][0]["usable_rows"], 0)

    def test_batches_are_unique_and_only_manifest_is_completion_marker(self) -> None:
        first = self.stage()
        second = self.stage()
        self.assertNotEqual(first.parent, second.parent)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())
        self.assertEqual(list(self.output.rglob("*.tmp")), [])

    def test_invalid_response_prevents_manifest_and_all_later_requests(self) -> None:
        changes = {
            "empty": lambda p: p["data"].update(rows=[]),
            "duplicate_columns": lambda p: p["data"]["columns"][1].update(name="TIME"),
            "missing_column": lambda p: p["data"]["columns"][1].update(name="OTHER"),
            "short_row": lambda p: p["data"]["rows"][0].pop(),
            "duplicate_date": lambda p: p["data"]["rows"][1].__setitem__(0, p["data"]["rows"][0][0]),
            "reverse_date": lambda p: p["data"]["rows"].reverse(),
            "naive_date": lambda p: p["data"]["rows"][0].__setitem__(0, "2026-08-31T00:00:00"),
            "malformed_date": lambda p: p["data"]["rows"][0].__setitem__(0, "not-a-date"),
            "future_date": lambda p: p["data"]["rows"][-1].__setitem__(0, "2026-09-03T00:00:00+08:00"),
            "before_begin_date": lambda p: p["data"]["rows"][0].__setitem__(0, "2022-12-30T00:00:00+08:00"),
            "bad_ohlc": lambda p: p["data"]["rows"][0].__setitem__(3, "3.9"),
            "zero_price": lambda p: p["data"]["rows"][0].__setitem__(1, "0"),
            "negative_amount": lambda p: p["data"]["rows"][0].__setitem__(5, "-1"),
            "negative_volume": lambda p: p["data"]["rows"][0].__setitem__(6, "-1"),
            "nan": lambda p: p["data"]["rows"][0].__setitem__(6, "NaN"),
            "infinity": lambda p: p["data"]["rows"][0].__setitem__(7, "Infinity"),
            "bool": lambda p: p["data"]["rows"][0].__setitem__(1, True),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                client = RecordedClient([mutate(response(), change)])
                with self.assertRaises(WindHistoryError):
                    self.stage(client)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(list(self.output.rglob("manifest.json")), [])

    def test_raw_adjusted_dates_must_match_exactly(self) -> None:
        client = RecordedClient([response(), response(dates=("2026-08-31", "2026-09-02"))])
        with self.assertRaisesRegex(WindHistoryError, "DATE_MISMATCH"):
            self.stage(client)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(list(self.output.rglob("manifest.json")), [])

    def test_current_day_requires_completed_close_in_shanghai_time(self) -> None:
        for now in (
            datetime(2026, 9, 2, 15, 9, 59, tzinfo=SHANGHAI),
            datetime(2026, 9, 2, 7, 9, 59, tzinfo=timezone.utc),
        ):
            with self.subTest(now=now), self.assertRaisesRegex(WindHistoryError, "INCOMPLETE"):
                self.stage(now=now)
        self.assertTrue(self.stage(now=datetime(2026, 9, 2, 7, 10, tzinfo=timezone.utc)).exists())

    def test_naive_observation_and_invalid_request_are_rejected_before_fetch(self) -> None:
        cases = (
            {"now": datetime(2026, 9, 2, 16)}, {"count": 0}, {"count": True},
            {"end_date": "20260902"}, {"end_date": "2022-12-31"},
            {"end_date": "2026-09-03"},
        )
        for values in cases:
            client = RecordedClient()
            with self.subTest(values=values), self.assertRaises(WindHistoryError):
                self.stage(client, **values)
            self.assertEqual(client.calls, [])

    def test_utc_row_timestamp_uses_shanghai_date(self) -> None:
        supplied = mutate(response(), lambda p: p["data"]["rows"][0].__setitem__(0, "2026-08-30T16:00:00Z"))
        self.write_configs(("510300",))
        manifest_path = self.stage(RecordedClient([supplied, supplied]))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["items"][0]["first_date"], "2026-08-31")

    def test_exchange_mismatch_is_rejected_before_fetch(self) -> None:
        metadata = metadata_fixture()
        metadata["items"][0]["trading"]["exchange"] = "SZSE"
        self.metadata.write_text(json.dumps(metadata), encoding="utf-8")
        client = RecordedClient()
        with self.assertRaisesRegex(WindHistoryError, "EXCHANGE"):
            self.stage(client)
        self.assertEqual(client.calls, [])

    def test_end_date_and_returned_dates_must_be_calendar_trading_days(self) -> None:
        for end_date in ("2026-08-30", "2026-05-01"):
            client = RecordedClient()
            with self.subTest(end_date=end_date), self.assertRaisesRegex(WindHistoryError, "TRADING_DAY"):
                self.stage(client, end_date=end_date)
            self.assertEqual(client.calls, [])
        for day in ("2026-08-30", "2026-05-01"):
            client = RecordedClient([response(dates=(day,))])
            with self.subTest(day=day), self.assertRaisesRegex(WindHistoryError, "TRADING_DAY"):
                self.stage(client)
            self.assertEqual(len(client.calls), 1)

    def test_calendar_coverage_ends_in_2026_before_any_request(self) -> None:
        client = RecordedClient()
        with self.assertRaisesRegex(WindHistoryError, "CALENDAR_COVERAGE"):
            self.stage(client, end_date="2027-01-04", now=datetime(2027, 1, 4, 16, tzinfo=SHANGHAI))
        self.assertEqual(client.calls, [])

    def test_missing_target_day_and_internal_trading_day_prevent_manifest(self) -> None:
        self.write_configs(("510300",))
        for dates in (("2026-08-31", "2026-09-01"), ("2026-08-31", "2026-09-02")):
            supplied = response(dates=dates)
            client = RecordedClient([supplied, supplied])
            with self.subTest(dates=dates), self.assertRaisesRegex(WindHistoryError, "HISTORY_INCOMPLETE|HISTORY_GAP"):
                self.stage(client)
            self.assertEqual(len(client.calls), 2)
            self.assertEqual(list(self.output.rglob("manifest.json")), [])

    def test_provider_errors_stop_batch_without_surfacing_private_message(self) -> None:
        cases = (
            {"ok": False, "code": "AUTH_ERROR", "message": "sensitive-provider-text"},
            {"isError": True, "content": [{"type": "text", "text": "sensitive-provider-text"}]},
            mutate(response(), lambda p: p.update(error={"code": "E42", "message": "sensitive-provider-text"})),
        )
        for envelope in cases:
            client = RecordedClient([envelope])
            with self.subTest(envelope=envelope), self.assertRaises(WindHistoryError) as raised:
                self.stage(client)
            self.assertNotIn("sensitive-provider-text", str(raised.exception))
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(list(self.output.rglob("manifest.json")), [])

    def test_manifest_not_published_if_atomic_replace_fails(self) -> None:
        with patch("etf_rotation.wind_history.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(WindHistoryError):
                self.stage()
        self.assertEqual(list(self.output.rglob("manifest.json")), [])
        self.assertEqual(list(self.output.rglob("*.tmp")), [])


class WindHistoryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.skill_dir = Path(self.temp.name)
        (self.skill_dir / "scripts").mkdir()
        (self.skill_dir / "scripts" / "cli.mjs").write_text("// test stub", encoding="utf-8")
        self.params = {"windcode": "510300.SH", "end_date": "2026-09-02"}

    def test_utf8_unique_parameter_files_cleanup_and_bounded_capture(self) -> None:
        seen = []
        expected = response()

        def run(command, **kwargs):
            self.assertEqual(command[:5], ["node", "scripts/cli.mjs", "call", "fund_data", "get_fund_kline"])
            self.assertTrue(command[5].startswith("@scripts/request-"))
            request_file = self.skill_dir / command[5][1:]
            seen.append(request_file)
            self.assertEqual(json.loads(request_file.read_text(encoding="utf-8")), self.params)
            self.assertFalse(request_file.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertEqual(kwargs["cwd"], self.skill_dir.resolve())
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertGreater(kwargs["timeout"], 0)
            self.assertLessEqual(kwargs["timeout"], 300)
            return subprocess.CompletedProcess(command, 0, json.dumps(expected), "private stderr")

        with patch("etf_rotation.wind_history.subprocess.run", side_effect=run):
            client = WindHistoryClient(self.skill_dir)
            self.assertEqual(client.fetch(self.params), expected)
            self.assertEqual(client.fetch(self.params), expected)
        self.assertNotEqual(seen[0], seen[1])
        self.assertTrue(all(not path.exists() for path in seen))

    def test_cleanup_on_timeout_invalid_json_nonzero_and_provider_error(self) -> None:
        cases = (
            subprocess.TimeoutExpired("private command", 1, output="private output"),
            subprocess.CompletedProcess([], 0, "private invalid json", "private stderr"),
            subprocess.CompletedProcess([], 1, "", "private stderr"),
            subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "code": "AUTH_ERROR", "message": "private message"}), ""),
        )
        for result in cases:
            kwargs = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=type(result).__name__), patch("etf_rotation.wind_history.subprocess.run", **kwargs):
                with self.assertRaises(WindHistoryError) as raised:
                    WindHistoryClient(self.skill_dir).fetch(self.params)
                self.assertNotIn("private", str(raised.exception))
                self.assertEqual(list((self.skill_dir / "scripts").glob("request-*.json")), [])

    def test_cli_reports_staging_status_and_manifest_without_data_dump(self) -> None:
        manifest_path = self.skill_dir / "manifest.json"
        manifest_path.write_text(json.dumps({
            "status": "BLOCKED_VOLUME_UNIT", "items": [{"rows": 3, "usable_rows": 2}],
            "staging_only": True,
        }), encoding="utf-8")
        output = io.StringIO()
        with patch("etf_rotation.wind_history.stage_history", return_value=manifest_path), patch("sys.stdout", output):
            status = main(["--end-date", "2026-09-02", "--skill-dir", str(self.skill_dir)])
        self.assertEqual(status, 0)
        self.assertIn("BLOCKED_VOLUME_UNIT", output.getvalue())
        self.assertIn(str(manifest_path), output.getvalue())
        self.assertNotIn("112233445", output.getvalue())

    def test_cli_failure_returns_nonzero_and_only_safe_error_code(self) -> None:
        output = io.StringIO()
        with patch("etf_rotation.wind_history.stage_history", side_effect=OSError("private-message")), patch("sys.stderr", output):
            status = main(["--end-date", "2026-09-02"])
        self.assertNotEqual(status, 0)
        self.assertNotIn("private-message", output.getvalue())


if __name__ == "__main__":
    unittest.main()
