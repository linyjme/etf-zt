from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from etf_rotation.swing_crosscheck import (
    CROSSCHECK_VERSION,
    CrosscheckError,
    IndependentBar,
    adjustment_events,
    canonical_source_is_independent,
    crosscheck_history,
    load_receipts,
    receipt_applies,
    write_receipts,
)
from etf_rotation.swing_data import DailyBar


SHANGHAI = timezone(timedelta(hours=8))
CHECKED_AT = datetime(2026, 9, 23, 17, 0, tzinfo=SHANGHAI)
EASTMONEY = "东方财富 kline (push2his.eastmoney.com)"
TENCENT = "腾讯 fqkline 独立交叉核验 (web.ifzq.gtimg.cn)"
TENCENT_CANONICAL = (
    "腾讯 fqkline 原始+前复权 (web.ifzq.gtimg.cn); amount=OHLC均价×成交量(手)×100估算"
)


def _bars(
    closes: tuple[float, ...],
    *,
    payout_on: dict[int, float] | None = None,
    source: str = EASTMONEY,
    symbol: str = "510300",
) -> tuple[DailyBar, ...]:
    """Subtractive front adjustment: bars before an event carry its payout."""
    payout_on = payout_on or {}
    bars: list[DailyBar] = []
    day = date(2026, 6, 1)
    previous = closes[0]
    for index, close in enumerate(closes):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        offset = round(sum(p for i, p in payout_on.items() if i > index), 3)
        scale = (close - offset) / close
        bars.append(DailyBar.from_mapping({
            "schema_version": 1, "symbol": symbol, "trading_date": day.isoformat(),
            "observed_at": f"{day.isoformat()}T15:10:00+08:00", "source": source,
            "open": close - 0.01, "high": close + 0.02, "low": close - 0.02,
            "close": close, "previous_close": previous,
            "volume": 1_000_000.0 + index, "amount": close * 1_000_000.0,
            "adjusted_open": (close - 0.01) * scale, "adjusted_high": (close + 0.02) * scale,
            "adjusted_low": (close - 0.02) * scale, "adjusted_close": close - offset,
            "is_final": True,
        }))
        previous = close
        day += timedelta(days=1)
    return tuple(bars)


def _independent(
    bars: tuple[DailyBar, ...], *, drop_adjusted_from: int | None = None,
) -> tuple[IndependentBar, ...]:
    return tuple(
        IndependentBar(
            trading_date=bar.trading_date, open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume,
            adjusted_close=(
                None if drop_adjusted_from is not None and index >= drop_adjusted_from
                else bar.adjusted_close
            ),
        )
        for index, bar in enumerate(bars)
    )


class AdjustmentEventTests(unittest.TestCase):
    def test_detects_subtractive_and_multiplicative_events_without_rounding_noise(self) -> None:
        closes = (4.000, 4.050, 3.980, 4.100, 4.070)
        # Subtractive convention: bars before the 0.069 distribution on day 3
        # sit 0.069 below their raw close; the offset is otherwise constant.
        subtractive = [
            (date(2026, 6, 1 + i), c, c - (0.069 if i < 3 else 0.0))
            for i, c in enumerate(closes)
        ]
        events = adjustment_events(subtractive, price_tick=0.001)
        self.assertEqual(
            [(e.trading_date, e.payout) for e in events],
            [(date(2026, 6, 4), 0.069)],
        )
        # Multiplicative convention rounded to the tick: factor 0.9 throughout
        # moves the offset every day but the ratio only by rounding noise.
        multiplicative = [(date(2026, 6, 1 + i), c, round(c * 0.9, 3)) for i, c in enumerate((0.700, 0.731, 0.699, 0.742, 0.715))]
        self.assertEqual(adjustment_events(multiplicative, price_tick=0.001), ())
        # A 0.003 monthly payout on a 1.1 ETF is still an event.
        small = [(date(2026, 6, 1), 1.100, 1.100), (date(2026, 6, 2), 1.105, 1.102)]
        self.assertEqual(len(adjustment_events(small, price_tick=0.001)), 1)


