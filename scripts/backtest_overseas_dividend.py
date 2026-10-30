"""Backtest the four MACD/DMI/KDJ variants on Nikkei, DAX and two dividend ETFs.

Nikkei and DAX bars come from a Wind daily kline dump. The dividend ETFs are
read from the local swing store. Nothing here is wired into the monitor.
"""

from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_rotation.swing_data import DailyBar
from backtest_dmi_kdj_rsi import (
    CONTRAST,
    Config,
    VARIANTS,
    _load_symbol,
    _split_trades,
    simulate,
    summarize,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXED = Config(14, 9, 3, 3, 6)
MARKETS = (
    ("N225", "日经225", Path("var/swing/research/wind-n225.json")),
    ("GDAXI", "德国DAX", Path("var/swing/research/wind-dax.json")),
)


def _bars_from_wind(path: Path, symbol: str) -> tuple[DailyBar, ...]:
    payload = json.loads(path.read_text(encoding="utf-16"))
    table = json.loads(payload["content"][0]["text"])["data"]
    columns = [column["name"] for column in table["columns"]]
    bars: list[DailyBar] = []
    previous_close = None
    for row in table["rows"]:
        record = dict(zip(columns, row))
        if record.get("TIME") in (None, "", "None"):
            continue
        trading_day = date.fromisoformat(str(record["TIME"])[:10])
        try:
            open_price = float(record["OPEN"])
            high = float(record["HIGH"])
            low = float(record["LOW"])
            close = float(record["MATCH"])
        except (TypeError, ValueError):
            continue
        if min(open_price, high, low, close) <= 0.0:
            continue
        prior = close if previous_close is None else previous_close
        volume = float(record["VOLUME"] or 0.0)
        amount = float(record["TURNOVER"] or 0.0)
        bars.append(DailyBar(
            schema_version=1,
            symbol=symbol,
            trading_date=trading_day,
            observed_at=datetime(trading_day.year, trading_day.month, trading_day.day, 15, tzinfo=SHANGHAI),
            source="Wind daily kline",
            open=open_price,
            high=high,
            low=low,
            close=close,
            previous_close=prior,
            volume=volume,
            amount=amount,
            adjusted_open=open_price,
            adjusted_high=high,
            adjusted_low=low,
            adjusted_close=close,
            is_final=True,
        ))
        previous_close = close
    bars.sort(key=lambda bar: bar.trading_date)
    return tuple(bars)


def _section(bars: tuple[DailyBar, ...], variant: str, *, macd_only: bool = False) -> dict:
    trades = simulate(bars, FIXED, variant=variant, macd_only=macd_only)
    training, holdout = _split_trades(trades, bars)
    return {
        "training": summarize(training, {bars[0].symbol: bars}),
        "holdout": summarize(holdout, {bars[0].symbol: bars}),
    }


def main() -> None:
    series: dict[str, tuple[DailyBar, ...]] = {}
    names = {symbol: name for symbol, name, _kind in CONTRAST}
    names.update({symbol: name for symbol, name, _path in MARKETS})
    root = Path("var/swing/daily_quotes.jsonl")
    for symbol, _name, _kind in CONTRAST:
        if symbol == "159263":
            continue
        series[symbol] = _load_symbol(root, symbol)
    for symbol, _name, path in MARKETS:
        series[symbol] = _bars_from_wind(path, symbol)
    report = {"schema_version": 1, "names": names, "markets": {}}
    for symbol, bars in series.items():
        item = {
            "bar_count": len(bars),
            "first_date": bars[0].trading_date.isoformat(),
            "last_date": bars[-1].trading_date.isoformat(),
            "baseline": _section(bars, "control", macd_only=True),
            "variants": {},
        }
        for variant, _label in VARIANTS:
            item["variants"][variant] = _section(bars, variant)
        report["markets"][symbol] = item
        training = item["variants"]["loose"]["training"]
        print(
            symbol, item["first_date"], item["last_date"], item["bar_count"],
            "loose", training["trades"], training["gross_win_rate"], training["payoff_ratio"],
        )
    destination = Path("var/swing/research/overseas_dividend_backtest.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
