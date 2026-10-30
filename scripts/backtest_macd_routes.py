"""Route 1: daily MACD(6,13,5) with a 3% hard stop.
Route 3: 60-minute MACD + DMI(14) + KDJ(9,3,3), capped at 15 bars.

Research only. Does not change the live monitor.
"""

from __future__ import annotations

from datetime import datetime

import json
from pathlib import Path

from etf_rotation.swing_data import DailyBar
from etf_rotation.swing_indicators import dmi_series, kdj_series, macd_series

from backtest_dmi_kdj_rsi import (  # noqa: E402
    FEE_RATE,
    _crosses,
    _load_symbol,
)


STOP_PCT = 0.03
POSITION = 0.20
ROUTE3_MAX_BARS = 15
DAILY = (
    ("588000", "科创50ETF", "etf"),
    ("159915", "创业板ETF", "etf"),
    ("512480", "半导体ETF", "etf"),
    ("512880", "证券ETF", "etf"),
    ("512100", "中证1000ETF", "etf"),
    ("000852", "中证1000", "index"),
)
MINUTE = (
    ("588000", "科创50ETF"),
    ("159915", "创业板ETF"),
    ("512480", "半导体ETF"),
    ("512880", "证券ETF"),
    ("512100", "中证1000ETF"),
)


def _bars_from_wind(path: Path, symbol: str) -> tuple[DailyBar, ...]:
    payload = json.loads(path.read_text(encoding="utf-16"))
    table = json.loads(payload["content"][0]["text"])["data"]
    columns = [column["name"] for column in table["columns"]]
    bars: list[DailyBar] = []
    previous = None
    for row in table["rows"]:
        record = dict(zip(columns, row))
        if record.get("TIME") in (None, "", "None"):
            continue
        try:
            stamp = datetime.fromisoformat(str(record["TIME"]))
            trading_day = stamp.date()
            open_price = float(record["OPEN"])
            high = float(record["HIGH"])
            low = float(record["LOW"])
            close = float(record["MATCH"])
        except (TypeError, ValueError):
            continue
        if min(open_price, high, low, close) <= 0.0 or low > high:
            continue
        prior = close if previous is None else previous
        bars.append(DailyBar(
            schema_version=1,
            symbol=symbol,
            trading_date=trading_day,
            observed_at=stamp,
            source="Wind kline",
            open=open_price,
            high=high,
            low=low,
            close=close,
            previous_close=prior,
            volume=float(record.get("VOLUME") or 0.0),
            amount=float(record.get("TURNOVER") or 0.0),
            adjusted_open=open_price,
            adjusted_high=high,
            adjusted_low=low,
            adjusted_close=close,
            is_final=True,
        ))
        previous = close
    bars.sort(key=lambda bar: bar.observed_at)
    return tuple(bars)


def _stamp(bar: DailyBar, when: datetime) -> DailyBar:
    return DailyBar(
        schema_version=bar.schema_version,
        symbol=bar.symbol,
        trading_date=bar.trading_date,
        observed_at=when,
        source=bar.source,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        previous_close=bar.previous_close,
        volume=bar.volume,
        amount=bar.amount,
        adjusted_open=bar.adjusted_open,
        adjusted_high=bar.adjusted_high,
        adjusted_low=bar.adjusted_low,
        adjusted_close=bar.adjusted_close,
        is_final=True,
    )


def simulate_macd_stop(bars: tuple[DailyBar, ...]) -> list[dict]:
    """MACD golden cross buys the next open. Dead cross or a 3% break sells."""
    if len(bars) < 3:
        return []
    closes = [bar.adjusted_close for bar in bars]
    dif, dea, _histogram = macd_series(closes, 6, 13, 5)
    trades = []
    index = 1
    while index < len(bars) - 1:
        golden, _dead = _crosses(dif, dea, index)
        if not golden:
            index += 1
            continue
        fill = index + 1
        entry = bars[fill].adjusted_open
        stop = entry * (1.0 - STOP_PCT)
        exit_index = None
        exit_price = None
        reason = None
        for cursor in range(fill, len(bars)):
            bar = bars[cursor]
            if bar.adjusted_low <= stop:
                exit_index = cursor
                exit_price = bar.adjusted_open if bar.adjusted_open <= stop else stop
                reason = "stop"
                break
            if cursor > fill and cursor < len(bars) - 1:
                _golden, dead = _crosses(dif, dea, cursor)
                if dead:
                    exit_index = cursor + 1
                    exit_price = bars[cursor + 1].adjusted_open
                    reason = "macd"
                    break
        if exit_index is None or exit_price is None:
            break
        trades.append({
            "symbol": bars[0].symbol,
            "signal_date": bars[index].trading_date.isoformat(),
            "entry_date": bars[fill].trading_date.isoformat(),
            "exit_date": bars[exit_index].trading_date.isoformat(),
            "entry_price": entry,
            "exit_price": exit_price,
            "reason": reason,
            "win": exit_price > entry,
            "ret": (exit_price * (1.0 - FEE_RATE)) / (entry * (1.0 + FEE_RATE)) - 1.0,
        })
        index = exit_index + 1
    return trades


