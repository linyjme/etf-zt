from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timezone
import json
import math
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from etf_rotation.swing_collector import (
    FIELDS1,
    FIELDS2,
    KLINE_ENDPOINT,
    KLINE_FALLBACK_ENDPOINT,
    EastmoneyDailyCollector,
)
from etf_rotation.swing_config import SwingWatchItem
from etf_rotation.swing_data import SwingDataError


NOW = datetime.fromisoformat("2026-08-31T15:10:00+08:00")


def kline_payload(
    symbol: str,
    market: int,
    *,
    adjusted: bool,
    dates: tuple[str, ...] = ("2026-08-27", "2026-08-28"),
    pre_k_price: object = 9.5,
) -> dict[str, object]:
    lines = []
    for offset, trading_day in enumerate(dates):
        raw_open = 10.0 + offset
        raw_close = 10.5 + offset
        raw_high = 11.0 + offset
        raw_low = 9.8 + offset
        scale = 0.5 if adjusted else 1.0
        lines.append(
            f"{trading_day},{raw_open * scale},{raw_close * scale},"
            f"{raw_high * scale},{raw_low * scale},{1000 + offset},"
            f"{10000 + offset},0,0,0,0"
        )
    return {
        "rc": 0,
        "data": {
            "code": symbol,
            "market": market,
            "name": "fixture ETF",
            "preKPrice": pre_k_price,
            "klines": lines,
        },
    }


def payload_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class FixtureTransport:
    def __init__(self) -> None:
        self.requests: list[tuple[Request, float]] = []

    def __call__(self, request: Request, timeout: float) -> bytes:
        self.requests.append((request, timeout))
        query = parse_qs(urlsplit(request.full_url).query)
        market_text, symbol = query["secid"][0].split(".")
        return payload_bytes(kline_payload(
            symbol,
            int(market_text),
            adjusted=query["fqt"] == ["1"],
        ))


class BrokenSequence(Sequence[SwingWatchItem]):
    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int) -> SwingWatchItem:
        raise RuntimeError("HOSTILE_WATCHLIST_SECRET")


class FalseyCallable:
    def __init__(self, result: object):
        self.result = result

    def __bool__(self) -> bool:
        return False

    def __call__(self, *args: object) -> object:
        return self.result


