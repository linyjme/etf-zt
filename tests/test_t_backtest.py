import unittest
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile

from etf_rotation.etf_metadata import TradingMetadata
from etf_rotation.t_backtest import FillBar, TAccount, TBacktester
from etf_rotation.t_monitor import MarketDataError, Quote, QuotePoint, WatchItem, load_watchlist


class WatchItemBacktestConfigTests(unittest.TestCase):
    def test_optional_backtest_overrides_are_validated_without_hardcoded_lot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "watchlist.json"
            path.write_text(json.dumps([
                {"symbol": "510300"},
                {
                    "symbol": "510500",
                    "base_notional_cny": 12_345.0,
                    "base_shares": 150,
                    "t_capacity_ratio": 0.25,
                    "t_capacity_shares": 50,
                },
            ]), encoding="utf-8")
            items = load_watchlist(path)
            self.assertIsNone(items[0].base_shares)
            self.assertIsNone(items[0].t_capacity_shares)
            self.assertIsNone(items[0].base_notional_cny)
            self.assertIsNone(items[0].t_capacity_ratio)
            self.assertEqual(items[1].base_shares, 150)
            self.assertEqual(items[1].t_capacity_shares, 50)
            self.assertEqual(items[1].base_notional_cny, 12_345.0)
            self.assertEqual(items[1].t_capacity_ratio, 0.25)

            for field, value in (
                ("base_shares", True),
                ("base_shares", -100),
                ("base_shares", 100.0),
                ("t_capacity_shares", False),
                ("t_capacity_shares", -100),
                ("t_capacity_shares", float("inf")),
                ("base_notional_cny", True),
                ("base_notional_cny", 0),
                ("base_notional_cny", -1),
                ("base_notional_cny", float("nan")),
                ("base_notional_cny", float("inf")),
                ("t_capacity_ratio", True),
                ("t_capacity_ratio", 0),
                ("t_capacity_ratio", -0.1),
                ("t_capacity_ratio", 1.01),
                ("t_capacity_ratio", float("nan")),
                ("t_capacity_ratio", float("inf")),
            ):
                with self.subTest(field=field, value=value):
                    path.write_text(json.dumps([{
                        "symbol": "510300", field: value,
                    }]), encoding="utf-8")
                    with self.assertRaisesRegex(MarketDataError, field):
                        load_watchlist(path)


class TAccountTests(unittest.TestCase):
    def test_low_buy_then_sell_uses_overnight_inventory(self) -> None:
        account = TAccount.create(base_shares=10_000, t_capacity_shares=2_000, first_price=10.0, lot_size=100, intraday_turnaround=False)
        account.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 9.8, 50_000), requested_shares=2_000)
        self.assertEqual(account.today_bought_shares, 2_000)
        self.assertEqual(account.overnight_sellable_shares, 10_000)
        account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:30:00+08:00", 10.1, 50_000), requested_shares=2_000)
        self.assertEqual(account.today_bought_shares, 2_000)
        self.assertEqual(account.overnight_sellable_shares, 8_000)
        self.assertEqual(len(account.completed_pairs), 1)
        self.assertEqual(account.total_shares, 10_000)

    def test_high_sell_then_buyback_restores_total_inventory(self) -> None:
        account = TAccount.create(base_shares=10_000, t_capacity_shares=2_000, first_price=10.0, lot_size=100, intraday_turnaround=False)
        account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 10.2, 50_000), 2_000)
        account.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:30:00+08:00", 9.9, 50_000), 2_000)
        self.assertEqual(account.total_shares, 10_000)
        self.assertEqual(len(account.completed_pairs), 1)
        self.assertGreater(account.completed_pairs[0].net_pnl, 0)

    def test_zero_volume_and_participation_limit_prevent_impossible_fills(self) -> None:
        account = TAccount.create(base_shares=10_000, t_capacity_shares=2_000, first_price=10.0, lot_size=100, intraday_turnaround=False)
        rejected = account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 10.2, 0), 2_000)
        self.assertEqual(rejected.reason, "ZERO_VOLUME")
        limited = account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:01:00+08:00", 10.2, 20), 2_000)
        self.assertEqual(limited.shares, 200)

    def test_no_completed_pairs_never_claims_outperformance(self) -> None:
        result = TBacktester().summarize_no_trade(base_shares=1_000, reserve_cash=2_000, first_price=10.0, last_price=9.0)
        self.assertEqual(result.status, "NO_COMPLETED_PAIRS")
        self.assertEqual(result.t_net_gain_cny, 0.0)
        self.assertEqual(result.strategy_ending_equity_cny, result.baseline_ending_equity_cny)


