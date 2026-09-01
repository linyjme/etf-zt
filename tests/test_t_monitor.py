from dataclasses import replace
from datetime import datetime, timedelta
import inspect
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from etf_rotation.etf_metadata import EtfMetadataStore, MetadataError
from etf_rotation.quote_collector import (
    SOURCE_NAME, TRENDS2_ENDPOINT, TRENDS2_FALLBACK_ENDPOINT,
    Trends2QuoteCollector, market_for_symbol,
)
from etf_rotation.t_monitor import (
    AlertHistoryStore, JsonQuoteAdapter, MarketDataError, QuoteHistoryStore,
    MonitorSignal, TMonitorEngine, WatchItem, load_watchlist, snapshot_to_dict,
)
from etf_rotation.t_web import MonitorApplication, PAGE, create_server
from tests.regime_fixtures import confirmed_range_quote


NOW = "2026-08-28T10:00:00+08:00"


def run_page_helpers(body: str) -> object:
    start_marker = "/* PAGE_HELPERS_START */"
    end_marker = "/* PAGE_HELPERS_END */"
    if start_marker not in PAGE or end_marker not in PAGE:
        raise AssertionError("PAGE does not expose its pure JavaScript helpers")
    helpers = PAGE.split(start_marker, 1)[1].split(end_marker, 1)[0]
    source = (
        "const STALE_AFTER_MS=60000,DEFAULT_GRID_WIDTH_PCT=0.002;\n"
        + helpers + "\n" + body
    )
    completed = subprocess.run(
        ["node", "-"], input=source, text=True, encoding="utf-8",
        capture_output=True, check=False,
    )
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def quote(
    price: float = 10.3,
    average_price: float = 10.0,
    previous_close: float = 10.0,
    prior_price: float | None = None,
    prior_time: str = "2026-08-28T09:30:00+08:00",
) -> dict[str, object]:
    if prior_price is None:
        prior_price = price
    return {
        "schema_version": 2,
        "symbol": "510300",
        "name": "沪深300ETF",
        "price": price,
        "average_price": average_price,
        "previous_close": previous_close,
        "timestamp": NOW,
        "observed_at": "2026-08-28T10:00:05+08:00",
        "source": "TEST_FIXTURE",
        "points": [
            [prior_time, prior_price, average_price],
            {"time": NOW, "price": price, "average_price": average_price},
        ],
    }


class JsonQuoteAdapterTests(unittest.TestCase):
    def test_accepts_list_wrapper_and_symbol_mapping_with_white_and_yellow_lines(self) -> None:
        adapter = JsonQuoteAdapter()
        shapes = (
            [quote()],
            {"quotes": [quote()]},
            {"510300": {key: value for key, value in quote().items() if key != "symbol"}},
        )
        for raw in shapes:
            with self.subTest(raw=raw):
                result = adapter.parse(raw)
                self.assertEqual(result["510300"].points[-1].price, 10.3)
                self.assertEqual(result["510300"].points[-1].average_price, 10.0)

    def test_rejects_naive_unsorted_or_incomplete_market_data(self) -> None:
        raw = quote()
        raw["timestamp"] = "2026-08-28T10:00:00"
        with self.assertRaisesRegex(MarketDataError, "必须带时区"):
            JsonQuoteAdapter().parse([raw])
        raw = quote()
        raw["points"] = list(reversed(raw["points"]))
        with self.assertRaisesRegex(MarketDataError, "严格递增"):
            JsonQuoteAdapter().parse([raw])
        raw = quote()
        del raw["average_price"]
        with self.assertRaisesRegex(MarketDataError, "average_price"):
            JsonQuoteAdapter().parse([raw])

    def test_rejects_timeline_latest_value_mismatch(self) -> None:
        raw = quote()
        raw["price"] = 10.4
        with self.assertRaisesRegex(MarketDataError, "最新值必须匹配"):
            JsonQuoteAdapter().parse([raw])
        raw = quote()
        raw["timestamp"] = "2026-08-28T10:01:00+08:00"
        with self.assertRaisesRegex(MarketDataError, "最新值必须匹配"):
            JsonQuoteAdapter().parse([raw])


class Trends2QuoteCollectorTests(unittest.TestCase):
    @staticmethod
    def response(symbol: str, market: int, minute: str = "2026-08-28 10:00") -> bytes:
        payload = {
            "rc": 0,
            "data": {
                "code": symbol,
                "market": market,
                "name": symbol,
                "preClose": 10.0,
                "trends": [
                    f"2026-08-28 09:30,10.0,10.1,10.2,9.9,100,1000,10.05",
                    f"{minute},10.1,10.3,10.4,10.0,200,2000,10.2",
                ],
            },
        }
        return json.dumps(payload).encode("utf-8")

    def test_market_mapping_uses_zero_for_shenzhen_and_one_for_shanghai(self) -> None:
        self.assertEqual(market_for_symbol("159915"), 0)
        self.assertEqual(market_for_symbol("510300"), 1)
        with self.assertRaisesRegex(MarketDataError, "无法映射"):
            market_for_symbol("400001")

    def test_collects_all_enabled_quotes_and_parses_trends2_fields(self) -> None:
        requests = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append((request.full_url, timeout))
            secid = parse_qs(urlsplit(request.full_url).query)["secid"][0]
            market, symbol = secid.split(".")
            return self.response(symbol, int(market))

        watchlist = (
            WatchItem("510300", "沪深300ETF", 0.02),
            WatchItem("159915", "创业板ETF", 0.02),
        )
        collector = Trends2QuoteCollector(
            transport=transport,
            now=lambda: datetime.fromisoformat(NOW),
        )
        payload = collector.collect(watchlist)
        self.assertEqual(payload["source"]["name"], payload["quotes"][0]["source"])
        self.assertTrue(payload["source"]["name"].startswith(SOURCE_NAME))
        self.assertIn("push2his.eastmoney.com", payload["source"]["name"])
        self.assertEqual([item["symbol"] for item in payload["quotes"]], ["510300", "159915"])
        self.assertEqual(payload["quotes"][0]["price"], 10.3)
        self.assertEqual(payload["quotes"][0]["average_price"], 10.2)
        self.assertEqual(payload["quotes"][0]["previous_close"], 10.0)
        self.assertEqual(payload["quotes"][0]["timestamp"], NOW)
        self.assertEqual(len(requests), 2)
        self.assertIn("secid=1.510300", requests[0][0])
        self.assertIn("secid=0.159915", requests[1][0])
        self.assertEqual(payload["source"]["endpoint"], TRENDS2_ENDPOINT)
        self.assertTrue(all(url.startswith(TRENDS2_ENDPOINT) for url in payload["source"]["urls"]))

    def test_retries_entire_batch_on_fallback_after_primary_transport_failure(self) -> None:
        requests = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request.full_url)
            secid = parse_qs(urlsplit(request.full_url).query)["secid"][0]
            market, symbol = secid.split(".")
            if request.full_url.startswith(TRENDS2_ENDPOINT) and symbol == "159915":
                raise OSError("primary connection ended prematurely")
            return self.response(symbol, int(market))

        watchlist = (
            WatchItem("510300", "沪深300ETF", 0.002),
            WatchItem("159915", "创业板ETF", 0.002),
        )
        payload = Trends2QuoteCollector(
            transport=transport,
            now=lambda: datetime.fromisoformat(NOW),
        ).collect(watchlist)

        self.assertEqual(payload["source"]["endpoint"], TRENDS2_FALLBACK_ENDPOINT)
        self.assertTrue(all(url.startswith(TRENDS2_FALLBACK_ENDPOINT) for url in payload["source"]["urls"]))
        self.assertEqual(
            [(urlsplit(url).netloc, parse_qs(urlsplit(url).query)["secid"][0]) for url in requests],
            [
                (urlsplit(TRENDS2_ENDPOINT).netloc, "1.510300"),
                (urlsplit(TRENDS2_ENDPOINT).netloc, "0.159915"),
                (urlsplit(TRENDS2_FALLBACK_ENDPOINT).netloc, "1.510300"),
                (urlsplit(TRENDS2_FALLBACK_ENDPOINT).netloc, "0.159915"),
            ],
        )

    def test_retries_fallback_when_primary_response_cannot_be_decoded(self) -> None:
        requests = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request.full_url)
            if request.full_url.startswith(TRENDS2_ENDPOINT):
                return b"not-json"
            return self.response("510300", 1)

        payload = Trends2QuoteCollector(
            transport=transport,
            now=lambda: datetime.fromisoformat(NOW),
        ).collect((WatchItem("510300", "沪深300ETF", 0.002),))

        self.assertEqual(payload["source"]["endpoint"], TRENDS2_FALLBACK_ENDPOINT)
        self.assertEqual(len(requests), 2)

    def test_does_not_fallback_for_business_response_failure(self) -> None:
        wrong_code = json.loads(self.response("159915", 0))
        wrong_market = json.loads(self.response("510300", 0))
        responses = (
            ({"rc": 1, "data": None}, "返回失败"),
            ({"rc": 0, "data": None}, "缺少 data"),
            (wrong_code, "代码不匹配"),
            (wrong_market, "市场不匹配"),
        )
        for response, message in responses:
            with self.subTest(message=message):
                requests = []

                def transport(request: Request, timeout: float) -> bytes:
                    requests.append(request.full_url)
                    return json.dumps(response).encode("utf-8")

                collector = Trends2QuoteCollector(transport=transport)
                with self.assertRaisesRegex(MarketDataError, message):
                    collector.collect((WatchItem("510300", "沪深300ETF", 0.002),))

                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0].startswith(TRENDS2_ENDPOINT))

    def test_reports_both_endpoints_when_primary_and_fallback_requests_fail(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            raise OSError(f"connection failed for {urlsplit(request.full_url).netloc}")

        collector = Trends2QuoteCollector(transport=transport)
        with self.assertRaisesRegex(
            MarketDataError,
            r"510300.*push2his\.eastmoney\.com.*510300.*push2delay\.eastmoney\.com",
        ):
            collector.collect((WatchItem("510300", "沪深300ETF", 0.002),))

    def test_preserves_primary_request_failure_when_fallback_has_business_error(self) -> None:
        requests = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request.full_url)
            if request.full_url.startswith(TRENDS2_ENDPOINT):
                raise OSError("primary transport down")
            return json.dumps({"rc": 1, "data": None}).encode("utf-8")

        collector = Trends2QuoteCollector(transport=transport)
        with self.assertRaises(MarketDataError) as caught:
            collector.collect((WatchItem("510300", "沪深300ETF", 0.002),))

        message = str(caught.exception)
        self.assertIn(TRENDS2_ENDPOINT, message)
        self.assertIn("primary transport down", message)
        self.assertIn(TRENDS2_FALLBACK_ENDPOINT, message)
        self.assertIn("返回失败", message)
        self.assertEqual(len(requests), 2)

    def test_persists_actual_primary_or_fallback_host_in_minute_history(self) -> None:
        cases = (
            (False, "push2his.eastmoney.com"),
            (True, "push2delay.eastmoney.com"),
        )
        for fail_primary, expected_host in cases:
            with self.subTest(expected_host=expected_host), tempfile.TemporaryDirectory() as temporary:
                def transport(request: Request, timeout: float) -> bytes:
                    if fail_primary and request.full_url.startswith(TRENDS2_ENDPOINT):
                        raise OSError("primary unavailable")
                    secid = parse_qs(urlsplit(request.full_url).query)["secid"][0]
                    market, symbol = secid.split(".")
                    return self.response(symbol, int(market))

                payload = Trends2QuoteCollector(
                    transport=transport,
                    now=lambda: datetime.fromisoformat(NOW),
                ).collect((WatchItem("510300", "沪深300ETF", 0.002),))
                quotes = JsonQuoteAdapter().parse(payload)
                history_path = Path(temporary) / "quotes.jsonl"
                QuoteHistoryStore(history_path).append(quotes)
                history_record = json.loads(
                    history_path.read_text(encoding="utf-8").splitlines()[0]
                )

                self.assertIn(expected_host, payload["source"]["name"])
                self.assertEqual(payload["source"]["name"], quotes["510300"].source)
                self.assertEqual(history_record["source"], quotes["510300"].source)
                self.assertIn(expected_host, history_record["source"])

    def test_collects_exactly_the_six_enabled_watchlist_etfs(self) -> None:
        watchlist_path = Path(__file__).resolve().parents[1] / "data" / "monitor" / "watchlist.json"
        watchlist = load_watchlist(watchlist_path)
        requested = []

        def transport(request: Request, timeout: float) -> bytes:
            secid = parse_qs(urlsplit(request.full_url).query)["secid"][0]
            market, symbol = secid.split(".")
            requested.append(symbol)
            return self.response(symbol, int(market))

        payload = Trends2QuoteCollector(
            transport=transport,
            now=lambda: datetime.fromisoformat(NOW),
        ).collect(watchlist)
        self.assertEqual(requested, [
            "510300", "510500", "563360", "512100", "159915", "588000",
        ])
        self.assertEqual(len(payload["quotes"]), 6)

    def test_rejects_incomplete_response_and_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.json"
            original = b'{"quotes":[]}\n'
            path.write_bytes(original)
            collector = Trends2QuoteCollector(
                transport=lambda request, timeout: json.dumps({
                    "rc": 0,
                    "data": {
                        "code": "510300", "market": 1, "name": "ETF",
                        "preClose": 10.0, "trends": [],
                    },
                }).encode("utf-8"),
            )
            with self.assertRaisesRegex(MarketDataError, "缺少分钟点"):
                collector.collect_to_file((WatchItem("510300", "ETF", 0.02),), path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])


