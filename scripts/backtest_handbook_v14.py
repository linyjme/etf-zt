"""Sequential backtest of handbook v1.4 A-type and B-type rules.

Decision prices are daily closes, the same approximation the handbook uses
for its own event study. One symbol is traded at a time. The live monitor
is not changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path

from etf_rotation.swing_data import DailyBar
from etf_rotation.swing_indicators import macd_series, rsi_series

from backtest_dmi_kdj_rsi import FEE_RATE, _load_symbol


TICK = 0.001
ROUND_TRIP_FEE = 0.00024


@dataclass(frozen=True)
class Spec:
    symbol: str
    name: str
    broad: bool
    environment: str  # hs300 or csi1000


SPECS = (
    Spec("515180", "中证红利", True, "hs300"),
    Spec("159915", "创业板指", False, "csi1000"),
    Spec("588000", "科创50", False, "csi1000"),
    Spec("510300", "沪深300", True, "hs300"),
    Spec("159307", "红利低波100", True, "hs300"),
    Spec("510500", "中证500", True, "csi1000"),
)
STYLE = {
    "159915": "growth",
    "588000": "growth",
    "515180": "dividend",
    "159307": "dividend",
    "510300": "neutral",
    "510500": "neutral",
}


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _sma_at(values: list[float], index: int, period: int) -> float | None:
    if index + 1 < period:
        return None
    return _mean(values[index - period + 1:index + 1])


def _direction(values: list[float], index: int, period: int) -> str | None:
    now = _sma_at(values, index, period)
    previous = _sma_at(values, index - 10, period)
    if now is None or previous is None or previous == 0.0:
        return None
    change = now / previous - 1.0
    if change >= 0.005:
        return "up"
    if change <= -0.005:
        return "down"
    return "flat"


def _rsi_min(rsis: list[float | None], index: int, window: int = 10) -> float | None:
    sample = [value for value in rsis[index - window + 1:index + 1] if value is not None]
    if len(sample) < window:
        return None
    return min(sample)


def _adx(highs: list[float], lows: list[float], closes: list[float]) -> list[float | None]:
    count = len(closes)
    true_range = [0.0] * count
    plus_dm = [0.0] * count
    minus_dm = [0.0] * count
    for index in range(1, count):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm[index] = up_move if up_move > down_move and up_move > 0.0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0.0 else 0.0
        true_range[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - closes[index - 1]),
            abs(lows[index] - closes[index - 1]),
        )
    dx = [None] * count
    for index in range(14, count):
        span = sum(true_range[index - 13:index + 1])
        if span <= 0.0:
            continue
        plus = 100.0 * sum(plus_dm[index - 13:index + 1]) / span
        minus = 100.0 * sum(minus_dm[index - 13:index + 1]) / span
        if plus + minus <= 0.0:
            continue
        dx[index] = 100.0 * abs(plus - minus) / (plus + minus)
    adx = [None] * count
    for index in range(19, count):
        window = dx[index - 5:index + 1]
        if any(value is None for value in window):
            continue
        adx[index] = sum(window) / 6.0
    return adx


def _atr(highs: list[float], lows: list[float], closes: list[float], index: int) -> float | None:
    if index < 14:
        return None
    ranges = []
    for cursor in range(index - 13, index + 1):
        previous = closes[cursor - 1] if cursor else closes[cursor]
        ranges.append(max(
            highs[cursor] - lows[cursor],
            abs(highs[cursor] - previous),
            abs(lows[cursor] - previous),
        ))
    return _mean(ranges)


def _long_shadow(open_price: float, high: float, low: float, close: float) -> bool:
    span = high - low
    if span <= 0.0:
        return False
    upper = high - max(open_price, close)
    body = abs(close - open_price)
    return upper >= body * 2.0 and upper >= span * 0.5


def _weekly(closes: list[float], dates: list[date]) -> list[tuple[date, float]]:
    weeks: list[tuple[date, float]] = []
    for trading_day, close in zip(dates, closes):
        key = trading_day.isocalendar()[:2]
        if weeks and weeks[-1][0].isocalendar()[:2] == key:
            weeks[-1] = (trading_day, close)
        else:
            weeks.append((trading_day, close))
    return weeks


def _completed_week_index(dates: list[date], index: int, weeks: list[tuple[date, float]]) -> int | None:
    """Index of the last week that ended before the current ISO week."""
    current = dates[index].isocalendar()[:2]
    found = None
    for position, (trading_day, _close) in enumerate(weeks):
        if trading_day.isocalendar()[:2] < current:
            found = position
        else:
            break
    return found


def _bollinger(closes: list[float], index: int) -> tuple[float, float] | None:
    middle = _sma_at(closes, index, 20)
    if middle is None:
        return None
    window = closes[index - 19:index + 1]
    variance = _mean([(value - middle) ** 2 for value in window])
    deviation = variance ** 0.5
    return middle + 2.0 * deviation, middle - 2.0 * deviation


def _style_series(growth: dict[date, float], dividend: dict[date, float], dates: list[date]) -> dict[date, str]:
    aligned = [day for day in dates if day in growth and day in dividend and dividend[day] > 0.0]
    ratio = [growth[day] / dividend[day] for day in aligned]
    raw: list[str] = []
    for index, _day in enumerate(aligned):
        ma60 = _sma_at(ratio, index, 60)
        direction = _direction(ratio, index, 20)
        if ma60 is None or direction is None or index < 20 or ratio[index - 20] == 0.0:
            raw.append("balance")
            continue
        momentum = ratio[index] / ratio[index - 20] - 1.0
        value = ratio[index]
        if value > ma60 and direction == "up" and momentum >= 0.05:
            raw.append("growth_strong")
        elif value > ma60 and direction in ("up", "flat") and 0.02 <= momentum < 0.05:
            raw.append("growth_mild")
        elif value < ma60 and direction == "down" and momentum <= -0.05:
            raw.append("div_strong")
        elif value < ma60 and direction in ("down", "flat") and -0.05 < momentum <= -0.02:
            raw.append("div_mild")
        else:
            raw.append("balance")
    confirmed = "balance"
    pending = None
    pending_count = 0
    result: dict[date, str] = {}
    adjacent = {
        ("growth_strong", "growth_mild"), ("growth_mild", "growth_strong"),
        ("div_strong", "div_mild"), ("div_mild", "div_strong"),
    }
    for day, tier in zip(aligned, raw):
        if tier == confirmed or (confirmed, tier) in adjacent:
            confirmed = tier
            pending = None
            pending_count = 0
        elif tier == pending:
            pending_count += 1
            if pending_count >= 2:
                confirmed = tier
                pending = None
                pending_count = 0
        else:
            pending = tier
            pending_count = 1
        result[day] = confirmed
    return result


def _overheat(growth: dict[date, float], dividend: dict[date, float], dates: list[date]) -> dict[date, str | None]:
    aligned = [day for day in dates if day in growth and day in dividend and dividend[day] > 0.0]
    ratio = [growth[day] / dividend[day] for day in aligned]
    marks: dict[date, str | None] = {}
    for index, day in enumerate(aligned):
        ma60 = _sma_at(ratio, index, 60)
        if ma60 is None or ma60 == 0.0:
            marks[day] = None
            continue
        gap = (ratio[index] - ma60) / ma60
        if gap > 0.15:
            marks[day] = "growth"
        elif gap < -0.15:
            marks[day] = "dividend"
        else:
            marks[day] = None
    return marks


def _market_ok(closes: list[float], index: int, weekly_unhealthy: bool) -> bool:
    if weekly_unhealthy:
        return False
    ma60 = _sma_at(closes, index, 60)
    direction = _direction(closes, index, 20)
    if ma60 is None or direction is None:
        return False
    return closes[index] > ma60 and direction in ("up", "flat")


def _weekly_unhealthy(weeks: list[tuple[date, float]], week_index: int | None, require_down: bool) -> bool:
    if week_index is None or week_index < 20:
        return False
    closes = [close for _day, close in weeks]
    ma20 = _mean(closes[week_index - 19:week_index + 1])
    if closes[week_index] >= ma20:
        return False
    if not require_down:
        return True
    previous = _mean(closes[week_index - 29:week_index - 9]) if week_index >= 29 else None
    if previous is None:
        return False
    return ma20 < previous * 0.995


@dataclass
class Position:
    kind: str
    entry_index: int
    entry: float
    stop: float
    initial_risk: float
    remaining: float
    half: bool
    peak: float
    scaled: bool = False
    breakeven: bool = False
    trailing: bool = False
    box_top: float | None = None
    above10: bool = False
    above20: bool = False


def _close_trade(position: Position, exit_price: float, equity: float) -> tuple[float, float]:
    risk = position.initial_risk
    if risk <= 0.0:
        return equity, 0.0
    move = exit_price - position.entry
    cost = 2.0 * TICK + position.entry * ROUND_TRIP_FEE
    r_multiple = (move - cost) * position.remaining / risk
    budget = 0.005 if position.half else 0.01
    equity *= 1.0 + budget * r_multiple
    return equity, r_multiple


def run_symbol(
    spec: Spec,
    bars: tuple[DailyBar, ...],
    hs300: dict[date, float],
    csi1000: dict[date, float],
    hs_weeks: list[tuple[date, float]],
    csi_weeks: list[tuple[date, float]],
    styles: dict[date, str],
    heats: dict[date, str | None],
    mode: str = "handbook",
) -> dict:
    closes = [bar.adjusted_close for bar in bars]
    highs = [bar.adjusted_high for bar in bars]
    lows = [bar.adjusted_low for bar in bars]
    opens = [bar.adjusted_open for bar in bars]
    volumes = [bar.volume for bar in bars]
    dates = [bar.trading_date for bar in bars]
    dif, dea, histogram = macd_series(closes, 6, 13, 5)
    rsis6 = rsi_series(closes, 6)
    rsis14 = rsi_series(closes, 14)
    adx = _adx(highs, lows, closes)
    own_weeks = _weekly(closes, dates)
    pullback = 0.03 if spec.broad else 0.05
    bias_low, bias_high = (-0.02, 0.04) if spec.broad else (-0.03, 0.06)
    bias6_cap = 0.03 if spec.broad else 0.05
    stop_cap = 0.04 if spec.broad else 0.07
    s1 = 0.08 if spec.broad else 0.12
    s2 = 80.0 if spec.broad else 85.0
    s3_jump = 0.04 if spec.broad else 0.06
    s3_bias = 0.05 if spec.broad else 0.08
    trail_period = 20 if spec.broad else 10
    group = STYLE[spec.symbol]
    equity = 1.0
    peak_equity = 1.0
    worst = 0.0
    position: Position | None = None
    cooldown_until = -1
    loss_streak = 0
    reduced_risk = False
    trades = []
    exit_next: str | None = None

    def record(reason: str, exit_index: int, exit_price: float) -> None:
        nonlocal equity, peak_equity, worst, position, cooldown_until, loss_streak, reduced_risk, exit_next
        assert position is not None
        equity, r_multiple = _close_trade(position, exit_price, equity)
        peak_equity = max(peak_equity, equity)
        worst = max(worst, (peak_equity - equity) / peak_equity if peak_equity else 0.0)
        trades.append({
            "entry": dates[position.entry_index].isoformat(),
            "exit": dates[exit_index].isoformat(),
            "reason": reason,
            "r": r_multiple,
            "win": r_multiple > 0.0,
            "kind": position.kind,
            "above10": position.above10,
            "above20": position.above20,
        })
        if reason in ("stop", "C2", "C3", "C4", "E1", "E2", "E4"):
            cooldown_until = exit_index + 5
        if r_multiple < 0.0:
            loss_streak += 1
        elif r_multiple >= 1.0:
            loss_streak = 0
            reduced_risk = False
        else:
            loss_streak = 0
        if loss_streak >= 3:
            reduced_risk = True
        position = None
        exit_next = None

    start = 60 if mode == "macd" else 250
    for index in range(start, len(bars) - 1):
        if exit_next and position is not None:
            record(exit_next, index, closes[index])
            exit_next = None
        if position is not None:
            held = index - position.entry_index + 1
            price = closes[index]
            r_now = (price - position.entry) / position.initial_risk
            if highs[index] > position.peak:
                position.peak = highs[index]
            if lows[index] <= position.stop:
                fill = opens[index] if opens[index] <= position.stop else position.stop
                record("stop", index, fill)
                continue
            if r_now >= 1.0 and not position.breakeven:
                position.stop = max(position.stop, position.entry)
                position.breakeven = True
            if r_now >= 2.0 or position.scaled:
                position.trailing = True
            made_new_high = highs[index] > max(highs[position.entry_index:index] or [highs[position.entry_index]])
            if position.kind == "B" and position.box_top and held <= 3 and price < position.box_top:
                record("E1", index, price)
                continue
            below_ma20 = (_sma_at(closes, index, 20) or price) > price
            macd_dead = index > 0 and dif[index - 1] >= dea[index - 1] and dif[index] < dea[index]
            turned_green = histogram[index - 1] > 0.0 >= histogram[index]
            if position.kind == "A" and below_ma20 and (macd_dead or turned_green):
                record("E2", index, price)
                continue
            week_pos = _completed_week_index(dates, index, own_weeks)
            if _weekly_unhealthy(own_weeks, week_pos, False):
                exit_next = "C3"
            hs_week = _completed_week_index(dates, index, hs_weeks)
            if _weekly_unhealthy(hs_weeks, hs_week, True):
                exit_next = "C4"
            ma60 = _sma_at(closes, index, 60)
            if ma60 and (price < ma60 * 0.98 or (
                index > 0 and closes[index - 1] < ma60 and price < (_sma_at(closes, index - 1, 60) or ma60)
            )):
                exit_next = "C2"
            if position is not None and position.trailing:
                trail = _sma_at(closes, index, trail_period)
                if trail and price < trail:
                    record("trail", index, price)
                    continue
            if position is not None and held >= 25 and r_now < 1.0:
                record("T25", index, price)
                continue
            if position is not None and held >= 10 and not made_new_high and r_now < 1.0:
                record("E3", index, price)
                continue
            if position is not None and not position.scaled:
                bias20 = price / (_sma_at(closes, index, 20) or price) - 1.0
                upper = _bollinger(closes, index)
                hot = (
                    rsis14[index] is not None
                    and rsis14[index] > s2
                    and upper is not None
                    and price > upper[0]
                )
                volume_base = _sma_at(volumes, index, 20)
                emotional = False
                if held >= 2 and volume_base:
                    gain = price / closes[index - 1] - 1.0 if closes[index - 1] else 0.0
                    bias6 = price / (_sma_at(closes, index, 6) or price) - 1.0
                    emotional = (
                        (volumes[index] >= volume_base * 2.5 and price > opens[index])
                        or (gain >= s3_jump and upper is not None and price > upper[0])
                        or (bias6 > s3_bias and volumes[index] >= volume_base * 1.5)
                    )
                if bias20 > s1 or hot or emotional:
                    position.remaining *= 0.5
                    position.scaled = True
                    position.trailing = True
                    position.stop = max(position.stop, position.entry)
            continue

        if index <= cooldown_until or loss_streak >= 5:
            continue
        if mode == "macd":
            ma20 = _sma_at(closes, index, 20)
            ma10 = _sma_at(closes, index, 10)
            if ma20 is None or ma10 is None or index < 1:
                continue
            price = closes[index]
            golden = dif[index - 1] <= dea[index - 1] and dif[index] > dea[index]
            above10 = price > ma10
            above20 = price > ma20
            if not (golden and (above10 or above20)):
                continue
            entry = price
            atr = _atr(highs, lows, closes, index)
            if atr is None:
                continue
            stop = entry - 2.0 * atr
            if stop >= entry:
                continue
            half = dif[index] < 0.0 or reduced_risk
            position = Position(
                kind="M",
                entry_index=index,
                entry=entry,
                stop=stop,
                initial_risk=entry - stop,
                remaining=0.5 if half else 1.0,
                half=half,
                peak=highs[index],
                above10=above10,
                above20=above20,
            )
            continue
        ma60 = _sma_at(closes, index, 60)
        ma20 = _sma_at(closes, index, 20)
        ma10 = _sma_at(closes, index, 10)
        ma20_dir = _direction(closes, index, 20)
        if ma60 is None or ma20 is None or ma10 is None or ma20_dir is None:
            continue
        price = closes[index]
        if not (price > ma60 and (ma20_dir == "up" or ma20 > ma60)):
            continue
        hs_week = _completed_week_index(dates, index, hs_weeks)
        csi_week = _completed_week_index(dates, index, csi_weeks)
        hs_dates = sorted(hs300)
        csi_dates = sorted(csi1000)
        hs_index = max((pos for pos, day in enumerate(hs_dates) if day <= dates[index]), default=None)
        csi_index = max((pos for pos, day in enumerate(csi_dates) if day <= dates[index]), default=None)
        hs_closes = [hs300[day] for day in hs_dates]
        csi_closes = [csi1000[day] for day in csi_dates]
        hs_ok = hs_index is not None and _market_ok(hs_closes, hs_index, _weekly_unhealthy(hs_weeks, hs_week, True))
        csi_ok = csi_index is not None and _market_ok(csi_closes, csi_index, _weekly_unhealthy(csi_weeks, csi_week, False))
        if _weekly_unhealthy(hs_weeks, hs_week, True):
            continue
        if spec.environment == "hs300" and not hs_ok:
            continue
        if spec.environment == "csi1000" and not csi_ok:
            continue
        state_half = hs_ok != csi_ok
        prior_day = dates[index - 1]
        tier = styles.get(prior_day, "balance")
        heat = heats.get(prior_day)
        style_name = STYLE[spec.symbol]
        if style_name == "growth" and tier == "div_strong":
            continue
        if style_name == "dividend" and tier == "growth_strong":
            continue
        if adx[index] is None or adx[index] < 25.0:
            continue
        week_pos = _completed_week_index(dates, index, own_weeks)
        if week_pos is None or week_pos < 20:
            continue
        week_closes = [close for _day, close in own_weeks]
        week_ma20 = _mean(week_closes[week_pos - 19:week_pos + 1])
        week_ma10 = _mean(week_closes[week_pos - 9:week_pos + 1]) if week_pos >= 9 else None
        ma10_falling = False
        if week_pos >= 12:
            last3 = [
                _mean(week_closes[week_pos - 9 - shift:week_pos + 1 - shift])
                for shift in (0, 1, 2)
            ]
            ma10_falling = last3[0] < last3[1] < last3[2]
        weekly_a = week_closes[week_pos] > week_ma20 and not ma10_falling
        if index < 60 or closes[index] <= closes[index - 60]:
            continue
        stage_slice = closes[index - 29:index + 1]
        stage_high = max(stage_slice)
        high_at = index - 29 + stage_slice.index(stage_high)
        days_since = index - high_at
        pull_low = min(closes[high_at:index + 1])
        drop = (stage_high - pull_low) / stage_high if stage_high else 0.0
        a1 = 5 <= days_since <= 15 and drop >= pullback
        a2 = min(lows[index - 14:index + 1]) <= ma10 * 1.01
        if not (a1 or a2):
            continue
        pull_start = high_at + 1
        pull_end = index
        if pull_end <= pull_start:
            continue
        quiet = 0
        counted = 0
        for cursor in range(pull_start, pull_end):
            base = _sma_at(volumes, cursor, 20)
            if base is None:
                continue
            counted += 1
            if volumes[cursor] < base:
                quiet += 1
        if counted == 0 or quiet <= counted / 2.0:
            continue
        reclaimed = None
        for cursor in range(max(1, index - 2), index + 1):
            line10 = _sma_at(closes, cursor, 10)
            line20 = _sma_at(closes, cursor, 20)
            previous10 = _sma_at(closes, cursor - 1, 10)
            previous20 = _sma_at(closes, cursor - 1, 20)
            crossed = (
                (line10 and previous10 and closes[cursor - 1] <= previous10 and closes[cursor] > line10)
                or (line20 and previous20 and closes[cursor - 1] <= previous20 and closes[cursor] > line20)
            )
            if crossed and not _long_shadow(opens[cursor], highs[cursor], lows[cursor], closes[cursor]):
                reclaimed = cursor
        if reclaimed is None:
            continue
        bias20 = price / ma20 - 1.0
        if not (bias_low <= bias20 <= bias_high):
            continue
        bias6 = price / (_sma_at(closes, index, 6) or price) - 1.0
        if bias6 > bias6_cap:
            continue
        spread = max(ma10, ma20, ma60) - min(ma10, ma20, ma60)
        if spread <= min(ma10, ma20, ma60) * 0.02 and ma20_dir == "flat":
            continue
        rsi_low = _rsi_min(rsis6, index, 10)
        volume_base = _sma_at(volumes, index, 20)
        a6_rsi = rsi_low is not None and rsi_low >= 40.0 and (rsis6[index] or 0.0) > 50.0
        a6_volume = (
            price > opens[index]
            and volume_base is not None
            and volumes[index] >= volume_base * 1.2
            and reclaimed == index
        )
        if not (a6_rsi or a6_volume):
            continue
        env_closes = hs_closes if spec.environment == "hs300" else csi_closes
        env_index = hs_index if spec.environment == "hs300" else csi_index
        if env_index is None or env_index < 20:
            continue
        relative = closes[index] / closes[index - 20] - env_closes[env_index] / env_closes[env_index - 20]
        if relative < -0.03:
            continue
        if rsis14[index] is not None and rsis14[index] < 30.0 and closes[index] <= min(closes[index - 20:index]):
            continue
        entry = price
        atr = _atr(highs, lows, closes, index)
        if atr is None:
            continue
        trough = min(lows[high_at:index + 1])
        stop = max(trough * 0.99, entry - 2.0 * atr)
        if stop >= entry:
            continue
        distance = (entry - stop) / entry
        if distance > stop_cap:
            continue
        half = state_half or dif[index] < 0.0 or (-0.03 <= relative < 0.0) or reduced_risk
        if spec.broad is False and 0.06 <= distance <= 0.07:
            half = True
        if style_name == "growth" and (tier == "div_mild" or heat == "growth"):
            half = True
        if style_name == "dividend" and (tier == "growth_mild" or heat == "dividend"):
            half = True
        if week_ma10 is None:
            week_ma10 = week_ma20
        position = Position(
            kind="A",
            entry_index=index,
            entry=entry,
            stop=stop,
            initial_risk=entry - stop,
            remaining=0.5 if half else 1.0,
            half=half,
            peak=highs[index],
        )
        if not weekly_a:
            continue
    if position is not None:
        record("open_end", len(bars) - 1, closes[-1])
    wins = [trade for trade in trades if trade["win"]]
    losses = [trade for trade in trades if not trade["win"]]
    payoff = None
    if wins and losses:
        payoff = (sum(trade["r"] for trade in wins) / len(wins)) / abs(sum(trade["r"] for trade in losses) / len(losses))
    return {
        "symbol": spec.symbol,
        "name": spec.name,
        "bars": len(bars),
        "first": dates[0].isoformat() if dates else None,
        "last": dates[-1].isoformat() if dates else None,
        "trades": len(trades),
        "win_rate": None if not trades else len(wins) / len(trades),
        "payoff_ratio": payoff,
        "avg_r": None if not trades else sum(trade["r"] for trade in trades) / len(trades),
        "ending_equity": equity,
        "max_drawdown": worst,
        "reasons": {reason: sum(1 for trade in trades if trade["reason"] == reason) for reason in sorted({trade["reason"] for trade in trades})},
        "trades_detail": trades,
    }


def main() -> None:
    daily = Path("var/swing/daily_quotes.jsonl")
    index = Path("var/swing/index_quotes.jsonl")
    loaded = {spec.symbol: _load_symbol(daily, spec.symbol) for spec in SPECS}
    hs_bars = _load_symbol(index, "000300")
    csi_bars = _load_symbol(index, "000852")
    growth = {bar.trading_date: bar.adjusted_close for bar in loaded["159915"]}
    dividend = {bar.trading_date: bar.adjusted_close for bar in loaded["515180"]}
    calendar = sorted(set(growth) & set(dividend))
    styles = _style_series(growth, dividend, calendar)
    heats = _overheat(growth, dividend, calendar)
    hs300 = {bar.trading_date: bar.adjusted_close for bar in hs_bars}
    csi1000 = {bar.trading_date: bar.adjusted_close for bar in csi_bars}
    hs_weeks = _weekly([hs300[day] for day in sorted(hs300)], sorted(hs300))
    csi_weeks = _weekly([csi1000[day] for day in sorted(csi1000)], sorted(csi1000))
    rows = [
        run_symbol(
            spec, loaded[spec.symbol], hs300, csi1000, hs_weeks, csi_weeks, styles, heats, mode="macd",
        )
        for spec in SPECS
        if loaded[spec.symbol]
    ]
    pooled = [trade for row in rows for trade in row["trades_detail"]]

    def _slice(name: str, chosen: list[dict]) -> dict:
        wins = [trade for trade in chosen if trade["win"]]
        losses = [trade for trade in chosen if not trade["win"]]
        payoff = None
        if wins and losses:
            payoff = (sum(trade["r"] for trade in wins) / len(wins)) / abs(
                sum(trade["r"] for trade in losses) / len(losses)
            )
        return {
            "name": name,
            "trades": len(chosen),
            "wins": len(wins),
            "win_rate": None if not chosen else len(wins) / len(chosen),
            "payoff_ratio": payoff,
            "avg_r": None if not chosen else sum(trade["r"] for trade in chosen) / len(chosen),
        }

    summary = {
        "rule": "MACD(6,13,5) golden cross and close above MA10 or MA20; DMI removed",
        "symbols": [
            {key: value for key, value in row.items() if key != "trades_detail"}
            for row in rows
        ],
        "pooled": _slice("六只合计", pooled),
        "above_ma10": _slice("金叉且收盘>MA10", [trade for trade in pooled if trade["above10"]]),
        "above_ma20": _slice("金叉且收盘>MA20", [trade for trade in pooled if trade["above20"]]),
        "above_either_only_ma10": _slice(
            "只站上MA10",
            [trade for trade in pooled if trade["above10"] and not trade["above20"]],
        ),
        "above_both": _slice(
            "同时站上MA10和MA20",
            [trade for trade in pooled if trade["above10"] and trade["above20"]],
        ),
    }
    destination = Path("var/swing/research/macd_cross_ma_backtest.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in summary["symbols"]:
        print(row["name"], row["trades"], row["win_rate"], row["payoff_ratio"], row["avg_r"], row["reasons"])
    for key in ("pooled", "above_ma10", "above_ma20", "above_either_only_ma10", "above_both"):
        item = summary[key]
        print(item["name"], item["trades"], item["win_rate"], item["payoff_ratio"], item["avg_r"])


if __name__ == "__main__":
    main()
