"""Deterministic fixtures shared by swing-monitor tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Sequence


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