class QuoteHistoryStoreTests(unittest.TestCase):
    def test_append_compatibility_persists_only_finalized_schema_v3_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            raw = quote()
            raw["points"] = [
                {
                    "timestamp": "2026-08-28T09:30:00+08:00",
                    "price": 10.3,
                    "average_price": 10.0,
                    "open": 10.3,
                    "high": 10.3,
                    "low": 10.3,
                    "volume": 100.0,
                    "amount": 103000.0,
                },
                {
                    "timestamp": NOW,
                    "price": 10.3,
                    "average_price": 10.0,
                    "open": 10.3,
                    "high": 10.3,
                    "low": 10.3,
                    "volume": 100.0,
                    "amount": 103000.0,
                },
            ]
            quotes = JsonQuoteAdapter().parse([raw])
            QuoteHistoryStore(path).append(quotes)
            QuoteHistoryStore(path).append(quotes)
            restored = QuoteHistoryStore(path).merge(quotes)
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["schema_version"], 3)
            self.assertEqual(records[0]["timestamp"], "2026-08-28T09:30:00+08:00")
            self.assertEqual(records[0]["observed_at"], "2026-08-28T10:00:05+08:00")
            self.assertTrue(records[0]["is_complete"])
            self.assertEqual(records[0]["source"], "TEST_FIXTURE")
            self.assertEqual(len(restored["510300"].points), 2)


class WatchlistTests(unittest.TestCase):
    def test_loads_configurable_grid_width_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "watchlist.json"
            path.write_text(json.dumps([
                {"symbol": "510300", "grid_width_pct": 0.015},
                {"symbol": "159915"},
            ]), encoding="utf-8")
            items = load_watchlist(path)
        self.assertEqual(items[0].grid_width_pct, 0.015)
        self.assertEqual(items[1].grid_width_pct, 0.002)

    def test_rejects_invalid_grid_width(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "watchlist.json"
            for value in (True, 0, 1, float("inf")):
                with self.subTest(value=value):
                    path.write_text(json.dumps([{
                        "symbol": "510300", "grid_width_pct": value,
                    }]), encoding="utf-8")
                    with self.assertRaisesRegex(MarketDataError, "grid_width_pct"):
                        load_watchlist(path)


class AlertHistoryStoreTests(unittest.TestCase):
    def test_records_new_candidates_and_ignores_legacy_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = AlertHistoryStore(Path(temporary) / "alerts.jsonl")
            store.append({
                "generated_at": NOW,
                "items": [
                    {
                        "symbol": "510300", "timestamp": NOW,
                        "action": "BUY_CANDIDATE", "strategy_version": "T_V3",
                    },
                    {
                        "symbol": "510500", "timestamp": NOW,
                        "action": "SELL_REMINDER", "strategy_version": "T_V2",
                    },
                    {
                        "symbol": "510100", "timestamp": NOW,
                        "action": "DEVIATION_OBSERVE", "strategy_version": "T_V3",
                    },
                ],
            })
            self.assertEqual(
                [item["action"] for item in store.query()],
                ["BUY_CANDIDATE"],
            )

    def test_preexisting_legacy_records_are_filtered_without_rewriting_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "alerts.jsonl"
            store = AlertHistoryStore(path)

            def event(symbol: str, action: str, version: str) -> dict[str, str]:
                return {
                    "symbol": symbol,
                    "timestamp": NOW,
                    "trading_date": "2026-08-28",
                    "action": action,
                    "strategy_version": version,
                }

            records = [
                event("legacy-buy", "BUY_REMINDER", "T_V2"),
                event("legacy-observe", "OBSERVE", "T_V2"),
                event("old-candidate", "BUY_CANDIDATE", "T_V2"),
                event("deviation", "DEVIATION_OBSERVE", "T_V3"),
                event("current-buy", "BUY_CANDIDATE", "T_V3"),
                event("current-sell", "SELL_CANDIDATE", "T_V3"),
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in records) + "\n",
                encoding="utf-8",
            )
            daily = store.daily_root / "2026-08-28" / "alerts.jsonl"
            daily.parent.mkdir(parents=True)
            daily.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            canonical_before = path.read_bytes()

            self.assertEqual(
                [item["symbol"] for item in store.query()],
                ["current-sell", "current-buy"],
            )
            self.assertEqual(
                [item["symbol"] for item in store._read_path(daily)],
                ["current-buy", "current-sell"],
            )
            self.assertEqual(path.read_bytes(), canonical_before)


