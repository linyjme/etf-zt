"""Three entry/exit pairings for the MACD(6,13,5) cross.

Scheme 1 is a 3-day rebound on ChiNext and STAR 50.
Scheme 2 requires price above a rising MA60, for CSI 300 and CSI dividend.
Scheme 3 is a MA20 trend trade, reported separately on the fast and slow groups.
The previous 116-trade run is only used to measure how far entries sat from MA60.
The live monitor is not changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from etf_rotation.swing_data import DailyBar
from etf_rotation.swing_indicators import kdj_series, macd_series

from backtest_dmi_kdj_rsi import _load_symbol
from backtest_handbook_v14 import (
    ROUND_TRIP_FEE,
    SPECS,
    TICK,
    _overheat,
    _sma_at,
    _style_series,
    _weekly,
    run_symbol,
)


FAST = ("588000", "159915")
SLOW_CORE = ("510300", "515180")
SLOW_EXTRA = ("159307", "510500")


@dataclass(frozen=True)
class Name:
    symbol: str
    label: str


NAMES = {spec.symbol: spec.name for spec in SPECS}


def _cost(entry: float, fraction: float) -> float:
    return fraction * (2.0 * TICK + entry * ROUND_TRIP_FEE)


def _stop_fill(open_price: float, stop: float) -> float:
    return open_price if open_price <= stop else stop


def _summarize(trades: list[dict]) -> dict:
    wins = [trade for trade in trades if trade["win"]]
    losses = [trade for trade in trades if not trade["win"]]
    payoff = None
    if wins and losses:
        payoff = (sum(trade["net_pct"] for trade in wins) / len(wins)) / abs(
            sum(trade["net_pct"] for trade in losses) / len(losses)
        )
    reasons: dict[str, int] = {}
    for trade in trades:
        reasons[trade["reason"]] = reasons.get(trade["reason"], 0) + 1
    below = [trade for trade in trades if trade["ma60_gap"] < 0.0]
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": None if not trades else len(wins) / len(trades),
        "payoff_ratio": payoff,
        "avg_net_pct": None if not trades else sum(trade["net_pct"] for trade in trades) / len(trades),
        "median_hold": None if not trades else sorted(trade["hold_days"] for trade in trades)[len(trades) // 2],
        "below_ma60": len(below),
        "below_ma60_win_rate": None if not below else sum(1 for trade in below if trade["win"]) / len(below),
        "reasons": reasons,
    }


def _finish(entry: float, exit_price: float, remaining: float, realized: float) -> float:
    realized += remaining * (exit_price - entry) - _cost(entry, remaining)
    return realized / entry


def _scheme1(bars: tuple[DailyBar, ...]) -> list[dict]:
    closes = [bar.adjusted_close for bar in bars]
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    opens = [bar.adjusted_open for bar in bars]
    dates = [bar.trading_date for bar in bars]
    dif, dea, _histogram = macd_series(closes, 6, 13, 5)
    k_line, d_line, _j = kdj_series(highs, lows, closes, 9, 3, 3)
    trades: list[dict] = []
    index = 70
    while index < len(bars) - 1:
        ma10 = _sma_at(closes, index, 10)
        ma60 = _sma_at(closes, index, 60)
        golden = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
        if ma10 is None or ma60 is None or not golden or closes[index] <= ma10:
            index += 1
            continue
        entry_index = index
        entry = closes[index]
        entry_low = lows[index]
        gap = entry / ma60 - 1.0
        exit_index = None
        exit_price = None
        reason = None
        for cursor in range(index + 1, len(bars)):
            line10 = _sma_at(closes, cursor, 10)
            macd_dead = dif[cursor - 1] >= dea[cursor - 1] and dif[cursor] < dea[cursor]
            kdj_dead = k_line[cursor - 1] >= d_line[cursor - 1] and k_line[cursor] < d_line[cursor]
            if lows[cursor] <= entry_low:
                exit_index = cursor
                exit_price = _stop_fill(opens[cursor], entry_low)
                reason = "entry_low"
                break
            if line10 is not None and closes[cursor] < line10:
                exit_index = cursor
                exit_price = closes[cursor]
                reason = "ma10"
                break
            if macd_dead or kdj_dead:
                exit_index = cursor
                exit_price = closes[cursor]
                reason = "macd_dead" if macd_dead else "kdj_dead"
                break
            if cursor - entry_index >= 3:
                exit_index = cursor
                exit_price = closes[cursor]
                reason = "day3"
                break
        if exit_index is None or exit_price is None or reason is None:
            exit_index = len(bars) - 1
            exit_price = closes[exit_index]
            reason = "open_end"
        net = _finish(entry, exit_price, 1.0, 0.0)
        trades.append({
            "entry": dates[entry_index].isoformat(),
            "exit": dates[exit_index].isoformat(),
            "hold_days": exit_index - entry_index,
            "reason": reason,
            "net_pct": net,
            "win": net > 0.0,
            "ma60_gap": gap,
        })
        index = exit_index + 1
    return trades


def _scheme2(bars: tuple[DailyBar, ...]) -> list[dict]:
    closes = [bar.adjusted_close for bar in bars]
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    opens = [bar.adjusted_open for bar in bars]
    dates = [bar.trading_date for bar in bars]
    dif, dea, _histogram = macd_series(closes, 6, 13, 5)
    trades: list[dict] = []
    index = 70
    while index < len(bars) - 1:
        ma10 = _sma_at(closes, index, 10)
        ma60 = _sma_at(closes, index, 60)
        ma60_prev = _sma_at(closes, index - 10, 60)
        golden = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
        trend = (
            ma10 is not None
            and ma60 is not None
            and ma60_prev is not None
            and closes[index] > ma10
            and closes[index] > ma60
            and ma60 > ma60_prev
        )
        if not golden or not trend:
            index += 1
            continue
        entry_index = index
        entry = closes[index]
        gap = entry / ma60 - 1.0
        span = []
        for cursor in range(index - 13, index + 1):
            previous = closes[cursor - 1]
            span.append(max(
                highs[cursor] - lows[cursor],
                abs(highs[cursor] - previous),
                abs(lows[cursor] - previous),
            ))
        stop = entry - 2.0 * (sum(span) / len(span))
        if stop >= entry:
            index += 1
            continue
        remaining = 1.0
        realized = 0.0
        exit_index = None
        reason = None
        for cursor in range(index + 1, len(bars)):
            line10 = _sma_at(closes, cursor, 10)
            line20 = _sma_at(closes, cursor, 20)
            line60 = _sma_at(closes, cursor, 60)
            if lows[cursor] <= stop:
                realized = _finish(entry, _stop_fill(opens[cursor], stop), remaining, realized)
                exit_index = cursor
                reason = "atr2"
                break
            full_reason = None
            if line20 is not None and closes[cursor] < line20:
                full_reason = "ma20"
            elif line60 is not None and closes[cursor] < line60:
                full_reason = "ma60"
            if full_reason:
                realized = _finish(entry, closes[cursor], remaining, realized)
                exit_index = cursor
                reason = full_reason
                break
            if remaining > 0.5 and line10 is not None and closes[cursor] < line10:
                realized += 0.5 * (closes[cursor] - entry) - _cost(entry, 0.5)
                remaining = 0.5
        if exit_index is None or reason is None:
            exit_index = len(bars) - 1
            realized = _finish(entry, closes[exit_index], remaining, realized)
            reason = "open_end"
        trades.append({
            "entry": dates[entry_index].isoformat(),
            "exit": dates[exit_index].isoformat(),
            "hold_days": exit_index - entry_index,
            "reason": reason,
            "net_pct": realized,
            "win": realized > 0.0,
            "ma60_gap": gap,
        })
        index = exit_index + 1
    return trades


def _scheme3(bars: tuple[DailyBar, ...]) -> list[dict]:
    closes = [bar.adjusted_close for bar in bars]
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    opens = [bar.adjusted_open for bar in bars]
    dates = [bar.trading_date for bar in bars]
    dif, dea, _histogram = macd_series(closes, 6, 13, 5)
    trades: list[dict] = []
    equity = 1.0
    index = 70
    while index < len(bars) - 1:
        ma20 = _sma_at(closes, index, 20)
        ma60 = _sma_at(closes, index, 60)
        golden = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
        if ma20 is None or ma60 is None or not golden or closes[index] <= ma20:
            index += 1
            continue
        entry_index = index
        entry = closes[index]
        stop = entry * 0.97
        gap = entry / ma60 - 1.0
        exit_index = None
        exit_price = None
        reason = None
        for cursor in range(index + 1, len(bars)):
            line20 = _sma_at(closes, cursor, 20)
            macd_dead = dif[cursor - 1] >= dea[cursor - 1] and dif[cursor] < dea[cursor]
            if lows[cursor] <= stop:
                exit_index = cursor
                exit_price = _stop_fill(opens[cursor], stop)
                reason = "stop3"
                break
            if line20 is not None and closes[cursor] < line20:
                exit_index = cursor
                exit_price = closes[cursor]
                reason = "ma20"
                break
            if macd_dead:
                exit_index = cursor
                exit_price = closes[cursor]
                reason = "macd_dead"
                break
        if exit_index is None or exit_price is None or reason is None:
            exit_index = len(bars) - 1
            exit_price = closes[exit_index]
            reason = "open_end"
        net = _finish(entry, exit_price, 1.0, 0.0)
        equity *= 1.0 + 0.20 * net
        trades.append({
            "entry": dates[entry_index].isoformat(),
            "exit": dates[exit_index].isoformat(),
            "hold_days": exit_index - entry_index,
            "reason": reason,
            "net_pct": net,
            "win": net > 0.0,
            "ma60_gap": gap,
            "equity": equity,
        })
        index = exit_index + 1
    return trades


def _old_gap(loaded: dict[str, tuple[DailyBar, ...]]) -> dict:
    hs_bars = _load_symbol(Path("var/swing/index_quotes.jsonl"), "000300")
    csi_bars = _load_symbol(Path("var/swing/index_quotes.jsonl"), "000852")
    growth = {bar.trading_date: bar.adjusted_close for bar in loaded["159915"]}
    dividend = {bar.trading_date: bar.adjusted_close for bar in loaded["515180"]}
    calendar = sorted(set(growth) & set(dividend))
    styles = _style_series(growth, dividend, calendar)
    heats = _overheat(growth, dividend, calendar)
    hs300 = {bar.trading_date: bar.adjusted_close for bar in hs_bars}
    csi1000 = {bar.trading_date: bar.adjusted_close for bar in csi_bars}
    hs_weeks = _weekly([hs300[day] for day in sorted(hs300)], sorted(hs300))
    csi_weeks = _weekly([csi1000[day] for day in sorted(csi1000)], sorted(csi1000))
    tagged = []
    for spec in SPECS:
        bars = loaded[spec.symbol]
        closes = [bar.adjusted_close for bar in bars]
        by_date = {bar.trading_date: pos for pos, bar in enumerate(bars)}
        row = run_symbol(spec, bars, hs300, csi1000, hs_weeks, csi_weeks, styles, heats, mode="macd")
        for trade in row["trades_detail"]:
            pos = by_date[date.fromisoformat(trade["entry"])]
            ma60 = _sma_at(closes, pos, 60)
            gap = None if ma60 is None else closes[pos] / ma60 - 1.0
            tagged.append({
                "symbol": spec.symbol,
                "name": spec.name,
                "reason": trade["reason"],
                "win": trade["win"],
                "r": trade["r"],
                "ma60_gap": gap,
            })

    def pack(chosen: list[dict]) -> dict:
        known = [trade for trade in chosen if trade["ma60_gap"] is not None]
        below = [trade for trade in known if trade["ma60_gap"] < 0.0]
        wins = [trade for trade in known if trade["win"]]
        return {
            "trades": len(known),
            "below_ma60": len(below),
            "below_share": None if not known else len(below) / len(known),
            "below_win_rate": None if not below else sum(1 for trade in below if trade["win"]) / len(below),
            "above_trades": len(known) - len(below),
            "above_win_rate": None if len(known) == len(below) else (
                sum(1 for trade in known if trade["ma60_gap"] >= 0.0 and trade["win"]) / (len(known) - len(below))
            ),
            "below_avg_r": None if not below else sum(trade["r"] for trade in below) / len(below),
            "above_avg_r": None if len(known) == len(below) else (
                sum(trade["r"] for trade in known if trade["ma60_gap"] >= 0.0) / (len(known) - len(below))
            ),
            "win_rate": None if not known else len(wins) / len(known),
            "c2": sum(1 for trade in known if trade["reason"] == "C2"),
            "c2_below": sum(1 for trade in below if trade["reason"] == "C2"),
        }

    losses = [trade for trade in tagged if not trade["win"]]
    return {
        "all": pack(tagged),
        "losses": pack(losses),
        "by_symbol": {NAMES[symbol]: pack([trade for trade in tagged if trade["symbol"] == symbol]) for symbol in NAMES},
    }


def _run_group(loaded: dict[str, tuple[DailyBar, ...]], symbols: tuple[str, ...], scheme) -> dict:
    rows = []
    pooled: list[dict] = []
    for symbol in symbols:
        trades = scheme(loaded[symbol])
        pooled.extend(trades)
        summary = _summarize(trades)
        summary["symbol"] = symbol
        summary["name"] = NAMES[symbol]
        if trades and "equity" in trades[-1]:
            summary["ending_equity_20pct"] = trades[-1]["equity"]
        rows.append(summary)
    group = _summarize(pooled)
    group["names"] = [NAMES[symbol] for symbol in symbols]
    return {"names": group["names"], "pooled": group, "symbols": rows}


def main() -> None:
    daily = Path("var/swing/daily_quotes.jsonl")
    loaded = {spec.symbol: _load_symbol(daily, spec.symbol) for spec in SPECS}
    slow = SLOW_CORE + SLOW_EXTRA
    report = {
        "cost": "two ticks of 0.001 plus 2.4 bps round trip, same as the previous run",
        "fills": "signal close; a stop gap fills at the open",
        "old_116_ma60": _old_gap(loaded),
        "scheme1_fast": _run_group(loaded, FAST, _scheme1),
        "scheme2_slow_core": _run_group(loaded, SLOW_CORE, _scheme2),
        "scheme2_slow_extra": _run_group(loaded, SLOW_EXTRA, _scheme2),
        "scheme3_fast": _run_group(loaded, FAST, _scheme3),
        "scheme3_slow": _run_group(loaded, slow, _scheme3),
    }
    destination = Path("var/swing/research/macd_three_schemes.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    old = report["old_116_ma60"]["all"]
    lost = report["old_116_ma60"]["losses"]
    print(
        "old",
        old["trades"],
        "below",
        old["below_ma60"],
        old["below_share"],
        "below_win",
        old["below_win_rate"],
        "below_R",
        old["below_avg_r"],
        "above_win",
        old["above_win_rate"],
        "above_R",
        old["above_avg_r"],
        "c2",
        old["c2"],
        "c2_below",
        old["c2_below"],
        "loss_below_share",
        lost["below_share"],
    )
    for key in ("scheme1_fast", "scheme2_slow_core", "scheme2_slow_extra", "scheme3_fast", "scheme3_slow"):
        pooled = report[key]["pooled"]
        print(
            key,
            pooled["trades"],
            pooled["win_rate"],
            pooled["payoff_ratio"],
            pooled["avg_net_pct"],
            pooled["median_hold"],
            pooled["reasons"],
        )
        for row in report[key]["symbols"]:
            print(
                " ",
                row["name"],
                row["trades"],
                row["win_rate"],
                row["payoff_ratio"],
                row["avg_net_pct"],
                row.get("ending_equity_20pct"),
                row["reasons"],
            )


if __name__ == "__main__":
    main()