def simulate_route3(bars: tuple[DailyBar, ...]) -> list[dict]:
    """60-minute MACD cross, confirmed by +DI and a KDJ cross. Flat by bar 15."""
    if len(bars) < 3:
        return []
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    closes = [bar.adjusted_close for bar in bars]
    dif, dea, _histogram = macd_series(closes, 6, 13, 5)
    plus, minus = dmi_series(highs, lows, closes, 14)
    k_values, d_values, _j = kdj_series(highs, lows, closes, 9, 3, 3)
    trades = []
    index = 1
    while index < len(bars) - 1:
        macd_golden, _dead = _crosses(dif, dea, index)
        kdj_golden, _kdj_dead = _crosses(k_values, d_values, index)
        direction = (
            plus[index] is not None
            and minus[index] is not None
            and plus[index] > minus[index]
        )
        if not (macd_golden and kdj_golden and direction):
            index += 1
            continue
        fill = index + 1
        entry = bars[fill].adjusted_open
        exit_index = None
        exit_price = None
        reason = None
        last_signal = min(fill + ROUTE3_MAX_BARS - 1, len(bars) - 2)
        for cursor in range(fill, last_signal + 1):
            _golden, dead = _crosses(dif, dea, cursor)
            if dead and cursor < len(bars) - 1:
                exit_index = cursor + 1
                exit_price = bars[cursor + 1].adjusted_open
                reason = "macd"
                break
            if cursor >= fill + ROUTE3_MAX_BARS - 1:
                exit_index = min(cursor + 1, len(bars) - 1)
                exit_price = bars[exit_index].adjusted_open
                reason = "time"
                break
        if exit_index is None or exit_price is None:
            break
        trades.append({
            "symbol": bars[0].symbol,
            "signal_date": bars[index].trading_date.isoformat(),
            "entry_date": bars[fill].trading_date.isoformat(),
            "exit_date": bars[exit_index].trading_date.isoformat(),
            "entry_price": entry,
            "exit_price": exit_price,
            "reason": reason,
            "win": exit_price > entry,
            "ret": (exit_price * (1.0 - FEE_RATE)) / (entry * (1.0 + FEE_RATE)) - 1.0,
        })
        index = exit_index + 1
    return trades


def _stats(trades: list[dict]) -> dict:
    count = len(trades)
    wins = [trade for trade in trades if trade["ret"] > 0.0]
    losses = [trade for trade in trades if trade["ret"] < 0.0]
    payoff = None
    if wins and losses:
        payoff = (sum(trade["ret"] for trade in wins) / len(wins)) / abs(
            sum(trade["ret"] for trade in losses) / len(losses)
        )
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for trade in trades:
        stake = equity * POSITION
        equity = equity - stake + stake * (1.0 + trade["ret"])
        peak = max(peak, equity)
        if peak > 0.0:
            worst = max(worst, (peak - equity) / peak)
    return {
        "trades": count,
        "win_rate": None if count == 0 else sum(1 for trade in trades if trade["win"]) / count,
        "payoff_ratio": payoff,
        "account_return": equity - 1.0,
        "account_max_drawdown": worst,
        "stops": sum(1 for trade in trades if trade["reason"] == "stop"),
    }


def _split(trades: list[dict], bars: tuple[DailyBar, ...], holdout_bars: int) -> tuple[list[dict], list[dict]]:
    cutoff = bars[max(0, len(bars) - holdout_bars)].trading_date.isoformat()
    training = [trade for trade in trades if trade["signal_date"] < cutoff]
    holdout = [trade for trade in trades if trade["signal_date"] >= cutoff]
    return training, holdout


def _pooled(parts: list[tuple[str, tuple[DailyBar, ...]]], runner, holdout_bars: int) -> dict:
    training: list[dict] = []
    holdout: list[dict] = []
    by_symbol = {}
    for symbol, bars in parts:
        trades = runner(bars)
        train, held = _split(trades, bars, holdout_bars)
        training.extend(train)
        holdout.extend(held)
        by_symbol[symbol] = {
            "bars": len(bars),
            "first": bars[0].trading_date.isoformat(),
            "last": bars[-1].trading_date.isoformat(),
            "training": _stats(train),
            "holdout": _stats(held),
        }
    return {"training": _stats(training), "holdout": _stats(holdout), "by_symbol": by_symbol}


def main() -> None:
    daily_parts = []
    for symbol, _name, kind in DAILY:
        path = Path("var/swing/index_quotes.jsonl" if kind == "index" else "var/swing/daily_quotes.jsonl")
        bars = _load_symbol(path, symbol)
        if bars:
            daily_parts.append((symbol, bars))
    for symbol, filename in (("N225", "wind-n225.json"), ("GDAXI", "wind-dax.json")):
        path = Path("var/swing/research") / filename
        if path.exists():
            daily_parts.append((symbol, _bars_from_wind(path, symbol)))
    minute_parts = []
    for symbol, _name in MINUTE:
        path = Path("var/swing/research") / f"wind-60m-{symbol}.json"
        if path.exists():
            minute_parts.append((symbol, _bars_from_wind(path, symbol)))
    report = {
        "route1": _pooled(daily_parts, simulate_macd_stop, 252),
        "route3": _pooled(minute_parts, simulate_route3, 1008) if minute_parts else None,
    }
    destination = Path("var/swing/research/macd_routes_backtest.json")
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    route1 = report["route1"]
    print("route1", route1["training"]["trades"], route1["training"]["win_rate"], route1["training"]["payoff_ratio"])
    if report["route3"]:
        route3 = report["route3"]
        print("route3", route3["training"]["trades"], route3["training"]["win_rate"], route3["training"]["payoff_ratio"])


if __name__ == "__main__":
    main()
