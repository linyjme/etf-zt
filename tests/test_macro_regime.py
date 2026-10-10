from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from etf_rotation.macro_regime import (
    ERP_TIERS,
    SERIES_CN10Y,
    SERIES_CSI300_PE,
    STYLE_TIERS,
    MacroCollector,
    MacroRegimeError,
    MacroRegimeService,
    MacroSeriesStore,
    SeriesPoint,
    combine_guidance,
    compute_erp,
    compute_style_ratio,
    percentile_rank,
)
from tests import test_swing_web as web_fixtures
from tests.swing_helpers import swing_strategy_bars


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 10, 10, 0, tzinfo=SHANGHAI)


def _trading_days(count: int, end: date = date(2026, 10, 9)) -> list[date]:
    days: list[date] = []
    day = end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def _points(series: str, values: dict[date, float]) -> dict[date, SeriesPoint]:
    return {
        day: SeriesPoint(series, day, value, "test", NOW.isoformat())
        for day, value in values.items()
    }


class IndicatorMathTests(unittest.TestCase):
    def test_percentile_rank_counts_strictly_below(self) -> None:
        self.assertEqual(percentile_rank([1, 2, 3, 4], 3), 50.0)
        self.assertEqual(percentile_rank([1, 2, 3, 4], 5), 100.0)
        self.assertEqual(percentile_rank([1, 2, 3, 4], 1), 0.0)
        with self.assertRaises(MacroRegimeError):
            percentile_rank([], 1.0)

    def test_erp_tier_follows_percentile_and_rate_correction_downgrades(self) -> None:
        days = _trading_days(2000)
        # PE drifts 10 -> 16 over the window; the yield is flat at 3%, then the
        # latest day has the same PE but a yield collapse to 1.5%.
        pe = {day: 10.0 + 6.0 * index / len(days) for index, day in enumerate(days)}
        yields = {day: 3.0 for day in days}
        latest = days[-1]
        pe[latest] = 13.0
        base = compute_erp(_points(SERIES_CSI300_PE, pe), _points(SERIES_CN10Y, yields), minimum_days=1000)
        self.assertEqual(base["status"], "OK")
        self.assertEqual(base["as_of"], latest.isoformat())
        self.assertAlmostEqual(base["erp_pct"], 100 / 13.0 - 3.0, places=4)
        self.assertFalse(base["rate_driven"])
        self.assertEqual(base["tier"], base["raw_tier"])

        yields[latest] = 1.5
        corrected = compute_erp(_points(SERIES_CSI300_PE, pe), _points(SERIES_CN10Y, yields), minimum_days=1000)
        self.assertGreater(corrected["erp_percentile"], base["erp_percentile"])
        self.assertEqual(corrected["pe_percentile"], base["pe_percentile"])
        self.assertTrue(corrected["rate_driven"])
        keys = [row[1] for row in ERP_TIERS]
        self.assertEqual(keys.index(corrected["tier"]), min(keys.index(corrected["raw_tier"]) + 1, len(keys) - 1))
        self.assertEqual(corrected["cn10y_as_of"], latest.isoformat())

    def test_erp_uses_latest_yield_on_or_before_each_pe_date(self) -> None:
        days = _trading_days(1200)
        pe = {day: 12.0 for day in days}
        # Yields only published on the first day and a non-trading Saturday.
        saturday = days[-1] + timedelta(days=(5 - days[-1].weekday()) % 7 or 7) - timedelta(days=7)
        yields = {days[0]: 2.0, saturday: 1.0}
        result = compute_erp(_points(SERIES_CSI300_PE, pe), _points(SERIES_CN10Y, yields), minimum_days=1000)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["cn10y_yield_pct"], 1.0)
        self.assertEqual(result["cn10y_as_of"], saturday.isoformat())

    def test_erp_fails_closed_without_enough_history(self) -> None:
        days = _trading_days(300)
        result = compute_erp(
            _points(SERIES_CSI300_PE, {d: 12.0 for d in days}),
            _points(SERIES_CN10Y, {d: 2.0 for d in days}),
        )
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["reason"], "INSUFFICIENT_HISTORY")
        self.assertEqual(compute_erp({}, {})["reason"], "MISSING_SERIES")

    def test_style_ratio_tiers_and_switch_confirmation(self) -> None:
        growth = list(swing_strategy_bars(600, symbol="159915"))
        dividend = list(swing_strategy_bars(600, symbol="515180"))
        # Growth outruns dividend into the last session: percentile near 100.
        from dataclasses import replace
        growth = [replace(bar, adjusted_close=bar.adjusted_close * (1 + index / 300)) for index, bar in enumerate(growth)]
        hot = compute_style_ratio(growth, dividend, minimum_days=500)
        self.assertEqual(hot["status"], "OK")
        self.assertEqual(hot["tier"], "GROWTH_HOT")
        self.assertEqual(hot["growth_multiplier"], 0.5)
        self.assertTrue(hot["above_ma20"])
        self.assertFalse(hot["switch_confirmed"])

        # Collapse the last 25 sessions below the moving average with the
        # average turning down: still top-decile level, switch now confirmed.
        for index in range(575, 600):
            growth[index] = replace(growth[index], adjusted_close=growth[574].adjusted_close * (1 - (index - 574) * 0.0004))
        confirmed = compute_style_ratio(growth, dividend, minimum_days=500)
        self.assertEqual(confirmed["tier"], "GROWTH_HOT")
        self.assertFalse(confirmed["above_ma20"])
        self.assertTrue(confirmed["ma20_falling"])
        self.assertTrue(confirmed["switch_confirmed"])

        # Growth collapsing relative to dividend: bottom tier, no confirmation needed.
        weak = [replace(bar, adjusted_close=bar.adjusted_close * (2 - index / 300)) for index, bar in enumerate(swing_strategy_bars(600, symbol="159915"))]
        result = compute_style_ratio(weak, dividend, minimum_days=500)
        self.assertEqual(result["tier"], "GROWTH_WEAK")
        self.assertEqual(result["growth_multiplier"], 1.5)
        self.assertEqual(result["dividend_multiplier"], 0.8)

    def test_style_ratio_fails_closed_on_short_or_missing_history(self) -> None:
        short = compute_style_ratio(swing_strategy_bars(100, symbol="159915"), swing_strategy_bars(100, symbol="515180"))
        self.assertEqual(short["reason"], "INSUFFICIENT_HISTORY")
        self.assertEqual(compute_style_ratio((), ())["reason"], "MISSING_SERIES")

    def test_combined_guidance_matrix(self) -> None:
        erp = {"status": "OK", "tier": "CHEAP", "dca_multiplier": 1.5}
        style = {"status": "OK", "tier": "GROWTH_WARM", "growth_multiplier": 0.8, "dividend_multiplier": 1.1}
        result = combine_guidance(erp, style)
        self.assertEqual((result["total_action"], result["style_lean"]), ("ADD", "DIVIDEND"))
        self.assertEqual(result["summary"], "加仓，新增资金主投红利")
        self.assertEqual(result["dca_total_multiplier"], 1.5)
        self.assertEqual(combine_guidance({"status": "UNAVAILABLE"}, style)["status"], "UNAVAILABLE")
        reduce = combine_guidance({**erp, "tier": "VERY_RICH"}, {**style, "tier": "GROWTH_WEAK"})
        self.assertEqual(reduce["summary"], "减仓，先减红利留成长")
        self.assertEqual(len(STYLE_TIERS), 5)


