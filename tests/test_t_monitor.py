from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from etf_rotation.etf_metadata import EtfMetadataStore, MetadataError
from etf_rotation.quote_collector import SOURCE_NAME, Trends2QuoteCollector, market_for_symbol
from etf_rotation.t_monitor import (
    JsonQuoteAdapter, MarketDataError, QuoteHistoryStore, TMonitorEngine, WatchItem,
    load_watchlist, snapshot_to_dict,
)
from etf_rotation.t_web import MonitorApplication, PAGE, create_server


NOW = "2026-08-28T10:00:00+08:00"


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
        "symbol": "510300",
        "name": "沪深300ETF",
        "price": price,
        "average_price": average_price,
        "previous_close": previous_close,
        "timestamp": NOW,
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
        self.assertEqual(payload["source"]["name"], SOURCE_NAME)
        self.assertEqual([item["symbol"] for item in payload["quotes"]], ["510300", "159915"])
        self.assertEqual(payload["quotes"][0]["price"], 10.3)
        self.assertEqual(payload["quotes"][0]["average_price"], 10.2)
        self.assertEqual(payload["quotes"][0]["previous_close"], 10.0)
        self.assertEqual(payload["quotes"][0]["timestamp"], NOW)
        self.assertEqual(len(requests), 2)
        self.assertIn("secid=1.510300", requests[0][0])
        self.assertIn("secid=0.159915", requests[1][0])

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
    def test_appends_unique_timestamps_and_restores_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "quotes.jsonl"
            quotes = JsonQuoteAdapter().parse([quote()])
            QuoteHistoryStore(path).append(quotes)
            QuoteHistoryStore(path).append(quotes)
            restored = QuoteHistoryStore(path).merge(JsonQuoteAdapter().parse([quote()]))
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 2)
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


class TMonitorEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = JsonQuoteAdapter()
        self.watchlist = (WatchItem("510300", "沪深300ETF", 0.02),)

    def evaluate(self, raw: dict[str, object]) -> dict[str, object]:
        snapshot = TMonitorEngine().evaluate(self.watchlist, self.adapter.parse([raw]))
        return snapshot_to_dict(snapshot)["items"][0]

    def test_uses_average_grid_and_previous_close_zero_axis(self) -> None:
        cases = (
            (quote(11.0, 10.0, 10.0), "SELL_REMINDER"),
            (quote(9.0, 10.0, 10.0), "BUY_REMINDER"),
            (quote(10.21, 10.0, 10.1), "WAIT"),
            (quote(10.59, 10.0, 9.6), "WAIT"),
            (quote(10.6, 10.0, 9.6), "SELL_REMINDER"),
            (quote(9.4, 10.0, 10.4), "BUY_REMINDER"),
            (quote(10.1, 10.0, 10.0), "WAIT"),
        )
        for raw, expected in cases:
            with self.subTest(price=raw["price"], previous_close=raw["previous_close"]):
                self.assertEqual(self.evaluate(raw)["action"], expected)

    def test_grid_width_is_configurable(self) -> None:
        raw = quote(10.8, 10.0, 10.0)
        narrow = (WatchItem("510300", "沪深300ETF", 0.015),)
        wide = (WatchItem("510300", "沪深300ETF", 0.02),)
        quotes = self.adapter.parse([raw])
        self.assertEqual(TMonitorEngine().evaluate(narrow, quotes).signals[0].action, "SELL_REMINDER")
        self.assertEqual(TMonitorEngine().evaluate(wide, quotes).signals[0].action, "WAIT")

    def test_waits_when_either_mean_deviation_or_previous_close_distance_is_below_threshold(self) -> None:
        item = self.evaluate(quote(10.6, 10.0, 10.0))
        self.assertEqual(item["action"], "WAIT")
        self.assertEqual(item["white_yellow_deviation_grids"], 3.0)
        self.assertEqual(item["previous_close_distance_grids"], 3.0)

    def test_outputs_grid_distances_for_mean_reversion_and_fast_rise(self) -> None:
        item = self.evaluate(quote(11.0, 10.0, 10.0, prior_price=10.5))
        self.assertEqual(item["action"], "SELL_REMINDER")
        self.assertEqual(item["white_yellow_deviation_grids"], 5.0)
        self.assertEqual(item["previous_close_distance_grids"], 5.0)
        self.assertEqual(item["fast_rise_grids"], 0.0)

    def test_fast_rise_takes_priority_over_sell_reminder(self) -> None:
        raw = quote(
            11.0, 10.0, 10.0, prior_price=10.0,
            prior_time="2026-08-28T09:57:00+08:00",
        )
        item = self.evaluate(raw)
        self.assertEqual(item["action"], "OBSERVE")
        self.assertEqual(item["label"], "快速上冲 5.00 格，优先观望")
        self.assertEqual(item["fast_rise_grids"], 5.0)

    def test_regime_fields_are_exposed_and_short_history_is_uncertain(self) -> None:
        item = self.evaluate(quote(10.1))
        self.assertEqual(item["regime_state"], "UNCERTAIN")
        self.assertEqual(item["regime_label"], "样本不足，暂停做T")
        self.assertIsNone(item["path_efficiency"])

    def test_uptrend_blocks_countertrend_sell_reminder(self) -> None:
        points = []
        for index in range(20):
            price = 10.0 + index * 0.08
            average = 10.0 + index * 0.03
            points.append({
                "timestamp": f"2026-08-28T09:{30 + index:02d}:00+08:00",
                "price": price,
                "average_price": average,
            })
        raw = quote(11.52, 10.57, 10.0)
        raw["timestamp"] = points[-1]["timestamp"]
        raw["points"] = points
        item = self.evaluate(raw)
        self.assertEqual(item["regime_state"], "UPTREND")
        self.assertEqual(item["action"], "OBSERVE")
        self.assertIn("上涨趋势日", item["label"])

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
        payload = snapshot_to_dict(TMonitorEngine().evaluate(watchlist, {}))
        self.assertEqual(payload["errors"], [])
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["symbol"], "missing")
        self.assertEqual(item["status"], "MISSING_QUOTE")
        self.assertEqual(item["action"], "UNAVAILABLE")
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
            )
            self.assertFalse(application.refresh_once())
            payload = application.snapshot()
            self.assertEqual(quotes_path.read_bytes(), original)
            self.assertEqual(payload["items"][0]["price"], 10.3)
            self.assertEqual(payload["source"]["name"], SOURCE_NAME)
            self.assertEqual(payload["refresh_error"], "远端明确失败")
            self.assertEqual(payload["errors"], [])


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
        self.server = create_server("127.0.0.1", 0, self.quotes, self.watchlist)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_page_draws_white_price_yellow_average_and_zero_axis(self) -> None:
        self.assertIn("price-line", PAGE)
        self.assertIn("average-line", PAGE)
        self.assertIn("zero-line", PAGE)
        self.assertIn("白线实时价，黄线均价", PAGE)
        self.assertIn("item.previous_close", PAGE)

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

    def test_page_renders_only_selected_item_and_keeps_selection_on_updates(self) -> None:
        self.assertIn("let latestData=null,backtests=new Map(),selectedSymbol=null", PAGE)
        self.assertIn("item.symbol===selectedSymbol", PAGE)
        self.assertIn("selectedSymbol=button.dataset.symbol", PAGE)
        self.assertIn("if(!items.some(item=>item.symbol===selectedSymbol))", PAGE)
        self.assertIn("const item=items.find(current=>current.symbol===selectedSymbol)", PAGE)
        self.assertIn("const item=items.find(current=>current.symbol===selectedSymbol)", PAGE)
        self.assertIn("detail.innerHTML=`<article class=\"card\"", PAGE)
        self.assertIn("backtestSummary(item.symbol)", PAGE)
        self.assertIn("<h3>独立回测</h3>", PAGE)

    def test_page_reloads_daily_history_after_every_detail_render(self) -> None:
        self.assertIn("dot.classList.toggle('stale',stale);loadAlerts();loadDailyHistory()", PAGE)
        self.assertIn("alertHistoryCache.get(item.symbol)", PAGE)
        self.assertIn("/api/alerts?symbol=${encodeURIComponent(symbol)}&limit=30", PAGE)
        self.assertIn("sequence!==alertRequestSequence||symbol!==selectedSymbol", PAGE)
        self.assertIn("historyDate.addEventListener('change',loadDailyHistory)", PAGE)
        self.assertIn("if((data.dates||[]).includes(selected))historyDate.value=selected", PAGE)
        self.assertNotIn("render(await response.json());loadAlerts();loadDailyHistory()", PAGE)

    def test_page_highlights_fresh_golden_window_only(self) -> None:
        self.assertIn("item.action==='BUY_REMINDER'||item.action==='SELL_REMINDER'", PAGE)
        self.assertIn("golden=!stale", PAGE)
        self.assertIn("做T黄金窗口", PAGE)
        self.assertIn("回补提醒", PAGE)
        self.assertIn("减仓提醒", PAGE)
        self.assertIn("watch-item.opportunity", PAGE)
        self.assertIn('role="alert"', PAGE)

    def test_page_can_toggle_missing_quote_placeholders(self) -> None:
        self.assertIn('id="show-missing" type="checkbox" checked', PAGE)
        self.assertIn("showMissing.checked?allItems:allItems.filter", PAGE)
        self.assertIn("item.status!=='MISSING_QUOTE'", PAGE)
        self.assertIn("行情缺失，指标不可用", PAGE)
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
            {"symbol": "510300", "name": "沪深300ETF", "grid_width_pct": 0.02},
            {"symbol": "159915", "name": "创业板ETF", "grid_width_pct": 0.02},
            {"symbol": "disabled", "name": "已禁用", "grid_width_pct": 0.02, "enabled": False},
        ], ensure_ascii=False), encoding="utf-8")
        with urlopen(self.base + "/api/backtest", timeout=2) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["execution"], "NEXT_POINT")
        self.assertEqual([item["symbol"] for item in payload["items"]], ["510300", "159915"])
        self.assertEqual(payload["items"][0]["status"], "OK")
        self.assertIn("maximum_drawdown", payload["items"][0])
        self.assertEqual(payload["items"][1]["status"], "MISSING_QUOTE")
        self.assertIsNone(payload["items"][1]["initial_capital_cny"])
        self.assertIsNone(payload["items"][1]["ending_value_cny"])
        self.assertEqual(payload["buy_commission_rate"], 0.00012)
        self.assertEqual(payload["minimum_commission_cny"], 0.0)
        self.assertTrue(payload["commission_minimum_waived"])

    def test_page_marks_each_stale_market_time_and_shows_independent_backtest(self) -> None:
        self.assertIn("const STALE_AFTER_MS=60000", PAGE)
        self.assertIn("refreshedAt.getTime()-marketAt.getTime()>STALE_AFTER_MS", PAGE)
        self.assertIn("--stale:#ff3b30", PAGE)
        self.assertIn("当前行情数据已过期，请勿按对应价格操作", PAGE)
        self.assertIn("staleBanner.classList.toggle('visible',stale)", PAGE)
        self.assertIn("statusNode.textContent=stale?'当前行情已过期':'实时监控中'", PAGE)
        self.assertIn("/api/backtest", PAGE)
        self.assertIn("<h3>独立回测</h3>", PAGE)
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
        self.assertEqual(health, {"status": "ok", "mode": "MONITOR_ONLY"})

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

    def test_trading_post_is_still_rejected(self) -> None:
        status, payload = self.post("/api/order", {})
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"], "read_only")


if __name__ == "__main__":
    unittest.main()