class TBacktesterRunTests(unittest.TestCase):
    trading = TradingMetadata(
        "SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100,
    )

    @staticmethod
    def market_quote(
        prices: tuple[float, ...], volumes: tuple[float, ...], *, complete_count: int,
    ) -> Quote:
        start = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
        points = tuple(
            QuotePoint(
                start + timedelta(minutes=index),
                price,
                10.0,
                volume=volumes[index],
            )
            for index, price in enumerate(prices)
        )
        observed_at = points[complete_count - 1].timestamp + timedelta(minutes=1)
        if complete_count < len(points):
            observed_at = points[complete_count].timestamp + timedelta(seconds=5)
        return Quote(
            "510300", "ETF", points[-1].price, 10.0, 10.0,
            points[-1].timestamp, points, observed_at, "TEST_FIXTURE",
        )

    def test_run_executes_signal_on_next_completed_bar_with_volume_partial_fill(self) -> None:
        seen: list[str] = []

        def signal(decision_quote: Quote, _item: WatchItem, _bar: QuotePoint) -> str:
            seen.append(decision_quote.timestamp.isoformat())
            return "BUY_CANDIDATE"

        market_quote = self.market_quote(
            (10.0, 9.8, 9.7), (10_000, 10, 10_000), complete_count=2,
        )
        result = TBacktester(signal_callback=signal).run(
            market_quote,
            WatchItem("510300", "ETF", 0.002, True, 1_000, 200),
            self.trading,
        )

        self.assertEqual(seen, ["2026-08-28T10:00:00+08:00"])
        self.assertEqual(result.execution_mode, "NEXT_COMPLETED_BAR")
        self.assertEqual(result.open_leg_count, 1)
        self.assertEqual(result.open_legs[0].timestamp, "2026-08-28T10:01:00+08:00")
        self.assertEqual(result.open_legs[0].shares, 100)
        self.assertEqual(result.rejections[0].reason, "PARTICIPATION_LIMIT")
        self.assertEqual(result.last_price, 9.8)

    def test_run_with_no_actions_matches_same_inventory_and_cash_baseline(self) -> None:
        market_quote = self.market_quote(
            (10.0, 9.0), (10_000, 10_000), complete_count=2,
        )
        result = TBacktester(signal_callback=lambda *_: "WAIT").run(
            market_quote,
            WatchItem("510300", "ETF", 0.002, True, 1_000, 200),
            self.trading,
            reserve_cash=2_000.0,
        )
        self.assertEqual(result.status, "NO_COMPLETED_PAIRS")
        self.assertEqual(result.strategy_ending_equity_cny, 11_000.0)
        self.assertEqual(result.baseline_ending_equity_cny, 11_000.0)
        self.assertEqual(result.t_net_gain_cny, 0.0)
        self.assertIsNone(result.outperformed_baseline)

    def test_run_resolves_all_overrides_by_priority_for_non_hundred_lot(self) -> None:
        trading = TradingMetadata(
            "SSE", "TEST_ETF", False, 1, 50, 0.001, 0.10, 10,
        )
        market_quote = self.market_quote((10.0,), (10_000,), complete_count=1)
        explicit = TBacktester(signal_callback=lambda *_: "WAIT").run(
            market_quote,
            WatchItem(
                "510300", "ETF", 0.002,
                base_shares=150,
                t_capacity_shares=50,
                base_notional_cny=9_999.0,
                t_capacity_ratio=0.90,
            ),
            trading,
        )
        self.assertEqual(explicit.base_shares, 150)
        self.assertEqual(explicit.t_capacity_shares, 50)

        derived = TBacktester(signal_callback=lambda *_: "WAIT").run(
            market_quote,
            WatchItem(
                "510300", "ETF", 0.002,
                base_notional_cny=1_234.0,
                t_capacity_ratio=0.60,
            ),
            trading,
        )
        self.assertEqual(derived.base_shares, 100)
        self.assertEqual(derived.t_capacity_shares, 50)

        defaults = TBacktester(signal_callback=lambda *_: "WAIT").run(
            market_quote, WatchItem("510300", "ETF", 0.002), trading,
        )
        self.assertEqual(defaults.base_shares, 1_500)
        self.assertEqual(defaults.t_capacity_shares, 300)

    def test_explicit_shares_must_match_metadata_lot_size(self) -> None:
        market_quote = self.market_quote((10.0,), (10_000,), complete_count=1)
        with self.assertRaisesRegex(ValueError, "base_shares.*100"):
            TBacktester().run(
                market_quote,
                WatchItem("510300", "ETF", 0.002, base_shares=150),
                self.trading,
            )
        with self.assertRaisesRegex(ValueError, "t_capacity_shares.*100"):
            TBacktester().run(
                market_quote,
                WatchItem(
                    "510300", "ETF", 0.002,
                    base_shares=200,
                    t_capacity_shares=50,
                ),
                self.trading,
            )

    def test_run_rolls_today_buys_to_overnight_and_skips_cross_day_signal(self) -> None:
        day_one = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
        day_two = datetime.fromisoformat("2026-08-31T10:00:00+08:00")
        points = (
            QuotePoint(day_one, 10.0, 10.0, volume=10_000),
            QuotePoint(day_one + timedelta(minutes=1), 9.8, 10.0, volume=10_000),
            QuotePoint(day_two, 9.9, 9.9, volume=10_000),
            QuotePoint(day_two + timedelta(minutes=1), 10.1, 9.9, volume=10_000),
        )
        market_quote = Quote(
            "510300", "ETF", 10.1, 9.9, 10.0, points[-1].timestamp,
            points, points[-1].timestamp + timedelta(minutes=1), "TEST_FIXTURE",
        )
        seen: list[str] = []

        def signal(decision_quote: Quote, _item: WatchItem, _bar: QuotePoint) -> str:
            seen.append(decision_quote.timestamp.isoformat())
            return "BUY_CANDIDATE" if decision_quote.timestamp.date() == day_one.date() else "SELL_CANDIDATE"

        result = TBacktester(signal_callback=signal).run(
            market_quote,
            WatchItem(
                "510300", "ETF", 0.002,
                base_shares=1_000,
                t_capacity_shares=100,
            ),
            self.trading,
        )
        self.assertEqual(seen, [day_one.isoformat(), day_two.isoformat()])
        self.assertEqual(result.completed_pair_count, 1)
        self.assertEqual(result.open_leg_count, 0)
        self.assertEqual(result.today_bought_shares, 0)
        self.assertEqual(result.overnight_sellable_shares, 1_000)

    def test_records_and_backtest_result_are_json_serializable(self) -> None:
        account = TAccount.create(
            base_shares=1_000,
            t_capacity_shares=200,
            first_price=10.0,
            lot_size=100,
            intraday_turnaround=False,
        )
        bar = FillBar("2026-08-28T10:00:00+08:00", 9.8, 50_000)
        buy = account.execute("BUY_CANDIDATE", bar, 200)
        account.execute(
            "SELL_CANDIDATE",
            FillBar("2026-08-28T10:01:00+08:00", 10.1, 50_000),
            200,
        )
        rejected = account.execute(
            "SELL_CANDIDATE",
            FillBar("2026-08-28T10:02:00+08:00", 10.1, 0),
            200,
        )
        result = TBacktester().summarize(
            account, first_price=10.0, last_price=10.1,
        )
        for record in (bar, buy, account.completed_pairs[0], rejected, result):
            with self.subTest(record=type(record).__name__):
                self.assertIsInstance(json.loads(json.dumps(record.to_dict())), dict)

    def test_t_plus_one_sale_never_consumes_today_bought_inventory(self) -> None:
        account = TAccount.create(base_shares=100, t_capacity_shares=200, first_price=10.0, lot_size=100, intraday_turnaround=False)
        account.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 9.0, 50_000), 200)
        sale = account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:30:00+08:00", 10.0, 50_000), 200)
        self.assertEqual(sale.shares, 100)
        self.assertEqual(account.today_bought_shares, 200)
        self.assertEqual(account.overnight_sellable_shares, 0)
        self.assertEqual(account.total_shares, 200)

    def test_rejections_distinguish_lot_inventory_and_cash_limits(self) -> None:
        account = TAccount.create(base_shares=0, t_capacity_shares=100, first_price=10.0, lot_size=100, intraday_turnaround=False)
        self.assertEqual(account.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:00:00+08:00", 10.0, 50_000), 50).reason, "LOT_SIZE")
        self.assertEqual(account.execute("SELL_CANDIDATE", FillBar("2026-08-28T10:01:00+08:00", 10.0, 50_000), 100).reason, "INSUFFICIENT_INVENTORY")
        no_cash = TAccount.create(base_shares=100, t_capacity_shares=100, first_price=10.0, lot_size=100, intraday_turnaround=False, reserve_cash=0.0)
        self.assertEqual(no_cash.execute("BUY_CANDIDATE", FillBar("2026-08-28T10:02:00+08:00", 10.0, 50_000), 100).reason, "INSUFFICIENT_CASH")

    def test_unclosed_sell_leg_is_marked_and_reports_sell_fly_loss(self) -> None:
        start = datetime.fromisoformat("2026-08-28T10:00:00+08:00")
        points = (
            QuotePoint(start, 10.0, 10.0, volume=10_000),
            QuotePoint(start + timedelta(minutes=1), 11.0, 10.0, volume=10_000),
        )
        market_quote = Quote(
            "510300", "ETF", 11.0, 10.0, 10.0, points[-1].timestamp,
            points, points[-1].timestamp + timedelta(minutes=1), "TEST_FIXTURE",
        )
        trading = TradingMetadata("SSE", "DOMESTIC_EQUITY_ETF", False, 1, 100, 0.001, 0.10, 100)
        result = TBacktester(signal_callback=lambda *_: "SELL_CANDIDATE").run(
            market_quote,
            WatchItem("510300", "ETF", 0.002, True, 1_000, 200),
            trading,
        )
        self.assertEqual(result.status, "OPEN_LEG")
        self.assertEqual(result.open_leg_count, 1)
        self.assertEqual(result.open_legs[0].side, "SELL")
        self.assertGreater(result.sell_fly_loss_cny, 0)
        self.assertEqual(result.t_net_gain_cny, 0.0)


if __name__ == "__main__":
    unittest.main()
