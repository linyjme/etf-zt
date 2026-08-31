from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import tempfile
import unittest
from urllib.request import Request

from etf_rotation.etf_metadata import EtfMetadata, IndexMetadata, TradingMetadata
from etf_rotation.market_data import (
    MarketDataValidator,
    MarketHealthClassifier,
    MinuteHistoryStore,
    finalized_points,
    load_closed_dates,
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


def history_quote(price: float, previous_close: float, observed_at: str) -> Quote:
    timestamp = datetime.fromisoformat("2026-08-28T09:30:00+08:00")
    item = QuotePoint(
        timestamp=timestamp,
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
        timestamp=timestamp,
        points=(item,),
        observed_at=datetime.fromisoformat(observed_at),
        source="TEST",
    )


class MarketDataTests(unittest.TestCase):
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
