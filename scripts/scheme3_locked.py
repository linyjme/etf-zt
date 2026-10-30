"""Locked scheme-3 book. The live swing monitor is not changed.

Entry is a MACD(6, 13, 5) golden cross with the close above MA20.
The order fills at the next session's open. A position leaves at the next
open after a MACD dead cross or a close back under MA20. A 3% drop from the
fill, or an intraday break of the prior session's MA20, leaves the same day.
No time stop, KDJ, DMI, MA60, weekly rule, or ATR stop is used.
The optional "MA20 rising for two days" filter is intentionally off.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from etf_rotation.swing_data import DailyBar
from etf_rotation.swing_indicators import macd_series

from backtest_dmi_kdj_rsi import _load_symbol
from backtest_handbook_v14 import ROUND_TRIP_FEE, TICK, _sma_at


CORE = (
    ("588000", "科创50"),
    ("159915", "创业板"),
)
BACKUP = (("512480", "半导体"),)
BLACKLIST = (
    ("159307", "红利低波100"),
    ("515180", "中证红利"),
    ("510300", "沪深300"),
    ("510500", "中证500"),
)
WEIGHT = 0.20
MAX_POSITIONS = 3
STOP_PCT = 0.03
WARMUP = 60


@dataclass
class _Position:
    entry_index: int
    entry: float
    notional: float
    signal: str


def _cost_fraction(entry: float) -> float:
    return (2.0 * TICK) / entry + ROUND_TRIP_FEE


def _net_pct(entry: float, exit_price: float) -> float:
    return (exit_price - entry) / entry - _cost_fraction(entry)


def _first_barrier(
    open_price: float,
    low: float,
    stop: float,
    ma20: float | None,
) -> tuple[str, float] | None:
    """Return the first downside level touched, filling through gaps at the open."""
    levels = [("stop3", stop)]
    if ma20 is not None:
        levels.append(("ma20_intraday", ma20))
    hit = [(name, level) for name, level in levels if low <= level]
    if not hit:
        return None
    name, level = max(hit, key=lambda item: item[1])
    return name, open_price if open_price <= level else level


def _summarize(trades: list[dict]) -> dict:
    closed = [trade for trade in trades if trade["reason"] != "open_end"]
    wins = [trade for trade in closed if trade["win"]]
    losses = [trade for trade in closed if not trade["win"]]
    payoff = None
    if wins and losses:
        payoff = (sum(trade["net_pct"] for trade in wins) / len(wins)) / abs(
            sum(trade["net_pct"] for trade in losses) / len(losses)
        )
    reasons: dict[str, int] = {}
    for trade in closed:
        reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
    streak = worst = 0
    for trade in closed:
        streak = streak + 1 if not trade["win"] else 0
        worst = max(worst, streak)
    holds = sorted(trade["hold_days"] for trade in closed)
    return {
        "trades": len(closed),
        "open_end": len(trades) - len(closed),
        "wins": len(wins),
        "win_rate": None if not closed else len(wins) / len(closed),
        "payoff_ratio": payoff,
        "avg_net_pct": None if not closed else sum(trade["net_pct"] for trade in closed) / len(closed),
        "median_hold": None if not holds else holds[len(holds) // 2],
        "longest_losing_streak": worst,
        "same_day_exit": sum(1 for trade in closed if trade["hold_days"] == 0),
        "reasons": reasons,
    }


def simulate_symbol(bars: tuple[DailyBar, ...]) -> list[dict]:
    """One symbol, always able to take the next signal. Returns are on the position."""
    closes = [bar.adjusted_close for bar in bars]
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    opens = [bar.adjusted_open for bar in bars]
    dates = [bar.trading_date for bar in bars]
    dif, dea, _histogram = macd_series(closes, 6, 13, 5)
    trades: list[dict] = []
    pending: str | None = None
    pending_reason = ""
    position: _Position | None = None

    def close_at(index: int, price: float, reason: str) -> None:
        nonlocal position, pending
        assert position is not None
        net = _net_pct(position.entry, price)
        trades.append({
            "entry_date": dates[position.entry_index].isoformat(),
            "exit_date": dates[index].isoformat(),
            "hold_days": index - position.entry_index,
            "reason": reason,
            "net_pct": net,
            "win": net > 0.0,
            "entry": position.entry,
            "exit": price,
        })
        position = None
        pending = None

    for index in range(WARMUP, len(bars)):
        prior_ma20 = _sma_at(closes, index - 1, 20)
        if position is not None and pending == "sell":
            close_at(index, opens[index], pending_reason or "signal_next_open")
        if position is not None:
            barrier = _first_barrier(
                opens[index], lows[index], position.entry * (1.0 - STOP_PCT), prior_ma20,
            )
            if barrier is not None:
                close_at(index, barrier[1], barrier[0])
        if position is None and pending == "buy":
            position = _Position(index, opens[index], 1.0, "buy")
            pending = None
            barrier = _first_barrier(
                opens[index], lows[index], position.entry * (1.0 - STOP_PCT), prior_ma20,
            )
            if barrier is not None:
                close_at(index, barrier[1], barrier[0])
        if index == len(bars) - 1:
            if position is not None:
                close_at(index, closes[index], "open_end")
            break
        ma20 = _sma_at(closes, index, 20)
        golden = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
        dead = dif[index - 1] >= dea[index - 1] and dif[index] < dea[index]
        if position is not None and (dead or (ma20 is not None and closes[index] < ma20)):
            pending = "sell"
            pending_reason = "macd_next_open" if dead else "ma20_next_open"
        elif position is None and golden and ma20 is not None and closes[index] > ma20:
            pending = "buy"
            pending_reason = ""
    return trades


def _equity_path(
    loaded: dict[str, tuple[DailyBar, ...]],
    names: tuple[tuple[str, str], ...],
) -> dict:
    series = {
        symbol: bars for symbol, _label in names if (bars := loaded.get(symbol))
    }
    frames = {
        symbol: {bar.trading_date: index for index, bar in enumerate(bars)}
        for symbol, bars in series.items()
    }
    calendar = sorted(set().union(*(frame.keys() for frame in frames.values())))
    indicators = {}
    arrays = {}
    for symbol, bars in series.items():
        closes = [bar.adjusted_close for bar in bars]
        arrays[symbol] = {
            "close": closes,
            "open": [bar.adjusted_open for bar in bars],
            "low": [bar.adjusted_low for bar in bars],
            "date": [bar.trading_date for bar in bars],
        }
        dif, dea, _histogram = macd_series(closes, 6, 13, 5)
        indicators[symbol] = (dif, dea)
    cash = 1.0
    peak = 1.0
    worst = 0.0
    positions: dict[str, _Position] = {}
    pending: dict[str, str] = {}
    pending_reason: dict[str, str] = {}
    trades: list[dict] = []
    skipped = 0

    def mark(day: date, price_key: str) -> float:
        value = cash
        for symbol, position in positions.items():
            index = frames[symbol].get(day)
            if index is None:
                continue
            price = arrays[symbol][price_key][index]
            value += position.notional * price / position.entry
        return value

    def close_position(symbol: str, index: int, price: float, reason: str) -> None:
        nonlocal cash
        position = positions.pop(symbol)
        pending.pop(symbol, None)
        net = _net_pct(position.entry, price)
        cash += position.notional * (1.0 + net)
        trades.append({
            "symbol": symbol,
            "entry_date": arrays[symbol]["date"][position.entry_index].isoformat(),
            "exit_date": arrays[symbol]["date"][index].isoformat(),
            "hold_days": index - position.entry_index,
            "reason": reason,
            "net_pct": net,
            "win": net > 0.0,
        })

    for day in calendar:
        ready = []
        for symbol, _label in names:
            index = frames[symbol].get(day)
            if index is None or index < WARMUP:
                continue
            ready.append((symbol, index))
        for symbol, index in ready:
            if symbol in positions and pending.get(symbol) == "sell":
                close_position(symbol, index, arrays[symbol]["open"][index], pending_reason.get(symbol, "signal_next_open"))
        equity = mark(day, "open")
        for symbol, index in ready:
            if symbol in positions or pending.get(symbol) != "buy":
                continue
            if len(positions) >= MAX_POSITIONS or cash < WEIGHT * equity:
                skipped += 1
                pending.pop(symbol, None)
                continue
            notional = WEIGHT * equity
            cash -= notional
            positions[symbol] = _Position(index, arrays[symbol]["open"][index], notional, "buy")
            pending.pop(symbol, None)
        for symbol, index in list(ready):
            position = positions.get(symbol)
            if position is None or position.entry_index != index and pending.get(symbol) == "sell":
                continue
            if position.entry_index == index or symbol in positions:
                prior_ma20 = _sma_at(arrays[symbol]["close"], index - 1, 20)
                barrier = _first_barrier(
                    arrays[symbol]["open"][index],
                    arrays[symbol]["low"][index],
                    position.entry * (1.0 - STOP_PCT),
                    prior_ma20,
                )
                if barrier is not None and symbol in positions:
                    close_position(symbol, index, barrier[1], barrier[0])
        for symbol, index in ready:
            if index == len(series[symbol]) - 1:
                if symbol in positions:
                    close_position(symbol, index, arrays[symbol]["close"][index], "open_end")
                continue
            closes = arrays[symbol]["close"]
            dif, dea = indicators[symbol]
            ma20 = _sma_at(closes, index, 20)
            golden = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
            dead = dif[index - 1] >= dea[index - 1] and dif[index] < dea[index]
            if symbol in positions and (dead or (ma20 is not None and closes[index] < ma20)):
                pending[symbol] = "sell"
                pending_reason[symbol] = "macd_next_open" if dead else "ma20_next_open"
            elif symbol not in positions and golden and ma20 is not None and closes[index] > ma20:
                pending[symbol] = "buy"
        equity = mark(day, "close")
        peak = max(peak, equity)
        if peak:
            worst = max(worst, (peak - equity) / peak)
    summary = _summarize(trades)
    summary["ending_equity"] = cash
    summary["max_drawdown"] = worst
    summary["skipped_entries"] = skipped
    summary["weight"] = WEIGHT
    summary["max_positions"] = MAX_POSITIONS
    return summary


def _self_check() -> None:
    barrier = _first_barrier(1.0, 0.96, 0.97, 0.99)
    if barrier != ("ma20_intraday", 0.99):
        raise RuntimeError(f"MA20 must be hit before the 3% stop, got {barrier}")
    gap = _first_barrier(0.90, 0.90, 0.97, 0.99)
    if gap != ("ma20_intraday", 0.90):
        raise RuntimeError(f"a gap through both levels fills at the open, got {gap}")
    stop_only = _first_barrier(1.0, 0.96, 0.97, 0.95)
    if stop_only != ("stop3", 0.97):
        raise RuntimeError(f"stop above MA20 is the first hit, got {stop_only}")


def main() -> None:
    _self_check()
    daily = Path("var/swing/daily_quotes.jsonl")
    pool = CORE + BACKUP
    loaded = {
        symbol: _load_symbol(daily, symbol)
        for symbol, _label in pool + BLACKLIST
    }
    symbols = []
    for symbol, label in pool:
        trades = simulate_symbol(loaded[symbol])
        summary = _summarize(trades)
        summary["symbol"] = symbol
        summary["name"] = label
        summary["role"] = "backup" if (symbol, label) in BACKUP else "core"
        summary["first"] = loaded[symbol][0].trading_date.isoformat()
        summary["last"] = loaded[symbol][-1].trading_date.isoformat()
        symbols.append(summary)
    blacklist = []
    for symbol, label in BLACKLIST:
        summary = _summarize(simulate_symbol(loaded[symbol]))
        summary["symbol"] = symbol
        summary["name"] = label
        blacklist.append(summary)
    report = {
        "rules": {
            "entry": "MACD(6,13,5) golden cross and close > MA20",
            "fill": "next session open",
            "stop": "3% below the fill, intraday",
            "exit": "next open after MACD dead cross or close < MA20; intraday if price breaks the prior MA20",
            "weight": WEIGHT,
            "max_positions": MAX_POSITIONS,
            "ma20_slope_filter": False,
            "dropped": ["time stop", "KDJ", "DMI", "MA60", "weekly", "ATR"],
        },
        "pool": symbols,
        "book": _equity_path(loaded, pool),
        "blacklist_check": blacklist,
    }
    destination = Path("var/swing/research/scheme3_locked.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in symbols:
        print(
            row["role"], row["name"], row["trades"], row["win_rate"], row["payoff_ratio"],
            row["avg_net_pct"], row["median_hold"], row["longest_losing_streak"], row["reasons"],
        )
    book = report["book"]
    print(
        "book", book["trades"], book["win_rate"], book["payoff_ratio"], book["avg_net_pct"],
        book["ending_equity"], book["max_drawdown"], book["skipped_entries"], book["longest_losing_streak"],
    )
    for row in blacklist:
        print("black", row["name"], row["trades"], row["win_rate"], row["payoff_ratio"], row["avg_net_pct"])


if __name__ == "__main__":
    main()
