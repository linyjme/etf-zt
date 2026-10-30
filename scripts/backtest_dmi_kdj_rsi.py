"""Read-only short-swing backtest led by MACD(6,13,5).

A long entry needs a MACD golden cross, +DI above -DI, and ADX above 20.
KDJ golden cross or an RSI that is below 80 and turning up must also be true.
Exits are a MACD dead cross, a KDJ dead cross above 70, a close under MA5,
RSI above 80 turning down, or the close of the third day in the position.
Fills use the next bar's adjusted open. The script does not change the monitor.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import argparse
import json
from pathlib import Path
from typing import Any

from etf_rotation.swing_data import DailyBar
from etf_rotation.swing_indicators import (
    adx_series,
    classify_regimes,
    dmi_series,
    kdj_series,
    macd_series,
)


FEE_RATE = 0.00012
RSI_ENTRY_CAP = 80.0
RSI_EXIT_FLOOR = 80.0
ADX_MINIMUM = 20.0
KDJ_HIGH = 70.0
HOLDING_DAYS = 3
MACD_FAST = 6
MACD_SLOW = 13
MACD_SIGNAL = 5
HOLDOUT_BARS = 252
MINIMUM_SELECTION_TRADES = 30
MINIMUM_HOLDOUT_TRADES = 10
REGIMES = ("UP", "DOWN", "RANGE")

PRIMARY = (
    ("588000", "科创50ETF", "etf"),
    ("159915", "创业板ETF", "etf"),
    ("512480", "半导体ETF", "etf"),
    ("512880", "证券ETF", "etf"),
    ("512100", "中证1000ETF", "etf"),
    ("000852", "中证1000", "index"),
)
CONTRAST = (
    ("159307", "红利低波100ETF", "etf"),
    ("515180", "中证红利ETF", "etf"),
    ("159263", "价值100ETF", "etf"),
)
SHORT_SAMPLE = (
    ("563360", "中证A500ETF", "etf"),
    ("159259", "成长100ETF", "etf"),
)
VARIANTS = (
    ("control", "对照组：去掉RSI，ADX>20，平仓为MACD死叉、K>70死叉、跌破MA5或满3天"),
    ("exit_opt", "平仓优化：MACD死叉、跌破MA10、最高价回撤2%或满3天"),
    ("loose", "宽松准入：ADX>15；上涨用MACD死叉或MA10，其余市况K>70止盈"),
    ("volume", "量能确认：对照组，且当日成交量大于前5日均量"),
)
INDEX_CANDIDATES = (
    ("930955", "中证红利低波动100"),
    ("000922", "中证红利"),
    ("980081", "国证价值100"),
    ("000510", "中证A500"),
    ("000688", "科创50"),
    ("399006", "创业板指"),
    ("980080", "国证成长100"),
)


@dataclass(frozen=True)
class Config:
    dmi_period: int
    rsv_period: int
    k_smooth: int
    d_smooth: int
    rsi_period: int

    @property
    def key(self) -> str:
        return (
            f"DMI{self.dmi_period}-KDJ{self.rsv_period}"
            f"{self.k_smooth}{self.d_smooth}-RSI{self.rsi_period}"
        )


def parameter_grid() -> tuple[Config, ...]:
    configs: list[Config] = []
    for dmi_period in (12, 14):
        for rsv_period in (9, 6):
            for rsi_period in (6, 9, 14):
                configs.append(Config(dmi_period, rsv_period, 3, 3, rsi_period))
    return tuple(configs)


@dataclass(frozen=True)
class Trade:
    symbol: str
    regime: str
    signal_date: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    holdout: bool

    @property
    def gross_win(self) -> bool:
        return self.exit_price > self.entry_price

    @property
    def net_return(self) -> float:
        entry_cost = self.entry_price * (1.0 + FEE_RATE)
        exit_proceeds = self.exit_price * (1.0 - FEE_RATE)
        return exit_proceeds / entry_cost - 1.0

    @property
    def net_win(self) -> bool:
        return self.net_return > 0.0


def _load_symbol(path: Path, symbol: str) -> tuple[DailyBar, ...]:
    bars: list[DailyBar] = []
    marker = f'"symbol":"{symbol}"'
    for line in path.read_text(encoding="utf-8").splitlines():
        if marker not in line:
            continue
        bars.append(DailyBar.from_mapping(json.loads(line)))
    bars.sort(key=lambda bar: bar.trading_date)
    return tuple(bars)


def _crosses(fast: Sequence[float], slow: Sequence[float], index: int) -> tuple[bool, bool]:
    previous = fast[index - 1] - slow[index - 1]
    current = fast[index] - slow[index]
    return previous <= 0.0 < current, previous >= 0.0 > current


def _ma(closes: Sequence[float], index: int, period: int) -> float | None:
    if index + 1 < period:
        return None
    return sum(closes[index - period + 1:index + 1]) / period


def simulate(
    bars: Sequence[DailyBar],
    config: Config,
    *,
    variant: str = "control",
    macd_only: bool = False,
) -> tuple[Trade, ...]:
    """Return completed long round trips. A position still open at the sample end is dropped."""
    if len(bars) < 2:
        return ()
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    closes = [bar.adjusted_close for bar in bars]
    opens = [bar.adjusted_open for bar in bars]
    volumes = [bar.volume for bar in bars]
    dates = [bar.trading_date.isoformat() for bar in bars]
    dif, dea, _histogram = macd_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    plus_di, minus_di = dmi_series(highs, lows, closes, config.dmi_period)
    adx = adx_series(plus_di, minus_di, config.dmi_period)
    k_values, d_values, _j_values = kdj_series(
        highs, lows, closes, config.rsv_period, config.k_smooth, config.d_smooth,
    )
    regimes = classify_regimes(closes)
    holdout_start = max(0, len(bars) - HOLDOUT_BARS)
    adx_floor = 15.0 if variant == "loose" else ADX_MINIMUM
    trades: list[Trade] = []
    entry_signal: int | None = None
    for index in range(1, len(bars) - 1):
        macd_golden, macd_dead = _crosses(dif, dea, index)
        if entry_signal is None:
            if macd_only:
                if macd_golden:
                    entry_signal = index
                continue
            plus = plus_di[index]
            minus = minus_di[index]
            trend_strength = adx[index]
            kdj_golden, _kdj_dead = _crosses(k_values, d_values, index)
            direction_ok = (
                plus is not None
                and minus is not None
                and trend_strength is not None
                and plus > minus
                and trend_strength > adx_floor
            )
            volume_ok = True
            if variant == "volume":
                if index < 5:
                    volume_ok = False
                else:
                    volume_ok = volumes[index] > sum(volumes[index - 5:index]) / 5.0
            if (
                macd_golden
                and direction_ok
                and kdj_golden
                and volume_ok
                and regimes[index] is not None
            ):
                entry_signal = index
            continue
        _kdj_golden, kdj_dead = _crosses(k_values, d_values, index)
        ma5 = _ma(closes, index, 5)
        ma10 = _ma(closes, index, 10)
        time_stop = index >= entry_signal + HOLDING_DAYS
        fill_in = entry_signal + 1
        trail = False
        if index >= fill_in:
            peak = max(highs[fill_in:index + 1])
            trail = closes[index] <= peak * (1.0 - 0.02)
        regime_now = regimes[index]
        if macd_only:
            exit_now = macd_dead
        elif variant == "exit_opt":
            exit_now = (
                macd_dead
                or time_stop
                or trail
                or (ma10 is not None and closes[index] < ma10)
            )
        elif variant == "loose" and regime_now != "UP":
            exit_now = time_stop or k_values[index] > KDJ_HIGH
        elif variant == "loose":
            exit_now = (
                macd_dead
                or time_stop
                or (ma10 is not None and closes[index] < ma10)
            )
        else:
            exit_now = (
                macd_dead
                or time_stop
                or (kdj_dead and k_values[index] > KDJ_HIGH)
                or (ma5 is not None and closes[index] < ma5)
            )
        if not exit_now:
            continue
        fill_out = index + 1
        trades.append(Trade(
            symbol=bars[index].symbol,
            regime=str(regimes[entry_signal] or "WARMUP"),
            signal_date=dates[entry_signal],
            entry_date=dates[fill_in],
            exit_date=dates[fill_out],
            entry_price=opens[fill_in],
            exit_price=opens[fill_out],
            holdout=entry_signal >= holdout_start,
        ))
        entry_signal = None
    return tuple(trades)


def _payoff(trades: Sequence[Trade]) -> float | None:
    wins = [trade.net_return for trade in trades if trade.net_return > 0.0]
    losses = [trade.net_return for trade in trades if trade.net_return < 0.0]
    if not wins or not losses:
        return None
    return (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))


def _max_drawdown(bars: Sequence[DailyBar], trades: Sequence[Trade]) -> float:
    """Peak-to-trough decline of a one-unit long book, as a positive fraction."""
    if not bars:
        return 0.0
    buys: dict[int, Trade] = {}
    sells: dict[int, Trade] = {}
    dates = [bar.trading_date.isoformat() for bar in bars]
    index_by_date = {trading_date: index for index, trading_date in enumerate(dates)}
    for trade in trades:
        buy_at = index_by_date.get(trade.entry_date)
        sell_at = index_by_date.get(trade.exit_date)
        if buy_at is None or sell_at is None:
            continue
        buys[buy_at] = trade
        sells[sell_at] = trade
    cash = 1.0
    shares = 0.0
    peak = 1.0
    worst = 0.0
    for index, bar in enumerate(bars):
        if index in buys and shares == 0.0:
            shares = cash / (bar.adjusted_open * (1.0 + FEE_RATE))
            cash = 0.0
        if index in sells and shares > 0.0:
            cash = shares * bar.adjusted_open * (1.0 - FEE_RATE)
            shares = 0.0
        equity = cash + shares * bar.adjusted_close
        if equity > peak:
            peak = equity
        if peak > 0.0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def summarize(trades: Sequence[Trade], bars_by_symbol: dict[str, Sequence[DailyBar]]) -> dict[str, Any]:
    wins = sum(1 for trade in trades if trade.gross_win)
    net_wins = sum(1 for trade in trades if trade.net_win)
    count = len(trades)
    by_regime: dict[str, Any] = {}
    for regime in REGIMES:
        group = [trade for trade in trades if trade.regime == regime]
        group_count = len(group)
        by_regime[regime] = {
            "trades": group_count,
            "gross_win_rate": None if group_count == 0 else sum(1 for trade in group if trade.gross_win) / group_count,
            "net_win_rate": None if group_count == 0 else sum(1 for trade in group if trade.net_win) / group_count,
            "payoff_ratio": _payoff(group),
        }
    by_symbol: dict[str, Any] = {}
    symbols = sorted({trade.symbol for trade in trades} | set(bars_by_symbol))
    for symbol in symbols:
        group = [trade for trade in trades if trade.symbol == symbol]
        group_count = len(group)
        symbol_bars = bars_by_symbol.get(symbol, ())
        buy_hold = None
        if len(symbol_bars) >= 2 and symbol_bars[0].adjusted_close > 0.0:
            buy_hold = symbol_bars[-1].adjusted_close / symbol_bars[0].adjusted_close - 1.0
        by_symbol[symbol] = {
            "trades": group_count,
            "gross_win_rate": None if group_count == 0 else sum(1 for trade in group if trade.gross_win) / group_count,
            "net_win_rate": None if group_count == 0 else sum(1 for trade in group if trade.net_win) / group_count,
            "payoff_ratio": _payoff(group),
            "max_drawdown": _max_drawdown(symbol_bars, group) if symbol_bars else None,
            "buy_and_hold_return": buy_hold,
            "first_date": symbol_bars[0].trading_date.isoformat() if symbol_bars else None,
            "last_date": symbol_bars[-1].trading_date.isoformat() if symbol_bars else None,
            "bar_count": len(symbol_bars),
        }
    return {
        "trades": count,
        "gross_win_rate": None if count == 0 else wins / count,
        "net_win_rate": None if count == 0 else net_wins / count,
        "payoff_ratio": _payoff(trades),
        "max_drawdown": max(
            (
                value["max_drawdown"]
                for value in by_symbol.values()
                if value["max_drawdown"] is not None
            ),
            default=None,
        ),
        "by_regime": by_regime,
        "by_symbol": by_symbol,
    }


def _split_trades(
    trades: Sequence[Trade], bars: Sequence[DailyBar],
) -> tuple[tuple[Trade, ...], tuple[Trade, ...]]:
    holdout_start = max(0, len(bars) - HOLDOUT_BARS)
    dates = [bar.trading_date.isoformat() for bar in bars]
    cutoff = dates[holdout_start]
    training = tuple(trade for trade in trades if trade.signal_date < cutoff)
    holdout = tuple(trade for trade in trades if trade.signal_date >= cutoff)
    return training, holdout


def _selection_key(summary: dict[str, Any]) -> tuple[float, float, float]:
    win_rate = summary["gross_win_rate"]
    payoff = summary["payoff_ratio"]
    drawdown = summary["max_drawdown"]
    return (
        -1.0 if win_rate is None else -float(win_rate),
        -1.0 if payoff is None else -float(payoff),
        1.0 if drawdown is None else float(drawdown),
    )


def build_report(
    bars_by_symbol: dict[str, tuple[DailyBar, ...]],
    names: dict[str, str],
    primary_symbols: Sequence[str],
    contrast_symbols: Sequence[str],
    short_symbols: Sequence[str],
    missing_indexes: Sequence[dict[str, str]],
) -> dict[str, Any]:
    fixed = Config(14, 9, 3, 3, 6)
    configs = []
    for variant, label in VARIANTS:
        training_trades: list[Trade] = []
        holdout_trades: list[Trade] = []
        contrast_trades: list[Trade] = []
        short_trades: list[Trade] = []
        training_bars: dict[str, Sequence[DailyBar]] = {}
        holdout_bars: dict[str, Sequence[DailyBar]] = {}
        contrast_bars: dict[str, Sequence[DailyBar]] = {}
        short_bars: dict[str, Sequence[DailyBar]] = {}
        for symbol, bars in bars_by_symbol.items():
            trades = simulate(bars, fixed, variant=variant)
            if symbol in contrast_symbols:
                contrast_trades.extend(trades)
                contrast_bars[symbol] = bars
                continue
            if symbol in short_symbols:
                short_trades.extend(trades)
                short_bars[symbol] = bars
                continue
            training, holdout = _split_trades(trades, bars)
            training_trades.extend(training)
            holdout_trades.extend(holdout)
            training_bars[symbol] = bars
            holdout_bars[symbol] = bars
        configs.append({
            "key": variant,
            "label": label,
            "dmi_period": 14,
            "kdj": [9, 3, 3],
            "training": summarize(training_trades, training_bars),
            "holdout": summarize(holdout_trades, holdout_bars),
            "contrast": summarize(contrast_trades, contrast_bars),
            "short_sample": summarize(short_trades, short_bars),
        })
    baseline_training: list[Trade] = []
    baseline_holdout: list[Trade] = []
    baseline_bars: dict[str, Sequence[DailyBar]] = {}
    for symbol, bars in bars_by_symbol.items():
        if symbol in contrast_symbols or symbol in short_symbols:
            continue
        trades = simulate(bars, fixed, macd_only=True)
        training, holdout = _split_trades(trades, bars)
        baseline_training.extend(training)
        baseline_holdout.extend(holdout)
        baseline_bars[symbol] = bars
    goal = [
        item for item in configs
        if item["training"]["trades"] >= MINIMUM_SELECTION_TRADES
        and (item["training"]["gross_win_rate"] or 0.0) >= 0.48
        and (item["training"]["payoff_ratio"] or 0.0) >= 1.8
    ]
    selected = None
    if goal:
        winner = max(goal, key=lambda item: float(item["training"]["payoff_ratio"]))
        training_rate = float(winner["training"]["gross_win_rate"])
        training_payoff = float(winner["training"]["payoff_ratio"])
        holdout_rate = winner["holdout"]["gross_win_rate"]
        holdout_payoff = winner["holdout"]["payoff_ratio"]
        holdout_count = winner["holdout"]["trades"]
        if holdout_count < MINIMUM_HOLDOUT_TRADES or holdout_rate is None or holdout_payoff is None:
            executable = False
            reason = "HOLDOUT_TOO_SMALL"
        elif float(holdout_rate) < 0.45 or float(holdout_payoff) < 1.8:
            executable = False
            reason = "HOLDOUT_MISSED_GOAL"
        else:
            executable = True
            reason = "HOLDOUT_HELD"
        selected = {
            "key": winner["key"],
            "executable": executable,
            "reason": reason,
            "training_gross_win_rate": training_rate,
            "training_payoff_ratio": training_payoff,
            "holdout_gross_win_rate": holdout_rate,
            "holdout_payoff_ratio": holdout_payoff,
            "training_trades": winner["training"]["trades"],
            "holdout_trades": holdout_count,
        }
    return {
        "schema_version": 3,
        "rules": {
            "macd": [MACD_FAST, MACD_SLOW, MACD_SIGNAL],
            "entry": "MACD golden cross, +DI > -DI, ADX floor, and KDJ(9,3,3) golden cross. No RSI.",
            "variants": [label for _key, label in VARIANTS],
            "baseline": "MACD golden cross and dead cross only",
            "goal": "training win rate at least 48% and payoff at least 1.8; holdout win rate at least 45% and payoff at least 1.8",
        },
        "fee_rate": FEE_RATE,
        "adx_minimum": ADX_MINIMUM,
        "holding_days": HOLDING_DAYS,
        "holdout_bars": HOLDOUT_BARS,
        "minimum_selection_trades": MINIMUM_SELECTION_TRADES,
        "names": names,
        "primary_symbols": list(primary_symbols),
        "contrast_symbols": list(contrast_symbols),
        "short_symbols": list(short_symbols),
        "missing_indexes": list(missing_indexes),
        "baseline": {
            "key": "MACD6135",
            "training": summarize(baseline_training, baseline_bars),
            "holdout": summarize(baseline_holdout, baseline_bars),
        },
        "configs": configs,
        "selected": selected,
    }


def _round_numbers(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {key: _round_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round_numbers(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest DMI, KDJ and RSI on stored daily bars")
    parser.add_argument("--daily", type=Path, default=Path("var/swing/daily_quotes.jsonl"))
    parser.add_argument("--index", type=Path, default=Path("var/swing/index_quotes.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("var/swing/research/dmi_kdj_rsi_backtest.json"))
    args = parser.parse_args()
    universe = PRIMARY + CONTRAST + SHORT_SAMPLE
    names = {symbol: name for symbol, name, _kind in universe}
    names.update({symbol: name for symbol, name in INDEX_CANDIDATES})
    bars_by_symbol: dict[str, tuple[DailyBar, ...]] = {}
    primary_symbols = [symbol for symbol, _name, _kind in PRIMARY]
    missing = []
    for symbol, _name, kind in universe:
        path = args.index if kind == "index" else args.daily
        bars = _load_symbol(path, symbol)
        if bars:
            bars_by_symbol[symbol] = bars
        else:
            missing.append({"code": symbol, "name": names[symbol]})
            primary_symbols = [item for item in primary_symbols if item != symbol]
    for symbol, name in INDEX_CANDIDATES:
        bars = _load_symbol(args.index, symbol)
        if bars:
            bars_by_symbol[symbol] = bars
            primary_symbols.append(symbol)
        else:
            missing.append({"code": symbol, "name": name})
    missing.append({"code": "HSTECH", "name": "恒生科技ETF不在仓库"})
    report = _round_numbers(build_report(
        bars_by_symbol,
        names,
        primary_symbols,
        [symbol for symbol, _name, _kind in CONTRAST],
        [symbol for symbol, _name, _kind in SHORT_SAMPLE],
        missing,
    ))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for item in report["configs"]:
        training = item["training"]
        holdout = item["holdout"]
        print(
            f"{item['key']} train n={training['trades']} win={training['gross_win_rate']} "
            f"pay={training['payoff_ratio']} | hold n={holdout['trades']} "
            f"win={holdout['gross_win_rate']} pay={holdout['payoff_ratio']}"
        )
    selected = report["selected"]
    print("selected", selected)


if __name__ == "__main__":
    main()