class CrosscheckHistoryTests(unittest.TestCase):
    def test_matching_independent_series_passes_and_verifies_adjustment(self) -> None:
        bars = _bars((4.0, 4.05, 3.98, 4.10, 4.07, 4.12), payout_on={3: 0.069})
        receipt = crosscheck_history(
            bars, _independent(bars), source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "PASSED")
        self.assertEqual(receipt.adjustment_status, "VERIFIED")
        self.assertEqual(receipt.compared_count, 6)
        self.assertEqual(receipt.mismatches, ())
        self.assertEqual(len(receipt.adjustment_events), 1)
        self.assertEqual(receipt.adjustment_events, receipt.independent_adjustment_events)
        self.assertEqual(receipt.calculation_version, CROSSCHECK_VERSION)
        self.assertEqual(receipt.sample_start, bars[0].trading_date.isoformat())
        self.assertEqual(receipt.sample_end, bars[-1].trading_date.isoformat())
        self.assertTrue(receipt.data_version.startswith("sha256:"))
        self.assertTrue(receipt_applies(receipt.to_dict(), bars))
        self.assertFalse(receipt_applies(receipt.to_dict(), bars[:-1]))

    def test_price_volume_and_missing_bar_mismatches_fail_closed(self) -> None:
        bars = _bars((4.0, 4.05, 3.98, 4.10))
        independent = list(_independent(bars))
        independent[1] = IndependentBar(
            trading_date=independent[1].trading_date, open=independent[1].open,
            high=independent[1].high, low=independent[1].low,
            close=independent[1].close + 0.002, volume=independent[1].volume,
            adjusted_close=independent[1].adjusted_close + 0.002,
        )
        independent[2] = IndependentBar(
            trading_date=independent[2].trading_date, open=independent[2].open,
            high=independent[2].high, low=independent[2].low, close=independent[2].close,
            volume=independent[2].volume * 1.02, adjusted_close=independent[2].adjusted_close,
        )
        del independent[3]
        receipt = crosscheck_history(
            bars, independent, source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "FAILED")
        self.assertEqual(receipt.adjustment_status, "REVIEW")
        self.assertEqual(
            sorted(item["reason"] for item in receipt.mismatches),
            ["MISSING_INDEPENDENT_BAR", "PRICE_MISMATCH", "VOLUME_MISMATCH"],
        )
        self.assertEqual(receipt.compared_count, 3)

    def test_one_tick_price_difference_is_tolerated(self) -> None:
        bars = _bars((4.0, 4.05, 3.98))
        independent = list(_independent(bars))
        independent[0] = IndependentBar(
            trading_date=independent[0].trading_date, open=independent[0].open + 0.001,
            high=independent[0].high, low=independent[0].low, close=independent[0].close,
            volume=independent[0].volume, adjusted_close=independent[0].adjusted_close,
        )
        receipt = crosscheck_history(
            bars, independent, source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "PASSED")

    def test_adjustment_event_disagreement_keeps_review_but_prices_pass(self) -> None:
        bars = _bars((4.0, 4.05, 3.98, 4.10, 4.07), payout_on={3: 0.069})
        # Independent provider never registers the distribution.
        independent = tuple(
            IndependentBar(
                trading_date=bar.trading_date, open=bar.open, high=bar.high, low=bar.low,
                close=bar.close, volume=bar.volume, adjusted_close=bar.close,
            )
            for bar in bars
        )
        receipt = crosscheck_history(
            bars, independent, source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "PASSED")
        self.assertEqual(receipt.adjustment_status, "REVIEW")
        self.assertEqual(len(receipt.adjustment_events), 1)
        self.assertEqual(receipt.independent_adjustment_events, ())

    def test_incomplete_independent_adjusted_series_cannot_verify_adjustment(self) -> None:
        bars = _bars((4.0, 4.05, 3.98, 4.10))
        receipt = crosscheck_history(
            bars, _independent(bars, drop_adjusted_from=3),
            source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "PASSED")
        self.assertEqual(receipt.adjustment_status, "REVIEW")
        self.assertIn("INDEPENDENT_ADJUSTED_INCOMPLETE", receipt.warnings)

    def test_same_provider_is_never_independent(self) -> None:
        bars = _bars((4.0, 4.05, 3.98), source=TENCENT_CANONICAL)
        receipt = crosscheck_history(
            bars, _independent(bars), source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        self.assertEqual(receipt.crosscheck_status, "FAILED")
        self.assertEqual(receipt.adjustment_status, "REVIEW")
        self.assertIn("CROSSCHECK_SOURCE_NOT_INDEPENDENT", receipt.warnings)
        self.assertTrue(canonical_source_is_independent(EASTMONEY, TENCENT))
        self.assertFalse(canonical_source_is_independent(TENCENT_CANONICAL, TENCENT))

    def test_rejects_unsafe_inputs(self) -> None:
        bars = _bars((4.0, 4.05))
        independent = _independent(bars)
        with self.assertRaises(CrosscheckError):
            crosscheck_history((), independent, source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001)
        with self.assertRaises(CrosscheckError):
            crosscheck_history(bars, independent, source=TENCENT, checked_at=CHECKED_AT, price_tick=0.0)
        with self.assertRaises(CrosscheckError):
            crosscheck_history(
                bars, independent, source=TENCENT,
                checked_at=CHECKED_AT.replace(tzinfo=None), price_tick=0.001,
            )
        with self.assertRaises(CrosscheckError):
            crosscheck_history(bars, independent + independent[:1], source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001)
        with self.assertRaises(CrosscheckError):
            crosscheck_history(bars + _bars((1.0,), symbol="510500"), independent, source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001)
        with self.assertRaises(CrosscheckError):
            IndependentBar(date(2026, 6, 1), 1.0, 1.0, 1.0, 0.0, 1.0)


class ReceiptFileTests(unittest.TestCase):
    def test_write_and_load_round_trip_and_malformed_files_yield_nothing(self) -> None:
        bars = _bars((4.0, 4.05, 3.98))
        receipt = crosscheck_history(
            bars, _independent(bars), source=TENCENT, checked_at=CHECKED_AT, price_tick=0.001,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "crosscheck_receipts.json"
            payload = write_receipts(path, (receipt,), generated_at=CHECKED_AT)
            self.assertEqual(payload["schema_version"], 1)
            self.assertTrue(payload["research_only"])
            loaded = load_receipts(path)
            self.assertEqual(set(loaded), {"510300"})
            self.assertEqual(loaded["510300"]["crosscheck_status"], "PASSED")
            self.assertTrue(receipt_applies(loaded["510300"], bars))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["items"]["510300"]["data_version"], receipt.data_version)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_receipts(path), {})
            path.write_text(json.dumps({"schema_version": 2, "items": {}}), encoding="utf-8")
            self.assertEqual(load_receipts(path), {})
            self.assertEqual(load_receipts(Path(directory) / "missing.json"), {})
            self.assertEqual(load_receipts(None), {})
            with self.assertRaises(CrosscheckError):
                write_receipts(path, (receipt, receipt), generated_at=CHECKED_AT)


if __name__ == "__main__":
    unittest.main()
