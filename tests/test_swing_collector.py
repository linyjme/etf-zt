from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import etf_rotation.eastmoney_client as eastmoney_client
from etf_rotation.constants import DEFAULT_SWING_HISTORY_COUNT
from etf_rotation.eastmoney_client import EastmoneyMarketError
from etf_rotation.swing_collector import (
    FIELDS1,
    FIELDS2,
    KLINE_ENDPOINT,
    KLINE_FALLBACK_ENDPOINT,
    TENCENT_KLINE_ENDPOINT,
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


def tencent_payload(
    symbol: str,
    *,
    adjusted: bool,
    dates: tuple[str, ...] = ("2026-08-26", "2026-08-27", "2026-08-28"),
) -> dict[str, object]:
    market_symbol = f"{'sh' if symbol.startswith(('5', '6')) else 'sz'}{symbol}"
    scale = 0.5 if adjusted else 1.0
    lines = []
    for offset, trading_day in enumerate(dates):
        lines.append([
            trading_day,
            str((9.0 + offset) * scale),
            str((9.5 + offset) * scale),
            str((10.0 + offset) * scale),
            str((8.0 + offset) * scale),
            str(1000 + offset),
        ])
    return {
        "code": 0,
        "msg": "",
        "data": {
            market_symbol: {
                "qfqday" if adjusted else "day": lines,
            },
        },
    }


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


class FalseyFixtureTransport(FixtureTransport):
    def __bool__(self) -> bool:
        return False


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


class HostileSymbol(str):
    def __str__(self) -> str:
        raise AssertionError("hostile __str__ called")

    def __repr__(self) -> str:
        raise AssertionError("hostile __repr__ called")

    def __format__(self, format_spec: str) -> str:
        raise AssertionError("hostile __format__ called")


class FakeUrlResponse:
    def __init__(self, body: bytes, content_length: str | None = None):
        self.body = body
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length
        self.read_limits: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        return None

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        return self.body


class SharedEastmoneyTransportTests(unittest.TestCase):
    def test_urlopen_transport_prechecks_and_bounds_response_bytes(self) -> None:
        request = Request("https://fixture.invalid/data")
        good = FakeUrlResponse(b"{}")
        declared_oversize = FakeUrlResponse(b"", "9")
        streamed_oversize = FakeUrlResponse(b"123456789")

        with (
            patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
            patch.object(eastmoney_client.shutil, "which", return_value=None),
            patch.object(eastmoney_client, "urlopen", return_value=good),
        ):
            self.assertEqual(eastmoney_client._default_transport(request, 2.0), b"{}")
        self.assertEqual(good.read_limits, [9])

        for response in (declared_oversize, streamed_oversize):
            with self.subTest(response=response):
                with (
                    patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
                    patch.object(eastmoney_client.shutil, "which", return_value=None),
                    patch.object(eastmoney_client, "urlopen", return_value=response),
                ):
                    with self.assertRaisesRegex(OSError, "大小限制"):
                        eastmoney_client._default_transport(request, 2.0)
        self.assertEqual(declared_oversize.read_limits, [])
        self.assertEqual(streamed_oversize.read_limits, [9])

        with (
            patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
            patch.object(eastmoney_client.shutil, "which", return_value=None),
            patch.object(
                eastmoney_client,
                "urlopen",
                side_effect=OSError("HOSTILE_URLOPEN_SECRET"),
            ),
        ):
            with self.assertRaises(OSError) as caught:
                eastmoney_client._default_transport(request, 2.0)
        self.assertNotIn("HOSTILE_URLOPEN_SECRET", str(caught.exception))

    def test_curl_transport_sets_limit_and_rejects_oversize_stdout_safely(self) -> None:
        request = Request("https://fixture.invalid/HOSTILE_URL_SECRET")
        completed = SimpleNamespace(
            stdout=b"123456789",
            stderr=b"HOSTILE_STDERR_SECRET",
            returncode=0,
        )
        with (
            patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
            patch.object(eastmoney_client.shutil, "which", return_value="curl.exe"),
            patch.object(eastmoney_client.subprocess, "run", return_value=completed) as run,
            patch.object(eastmoney_client.time, "sleep"),
            patch.object(
                eastmoney_client,
                "_powershell_transport",
                side_effect=OSError("HOSTILE_POWERSHELL_SECRET"),
            ),
        ):
            with self.assertRaises(OSError) as caught:
                eastmoney_client._default_transport(request, 2.0)

        self.assertNotIn("HOSTILE_STDERR_SECRET", str(caught.exception))
        self.assertNotIn("HOSTILE_POWERSHELL_SECRET", str(caught.exception))
        for call in run.call_args_list:
            command = call.args[0]
            self.assertIn("--max-filesize", command)
            self.assertEqual(command[command.index("--max-filesize") + 1], "8")

        with (
            patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
            patch.object(eastmoney_client.shutil, "which", return_value="curl.exe"),
            patch.object(
                eastmoney_client.subprocess,
                "run",
                side_effect=OSError("HOSTILE_CURL_PROCESS_SECRET"),
            ),
            patch.object(eastmoney_client.time, "sleep"),
            patch.object(
                eastmoney_client,
                "_powershell_transport",
                side_effect=OSError("HOSTILE_POWERSHELL_SECRET"),
            ),
        ):
            with self.assertRaises(OSError) as caught:
                eastmoney_client._default_transport(request, 2.0)
        self.assertNotIn("HOSTILE_CURL_PROCESS_SECRET", str(caught.exception))
        self.assertNotIn("HOSTILE_POWERSHELL_SECRET", str(caught.exception))

    def test_curl_nonzero_exit_never_accepts_valid_json_stdout(self) -> None:
        request = Request("https://fixture.invalid/data")
        curl_stdout = b'{"rc":0,"partial":true}'
        fallback = b'{"rc":0,"fallback":true}'
        completed = SimpleNamespace(
            stdout=curl_stdout,
            stderr=b"HOSTILE_CURLE_FILESIZE_SECRET",
            returncode=63,
        )
        with (
            patch.object(eastmoney_client.shutil, "which", return_value="curl.exe"),
            patch.object(eastmoney_client.subprocess, "run", return_value=completed) as run,
            patch.object(eastmoney_client.time, "sleep"),
            patch.object(
                eastmoney_client,
                "_powershell_transport",
                return_value=fallback,
            ) as powershell,
        ):
            result = eastmoney_client._default_transport(request, 2.0)

        self.assertEqual(result, fallback)
        self.assertNotEqual(result, curl_stdout)
        self.assertEqual(run.call_count, 3)
        powershell.assert_called_once_with(request, 2.0)

    def test_powershell_transport_streams_with_encoded_inputs_and_bounds_output(self) -> None:
        request = Request(
            "https://fixture.invalid/HOSTILE_URL_SECRET",
            headers={"Accept": "application/json", "X-Fixture": "header-value"},
        )
        completed = SimpleNamespace(stdout=base64.b64encode(b"{}") + b"\n")
        with (
            patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
            patch.object(eastmoney_client.subprocess, "run", return_value=completed) as run,
        ):
            self.assertEqual(eastmoney_client._powershell_transport(request, 2.0), b"{}")

        command = run.call_args.args[0]
        script = command[-1]
        self.assertIn("ResponseHeadersRead", script)
        self.assertIn("ReadAsync", script)
        self.assertIn("$max=8", script)
        self.assertIn("ConvertFrom-Json", script)
        self.assertNotIn(request.full_url, script)
        self.assertNotIn("header-value", script)

        oversized_outputs = (
            b"A" * 13,
            base64.b64encode(b"123456789"),
        )
        for stdout in oversized_outputs:
            with self.subTest(stdout_length=len(stdout)):
                with (
                    patch.object(eastmoney_client, "MAX_RESPONSE_BYTES", 8),
                    patch.object(
                        eastmoney_client.subprocess,
                        "run",
                        return_value=SimpleNamespace(stdout=stdout),
                    ),
                ):
                    with self.assertRaisesRegex(OSError, "大小限制"):
                        eastmoney_client._powershell_transport(request, 2.0)

        with patch.object(
            eastmoney_client.subprocess,
            "run",
            side_effect=OSError("HOSTILE_POWERSHELL_PROCESS_SECRET"),
        ):
            with self.assertRaises(OSError) as caught:
                eastmoney_client._powershell_transport(request, 2.0)
        self.assertNotIn("HOSTILE_POWERSHELL_PROCESS_SECRET", str(caught.exception))

    def test_shared_mapper_never_formats_non_builtin_string(self) -> None:
        with self.assertRaisesRegex(EastmoneyMarketError, "6位数字"):
            eastmoney_client.market_for_symbol(HostileSymbol("510300"))


class EastmoneyDailyCollectorTests(unittest.TestCase):
    def collector(self, transport=None, now=None, timeout: float = 8.0):
        return EastmoneyDailyCollector(
            timeout=timeout,
            transport=FixtureTransport() if transport is None else transport,
            now=(lambda: NOW) if now is None else now,
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
        self.assertEqual(
            {
                parse_qs(urlsplit(request.full_url).query)["lmt"][0]
                for request, _ in transport.requests
            },
            {str(DEFAULT_SWING_HISTORY_COUNT)},
        )

    def test_normalizes_independently_rounded_adjusted_ohlc_to_one_scale(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            payload = kline_payload(
                "510300", 1, adjusted=query["fqt"] == ["1"],
            )
            if query["fqt"] == ["1"]:
                # Eastmoney rounds each adjusted field independently. The
                # close-derived factor remains authoritative within one tick.
                payload["data"]["klines"][0] = (
                    "2026-08-27,5.004,5.25,5.506,4.894,1000,10000,0,0,0,0"
                )
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
        )

        self.assertEqual(len(bars), 2)
        first = bars[0]
        scale = first.adjusted_close / first.close
        self.assertAlmostEqual(first.adjusted_open / first.open, scale)
        self.assertAlmostEqual(first.adjusted_high / first.high, scale)
        self.assertAlmostEqual(first.adjusted_low / first.low, scale)
        self.assertEqual(first.adjusted_open, first.open * scale)

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

    def test_rejects_response_with_more_rows_than_requested_count(self) -> None:
        transport = FixtureTransport()
        with self.assertRaisesRegex(SwingDataError, "count"):
            self.collector(transport).collect(
                (SwingWatchItem("510300", True),),
                date(2026, 8, 28),
                count=1,
            )
        self.assertEqual(len(transport.requests), 1)

    def test_primary_request_failure_retries_only_that_symbol_from_fallback(self) -> None:
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
            ("push2delay.eastmoney.com", "0.159915", "0"),
            ("push2delay.eastmoney.com", "0.159915", "1"),
        ])
        by_symbol = {bar.symbol: bar.source for bar in bars}
        self.assertEqual(by_symbol["510300"], "东方财富 kline (push2his.eastmoney.com)")
        self.assertEqual(by_symbol["159915"], "东方财富 kline (push2delay.eastmoney.com)")

    def test_both_request_failures_use_tencent_raw_and_qfq_final_fallback(self) -> None:
        requests: list[str] = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request.full_url)
            if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                raise OSError("HOSTILE_TRANSPORT_SECRET")
            param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
            symbol = param[0][2:]
            return payload_bytes(tencent_payload(
                symbol,
                adjusted=param[-1] == "qfq",
            ))

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
        )

        self.assertEqual(len(requests), 4)
        tencent_requests = requests[-2:]
        self.assertTrue(all(url.startswith(TENCENT_KLINE_ENDPOINT) for url in tencent_requests))
        params = [parse_qs(urlsplit(url).query)["param"][0].split(",") for url in tencent_requests]
        self.assertEqual(params, [
            ["sh510300", "day", "", "2026-08-28", "3", ""],
            ["sh510300", "day", "", "2026-08-28", "3", "qfq"],
        ])
        self.assertEqual([bar.trading_date for bar in bars], [date(2026, 8, 27), date(2026, 8, 28)])
        self.assertEqual([bar.previous_close for bar in bars], [9.5, 10.5])
        self.assertEqual([bar.adjusted_close for bar in bars], [5.25, 5.75])
        self.assertAlmostEqual(bars[0].amount, (10.0 + 11.0 + 9.0 + 10.5) / 4 * 1001 * 100)
        self.assertEqual({bar.source for bar in bars}, {
            "腾讯 fqkline 原始+前复权 (web.ifzq.gtimg.cn); amount=OHLC均价×成交量(手)×100估算",
        })

    def test_tencent_qfq_request_accepts_day_when_qfqday_is_absent(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                raise OSError("eastmoney down")
            payload = tencent_payload("563360", adjusted=False)
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("563360", True),), date(2026, 8, 28), count=2,
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual(
            [bar.adjusted_close for bar in bars],
            [bar.close for bar in bars],
        )

    def test_tencent_qfqday_takes_priority_over_day(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                raise OSError("eastmoney down")
            param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
            adjusted = param[-1] == "qfq"
            payload = tencent_payload("510300", adjusted=adjusted)
            if adjusted:
                payload["data"]["sh510300"]["day"] = tencent_payload(
                    "510300", adjusted=False,
                )["data"]["sh510300"]["day"]
                payload["data"]["sh510300"]["qfqday"] = []
            return payload_bytes(payload)

        with self.assertRaisesRegex(SwingDataError, "腾讯最终回退失败"):
            self.collector(transport).collect(
                (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
            )

    def test_all_three_sources_fail_with_safe_context_and_preserve_cause(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            raise OSError("HOSTILE_TRANSPORT_SECRET")

        with self.assertRaises(SwingDataError) as caught:
            self.collector(transport).collect(
                (SwingWatchItem("510300", True),), date(2026, 8, 28),
            )
        message = str(caught.exception)
        self.assertIn("主备端点请求均失败且腾讯最终回退失败", message)
        self.assertIn("push2his.eastmoney.com", message)
        self.assertIn("push2delay.eastmoney.com", message)
        self.assertIn("web.ifzq.gtimg.cn", message)
        self.assertNotIn("HOSTILE_TRANSPORT_SECRET", message)
        self.assertIsNotNone(caught.exception.__cause__)

    def test_tencent_final_fallback_strictly_rejects_malformed_raw_and_qfq(self) -> None:
        cases = []
        missing_qfq = tencent_payload("510300", adjusted=True)
        missing_qfq["data"]["sh510300"] = {"day": []}
        cases.append(("missing qfq", missing_qfq, True))
        extra_field = tencent_payload("510300", adjusted=False)
        extra_field["data"]["sh510300"]["day"][1].append("unexpected")
        cases.append(("extra field", extra_field, False))
        wrong_code = tencent_payload("159915", adjusted=False)
        cases.append(("wrong code", wrong_code, False))
        duplicate = tencent_payload("510300", adjusted=True)
        duplicate["data"]["sh510300"]["qfqday"][2][0] = "2026-08-27"
        cases.append(("duplicate date", duplicate, True))
        nonfinite = tencent_payload("510300", adjusted=False)
        nonfinite["data"]["sh510300"]["day"][1][2] = "nan"
        cases.append(("nonfinite", nonfinite, False))

        for label, bad_payload, bad_adjusted in cases:
            with self.subTest(label=label):
                def transport(request: Request, timeout: float) -> bytes:
                    if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                        raise OSError("eastmoney down")
                    param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
                    adjusted = param[-1] == "qfq"
                    payload = bad_payload if adjusted == bad_adjusted else tencent_payload(
                        "510300", adjusted=adjusted,
                    )
                    return payload_bytes(payload)

                with self.assertRaisesRegex(SwingDataError, "腾讯最终回退失败"):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
                    )

    def test_tencent_final_fallback_completes_adjusted_suffix_when_provider_lags(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                raise OSError("eastmoney down")
            param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
            adjusted = param[-1] == "qfq"
            dates = (
                ("2026-08-26", "2026-08-27")
                if adjusted else ("2026-08-26", "2026-08-27", "2026-08-28")
            )
            return payload_bytes(tencent_payload("510300", adjusted=adjusted, dates=dates))

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
        )
        self.assertEqual([bar.trading_date for bar in bars], [date(2026, 8, 27), date(2026, 8, 28)])

    def test_tencent_final_fallback_rejects_interior_date_mismatch_or_scale(self) -> None:
        for mode in ("dates", "scale"):
            with self.subTest(mode=mode):
                def transport(request: Request, timeout: float) -> bytes:
                    if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                        raise OSError("eastmoney down")
                    param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
                    adjusted = param[-1] == "qfq"
                    dates = (
                        ("2026-08-26", "2026-08-28")
                        if adjusted and mode == "dates"
                        else ("2026-08-26", "2026-08-27", "2026-08-28")
                    )
                    payload = tencent_payload("510300", adjusted=adjusted, dates=dates)
                    if adjusted and mode == "scale":
                        payload["data"]["sh510300"]["qfqday"][1][4] = "4.4"
                    return payload_bytes(payload)

                with self.assertRaisesRegex(SwingDataError, "腾讯最终回退失败"):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
                    )

    def test_tencent_final_fallback_accepts_two_decimal_rounding_on_scale(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                raise OSError("eastmoney down")
            param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
            adjusted = param[-1] == "qfq"
            payload = tencent_payload("510300", adjusted=adjusted)
            if adjusted:
                payload["data"]["sh510300"]["qfqday"][1][1] = "5.005"
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
        )
        self.assertEqual(len(bars), 2)
        self.assertTrue(all("腾讯" in bar.source for bar in bars))

    def test_tencent_uses_adjusted_as_raw_when_unadjusted_has_corporate_action_gap(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            if not request.full_url.startswith(TENCENT_KLINE_ENDPOINT):
                raise OSError("eastmoney down")
            param = parse_qs(urlsplit(request.full_url).query)["param"][0].split(",")
            adjusted = param[-1] == "qfq"
            payload = tencent_payload("510300", adjusted=adjusted)
            if not adjusted:
                payload["data"]["sh510300"]["day"][2][1:5] = ["2.0", "2.1", "2.2", "1.9"]
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
        )
        self.assertEqual(len(bars), 2)
        self.assertTrue(all("一致前复权序列" in bar.source for bar in bars))
        self.assertEqual(bars[0].close, bars[0].adjusted_close)
        self.assertEqual(bars[1].close, bars[1].adjusted_close)
        self.assertEqual(bars[1].previous_close, bars[0].close)

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

    def test_accepts_eastmoney_subtractive_adjustment_and_keeps_provider_amount(self) -> None:
        # Real 510300 bars for 2023-08-28 / 2024-10-08: Eastmoney subtracts the
        # cumulative distribution (0.280 / 0.211) from every field, so the
        # open/high/low sit 12-18 ticks away from the close-derived scale.
        raw_lines = (
            "2023-08-28,4.011,3.837,4.011,3.810,1000,2624224493.0,0,0,0,0",
            "2024-10-08,4.656,4.412,4.656,4.208,1100,2987237086.0,0,0,0,0",
        )
        adjusted_lines = (
            "2023-08-28,3.731,3.557,3.731,3.530,1000,2624224493.0,0,0,0,0",
            "2024-10-08,4.445,4.201,4.445,3.997,1100,2987237086.0,0,0,0,0",
        )

        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertIn(urlsplit(request.full_url).netloc, {urlsplit(KLINE_ENDPOINT).netloc})
            payload = kline_payload("510300", 1, adjusted=query["fqt"] == ["1"])
            payload["data"]["klines"] = list(
                adjusted_lines if query["fqt"] == ["1"] else raw_lines
            )
            return payload_bytes(payload)

        bars = self.collector(transport).collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28), count=2,
        )

        self.assertEqual(len(bars), 2)
        for bar, expected_close in zip(bars, (3.557, 4.201)):
            self.assertTrue(bar.source.startswith("东方财富 kline ("))
            self.assertEqual(bar.adjusted_close, expected_close)
            scale = bar.adjusted_close / bar.close
            self.assertEqual(bar.adjusted_open, bar.open * scale)
            self.assertEqual(bar.adjusted_low, bar.low * scale)
        self.assertEqual(bars[0].amount, 2624224493.0)

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

    def test_rejects_raw_or_adjusted_inversion_before_filtering(self) -> None:
        for inverted_adjusted in (False, True):
            with self.subTest(inverted_adjusted=inverted_adjusted):
                def transport(request: Request, timeout: float) -> bytes:
                    query = parse_qs(urlsplit(request.full_url).query)
                    adjusted = query["fqt"] == ["1"]
                    dates = (
                        ("2026-09-01", "2026-08-28")
                        if adjusted == inverted_adjusted
                        else ("2026-08-28",)
                    )
                    return payload_bytes(kline_payload(
                        "510300", 1, adjusted=adjusted, dates=dates,
                    ))

                with self.assertRaisesRegex(SwingDataError, "严格递增"):
                    self.collector(transport).collect(
                        (SwingWatchItem("510300", True),), date(2026, 9, 1),
                    )

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
        transport = FalseyFixtureTransport()
        now = FalseyCallable(NOW)
        collector = self.collector(transport=transport, now=now)

        bars = collector.collect(
            (SwingWatchItem("510300", True),), date(2026, 8, 28),
        )

        self.assertEqual(len(bars), 2)
        self.assertEqual(len(transport.requests), 2)

    def test_daily_collector_never_formats_hostile_symbol_subclass(self) -> None:
        with self.assertRaisesRegex(SwingDataError, "代码或市场无效"):
            self.collector().collect(
                (SwingWatchItem(HostileSymbol("510300"), True),),
                date(2026, 8, 28),
            )

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


