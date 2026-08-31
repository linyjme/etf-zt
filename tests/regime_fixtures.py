from datetime import datetime, timedelta

from etf_rotation.t_monitor import Quote, QuotePoint


def _start(value: str) -> datetime:
    return datetime.fromisoformat(f"2026-08-28T{value}:00+08:00")


def _point(timestamp: datetime, price: float, average: float) -> QuotePoint:
    low = min(price, average) * 0.9999
    high = max(price, average) * 1.0001
    return QuotePoint(timestamp, price, average, price, high, low, 1000.0, price * 1000.0 * 100.0)


def alternating_points(count: int, start: str = "09:30") -> tuple[QuotePoint, ...]:
    origin = _start(start)
    average = 10.0
    return tuple(
        _point(origin + timedelta(minutes=index), average * (1.0005 if index % 2 == 0 else 0.9995), average)
        for index in range(count)
    )


def one_sided_points(count: int, start: str = "09:30") -> tuple[QuotePoint, ...]:
    origin = _start(start)
    return tuple(_point(origin + timedelta(minutes=index), 10.01, 10.0) for index in range(count))


def trending_points(count: int, direction: int, start: str = "09:30") -> tuple[QuotePoint, ...]:
    origin = _start(start)
    points = []
    for index in range(count):
        average = 10.0 + direction * index * 0.0015
        price = average * (1.001 if direction > 0 else 0.999)
        points.append(_point(origin + timedelta(minutes=index), price, average))
    return tuple(points)


def boundary_slope_points(
    count: int,
    direction: int,
    *,
    alternating: bool,
    start: str = "09:30",
) -> tuple[QuotePoint, ...]:
    origin = _start(start)
    minute_factor = (1.0 + direction * 0.001) ** (1.0 / 19.0)
    points = []
    for index in range(count):
        average = 10.0 * minute_factor ** index
        if alternating:
            price = average * (1.0005 if index % 2 == 0 else 0.9995)
        else:
            price = average * (1.001 if direction > 0 else 0.999)
        points.append(_point(origin + timedelta(minutes=index), price, average))
    return tuple(points)


def confirmed_range_quote(
    previous_deviation: float,
    current_deviation: float,
    previous_close_distance: float = 0.02,
) -> Quote:
    average = 10.0
    previous = _point(_start("10:00"), average * (1 + previous_deviation), average)
    current = _point(_start("10:01"), average * (1 + current_deviation), average)
    previous_close = current.price * (1 + previous_close_distance if current_deviation < 0 else 1 - previous_close_distance)
    return Quote(
        symbol="510300",
        name="沪深300ETF",
        price=current.price,
        average_price=current.average_price,
        previous_close=previous_close,
        timestamp=current.timestamp,
        points=(previous, current),
        observed_at=current.timestamp + timedelta(minutes=1),
        source="TEST",
    )