class MonitorSignalCompatibilityTests(unittest.TestCase):
    def test_legacy_positional_optional_fields_keep_their_original_slots(self) -> None:
        timestamp = datetime.fromisoformat(NOW)
        signal = MonitorSignal(
            "510300", "ETF", "OK", "WAIT", "等待",
            10.0, 10.0, 10.0, 10.02, 9.98, 0.002,
            0.0, 0.0, 0.0, 0.0, timestamp,
            "LEGACY_SAFETY", "LEGACY_VERSION", 0.123,
        )
        self.assertEqual(signal.timestamp, timestamp)
        self.assertEqual(signal.safety, "LEGACY_SAFETY")
        self.assertEqual(signal.strategy_version, "LEGACY_VERSION")
        self.assertEqual(signal.deviation_pct, 0.123)
        self.assertEqual(signal.health_status, "UNKNOWN")


class TMonitorEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = JsonQuoteAdapter()
        self.watchlist = (WatchItem("510300", "沪深300ETF", 0.02),)

    def evaluate(self, raw: dict[str, object]) -> dict[str, object]:
        snapshot = TMonitorEngine().evaluate(self.watchlist, self.adapter.parse([raw]))
        return snapshot_to_dict(snapshot)["items"][0]

    def test_uses_average_grid_and_previous_close_zero_axis(self) -> None:
        cases = (
            (quote(11.0, 10.0, 10.0), "DEVIATION_OBSERVE"),
            (quote(9.0, 10.0, 10.0), "DEVIATION_OBSERVE"),
            (quote(10.21, 10.0, 10.1), "WAIT"),
            (quote(10.59, 10.0, 9.6), "WAIT"),
            (quote(10.6, 10.0, 9.6), "DEVIATION_OBSERVE"),
            (quote(9.4, 10.0, 10.4), "DEVIATION_OBSERVE"),
            (quote(10.1, 10.0, 10.0), "WAIT"),
        )
        for raw, expected in cases:
            with self.subTest(price=raw["price"], previous_close=raw["previous_close"]):
                raw["observed_at"] = "2026-08-28T10:01:00+08:00"
                self.assertEqual(self.evaluate(raw)["action"], expected)

    def test_grid_width_is_configurable(self) -> None:
        raw = quote(10.5, 10.0, 10.0)
        raw["observed_at"] = "2026-08-28T10:01:00+08:00"
        narrow = (WatchItem("510300", "沪深300ETF", 0.015),)
        wide = (WatchItem("510300", "沪深300ETF", 0.02),)
        quotes = self.adapter.parse([raw])
        self.assertEqual(TMonitorEngine().evaluate(narrow, quotes).signals[0].action, "DEVIATION_OBSERVE")
        self.assertEqual(TMonitorEngine().evaluate(wide, quotes).signals[0].action, "WAIT")

    def test_observes_when_deviation_is_large_but_previous_close_distance_blocks(self) -> None:
        raw = quote(10.6, 10.0, 10.0)
        raw["observed_at"] = "2026-08-28T10:01:00+08:00"
        item = self.evaluate(raw)
        self.assertEqual(item["action"], "DEVIATION_OBSERVE")
        self.assertEqual(item["white_yellow_deviation_grids"], 3.0)
        self.assertEqual(item["previous_close_distance_grids"], 3.0)
        self.assertIn("PREVIOUS_CLOSE_DISTANCE_BELOW_5_GRIDS", item["blocked_reasons"])

    def test_outputs_grid_distances_for_mean_reversion_and_fast_rise(self) -> None:
        raw = quote(11.0, 10.0, 10.0, prior_price=10.5)
        raw["observed_at"] = "2026-08-28T10:01:00+08:00"
        item = self.evaluate(raw)
        self.assertEqual(item["action"], "DEVIATION_OBSERVE")
        self.assertEqual(item["white_yellow_deviation_grids"], 5.0)
        self.assertEqual(item["previous_close_distance_grids"], 5.0)
        self.assertEqual(item["fast_rise_grids"], 0.0)

    def test_fast_rise_blocks_candidate_and_keeps_neutral_observation(self) -> None:
        raw = quote(
            11.0, 10.0, 10.0, prior_price=10.0,
            prior_time="2026-08-28T09:57:00+08:00",
        )
        raw["observed_at"] = "2026-08-28T10:01:00+08:00"
        item = self.evaluate(raw)
        self.assertEqual(item["action"], "DEVIATION_OBSERVE")
        self.assertEqual(item["label"], "偏离观察")
        self.assertEqual(item["fast_rise_grids"], 5.0)
        self.assertIn("FAST_RISE", item["blocked_reasons"])

    def test_confirmed_range_narrowing_finalized_points_produce_neutral_candidate(self) -> None:
        market_quote = confirmed_range_quote(-0.008, -0.007)
        watchlist = (WatchItem("510300", "沪深300ETF", 0.002),)
        snapshot = TMonitorEngine().evaluate(
            watchlist, {market_quote.symbol: market_quote}, market_quote.observed_at,
        )
        item = snapshot_to_dict(snapshot)["items"][0]
        self.assertEqual(item["action"], "BUY_CANDIDATE")
        self.assertEqual(item["label"], "做T候选")
        self.assertEqual(item["health_status"], "REALTIME")
        self.assertEqual(item["health_reason"], "行情实时")
        self.assertEqual(item["regime_state"], "RANGE")
        self.assertEqual(item["range_confirmation_count"], 3)
        self.assertGreater(item["expected_gross_edge_pct"], item["round_trip_cost_pct"])
        self.assertGreater(item["expected_net_edge_pct"], 0)
        self.assertEqual(item["blocked_reasons"], [])
        self.assertEqual(item["signal_level"], "NONE")

    def test_in_progress_point_is_excluded_from_candidate_and_regime_decision(self) -> None:
        market_quote = confirmed_range_quote(-0.008, -0.007)
        in_progress_at = market_quote.timestamp + timedelta(minutes=1)
        in_progress = replace(
            market_quote.points[-1], timestamp=in_progress_at, price=11.0,
            average_price=10.0,
        )
        live_quote = replace(
            market_quote,
            price=in_progress.price,
            average_price=in_progress.average_price,
            timestamp=in_progress.timestamp,
            points=market_quote.points + (in_progress,),
            observed_at=in_progress.timestamp + timedelta(seconds=5),
        )
        signal = TMonitorEngine().evaluate(
            (WatchItem("510300", "沪深300ETF", 0.002),),
            {live_quote.symbol: live_quote},
            live_quote.observed_at,
        ).signals[0]
        self.assertEqual(signal.action, "BUY_CANDIDATE")
        self.assertEqual(signal.price, market_quote.points[-1].price)
        self.assertEqual(signal.timestamp, market_quote.points[-1].timestamp)
        self.assertEqual(signal.regime_state, "RANGE")

    def test_generated_at_takes_priority_when_classifying_health(self) -> None:
        market_quote = confirmed_range_quote(-0.008, -0.007)
        generated_at = market_quote.timestamp + timedelta(minutes=5)
        item = snapshot_to_dict(TMonitorEngine().evaluate(
            (WatchItem("510300", "沪深300ETF", 0.002),),
            {market_quote.symbol: market_quote}, generated_at,
        ))["items"][0]
        self.assertEqual(item["action"], "DEVIATION_OBSERVE")
        self.assertEqual(item["health_status"], "OUTAGE")
        self.assertIn("MARKET_NOT_REALTIME", item["blocked_reasons"])

    def test_all_symbols_share_one_health_clock_for_fresh_and_stale_quotes(self) -> None:
        fresh = confirmed_range_quote(-0.008, -0.007)
        shift = timedelta(minutes=4)
        stale_points = tuple(
            replace(point, timestamp=point.timestamp - shift)
            for point in fresh.points
        )
        stale = replace(
            fresh,
            symbol="510500",
            name="中证500ETF",
            price=stale_points[-1].price,
            average_price=stale_points[-1].average_price,
            timestamp=stale_points[-1].timestamp,
            points=stale_points,
            observed_at=fresh.observed_at - shift,
        )
        current = fresh.observed_at

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return current if tz is None else current.astimezone(tz)

        with patch("etf_rotation.t_monitor.datetime", FixedDatetime):
            snapshot = TMonitorEngine().evaluate(
                (
                    WatchItem("510300", "沪深300ETF", 0.002),
                    WatchItem("510500", "中证500ETF", 0.002),
                ),
                {fresh.symbol: fresh, stale.symbol: stale},
            )
        signals = {item.symbol: item for item in snapshot.signals}
        self.assertEqual(snapshot.generated_at, current)
        self.assertEqual(signals["510300"].health_status, "REALTIME")
        self.assertEqual(signals["510300"].action, "BUY_CANDIDATE")
        self.assertEqual(signals["510500"].health_status, "OUTAGE")
        self.assertEqual(signals["510500"].action, "DEVIATION_OBSERVE")

    def test_lunch_close_and_weekend_never_produce_candidates(self) -> None:
        market_quote = confirmed_range_quote(-0.008, -0.007)
        watchlist = (WatchItem("510300", "沪深300ETF", 0.002),)
        cases = (
            ("2026-08-28T12:00:00+08:00", "LUNCH_BREAK"),
            ("2026-08-28T15:01:00+08:00", "CLOSED"),
            ("2026-08-29T10:02:00+08:00", "CLOSED"),
        )
        for value, expected_health in cases:
            with self.subTest(value=value):
                signal = TMonitorEngine().evaluate(
                    watchlist,
                    {market_quote.symbol: market_quote},
                    datetime.fromisoformat(value),
                ).signals[0]
                self.assertEqual(signal.health_status, expected_health)
                self.assertNotIn(
                    signal.action, {"BUY_CANDIDATE", "SELL_CANDIDATE"},
                )
                self.assertIn("MARKET_NOT_REALTIME", signal.blocked_reasons)

    def test_zero_grid_width_waits_without_dividing(self) -> None:
        market_quote = confirmed_range_quote(-0.008, -0.007)
        item = snapshot_to_dict(TMonitorEngine().evaluate(
            (WatchItem("510300", "沪深300ETF", 0.0),),
            {market_quote.symbol: market_quote},
        ))["items"][0]
        self.assertEqual(item["action"], "WAIT")
        self.assertIn("INVALID_GRID_WIDTH", item["blocked_reasons"])
        self.assertIsNone(item["white_yellow_deviation_grids"])

    def test_regime_fields_are_exposed_and_short_history_is_uncertain(self) -> None:
        item = self.evaluate(quote(10.1))
        self.assertEqual(item["regime_state"], "UNCERTAIN")
        self.assertEqual(item["regime_label"], "样本不足，暂停做T")
        self.assertIsNone(item["regime_score"])
        self.assertEqual(item["regime_sample_count"], 1)
        self.assertIsNone(item["path_efficiency"])
        self.assertIsNone(item["one_side_ratio"])
        self.assertIsNone(item["vwap_crossings"])
        self.assertIsNone(item["vwap_slope"])
        self.assertEqual(item["above_vwap_count"], 0)
        self.assertEqual(item["below_vwap_count"], 0)
        self.assertEqual(item["range_confirmation_count"], 0)
        self.assertEqual(item["trend_confirmation_count"], 0)
        self.assertEqual(item["regime_reasons"], ["INSUFFICIENT_SAMPLES"])
        self.assertEqual(item["action"], "WAIT")
        self.assertIn("INSUFFICIENT_FINALIZED_POINTS", item["blocked_reasons"])

    def test_confirmed_uptrend_is_observation_only(self) -> None:
        points = []
        for index in range(21):
            price = 10.0 + index * 0.08
            average = 10.0 + index * 0.03
            points.append({
                "timestamp": f"2026-08-28T09:{30 + index:02d}:00+08:00",
                "price": price,
                "average_price": average,
            })
        raw = quote(11.6, 10.6, 10.0)
        raw["timestamp"] = points[-1]["timestamp"]
        raw["points"] = points
        item = self.evaluate(raw)
        self.assertEqual(item["regime_state"], "UPTREND")
        self.assertEqual(item["trend_confirmation_count"], 2)
        self.assertEqual(item["regime_reasons"], ["UPTREND_CONFIRMED"])
        self.assertEqual(item["action"], "DEVIATION_OBSERVE")
        self.assertEqual(item["label"], "偏离观察")
        self.assertIn("REGIME_NOT_RANGE", item["blocked_reasons"])

    def test_payload_contains_trade_markers_only_for_confirmed_range(self) -> None:
        item = self.evaluate(quote(10.1))
        self.assertEqual(item["trade_markers"], [])
        self.assertIn("trade_markers", PAGE)
        self.assertIn("marker.type==='B'", PAGE)
        self.assertIn("marker.type==='B'?'#32d296':'#ff5964'", PAGE)

    def test_payload_contains_chart_inputs_and_remains_monitor_only(self) -> None:
        item = self.evaluate(quote(10.1))
        self.assertEqual(item["average_price"], 10.0)
        self.assertEqual(item["previous_close"], 10.0)
        self.assertEqual(item["upper_grid_price"], 10.2)
        self.assertEqual(item["lower_grid_price"], 9.8)
        self.assertEqual(item["grid_width_pct"], 0.02)
        self.assertEqual(item["white_yellow_deviation_grids"], 0.5)
        self.assertEqual(item["previous_close_distance_grids"], 0.5)
        self.assertEqual(item["fast_rise_grids"], 0.0)
        self.assertEqual(item["points"][-1]["average_price"], 10.0)
        self.assertEqual(item["safety"], "MONITOR_ONLY")

    def test_missing_quote_has_placeholder_item_and_disabled_item_is_skipped(self) -> None:
        watchlist = (
            WatchItem("missing", "缺行情", 0.02),
            WatchItem("disabled", "已禁用", 0.02, False),
        )
        payload = snapshot_to_dict(TMonitorEngine().evaluate(
            watchlist, {}, datetime.fromisoformat("2026-08-28T16:00:00+08:00"),
        ))
        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["symbol"], "missing")
        self.assertEqual(item["status"], "MISSING_QUOTE")
        self.assertEqual(item["action"], "UNAVAILABLE")
        self.assertEqual(item["label"], "缺少当日行情")
        self.assertEqual(item["health_status"], "CLOSED")
        for field in (
            "price", "average_price", "previous_close", "upper_grid_price",
            "lower_grid_price", "change_pct", "white_yellow_deviation_grids",
            "previous_close_distance_grids", "fast_rise_grids", "timestamp",
        ):
            self.assertIsNone(item[field])
        self.assertEqual(item["points"], [])


