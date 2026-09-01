"""Deterministic fixtures shared by swing-monitor tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from etf_rotation.swing_data import DailyBar


SHANGHAI = timezone(timedelta(hours=8))
SWING_SYMBOLS = (
    "510300",
    "510500",
    "563360",
    "512100",
    "159915",
    "588000",
)


def metadata_fixture(
    symbols: Sequence[str] = SWING_SYMBOLS,
) -> dict[str, object]:
    """Return valid ETF metadata for the requested swing symbols."""
    exchanges = {"159915": "SZSE"}
    return {
        "schema_version": 2,
        "items": [
            {
                "symbol": symbol,
                "name": f"{symbol} ETF",
                "index": {
                    "code": f"{index:06d}",
                    "name": f"Index {index}",
                    "provider": "TEST",
                },
                "trading": {
                    "exchange": exchanges.get(symbol, "SSE"),
                    "asset_type": "DOMESTIC_EQUITY_ETF",
                    "intraday_turnaround": False,
                    "sellable_delay_days": 1,
                    "lot_size": 100,
                    "price_tick": 0.001,
                    "price_limit_pct": 0.20,
                    "volume_unit_shares": 100,
                },
            }
            for index, symbol in enumerate(symbols, start=1)
        ],
    }


def completed_daily_bars(
    count: int = 80,
    *,
    symbol: str = "510300",
    first_close: float = 4.0,
    daily_step: float = 0.01,
) -> tuple[dict[str, object], ...]:
    """Return deterministic completed weekday bars in Shanghai time."""
    timestamp = datetime(2026, 1, 5, 15, 0, tzinfo=SHANGHAI)
    bars: list[dict[str, object]] = []
    while len(bars) < count:
        if timestamp.weekday() < 5:
            close = first_close + len(bars) * daily_step
            bars.append({
                "symbol": symbol,
                "timestamp": timestamp.isoformat(),
                "open": close - 0.01,
                "high": close + 0.02,
                "low": close - 0.02,
                "close": close,
                "volume": 1_000_000.0 + len(bars) * 1_000.0,
                "amount": close * (1_000_000.0 + len(bars) * 1_000.0),
            })
        timestamp += timedelta(days=1)
    return tuple(bars)


def daily_bar_mapping(
    *,
    symbol: str = "510300",
    trading_date: date | str = "2026-08-28",
    observed_at: str = "2026-08-28T15:10:00+08:00",
    previous_close: float = 10.0,
    open_price: float = 10.0,
    high: float = 10.1,
    low: float = 9.9,
    close: float = 10.05,
    volume: float = 1_000.0,
    amount: float = 1_000_000.0,
    adjustment_scale: float = 1.1,
) -> dict[str, object]:
    """Return one exact schema-v1 completed daily-bar mapping."""
    date_text = (
        trading_date.isoformat()
        if isinstance(trading_date, date)
        else trading_date
    )
    return {
        "schema_version": 1,
        "symbol": symbol,
        "trading_date": date_text,
        "observed_at": observed_at,
        "source": "TEST_DAILY",
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "previous_close": previous_close,
        "volume": volume,
        "amount": amount,
        "adjusted_open": open_price * adjustment_scale,
        "adjusted_high": high * adjustment_scale,
        "adjusted_low": low * adjustment_scale,
        "adjusted_close": close * adjustment_scale,
        "is_final": True,
    }


def swing_strategy_bars(
    count: int = 70,
    *,
    symbol: str = "510300",
    pattern: str = "pullback_reclaim",
    raw_scale: float = 1.0,
) -> tuple[DailyBar, ...]:
    """Return strict completed bars with deterministic strategy patterns."""
    if count < 1:
        return ()
    closes = [100.0 + index * 0.1 for index in range(count)]
    highs = [close + 0.6 for close in closes]
    lows = [close - 0.6 for close in closes]
    opens = [close - 0.1 for close in closes]

    if pattern == "pullback_reclaim":
        if count >= 2:
            closes[-1] = highs[-2] + 0.1
            opens[-1] = closes[-1] - 0.2
            highs[-1] = closes[-1] + 0.6
            ma20 = sum(closes[-20:]) / min(20, count)
            lows[-1] = ma20
    elif pattern == "falling_ma60":
        closes = [120.0 - index * 0.1 for index in range(count)]
        highs = [close + 0.6 for close in closes]
        lows = [close - 0.6 for close in closes]
        opens = [close + 0.1 for close in closes]
    elif pattern == "gap":
        if count >= 15:
            closes[-14] = closes[-15] + 4.0
            opens[-14] = closes[-14]
            highs[-14] = closes[-14] + 0.2
            lows[-14] = closes[-14] - 0.2
    elif pattern == "exit":
        if count >= 2:
            prior_ma20 = sum(closes[-21:-1]) / 20
            closes[-2] = prior_ma20 - 0.2
            closes[-1] = prior_ma20 - 0.3
            for index in (-2, -1):
                opens[index] = closes[index] + 0.1
                highs[index] = closes[index] + 0.6
                lows[index] = closes[index] - 0.6
    elif pattern == "flat":
        closes = [100.0 for _ in range(count)]
        highs = [100.6 for _ in range(count)]
        lows = [99.4 for _ in range(count)]
        opens = [100.0 for _ in range(count)]
    elif pattern != "rising":
        raise ValueError(f"unknown strategy pattern: {pattern}")

    first_day = date(2026, 1, 5)
    days: list[date] = []
    candidate = first_day
    while len(days) < count:
        if candidate.weekday() < 5:
            days.append(candidate)
        candidate += timedelta(days=1)

    result: list[DailyBar] = []
    previous_raw_close = closes[0] * raw_scale
    for index, trading_day in enumerate(days):
        raw_open = opens[index] * raw_scale
        raw_high = highs[index] * raw_scale
        raw_low = lows[index] * raw_scale
        raw_close = closes[index] * raw_scale
        payload = daily_bar_mapping(
            symbol=symbol,
            trading_date=trading_day,
            observed_at=(
                datetime.combine(trading_day, datetime.min.time(), SHANGHAI)
                .replace(hour=15, minute=10)
                .isoformat()
            ),
            previous_close=previous_raw_close,
            open_price=raw_open,
            high=raw_high,
            low=raw_low,
            close=raw_close,
            volume=10_000.0 + index * 100.0,
            amount=raw_close * (10_000.0 + index * 100.0),
            adjustment_scale=1.0 / raw_scale,
        )
        result.append(DailyBar.from_mapping(payload))
        previous_raw_close = raw_close
    return tuple(result)


def replace_latest_adjusted(
    bars: Sequence[DailyBar],
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
) -> tuple[DailyBar, ...]:
    """Replace the latest adjusted OHLC while retaining its raw scale."""
    return replace_adjusted_bar(
        bars,
        -1,
        open_price=open_price,
        high=high,
        low=low,
        close=close,
    )


def replace_adjusted_bar(
    bars: Sequence[DailyBar],
    index: int,
    *,
    open_price: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
) -> tuple[DailyBar, ...]:
    """Replace one adjusted OHLC record while retaining its raw scale."""
    if not bars:
        raise ValueError("bars must not be empty")
    resolved_index = index if index >= 0 else len(bars) + index
    if not 0 <= resolved_index < len(bars):
        raise IndexError("bar index out of range")
    original = bars[resolved_index]
    scale = original.close / original.adjusted_close
    adjusted_open = original.adjusted_open if open_price is None else open_price
    adjusted_high = original.adjusted_high if high is None else high
    adjusted_low = original.adjusted_low if low is None else low
    adjusted_close = original.adjusted_close if close is None else close
    payload = original.to_dict()
    payload.update({
        "open": adjusted_open * scale,
        "high": adjusted_high * scale,
        "low": adjusted_low * scale,
        "close": adjusted_close * scale,
        "adjusted_open": adjusted_open,
        "adjusted_high": adjusted_high,
        "adjusted_low": adjusted_low,
        "adjusted_close": adjusted_close,
    })
    updated = list(bars)
    updated[resolved_index] = DailyBar.from_mapping(payload)
    if resolved_index + 1 < len(updated):
        following = updated[resolved_index + 1]
        following_payload = following.to_dict()
        following_payload["previous_close"] = adjusted_close * scale
        updated[resolved_index + 1] = DailyBar.from_mapping(following_payload)
    return tuple(updated)