class EnvironmentIndexCollectionTests(unittest.TestCase):
    def test_tencent_symbol_forces_shanghai_for_environment_indices(self) -> None:
        from etf_rotation.swing_collector import ENVIRONMENT_INDEX_PREFIX, tencent_market_symbol

        self.assertEqual(tencent_market_symbol("000300"), "sz000300")
        self.assertEqual(
            tencent_market_symbol("000300", ENVIRONMENT_INDEX_PREFIX["000300"]),
            "sh000300",
        )
        self.assertEqual(
            tencent_market_symbol("000852", ENVIRONMENT_INDEX_PREFIX["000852"]),
            "sh000852",
        )

    def test_collect_indices_requests_shanghai_codes_and_stays_out_of_etf_history(self) -> None:
        from etf_rotation.swing_data import IndexHistoryStore

        requests: list[str] = []

        def transport(request: Request, timeout: float) -> bytes:
            requests.append(request.full_url)
            query = parse_qs(urlsplit(request.full_url).query)
            param = query["param"][0]
            market_symbol = param.split(",", 1)[0]
            adjusted = param.endswith(",qfq")
            symbol = market_symbol[2:]
            payload = tencent_payload(symbol, adjusted=adjusted)
            body = next(iter(payload["data"].values()))
            payload["data"] = {market_symbol: body}
            return json.dumps(payload).encode("utf-8")

        bars = EastmoneyDailyCollector(
            transport=transport, now=lambda: NOW,
        ).collect_indices(date(2026, 8, 28), count=10)
        self.assertEqual({bar.symbol for bar in bars}, {"000300", "000852"})
        self.assertTrue(requests)
        self.assertTrue(all("sh000300" in url or "sh000852" in url for url in requests))
        self.assertFalse(any("sz000300" in url or "sz000852" in url for url in requests))
        with tempfile.TemporaryDirectory() as directory:
            store = IndexHistoryStore(Path(directory) / "index_quotes.jsonl")
            stored = store.upsert(bars)
            self.assertEqual(
                {bar.symbol for bar in store.load()},
                {"000300", "000852"},
            )
            self.assertEqual(stored[-1].symbol, "000852")


if __name__ == "__main__":
    unittest.main()