class MonitorRefreshTests(unittest.TestCase):
    def test_failed_refresh_keeps_old_quotes_and_exposes_real_error_and_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            quotes_path = root / "quotes.json"
            watchlist_path = root / "watchlist.json"
            quotes_path.write_text(json.dumps({
                "source": {"name": SOURCE_NAME, "endpoint": "verified"},
                "quotes": [quote()],
            }, ensure_ascii=False), encoding="utf-8")
            watchlist_path.write_text(json.dumps([{
                "symbol": "510300", "name": "沪深300ETF", "grid_width_pct": 0.02,
            }], ensure_ascii=False), encoding="utf-8")

            class FailingCollector:
                def collect_to_file(self, watchlist: object, path: Path) -> dict[str, object]:
                    raise MarketDataError("远端明确失败")

            original = quotes_path.read_bytes()
            application = MonitorApplication(
                quotes_path, watchlist_path, collector=FailingCollector(),
                clock=lambda: datetime.fromisoformat(
                    "2026-08-28T10:00:30+08:00",
                ),
            )
            self.assertFalse(application.refresh_once())
            payload = application.snapshot()
            self.assertEqual(quotes_path.read_bytes(), original)
            self.assertEqual(payload["items"][0]["price"], 10.3)
            self.assertEqual(payload["source"]["name"], SOURCE_NAME)
            self.assertEqual(payload["refresh_error"], "远端明确失败")
            self.assertEqual(payload["errors"], ["远端明确失败"])


