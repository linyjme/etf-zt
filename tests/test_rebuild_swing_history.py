from __future__ import annotations

from dataclasses import replace
from datetime import date
import unittest

from etf_rotation.swing_config import SwingWatchItem
from etf_rotation.swing_data import SwingDataError
from scripts.rebuild_swing_history import collect_primary_history
from tests.swing_helpers import swing_strategy_bars


EASTMONEY = "东方财富 kline (push2his.eastmoney.com)"
EASTMONEY_SPLIT = "东方财富 kline 一致前复权序列 (push2his.eastmoney.com); volume=按复权比例折算"
TENCENT = (
    "腾讯 fqkline 原始+前复权 (web.ifzq.gtimg.cn); "
    "amount=OHLC均价×成交量(手)×100估算"
)


class FakeCollector:
    def __init__(self, by_symbol: dict[str, object]) -> None:
        self.by_symbol = by_symbol
        self.calls: list[tuple[str, date, int]] = []

    def collect(self, watchlist, last_completed_date, count):
        (item,) = watchlist
        self.calls.append((item.symbol, last_completed_date, count))
        outcome = self.by_symbol[item.symbol]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class CollectPrimaryHistoryTests(unittest.TestCase):
    def test_only_primary_provider_reported_histories_are_accepted(self) -> None:
        primary = tuple(replace(bar, source=EASTMONEY) for bar in swing_strategy_bars(5))
        split = tuple(
            replace(bar, source=EASTMONEY_SPLIT)
            for bar in swing_strategy_bars(5, symbol="512480")
        )
        fallback = tuple(
            replace(bar, source=TENCENT) for bar in swing_strategy_bars(5, symbol="510500")
        )
        mixed = (*primary[:3], *tuple(replace(bar, source=TENCENT) for bar in primary[3:]))
        collector = FakeCollector({
            "510300": primary,
            "512480": split,
            "510500": fallback,
            "159915": SwingDataError("boom"),
            "588000": (),
            "512100": mixed,
        })
        items = tuple(
            SwingWatchItem(symbol, True)
            for symbol in ("510300", "512480", "510500", "159915", "588000", "512100")
        )

        collected, failures = collect_primary_history(
            items, collector, last_completed_date=date(2026, 9, 23), count=760,
        )

        self.assertEqual(set(collected), {"510300", "512480"})
        self.assertEqual(collected["510300"], primary)
        self.assertEqual(failures["510500"], "FALLBACK_PROVIDER:腾讯")
        self.assertEqual(failures["159915"], "SwingDataError")
        self.assertEqual(failures["588000"], "NO_BARS")
        self.assertEqual(failures["512100"], "FALLBACK_PROVIDER:东方财富,腾讯")
        self.assertEqual(
            collector.calls,
            [(item.symbol, date(2026, 9, 23), 760) for item in items],
        )


if __name__ == "__main__":
    unittest.main()