def _yield_payload(rows: list[tuple[str, float | None]]) -> bytes:
    return json.dumps({"result": {"pages": 1, "count": len(rows), "data": [
        {"SOLAR_DATE": f"{day} 00:00:00", "EMM00166466": value, "EMM00166462": 1.4}
        for day, value in rows
    ]}}).encode("utf-8")


def _csindex_payload(rows: list[tuple[str, float | None]], code: str = "000300") -> bytes:
    return json.dumps({"code": "200", "msg": "Success", "data": [
        {"tradeDate": day, "indexCode": code, "close": 4600.0, "peg": value} for day, value in rows
    ]}).encode("utf-8")


class CollectorTests(unittest.TestCase):
    def test_treasury_yield_skips_null_rows_and_labels_source(self) -> None:
        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["reportName"], ["RPTA_WEB_TREASURYYIELD"])
            return _yield_payload([("2026-10-09", 1.6864), ("2026-10-07", None), ("2026-10-08", 1.6899)])

        points = MacroCollector(transport=transport, now=lambda: NOW).collect_treasury_yield(pages=1)
        self.assertEqual([(p.observed_date.isoformat(), p.value) for p in points], [("2026-10-08", 1.6899), ("2026-10-09", 1.6864)])
        self.assertTrue(all(p.series == SERIES_CN10Y and "EMM00166466" in p.source for p in points))

    def test_treasury_yield_rejects_malformed_values(self) -> None:
        for payload in (_yield_payload([("2026-10-09", -1.0)]), b"{}", b"not json", _yield_payload([])):
            with self.subTest(payload=payload[:20]):
                with self.assertRaises(MacroRegimeError):
                    MacroCollector(transport=lambda r, t: payload).collect_treasury_yield(pages=1)

    def test_csi300_pe_is_fetched_per_calendar_year_and_validated(self) -> None:
        requests: list[tuple[str, str]] = []

        def transport(request: Request, timeout: float) -> bytes:
            query = parse_qs(urlsplit(request.full_url).query)
            requests.append((query["startDate"][0], query["endDate"][0]))
            self.assertEqual(query["indexCode"], ["000300"])
            year = query["startDate"][0][:4]
            return _csindex_payload([(f"{year}0105", 12.5), (f"{year}0106", None), (f"{year}0107", 12.7)])

        points = MacroCollector(transport=transport).collect_csi300_pe(start=date(2024, 6, 1), end=date(2026, 10, 10))
        self.assertEqual(requests, [("20240601", "20241231"), ("20250101", "20251231"), ("20260101", "20261010")])
        self.assertEqual(len(points), 6)
        self.assertEqual(points[0].observed_date, date(2024, 1, 5))
        self.assertEqual(points[-1].value, 12.7)
        with self.assertRaises(MacroRegimeError):
            MacroCollector(transport=lambda r, t: _csindex_payload([("20260105", 12.5)], code="000905")).collect_csi300_pe(start=date(2026, 1, 1), end=date(2026, 1, 31))
        with self.assertRaises(MacroRegimeError):
            MacroCollector(transport=lambda r, t: b'{"code":"500","data":null}').collect_csi300_pe(start=date(2026, 1, 1), end=date(2026, 1, 31))