class EtfMetadataTests(unittest.TestCase):
    @staticmethod
    def metadata_payload() -> dict[str, object]:
        return {
            "schema_version": 2,
            "items": [{
                "symbol": "510300",
                "name": "沪深300ETF",
                "index": {"code": "000300", "name": "沪深300", "provider": "中证指数"},
                "trading": {
                    "exchange": "SSE",
                    "asset_type": "DOMESTIC_EQUITY_ETF",
                    "intraday_turnaround": False,
                    "sellable_delay_days": 1,
                    "lot_size": 100,
                    "price_tick": 0.001,
                    "price_limit_pct": 0.10,
                    "volume_unit_shares": 100,
                },
            }],
        }

    def load_payload(self, payload: dict[str, object]) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "etf_metadata.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return EtfMetadataStore(path).load()

    def test_initial_mapping_contains_six_etfs(self) -> None:
        path = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
        metadata = EtfMetadataStore(path).load()
        self.assertEqual(metadata["510300"].index.code, "000300")
        self.assertEqual(metadata["159915"].index.code, "399006")
        self.assertEqual(len(metadata), 6)

    def test_initial_mapping_contains_exact_trading_attributes(self) -> None:
        path = Path(__file__).resolve().parents[1] / "data" / "monitor" / "etf_metadata.json"
        items = EtfMetadataStore(path).load()
        expected = {
            "510300": ("SSE", 0.10),
            "510500": ("SSE", 0.10),
            "563360": ("SSE", 0.10),
            "512100": ("SSE", 0.10),
            "159915": ("SZSE", 0.20),
            "588000": ("SSE", 0.20),
        }
        self.assertEqual(set(items), set(expected))
        for symbol, (exchange, price_limit_pct) in expected.items():
            trading = {
                "exchange": exchange,
                "asset_type": "DOMESTIC_EQUITY_ETF",
                "intraday_turnaround": False,
                "sellable_delay_days": 1,
                "lot_size": 100,
                "price_tick": 0.001,
                "price_limit_pct": price_limit_pct,
                "volume_unit_shares": 100,
            }
            self.assertEqual(items[symbol].trading.to_dict(), trading)
            self.assertEqual(items[symbol].to_dict()["trading"], trading)

    def test_requires_schema_version_two_and_trading_object(self) -> None:
        payload = self.metadata_payload()
        payload["schema_version"] = 1
        with self.assertRaisesRegex(MetadataError, "schema_version"):
            self.load_payload(payload)

        payload = self.metadata_payload()
        del payload["items"][0]["trading"]
        with self.assertRaisesRegex(MetadataError, "交易元数据"):
            self.load_payload(payload)

    def test_rejects_invalid_trading_attributes(self) -> None:
        cases = (
            ("exchange", "OTHER", "交易所"),
            ("asset_type", " ", "资产类型"),
            ("intraday_turnaround", 0, "日内回转"),
            ("sellable_delay_days", -1, "可卖延迟"),
            ("sellable_delay_days", False, "可卖延迟"),
            ("sellable_delay_days", 1.0, "可卖延迟"),
            ("lot_size", 0, "每手股数"),
            ("lot_size", True, "每手股数"),
            ("lot_size", 100.0, "每手股数"),
            ("price_tick", 0, "最小价位"),
            ("price_tick", True, "最小价位"),
            ("price_limit_pct", 0, "涨跌幅限制"),
            ("price_limit_pct", False, "涨跌幅限制"),
            ("volume_unit_shares", 0, "成交量单位"),
            ("volume_unit_shares", True, "成交量单位"),
        )
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                payload = self.metadata_payload()
                payload["items"][0]["trading"][field] = value
                with self.assertRaisesRegex(MetadataError, message):
                    self.load_payload(payload)

    def test_rejects_nonfinite_trading_numbers(self) -> None:
        for field, message in (("price_tick", "最小价位"), ("price_limit_pct", "涨跌幅限制")):
            for value in (float("nan"), float("inf")):
                with self.subTest(field=field, value=value):
                    payload = self.metadata_payload()
                    payload["items"][0]["trading"][field] = value
                    with self.assertRaisesRegex(MetadataError, message):
                        self.load_payload(payload)

    def test_rejects_unicode_digit_etf_symbol(self) -> None:
        payload = self.metadata_payload()
        payload["items"][0]["symbol"] = "５１０３００"
        with self.assertRaisesRegex(MetadataError, "ETF代码"):
            self.load_payload(payload)

    def test_rejects_unicode_digit_index_code(self) -> None:
        payload = self.metadata_payload()
        payload["items"][0]["index"]["code"] = "٠٠٠٣٠٠"
        with self.assertRaisesRegex(MetadataError, "指数代码"):
            self.load_payload(payload)


class MonitorWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.quotes = root / "quotes.json"
        self.watchlist = root / "watchlist.json"
        self.quotes.write_text(json.dumps([quote()], ensure_ascii=False), encoding="utf-8")
        self.watchlist.write_text(json.dumps([{
            "symbol": "510300",
            "name": "沪深300ETF",
            "grid_width_pct": 0.02,
        }], ensure_ascii=False), encoding="utf-8")
        self.metadata = root / "etf_metadata.json"
        self.metadata.write_text(json.dumps({"schema_version":2,"items":[{"symbol":"510300","name":"沪深300ETF","index":{"code":"000300","name":"沪深300","provider":"中证指数"},"trading":{"exchange":"SSE","asset_type":"DOMESTIC_EQUITY_ETF","intraday_turnaround":False,"sellable_delay_days":1,"lot_size":100,"price_tick":0.001,"price_limit_pct":0.10,"volume_unit_shares":100}}]}, ensure_ascii=False), encoding="utf-8")
        self.valuation = root / "valuation.json"
        self.valuation.write_text(json.dumps({"schema_version":1,"items":[{"index_code":"000300","index_name":"沪深300","status":"MISSING_VALUATION"}]}, ensure_ascii=False), encoding="utf-8")
        self.server = create_server(
            "127.0.0.1", 0, self.quotes, self.watchlist,
            metadata_path=self.metadata,
            valuation_path=self.valuation,
            clock=lambda: datetime.fromisoformat("2026-08-28T10:10:00+08:00"),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_valuation_endpoint_is_read_only_and_does_not_fabricate_values(self) -> None:
        with urlopen(self.base + "/api/etf/510300/valuation", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["index"]["code"], "000300")
        self.assertEqual(payload["status"], "MISSING_VALUATION")
        self.assertIsNone(payload["valuation"])

    def test_page_has_lazy_valuation_card(self) -> None:
        self.assertIn("关联指数与指数估值", PAGE)
        self.assertIn("/api/etf/${encodeURIComponent(symbol)}/valuation", PAGE)
        self.assertIn("不使用示例数字", PAGE)
        self.assertIn("估值等级", PAGE)

    def test_page_draws_white_price_yellow_average_and_zero_axis(self) -> None:
        self.assertIn("price-line", PAGE)
        self.assertIn("average-line", PAGE)
        self.assertIn("zero-line", PAGE)
        self.assertIn("白线实时价，黄线均价", PAGE)
        self.assertIn("item.previous_close", PAGE)

    def test_page_market_state_hard_gates_candidates_and_connection_status(self) -> None:
        cases = run_page_helpers(r"""
const timestamp='2026-08-28T10:00:00+08:00',now=Date.parse('2026-08-28T10:00:30+08:00');
const rows=[
  {health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',timestamp},
  {health_status:'DELAYED',status:'OK',action:'BUY_CANDIDATE',timestamp},
  {health_status:'OUTAGE',status:'OK',action:'SELL_CANDIDATE',timestamp},
  {health_status:'LUNCH_BREAK',status:'OK',action:'BUY_CANDIDATE',timestamp},
  {health_status:'CLOSED',status:'OK',action:'SELL_CANDIDATE',timestamp},
  {health_status:'OUTAGE',status:'MISSING_QUOTE',action:'BUY_CANDIDATE',timestamp},
  {health_status:'CLOSED',status:'MISSING_QUOTE',action:'BUY_CANDIDATE',timestamp:null},
];
console.log(JSON.stringify(rows.map(item=>marketPresentation(item,now))));
""")
        self.assertTrue(cases[0]["candidate"])
        self.assertEqual((cases[0]["statusText"], cases[0]["dotClass"]), ("实时监控中", "live"))
        expected = (
            ("行情延迟", "delayed"),
            ("行情断流", "outage"),
            ("午间休市", "paused"),
            ("已收盘", "closed"),
            ("行情缺失", "missing"),
            ("已收盘", "closed"),
        )
        for state, status in zip(cases[1:], expected):
            self.assertFalse(state["candidate"])
            self.assertEqual(state["displayLabel"], "偏离观察")
            self.assertEqual((state["statusText"], state["dotClass"]), status)

    def test_page_grid_bands_follow_each_points_vwap_and_keep_previous_close_flat(self) -> None:
        bands = run_page_helpers(r"""
const points=[{average_price:100},{average_price:102}];
console.log(JSON.stringify({
  upper3:points.map(point=>gridBandValue(point,0.002,3,1)),
  lower5:points.map(point=>gridBandValue(point,0.002,5,-1)),
}));
""")
        self.assertEqual(bands["upper3"], [100.6, 102.612])
        self.assertEqual(bands["lower5"], [99, 100.98])
        self.assertIn("pathValue(point=>gridBandValue(point,grid,3,1))", PAGE)
        self.assertIn("pathValue(point=>gridBandValue(point,grid,5,-1))", PAGE)
        self.assertIn("horizontal('zero-line',base,'昨收')", PAGE)
        self.assertNotIn("horizontal('three-grid'", PAGE)
        self.assertNotIn("horizontal('five-grid'", PAGE)

    def test_page_quote_updates_commit_atomically_after_sequence_and_revision_checks(self) -> None:
        state = run_page_helpers(r"""
let selectedSymbol='510300',quoteRequestSequence=1;
const quotePoints=new Map([['510300',new Map([['old',{timestamp:'old',price:1}]])]]),quoteRevisions=new Map([['510300',3]]);
const pending=[];
global.fetch=url=>new Promise(resolve=>pending.push({url,resolve}));
(async()=>{
  const older=loadQuoteUpdates('510300',3,1);
  quoteRequestSequence=2;
  const newer=loadQuoteUpdates('510300',3,2);
  pending[1].resolve({ok:true,json:async()=>({symbol:'510300',revision:5,reset:true,upserts:[{timestamp:'new',price:5}]})});
  await newer;
  pending[0].resolve({ok:true,json:async()=>({symbol:'510300',revision:4,reset:false,upserts:[{timestamp:'stale',price:4}]})});
  await older;
  quoteRequestSequence=3;
  const regressed=loadQuoteUpdates('510300',5,3);
  pending[2].resolve({ok:true,json:async()=>({symbol:'510300',revision:4,reset:false,upserts:[{timestamp:'regressed',price:4}]})});
  await regressed;
  quoteRequestSequence=4;
  const mismatched=loadQuoteUpdates('510300',5,4);
  pending[3].resolve({ok:true,json:async()=>({symbol:'159915',revision:6,reset:true,upserts:[{timestamp:'wrong',price:6}]})});
  let mismatchRejected=false;try{await mismatched}catch(error){mismatchRejected=true}
  console.log(JSON.stringify({
    revision:quoteRevisions.get('510300'),
    timestamps:[...quotePoints.get('510300').keys()],
    mismatchRejected,
  }));
})().catch(error=>{console.error(error);process.exitCode=1});
""")
        self.assertEqual(state["revision"], 5)
        self.assertEqual(state["timestamps"], ["new"])
        self.assertTrue(state["mismatchRejected"])

    def test_page_authoritative_quote_reset_accepts_lower_server_revision(self) -> None:
        state = run_page_helpers(r"""
let selectedSymbol='510300',quoteRequestSequence=1;
const quotePoints=new Map([['510300',new Map([['old',{timestamp:'old',price:100}]])]]),quoteRevisions=new Map([['510300',100]]);
global.fetch=async()=>({ok:true,json:async()=>({symbol:'510300',revision:1,reset:true,upserts:[{timestamp:'new',price:1}]})});
(async()=>{
  await loadQuoteUpdates('510300',100,1);
  console.log(JSON.stringify({revision:quoteRevisions.get('510300'),timestamps:[...quotePoints.get('510300').keys()]}));
})().catch(error=>{console.error(error);process.exitCode=1});
""")
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["timestamps"], ["new"])

    def test_page_feed_failure_revokes_all_candidate_decorations_until_recovery(self) -> None:
        states = run_page_helpers(r"""
const item={health_status:'REALTIME',status:'OK',action:'BUY_CANDIDATE',timestamp:'2026-08-28T10:00:00+08:00'};
const now=Date.parse('2026-08-28T10:00:30+08:00');
const decorate=state=>({navOpportunity:state.candidate,signalLabel:state.displayLabel,candidateAlert:state.candidate?'candidate-alert':''});
console.log(JSON.stringify([
  decorate(marketPresentation(item,now,{ready:true,message:''})),
  decorate(marketPresentation(item,now,{ready:false,message:'行情连接中断'})),
  decorate(marketPresentation(item,now,{ready:true,message:''})),
]));
""")
        self.assertEqual(states[0], {
            "navOpportunity": True, "signalLabel": "做T候选",
            "candidateAlert": "candidate-alert",
        })
        self.assertEqual(states[1], {
            "navOpportunity": False, "signalLabel": "偏离观察",
            "candidateAlert": "",
        })
        self.assertEqual(states[2], states[0])
        self.assertIn("marketPresentation(item,Date.now(),feedState)", PAGE)
        self.assertIn("marketPresentation(item,refreshedAt.getTime(),feedState)", PAGE)

    def test_page_feed_recovery_requires_summary_and_quotes_or_successful_poll(self) -> None:
        transitions = run_page_helpers(r"""
const initial={ready:true,summaryReady:true,message:''};
const failedState=nextFeedState(initial,'FAIL','连接失败');
const quotesOnly=nextFeedState(failedState,'QUOTES');
const summarized=nextFeedState(failedState,'SUMMARY');
const sseRecovered=nextFeedState(summarized,'QUOTES');
const pollRecovered=nextFeedState(failedState,'POLL');
console.log(JSON.stringify({failedState,quotesOnly,summarized,sseRecovered,pollRecovered}));
""")
        self.assertFalse(transitions["failedState"]["ready"])
        self.assertFalse(transitions["failedState"]["summaryReady"])
        self.assertFalse(transitions["quotesOnly"]["ready"])
        self.assertTrue(transitions["summarized"]["summaryReady"])
        self.assertFalse(transitions["summarized"]["ready"])
        self.assertTrue(transitions["sseRecovered"]["ready"])
        self.assertTrue(transitions["pollRecovered"]["ready"])

    def test_page_is_extracted_and_uses_candidate_language_with_evidence(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("etf_rotation.t_page"))
        for retired_text in ("黄金" + "窗口", "回补" + "提醒", "减仓" + "提醒"):
            self.assertNotIn(retired_text, PAGE)
        self.assertIn("做T候选", PAGE)
        self.assertIn("偏离观察", PAGE)
        for field in (
            "health", "health_reason", "path_efficiency", "one_side_ratio",
            "vwap_crossings", "vwap_slope", "above_vwap_count",
            "below_vwap_count", "range_confirmation_count",
            "trend_confirmation_count", "gross_edge_pct", "cost_pct",
            "net_edge_pct", "blocked_reasons",
        ):
            with self.subTest(field=field):
                self.assertIn(field, PAGE)

    def test_page_has_axes_thresholds_tooltip_and_incremental_quotes(self) -> None:
        self.assertIn('class="x-axis"', PAGE)
        self.assertIn('class="y-axis"', PAGE)
        self.assertIn('id="chart-tooltip"', PAGE)
        self.assertIn('class="three-grid"', PAGE)
        self.assertIn('class="five-grid"', PAGE)
        self.assertIn("gridBandValue", PAGE)
        self.assertIn("five labeled y ticks", PAGE)
        self.assertIn("sessionAwareTicks", PAGE)
        self.assertIn("/api/quotes?symbol=", PAGE)
        self.assertIn("loadQuoteUpdates", PAGE)
        self.assertIn("point.timestamp", PAGE)
        self.assertIn("payload.reset", PAGE)
        self.assertIn("payload.reset?new Map():new Map(current||[])", PAGE)
        self.assertIn("source.addEventListener('summary'", PAGE)
        self.assertIn("source.addEventListener('delta'", PAGE)
        self.assertIn("source.addEventListener('reset'", PAGE)
        self.assertIn("三格 0.60%", PAGE)
        self.assertIn("五格 1.00%", PAGE)

    def test_page_uses_explicit_backtest_and_replay_panel_names(self) -> None:
        self.assertIn("<h3>做T回测</h3>", PAGE)
        self.assertIn("<h3>信号粗回放</h3>", PAGE)
        self.assertIn("/api/t-backtest", PAGE)
        self.assertIn("/api/signal-replay", PAGE)
        self.assertNotIn("<h3>独立回测</h3>", PAGE)

    def test_page_has_left_watchlist_navigation_and_compact_add_form(self) -> None:
        self.assertIn('class="layout"', PAGE)
        self.assertLess(PAGE.index('<aside class="sidebar">'), PAGE.index('<section id="detail"'))
        self.assertIn("grid-template-columns:280px minmax(0,1fr)", PAGE)
        self.assertIn('id="watch-list"', PAGE)
        self.assertIn('aria-label="已监控标的"', PAGE)
        self.assertIn('id="watch-form" class="compact-form"', PAGE)
        self.assertIn('id="watch-symbol"', PAGE)
        self.assertIn('id="watch-name"', PAGE)
        self.assertIn('id="watch-submit"', PAGE)
        self.assertIn("fetch('/api/watchlist'", PAGE)
        self.assertIn("#form-error.success", PAGE)
        self.assertIn("formError.className='success'", PAGE)
        self.assertIn("formError.className='error'", PAGE)
        self.assertIn("等待下一轮行情刷新", PAGE)

    def test_page_renders_only_selected_item_and_keeps_selection_on_updates(self) -> None:
        self.assertIn("let latestData=null,backtests=new Map(),replays=new Map(),selectedSymbol=null", PAGE)
        self.assertIn("item.symbol===selectedSymbol", PAGE)
        self.assertIn("selectedSymbol=button.dataset.symbol", PAGE)
        self.assertIn("if(!items.some(item=>item.symbol===selectedSymbol))", PAGE)
        self.assertIn("const item=items.find(current=>current.symbol===selectedSymbol)", PAGE)
        self.assertIn("const item=items.find(current=>current.symbol===selectedSymbol)", PAGE)
        self.assertIn("detail.innerHTML=`<article class=\"card\"", PAGE)
        self.assertIn("backtestSummary(item.symbol)", PAGE)
        self.assertIn("replaySummary(item.symbol)", PAGE)
        self.assertIn("<h3>做T回测</h3>", PAGE)

    def test_page_reloads_daily_history_after_every_detail_render(self) -> None:
        self.assertIn("updateConnection(state);loadAlerts();loadDailyHistory()", PAGE)
        self.assertIn("alertHistoryCache.get(item.symbol)", PAGE)
        self.assertIn("/api/alerts?symbol=${encodeURIComponent(symbol)}&limit=30", PAGE)
        self.assertIn("sequence!==alertRequestSequence||symbol!==selectedSymbol", PAGE)
        self.assertIn("historyDate.addEventListener('change',loadDailyHistory)", PAGE)
        self.assertIn("if((data.dates||[]).includes(selected))historyDate.value=selected", PAGE)
        self.assertNotIn("render(await response.json());loadAlerts();loadDailyHistory()", PAGE)

    def test_page_highlights_fresh_candidates_with_neutral_language_only(self) -> None:
        self.assertIn("item.action==='BUY_CANDIDATE'||item.action==='SELL_CANDIDATE'", PAGE)
        self.assertIn("healthKey==='REALTIME'", PAGE)
        self.assertIn("candidate=Boolean(feed.ready)&&realtime&&!stale&&candidateAction", PAGE)
        self.assertIn("candidateAction=item.action==='BUY_CANDIDATE'||item.action==='SELL_CANDIDATE'", PAGE)
        self.assertIn("displayLabel:candidate?'做T候选':candidateAction||item.action==='DEVIATION_OBSERVE'?'偏离观察'", PAGE)
        self.assertIn("做T候选", PAGE)
        self.assertIn("偏离观察", PAGE)
        for retired_text in ("做T黄金" + "窗口", "回补" + "提醒", "减仓" + "提醒"):
            self.assertNotIn(retired_text, PAGE)
        self.assertIn("watch-item.opportunity", PAGE)
        self.assertIn('role="alert"', PAGE)

    def test_page_can_toggle_missing_quote_placeholders(self) -> None:
        self.assertIn('id="show-missing" type="checkbox" checked', PAGE)
        self.assertIn("showMissing.checked?allItems:allItems.filter", PAGE)
        self.assertIn("item.status!=='MISSING_QUOTE'", PAGE)
        self.assertIn("暂无当日行情，历史数据请从日期选择器查看", PAGE)
        self.assertIn("无可显示行情，请开启“显示无行情”", PAGE)
        self.assertIn("showMissing.addEventListener('change'", PAGE)

    def test_page_renders_market_time_inside_each_item_card(self) -> None:
        self.assertIn('id="refresh-time"', PAGE)
        self.assertIn("item.timestamp?new Date(item.timestamp):null", PAGE)
        self.assertIn('class="time market-time${stale?', PAGE)
        self.assertIn("页面刷新时间：'+refreshedAt.toLocaleString()", PAGE)
        self.assertNotIn('id="market-time"', PAGE)
        self.assertNotIn("new Date(data.generated_at)", PAGE)

    def test_backtest_endpoint_returns_independent_summary_per_enabled_item(self) -> None:
        self.watchlist.write_text(json.dumps([
            {
                "symbol": "510300", "name": "沪深300ETF", "grid_width_pct": 0.02,
                "base_notional_cny": 11_000.0, "t_capacity_ratio": 0.10,
            },
            {"symbol": "159915", "name": "创业板ETF", "grid_width_pct": 0.02},
            {"symbol": "disabled", "name": "已禁用", "grid_width_pct": 0.02, "enabled": False},
        ], ensure_ascii=False), encoding="utf-8")
        with urlopen(self.base + "/api/backtest", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["mode"], "T_BACKTEST")
        self.assertEqual(payload["execution_mode"], "NEXT_COMPLETED_BAR")
        self.assertEqual([item["symbol"] for item in payload["items"]], ["510300", "159915"])
        completed = payload["items"][0]
        self.assertEqual(completed["status"], "NO_COMPLETED_PAIRS")
        for field in (
            "baseline_equity_cny", "strategy_equity_cny", "t_net_gain_cny",
            "completed_pair_count", "open_leg_count", "inventory", "costs",
            "execution_mode",
        ):
            self.assertIn(field, completed)
        self.assertEqual(completed["execution_mode"], "NEXT_COMPLETED_BAR")
        self.assertEqual(completed["completed_pair_count"], 0)
        self.assertEqual(completed["open_leg_count"], 0)
        self.assertEqual(completed["base_shares"], 1_000)
        self.assertEqual(completed["t_capacity_shares"], 100)
        self.assertEqual(completed["t_net_gain_cny"], 0.0)
        self.assertIsNone(completed["outperformed_baseline"])
        self.assertEqual(payload["items"][1]["status"], "MISSING_QUOTE")
        self.assertIsNone(payload["items"][1]["baseline_equity_cny"])
        self.assertIsNone(payload["items"][1]["strategy_equity_cny"])
        self.assertEqual(payload["buy_commission_rate"], 0.00012)
        self.assertEqual(payload["minimum_commission_cny"], 0.0)
        self.assertTrue(payload["commission_minimum_waived"])
        self.assertEqual(
            self.server.application.backtest(),
            self.server.application.t_backtest(),
        )

    def test_snapshot_is_lightweight_and_quotes_endpoint_returns_upserts(self) -> None:
        with urlopen(self.base + "/api/snapshot", timeout=2) as response:
            snapshot = json.loads(response.read())
        self.assertNotIn("points", snapshot["items"][0])

        with urlopen(
            self.base + "/api/quotes?" + urlencode({"symbol": "510300", "since": 0}),
            timeout=2,
        ) as response:
            quotes = json.loads(response.read())
        self.assertEqual(quotes["symbol"], "510300")
        self.assertEqual(quotes["revision"], snapshot["revision"])
        self.assertEqual(len(quotes["upserts"]), 1)
        self.assertEqual(quotes["upserts"][0]["timestamp"], "2026-08-28T09:30:00+08:00")
        self.assertEqual(quotes["upserts"][0]["schema_version"], 3)
        self.assertEqual(quotes["upserts"][0]["trading_date"], "2026-08-28")
        self.assertEqual(quotes["upserts"][0]["observed_at"], "2026-08-28T10:00:05+08:00")
        self.assertTrue(quotes["upserts"][0]["is_complete"])

    def test_quotes_endpoint_strictly_validates_symbol_and_cursor(self) -> None:
        invalid_queries = (
            {"symbol": "５１０３００", "since": "0"},
            {"symbol": "159915", "since": "0"},
            {"symbol": "510300", "since": "-1"},
            {"symbol": "510300", "since": "1.0"},
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                try:
                    urlopen(
                        self.base + "/api/quotes?" + urlencode(query), timeout=2,
                    )
                except HTTPError as error:
                    try:
                        self.assertEqual(error.code, 400)
                    finally:
                        error.close()
                else:
                    self.fail(f"accepted invalid query: {query}")

    def test_backtest_replay_and_compatibility_endpoints_are_distinct(self) -> None:
        with urlopen(self.base + "/api/t-backtest", timeout=2) as response:
            true_backtest = json.loads(response.read())
        with urlopen(self.base + "/api/signal-replay", timeout=2) as response:
            replay = json.loads(response.read())
        with urlopen(self.base + "/api/backtest", timeout=2) as response:
            alias = json.loads(response.read())
        self.assertEqual(true_backtest["mode"], "T_BACKTEST")
        self.assertEqual(replay["mode"], "SIGNAL_ROUGH_REPLAY")
        self.assertEqual(alias["mode"], "T_BACKTEST")
        self.assertTrue(alias["deprecated_alias"])

    def test_serialized_summary_is_under_ten_percent_of_full_day_fixture(self) -> None:
        start = datetime.fromisoformat("2026-08-28T09:30:00+08:00")
        points = []
        for index in range(240):
            timestamp = start + timedelta(minutes=index)
            price = 10.001 if index % 2 else 9.999
            points.append([
                timestamp.isoformat(), price, 10.0, price,
                max(price, 10.0), min(price, 10.0), 1_000, price * 100_000,
            ])
        self.quotes.write_text(json.dumps([{
            "schema_version": 2,
            "symbol": "510300",
            "name": "沪深300ETF",
            "price": points[-1][1],
            "average_price": 10.0,
            "previous_close": 10.0,
            "timestamp": points[-1][0],
            "observed_at": (start + timedelta(minutes=241)).isoformat(),
            "source": "FULL_DAY_FIXTURE",
            "points": points,
        }], ensure_ascii=False), encoding="utf-8")
        application = MonitorApplication(
            self.quotes, self.watchlist,
            clock=lambda: datetime.fromisoformat("2026-08-28T13:32:00+08:00"),
        )

        summary_size = len(json.dumps(application.snapshot(), separators=(",", ":")))
        full_size = len(json.dumps(application._published, separators=(",", ":")))

        self.assertLess(summary_size, full_size * 0.10)

    def test_backtest_replay_recognizes_new_candidate_actions(self) -> None:
        source = inspect.getsource(MonitorApplication.signal_replay)
        self.assertIn('signal.action == "BUY_CANDIDATE"', source)
        self.assertIn('signal.action == "SELL_CANDIDATE"', source)
        self.assertNotIn('signal.action == "BUY_REMINDER"', source)
        self.assertNotIn('signal.action == "SELL_REMINDER"', source)

        payload = self.server.application.signal_replay()
        self.assertEqual(payload["mode"], "SIGNAL_ROUGH_REPLAY")
        self.assertEqual([item["symbol"] for item in payload["items"]], ["510300"])

        def keys(value: object) -> list[str]:
            if isinstance(value, dict):
                return [
                    str(key)
                    for key, nested in value.items()
                ] + [key for nested in value.values() for key in keys(nested)]
            if isinstance(value, list):
                return [key for nested in value for key in keys(nested)]
            return []

        for key in keys(payload):
            self.assertFalse(
                any(term in key.lower() for term in ("return", "outperformance", "equity", "pnl")),
                key,
            )

    def test_backtest_replay_executes_candidate_at_next_historical_point(self) -> None:
        candidate_quote = confirmed_range_quote(-0.008, -0.007)
        execution_point = replace(
            candidate_quote.points[-1],
            timestamp=candidate_quote.points[-1].timestamp + timedelta(minutes=1),
        )
        points = candidate_quote.points + (execution_point,)
        self.quotes.write_text(json.dumps([{
            "schema_version": 2,
            "symbol": candidate_quote.symbol,
            "name": candidate_quote.name,
            "price": execution_point.price,
            "average_price": execution_point.average_price,
            "previous_close": candidate_quote.previous_close,
            "timestamp": execution_point.timestamp.isoformat(),
            "observed_at": (execution_point.timestamp + timedelta(minutes=1)).isoformat(),
            "source": "TEST_FIXTURE",
            "points": [[
                point.timestamp.isoformat(), point.price, point.average_price,
                point.open, point.high, point.low, point.volume, point.amount,
            ] for point in points],
        }], ensure_ascii=False), encoding="utf-8")
        self.watchlist.write_text(json.dumps([{
            "symbol": candidate_quote.symbol,
            "name": candidate_quote.name,
            "grid_width_pct": 0.002,
        }], ensure_ascii=False), encoding="utf-8")

        result = MonitorApplication(self.quotes, self.watchlist).t_backtest()

        item = result["items"][0]
        self.assertEqual(item["execution_mode"], "NEXT_COMPLETED_BAR")
        self.assertEqual(item["completed_pair_count"], 0)
        self.assertEqual(item["open_leg_count"], 1)
        self.assertEqual(item["open_legs"][0]["side"], "BUY")
        self.assertEqual(item["open_legs"][0]["timestamp"], execution_point.timestamp.isoformat())
        self.assertAlmostEqual(
            item["t_net_gain_cny"],
            item["strategy_equity_cny"] - item["baseline_equity_cny"],
        )
        self.assertNotEqual(item["t_net_gain_cny"], 0.0)
        self.assertIsNone(item["outperformed_baseline"])

    def test_multi_day_replays_use_each_schema_v3_trading_day_previous_close(self) -> None:
        candidate = confirmed_range_quote(-0.008, -0.007)
        execution_point = replace(
            candidate.points[-1],
            timestamp=candidate.points[-1].timestamp + timedelta(minutes=1),
        )
        history_path = self.quotes.parent / "minute_history.jsonl"
        records = []
        for point in (*candidate.points, execution_point):
            records.append({
                "schema_version": 3,
                "symbol": candidate.symbol,
                "name": candidate.name,
                "trading_date": point.timestamp.date().isoformat(),
                "timestamp": point.timestamp.isoformat(),
                "observed_at": (point.timestamp + timedelta(minutes=1)).isoformat(),
                "is_complete": True,
                "source": "OLD_DAY_FIXTURE",
                "previous_close": candidate.previous_close,
                "open": point.open,
                "high": point.high,
                "low": point.low,
                "price": point.price,
                "average_price": point.average_price,
                "volume": point.volume,
                "amount": point.amount,
            })
        history_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        current_start = datetime.fromisoformat("2026-08-31T10:00:00+08:00")
        current_points = (
            [current_start.isoformat(), 9.5, 9.5, 9.5, 9.5, 9.5, 1_000, 950_000],
            [(current_start + timedelta(minutes=1)).isoformat(), 9.5, 9.5, 9.5, 9.5, 9.5, 1_000, 950_000],
        )
        self.quotes.write_text(json.dumps([{
            "schema_version": 2,
            "symbol": candidate.symbol,
            "name": candidate.name,
            "price": 9.5,
            "average_price": 9.5,
            "previous_close": candidate.price,
            "timestamp": current_points[-1][0],
            "observed_at": (current_start + timedelta(minutes=2)).isoformat(),
            "source": "CURRENT_DAY_FIXTURE",
            "points": current_points,
        }], ensure_ascii=False), encoding="utf-8")
        self.watchlist.write_text(json.dumps([{
            "symbol": candidate.symbol,
            "name": candidate.name,
            "grid_width_pct": 0.002,
        }], ensure_ascii=False), encoding="utf-8")
        application = MonitorApplication(
            self.quotes,
            self.watchlist,
            history_path=history_path,
        )

        backtest_item = application.t_backtest()["items"][0]
        replay_item = application.signal_replay()["items"][0]

        self.assertEqual(backtest_item["open_leg_count"], 1)
        self.assertEqual(
            backtest_item["open_legs"][0]["timestamp"],
            execution_point.timestamp.isoformat(),
        )
        self.assertIn({
            "action": "BUY_CANDIDATE",
            "signal_timestamp": candidate.points[-1].timestamp.isoformat(),
            "next_completed_timestamp": execution_point.timestamp.isoformat(),
        }, replay_item["actions"])

    def test_page_marks_each_stale_market_time_and_shows_t_backtest(self) -> None:
        self.assertIn("const STALE_AFTER_MS=60000", PAGE)
        self.assertIn("age<0||age>STALE_AFTER_MS", PAGE)
        self.assertIn("--stale:#ff3b30", PAGE)
        self.assertIn("当前行情数据已过期，请勿按对应价格操作", PAGE)
        self.assertIn("staleBanner.classList.toggle('visible',Boolean(state.unsafe))", PAGE)
        self.assertIn("updateConnection(state)", PAGE)
        self.assertIn("/api/t-backtest", PAGE)
        self.assertIn("<h3>做T回测</h3>", PAGE)
        self.assertIn("backtestSummary(item.symbol)", PAGE)

    def test_page_health_and_polling_snapshot(self) -> None:
        with urlopen(self.base + "/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("本地做T监控", page)
        self.assertIn("<svg", page)
        with urlopen(self.base + "/api/snapshot", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["items"][0]["action"], "WAIT")
        self.assertEqual(payload["items"][0]["average_price"], 10.0)
        self.assertFalse(payload["auto_trade"])
        with urlopen(self.base + "/health", timeout=2) as response:
            health = json.loads(response.read())
        self.assertEqual(health["mode"], "MONITOR_ONLY")
        self.assertEqual(health["revision"], 1)
        self.assertFalse(health["ok"])
        self.assertIn("OUTAGE", health["health_statuses"])

    def post(self, path: str, payload: object) -> tuple[int, dict[str, object]]:
        request = Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def test_watchlist_post_persists_valid_item(self) -> None:
        metadata = json.loads(self.metadata.read_text(encoding="utf-8"))
        metadata["items"].append({
            "symbol": "159915",
            "name": "创业板ETF",
            "index": {"code": "399006", "name": "创业板指", "provider": "深交所"},
            "trading": {
                "exchange": "SZSE",
                "asset_type": "DOMESTIC_EQUITY_ETF",
                "intraday_turnaround": False,
                "sellable_delay_days": 1,
                "lot_size": 100,
                "price_tick": 0.001,
                "price_limit_pct": 0.20,
                "volume_unit_shares": 100,
            },
        })
        self.metadata.write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8",
        )
        self.watchlist.write_text(json.dumps([{
            "symbol": "510300",
            "name": "沪深300ETF",
            "grid_width_pct": 0.02,
            "base_notional_cny": 11_000.0,
            "base_shares": 1_000,
            "t_capacity_ratio": 0.10,
            "t_capacity_shares": 100,
        }], ensure_ascii=False), encoding="utf-8")
        status, payload = self.post("/api/watchlist", {
            "symbol": "159915",
            "name": "创业板ETF",
        })
        self.assertEqual(status, 201)
        self.assertEqual(payload["item"]["symbol"], "159915")
        stored = json.loads(self.watchlist.read_text(encoding="utf-8"))
        self.assertEqual([item["symbol"] for item in stored["watchlist"]], [
            "510300", "159915",
        ])
        self.assertEqual(stored["watchlist"][1]["name"], "创业板ETF")
        self.assertEqual(stored["watchlist"][1]["grid_width_pct"], 0.002)
        self.assertTrue(stored["watchlist"][1]["enabled"])
        self.assertEqual(stored["watchlist"][0]["base_notional_cny"], 11_000.0)
        self.assertEqual(stored["watchlist"][0]["base_shares"], 1_000)
        self.assertEqual(stored["watchlist"][0]["t_capacity_ratio"], 0.10)
        self.assertEqual(stored["watchlist"][0]["t_capacity_shares"], 100)
        self.assertEqual(list(self.watchlist.parent.glob(".*.tmp")), [])

    def test_watchlist_post_rejects_invalid_symbol_without_changing_file(self) -> None:
        original = self.watchlist.read_bytes()
        for symbol in ("51030", "51030A", ""):
            with self.subTest(symbol=symbol):
                status, payload = self.post("/api/watchlist", {"symbol": symbol})
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "invalid_request")
                self.assertEqual(self.watchlist.read_bytes(), original)

    def test_watchlist_post_rejects_duplicate_without_changing_file(self) -> None:
        original = self.watchlist.read_bytes()
        status, payload = self.post("/api/watchlist", {
            "symbol": "510300",
            "name": "重复项",
        })
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "duplicate")
        self.assertEqual(self.watchlist.read_bytes(), original)

    def test_watchlist_post_rejects_missing_metadata_without_changing_file(self) -> None:
        original = self.watchlist.read_bytes()
        status, payload = self.post("/api/watchlist", {
            "symbol": "515180",
            "name": "中证红利",
        })
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"], "missing_metadata")
        self.assertEqual(payload["message"], "缺少交易元数据，无法添加: 515180")
        self.assertEqual(self.watchlist.read_bytes(), original)

    def test_trading_post_is_still_rejected(self) -> None:
        status, payload = self.post("/api/order", {})
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"], "read_only")


if __name__ == "__main__":
    unittest.main()