class EastmoneyDailyCollectorTests(unittest.TestCase):
    def collector(self, transport=None, now=None, timeout: float = 8.0):
        return EastmoneyDailyCollector(
            timeout=timeout,
            transport=transport or FixtureTransport(),
            now=now or (lambda: NOW),
        )

    def test_joins_raw_and_adjusted_bars_with_one_canonical_observation(self) -> None:
        transport = FixtureTransport()
        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True), SwingWatchItem("159915", True)),
            date(2026, 8, 28),
        )

        self.assertEqual(
            [(bar.symbol, bar.trading_date) for bar in bars],
            [
                ("159915", date(2026, 8, 27)),
                ("159915", date(2026, 8, 28)),
                ("510300", date(2026, 8, 27)),
                ("510300", date(2026, 8, 28)),
            ],
        )
        first = bars[0]
        self.assertEqual((first.open, first.close, first.adjusted_open), (10.0, 10.5, 5.0))
        self.assertEqual(first.previous_close, 9.5)
        self.assertEqual(bars[1].previous_close, 10.5)
        self.assertTrue(all(bar.is_final for bar in bars))
        self.assertEqual({bar.observed_at for bar in bars}, {NOW})
        self.assertEqual({bar.source for bar in bars}, {
            "东方财富 kline (push2his.eastmoney.com)",
        })

    def test_requests_exact_params_headers_and_raw_adjusted_pair_per_symbol(self) -> None:
        transport = FixtureTransport()
        self.collector(transport, timeout=3.5).collect(
            (SwingWatchItem("510300", True), SwingWatchItem("159915", False)),
            date(2026, 8, 28),
            count=321,
        )

        self.assertEqual(len(transport.requests), 2)
        self.assertEqual([timeout for _, timeout in transport.requests], [3.5, 3.5])
        self.assertEqual(
            [parse_qs(urlsplit(request.full_url).query) for request, _ in transport.requests],
            [
                {
                    "secid": ["1.510300"], "fields1": [FIELDS1],
                    "fields2": [FIELDS2], "klt": ["101"], "fqt": ["0"],
                    "lmt": ["321"], "end": ["20260828"],
                },
                {
                    "secid": ["1.510300"], "fields1": [FIELDS1],
                    "fields2": [FIELDS2], "klt": ["101"], "fqt": ["1"],
                    "lmt": ["321"], "end": ["20260828"],
                },
            ],
        )
        for request, _ in transport.requests:
            self.assertTrue(request.full_url.startswith(KLINE_ENDPOINT))
            headers = {key.lower(): value for key, value in request.header_items()}
            self.assertEqual(headers["accept"], "application/json")
            self.assertEqual(headers["user-agent"], "Mozilla/5.0")
            self.assertEqual(headers["referer"], "https://quote.eastmoney.com/")

    def test_primary_request_failure_refetches_whole_batch_from_fallback(self) -> None:
        requests: list[str] = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request.full_url)
            query = parse_qs(urlsplit(request.full_url).query)
            market_text, symbol = query["secid"][0].split(".")
            if request.full_url.startswith(KLINE_ENDPOINT) and symbol == "159915":
                raise OSError("primary down")
            return payload_bytes(kline_payload(
                symbol, int(market_text), adjusted=query["fqt"] == ["1"],
            ))

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True), SwingWatchItem("159915", True)),
            date(2026, 8, 28),
        )

        hosts_and_requests = [(
            urlsplit(url).netloc,
            parse_qs(urlsplit(url).query)["secid"][0],
            parse_qs(urlsplit(url).query)["fqt"][0],
        ) for url in requests]
        self.assertEqual(hosts_and_requests, [
            ("push2his.eastmoney.com", "1.510300", "0"),
            ("push2his.eastmoney.com", "1.510300", "1"),
            ("push2his.eastmoney.com", "0.159915", "0"),
            ("push2delay.eastmoney.com", "1.510300", "0"),
            ("push2delay.eastmoney.com", "1.510300", "1"),
            ("push2delay.eastmoney.com", "0.159915", "0"),
            ("push2delay.eastmoney.com", "0.159915", "1"),
        ])
        self.assertEqual({bar.source for bar in bars}, {
            "东方财富 kline (push2delay.eastmoney.com)",
        })

    def test_both_request_failures_have_safe_context_and_preserve_cause(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            raise OSError("HOSTILE_TRANSPORT_SECRET")

        with self.assertRaises(SwingDataError) as caught:
            self.collector(transport).collect(
                (SwingWatchItem("510300", True),), date(2026, 8, 28),
            )
        self.assertIn("主备端点请求均失败", str(caught.exception))
        self.assertNotIn("HOSTILE_TRANSPORT_SECRET", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)

    def test_primary_business_or_schema_failure_never_falls_back(self) -> None:
        bad_payloads = (
            {"rc": 1, "data": None},
            {"rc": 0, "data": None},
            kline_payload("159915", 0, adjusted=False),
            kline_payload("510300", 0, adjusted=False),
        )
        for bad in bad_payloads:
            with self.subTest(bad=bad):
                requests = []

                def transport(request: Request, timeout: float) -> bytes:
                    requests.append(request.full_url)
                    return payload_bytes(bad)

                with self.assertRaises(SwingDataError):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0].startswith(KLINE_ENDPOINT))

    def test_fallback_validation_error_after_request_failure_is_safe(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            if request.full_url.startswith(KLINE_ENDPOINT):
                raise OSError("HOSTILE_PRIMARY_SECRET")
            return payload_bytes({"rc": 9, "data": "HOSTILE_PAYLOAD_SECRET"})

        with self.assertRaises(SwingDataError) as caught:
            self.collector(transport).collect(
                (SwingWatchItem("510300", True),), date(2026, 8, 28),
            )
        self.assertIn("备用端点业务校验失败", str(caught.exception))
        self.assertNotIn("HOSTILE_PRIMARY_SECRET", str(caught.exception))
        self.assertNotIn("HOSTILE_PAYLOAD_SECRET", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)

    def test_fallback_cross_half_validation_after_request_failure_is_safe(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            if request.full_url.startswith(KLINE_ENDPOINT):
                raise OSError("HOSTILE_PRIMARY_SECRET")
            dates = ("2026-08-27",) if query["fqt"] == ["1"] else (
                "2026-08-27", "2026-08-28",
            )
            return payload_bytes(kline_payload(
                "510300", 1, adjusted=query["fqt"] == ["1"], dates=dates,
            ))

        with self.assertRaises(SwingDataError) as caught:
            self.collector(transport).collect(
                (SwingWatchItem("510300", True),), date(2026, 8, 28),
            )
        self.assertIn("备用端点业务校验失败", str(caught.exception))
        self.assertIn("push2his.eastmoney.com", str(caught.exception))
        self.assertIn("push2delay.eastmoney.com", str(caught.exception))
        self.assertNotIn("HOSTILE_PRIMARY_SECRET", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)

    def test_rejects_missing_half_date_mismatch_duplicate_and_bad_lines_atomically(self) -> None:
        cases = {}
        missing = kline_payload("510300", 1, adjusted=True)
        missing["data"]["klines"] = []
        cases["missing half"] = missing
        cases["date mismatch"] = kline_payload(
            "510300", 1, adjusted=True, dates=("2026-08-27",),
        )
        duplicate = kline_payload("510300", 1, adjusted=True)
        duplicate["data"]["klines"].append(duplicate["data"]["klines"][0])
        cases["duplicate"] = duplicate
        incomplete = kline_payload("510300", 1, adjusted=True)
        incomplete["data"]["klines"][0] = "2026-08-27,5,5.25"
        cases["incomplete"] = incomplete
        invalid_number = kline_payload("510300", 1, adjusted=True)
        invalid_number["data"]["klines"][0] = (
            "2026-08-27,5,nan,5.5,4.9,1000,10000"
        )
        cases["nonfinite"] = invalid_number
        invalid_date = kline_payload("510300", 1, adjusted=True)
        invalid_date["data"]["klines"][0] = (
            "2026-8-27,5,5.25,5.5,4.9,1000,10000"
        )
        cases["bad date"] = invalid_date

        for label, bad_adjusted in cases.items():
            with self.subTest(label=label):
                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    if query["fqt"] == ["1"]:
                        return payload_bytes(bad_adjusted)
                    return payload_bytes(kline_payload("510300", 1, adjusted=False))

                with self.assertRaises(SwingDataError):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )

    def test_rejects_invalid_payload_envelope_and_raw_previous_close(self) -> None:
        cases: list[object] = [
            [],
            {"rc": True, "data": {}},
            {"rc": 0, "data": []},
            {"rc": 0, "data": {"code": "510300", "market": 1, "klines": "x"}},
        ]
        for bad_pre_close in (None, 0, -1, True, "nan", math.inf):
            cases.append(kline_payload(
                "510300", 1, adjusted=False, pre_k_price=bad_pre_close,
            ))
        for bad in cases:
            with self.subTest(bad=bad):
                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    if query["fqt"] == ["0"]:
                        return payload_bytes(bad)
                    return payload_bytes(kline_payload("510300", 1, adjusted=True))

                with self.assertRaises(SwingDataError):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )

    def test_optional_name_must_be_nonblank_when_present(self) -> None:
        for bad_name in (None, "", "   ", 123):
            with self.subTest(bad_name=bad_name):
                bad = kline_payload("510300", 1, adjusted=False)
                bad["data"]["name"] = bad_name

                def transport(request: Request, timeout: float) -> bytes:
                    return payload_bytes(bad)

                with self.assertRaises(SwingDataError):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )

    def test_rejects_unsorted_dates_and_inconsistent_adjustment_scale(self) -> None:
        for mode in ("unsorted", "scale"):
            with self.subTest(mode=mode):
                adjusted = kline_payload("510300", 1, adjusted=True)
                if mode == "unsorted":
                    adjusted["data"]["klines"].reverse()
                else:
                    fields = adjusted["data"]["klines"][0].split(",")
                    fields[4] = "4.8"
                    adjusted["data"]["klines"][0] = ",".join(fields)

                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    payload = adjusted if query["fqt"] == ["1"] else kline_payload(
                        "510300", 1, adjusted=False,
                    )
                    return payload_bytes(payload)

                with self.assertRaises(SwingDataError):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )

    def test_completion_boundary_and_date_filters(self) -> None:
        dates = ("2026-08-28", "2026-08-31", "2026-09-01")

        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            return payload_bytes(kline_payload(
                "510300", 1, adjusted=query["fqt"] == ["1"], dates=dates,
            ))

        cases = (
            ("2026-08-31T15:09:59+08:00", date(2026, 9, 1), (date(2026, 8, 28),)),
            ("2026-08-31T15:10:00+08:00", date(2026, 9, 1), (date(2026, 8, 28), date(2026, 8, 31))),
            ("2026-08-31T16:00:00+08:00", date(2026, 8, 28), (date(2026, 8, 28),)),
        )
        for now_text, last_completed, expected in cases:
            with self.subTest(now=now_text, last_completed=last_completed):
                bars = self.collector(
                    transport, now=lambda value=now_text: datetime.fromisoformat(value),
                ).collect((SwingWatchItem("510300", True),), last_completed)
                self.assertEqual(tuple(bar.trading_date for bar in bars), expected)

    def test_accepts_one_sided_dates_excluded_by_completion_filters(self) -> None:
        cases = (
            (
                "raw-only future",
                ("2026-08-27", "2026-08-28", "2026-09-01"),
                ("2026-08-27", "2026-08-28"),
                "2026-08-31T15:10:00+08:00",
                date(2026, 9, 1),
            ),
            (
                "adjusted-only current incomplete",
                ("2026-08-27", "2026-08-28"),
                ("2026-08-27", "2026-08-28", "2026-08-31"),
                "2026-08-31T15:09:59+08:00",
                date(2026, 8, 31),
            ),
        )
        for label, raw_dates, adjusted_dates, now_text, last_completed in cases:
            with self.subTest(label=label):
                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    adjusted = query["fqt"] == ["1"]
                    return payload_bytes(kline_payload(
                        "510300",
                        1,
                        adjusted=adjusted,
                        dates=adjusted_dates if adjusted else raw_dates,
                    ))

                bars = self.collector(
                    transport,
                    now=lambda value=now_text: datetime.fromisoformat(value),
                ).collect((SwingWatchItem("510300", True),), last_completed)

                self.assertEqual(
                    tuple(bar.trading_date for bar in bars),
                    (date(2026, 8, 27), date(2026, 8, 28)),
                )
                self.assertEqual(
                    tuple(bar.previous_close for bar in bars),
                    (9.5, 10.5),
                )

    def test_rejects_raw_or_adjusted_duplicate_dates_before_filtering(self) -> None:
        cases = (
            (
                "raw future duplicate",
                ("2026-08-27", "2026-08-28", "2026-09-01", "2026-09-01"),
                ("2026-08-27", "2026-08-28"),
                "2026-08-31T15:10:00+08:00",
                date(2026, 9, 1),
            ),
            (
                "adjusted current-incomplete duplicate",
                ("2026-08-27", "2026-08-28"),
                ("2026-08-27", "2026-08-28", "2026-08-31", "2026-08-31"),
                "2026-08-31T15:09:59+08:00",
                date(2026, 8, 31),
            ),
        )
        for label, raw_dates, adjusted_dates, now_text, last_completed in cases:
            with self.subTest(label=label):
                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    adjusted = query["fqt"] == ["1"]
                    return payload_bytes(kline_payload(
                        "510300",
                        1,
                        adjusted=adjusted,
                        dates=adjusted_dates if adjusted else raw_dates,
                    ))

                with self.assertRaisesRegex(SwingDataError, "日期重复"):
                    self.collector(
                        transport,
                        now=lambda value=now_text: datetime.fromisoformat(value),
                    ).collect((SwingWatchItem("510300", True),), last_completed)

    def test_rejects_one_sided_retained_dates(self) -> None:
        cases = (
            (
                ("2026-08-27", "2026-08-28"),
                ("2026-08-27",),
            ),
            (
                ("2026-08-27",),
                ("2026-08-27", "2026-08-28"),
            ),
        )
        for raw_dates, adjusted_dates in cases:
            with self.subTest(raw_dates=raw_dates, adjusted_dates=adjusted_dates):
                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    adjusted = query["fqt"] == ["1"]
                    return payload_bytes(kline_payload(
                        "510300",
                        1,
                        adjusted=adjusted,
                        dates=adjusted_dates if adjusted else raw_dates,
                    ))

                with self.assertRaisesRegex(SwingDataError, "日期不匹配"):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )

    def test_adjusted_response_does_not_require_previous_close(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            adjusted = query["fqt"] == ["1"]
            payload = kline_payload("510300", 1, adjusted=adjusted)
            if adjusted:
                del payload["data"]["preKPrice"]
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28),
        )
        self.assertEqual(len(bars), 2)

    def test_previous_close_uses_response_continuity_when_future_suffix_is_filtered(self) -> None:
        dates = ("2026-08-27", "2026-08-28", "2026-08-31")

        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            return payload_bytes(kline_payload(
                "510300", 1, adjusted=query["fqt"] == ["1"],
                dates=dates, pre_k_price=9.5,
            ))

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28),
        )
        self.assertEqual(
            [(bar.trading_date, bar.previous_close) for bar in bars],
            [(date(2026, 8, 27), 9.5), (date(2026, 8, 28), 10.5)],
        )

    def test_first_retained_previous_close_uses_filtered_preceding_raw_row(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            adjusted = query["fqt"] == ["1"]
            payload = kline_payload("510300", 1, adjusted=adjusted)
            if adjusted:
                payload["data"]["klines"] = [
                    "2026-08-28,5,5.25,5.5,4.9,1000,10000,0,0,0,0",
                ]
            else:
                payload["data"]["klines"] = [
                    "2026-09-01,9,9.75,10,8.5,900,9000,0,0,0,0",
                    "2026-08-28,10,10.5,11,9.8,1000,10000,0,0,0,0",
                ]
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 9, 1),
        )

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].trading_date, date(2026, 8, 28))
        self.assertEqual(bars[0].previous_close, 9.75)

    def test_normalizes_aware_clock_to_shanghai_for_all_bars(self) -> None:
        utc_now = datetime(2026, 8, 31, 7, 10, tzinfo=timezone.utc)
        bars = self.collector(now=lambda: utc_now).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28),
        )
        self.assertEqual(bars[0].observed_at.isoformat(), "2026-08-31T15:10:00+08:00")

    def test_rejects_invalid_constructor_and_clock_values(self) -> None:
        for timeout in (0, -1, True, "8", math.inf, math.nan):
            with self.subTest(timeout=timeout):
                with self.assertRaises((TypeError, ValueError, SwingDataError)):
                    EastmoneyDailyCollector(timeout=timeout)
        for bad_now in (
            lambda: datetime(2026, 8, 31, 15, 10),
            lambda: date(2026, 8, 31),
            lambda: (_ for _ in ()).throw(RuntimeError("HOSTILE_CLOCK_SECRET")),
        ):
            with self.subTest(bad_now=bad_now):
                with self.assertRaises(SwingDataError) as caught:
                    self.collector(now=bad_now).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28),
                    )
                self.assertNotIn("HOSTILE_CLOCK_SECRET", str(caught.exception))

    def test_preserves_falsey_callable_dependencies(self) -> None:
        transport = FalseyCallable(b"{}")
        now = FalseyCallable(NOW)
        collector = EastmoneyDailyCollector(transport=transport, now=now)

        self.assertIs(collector.transport, transport)
        self.assertIs(collector.now, now)

    def test_rejects_invalid_call_inputs_without_transport_or_file_side_effects(self) -> None:
        requests = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request)
            return b"{}"

        invalid_calls = (
            ((), date(2026, 8, 28), 260),
            ((SwingWatchItem("510300", False),), date(2026, 8, 28), 260),
            ((SwingWatchItem("510300", True), SwingWatchItem("510300", True)), date(2026, 8, 28), 260),
            ((SwingWatchItem("510300", True), SwingWatchItem("510300", False)), date(2026, 8, 28), 260),
            ((SwingWatchItem("５１０３００", True),), date(2026, 8, 28), 260),
            ((SwingWatchItem("HOSTILE_WATCHLIST_SECRET", True),), date(2026, 8, 28), 260),
            ((SwingWatchItem("510300", 1),), date(2026, 8, 28), 260),
            ((object(),), date(2026, 8, 28), 260),
            (BrokenSequence(), date(2026, 8, 28), 260),
            ((SwingWatchItem("510300", True),), datetime(2026, 8, 28), 260),
            ((SwingWatchItem("510300", True),), date(2026, 8, 28), True),
            ((SwingWatchItem("510300", True),), date(2026, 8, 28), 0),
            ((SwingWatchItem("510300", True),), date(2026, 8, 28), 10001),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for watchlist, last_completed, count in invalid_calls:
                with self.subTest(watchlist=watchlist, last_completed=last_completed, count=count):
                    with self.assertRaises(SwingDataError) as caught:
                        self.collector(transport).collect(watchlist, last_completed, count)
                    self.assertNotIn("HOSTILE_WATCHLIST_SECRET", str(caught.exception))
            self.assertEqual(requests, [])
            self.assertEqual(__import__("os").listdir(temporary), [])

    def test_transport_decode_failure_falls_back_but_payload_value_errors_do_not(self) -> None:
        for primary_result in (b"not json", b"\xff"):
            with self.subTest(primary_result=primary_result):
                requests = []

                def transport(request: Request, timeout: float) -> bytes:
                    requests.append(request.full_url)
                    query = parse_qs(urlsplit(request.full_url).query)
                    if request.full_url.startswith(KLINE_ENDPOINT):
                        return primary_result
                    return payload_bytes(kline_payload(
                        "510300", 1, adjusted=query["fqt"] == ["1"],
                    ))

                bars = self.collector(transport).collect(
                    (SwingWatchItem("510300", True),), date(2026, 8, 28),
                )
                self.assertEqual(len(bars), 2)
                self.assertTrue(any(url.startswith(KLINE_FALLBACK_ENDPOINT) for url in requests))


if __name__ == "__main__":
    unittest.main()