class StoreAndServiceTests(unittest.TestCase):
    def test_store_round_trip_replaces_changed_values_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MacroSeriesStore(Path(directory) / "nested" / "macro.jsonl")
            first = SeriesPoint(SERIES_CN10Y, date(2026, 10, 9), 1.68, "src", NOW.isoformat())
            store.upsert((first,))
            store.upsert((SeriesPoint(SERIES_CN10Y, date(2026, 10, 9), 1.70, "src", NOW.isoformat()),))
            loaded = store.load()
            self.assertEqual(loaded[SERIES_CN10Y][date(2026, 10, 9)].value, 1.70)
            self.assertEqual(loaded[SERIES_CSI300_PE], {})
            store.path.write_text('{"series":"BOGUS"}\n', encoding="utf-8")
            with self.assertRaises(MacroRegimeError):
                store.load()

    def test_service_refreshes_once_per_interval_and_records_failures(self) -> None:
        calls = {"yield": 0, "pe": 0}

        class Collector:
            def collect_treasury_yield(self, **kwargs):
                calls["yield"] += 1
                days = _trading_days(1900)
                return tuple(SeriesPoint(SERIES_CN10Y, d, 2.0, "y", NOW.isoformat()) for d in days)

            def collect_csi300_pe(self, *, start, end):
                calls["pe"] += 1
                raise OSError("csindex down")

        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "daily_quotes.jsonl"
            bars = (*swing_strategy_bars(600, symbol="159915"), *swing_strategy_bars(600, symbol="515180"))
            history.write_text("".join(json.dumps(bar.to_dict(), ensure_ascii=False) + "\n" for bar in bars), encoding="utf-8")
            service = MacroRegimeService(Path(directory) / "macro.jsonl", history, Collector(), clock=lambda: NOW)
            snapshot = service.snapshot()
            self.assertEqual(calls, {"yield": 1, "pe": 1})
            self.assertEqual(snapshot["errors"], {SERIES_CSI300_PE: "OSError"})
            self.assertEqual(snapshot["erp"]["status"], "UNAVAILABLE")
            self.assertEqual(snapshot["style_ratio"]["status"], "OK")
            self.assertEqual(snapshot["combined"]["status"], "UNAVAILABLE")
            self.assertEqual(snapshot["series_coverage"][SERIES_CN10Y]["count"], 1900)
            self.assertTrue(snapshot["read_only"])
            # Within the refresh interval the stale PE series does not trigger another fetch.
            service.snapshot()
            self.assertEqual(calls, {"yield": 1, "pe": 1})

    def test_service_backfills_once_then_fetches_only_the_recent_tail(self) -> None:
        requests: list[tuple[str, object]] = []
        today = NOW.date()

        class Collector:
            def collect_treasury_yield(self, *, pages=6, page_size=500):
                requests.append(("yield", pages))
                days = _trading_days(pages * page_size, end=today)
                return tuple(SeriesPoint(SERIES_CN10Y, d, 2.0, "y", NOW.isoformat()) for d in days)

            def collect_csi300_pe(self, *, start, end):
                requests.append(("pe", (start, end)))
                days = [d for d in _trading_days(3000, end=today) if start <= d <= end]
                return tuple(SeriesPoint(SERIES_CSI300_PE, d, 12.0, "p", NOW.isoformat()) for d in days)

        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / "macro.jsonl"
            backfill_start = today - timedelta(days=11 * 365)
            service = MacroRegimeService(store_path, Path(directory) / "missing.jsonl", Collector(), clock=lambda: NOW)
            service.refresh()
            self.assertEqual(requests, [("yield", 6), ("pe", (backfill_start, today))])
            # A second service over the same store sees eleven years of
            # history and only re-fetches the recent tail of both series.
            stored_pe = MacroSeriesStore(store_path).load()[SERIES_CSI300_PE]
            later = MacroRegimeService(store_path, Path(directory) / "missing.jsonl", Collector(), clock=lambda: NOW)
            later.refresh()
            self.assertEqual(requests[2], ("yield", 1))
            self.assertEqual(requests[3], ("pe", (max(stored_pe) - timedelta(days=14), today)))
            self.assertEqual(later.errors, {})

    def test_service_without_collector_is_read_only_and_never_fetches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = MacroRegimeService(Path(directory) / "macro.jsonl", Path(directory) / "missing.jsonl", None, clock=lambda: NOW)
            snapshot = service.snapshot()
            self.assertEqual(snapshot["erp"]["reason"], "MISSING_SERIES")
            self.assertEqual(snapshot["style_ratio"]["reason"], "MISSING_SERIES")
            self.assertEqual(snapshot["errors"], {})
            self.assertEqual(service.refresh(), {})


class MacroWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = web_fixtures.SwingWebTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_page_api_and_navigation(self) -> None:
        status, body, content_type = self.fixture._get("/macro")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("股债性价比", body)
        self.assertIn('href="/macro" aria-current="page"', body)
        status, payload, _ = self.fixture._get("/api/macro")
        self.assertEqual(status, 200)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["mode"], "GUIDANCE_ONLY")
        self.assertIn(payload["erp"]["status"], ("OK", "UNAVAILABLE"))
        self.assertEqual(len(payload["erp_tiers"]), 5)
        for path in ("/", "/swing", "/pr", "/notifications"):
            _, page, _ = self.fixture._get(path)
            self.assertIn('href="/macro"', page)
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as caught:
            self.fixture._get("/api/macro?x=1")
        self.assertEqual(caught.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
