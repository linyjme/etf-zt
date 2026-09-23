from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


def expected_complete_minutes(trading_date: date) -> tuple[datetime, ...]:
    result: list[datetime] = []
    current = datetime.combine(
        trading_date, time(9, 30), tzinfo=SHANGHAI,
    )
    morning_end = current.replace(hour=11, minute=30)
    while current <= morning_end:
        result.append(current)
        current += timedelta(minutes=1)
    current = datetime.combine(
        trading_date, time(13, 1), tzinfo=SHANGHAI,
    )
    afternoon_end = current.replace(hour=15, minute=0)
    while current <= afternoon_end:
        result.append(current)
        current += timedelta(minutes=1)
    return tuple(result)


def parse_minute_payload(
    payload: object, trading_date: date,
) -> tuple[Mapping[str, object], ...]:
    if not isinstance(payload, Mapping):
        return ()
    candidates: object = payload.get("upserts")
    if isinstance(candidates, Mapping):
        candidates = candidates.get("upserts", ())
    if not isinstance(candidates, (tuple, list)):
        candidates = payload.get("points", ())
    if not isinstance(candidates, (tuple, list)):
        return ()
    accepted: list[Mapping[str, object]] = []
    timestamps: list[datetime] = []
    for point in candidates:
        if not isinstance(point, Mapping):
            return ()
        if (
            type(point.get("schema_version")) is not int
            or point.get("schema_version") != 3
            or point.get("trading_date") != trading_date.isoformat()
            or point.get("is_complete") is not True
            or type(point.get("timestamp")) is not str
        ):
            return ()
        try:
            parsed = datetime.fromisoformat(str(point["timestamp"]))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return ()
            local = parsed.astimezone(SHANGHAI)
        except Exception:
            return ()
        if (
            local.date() != trading_date
            or local.second != 0
            or local.microsecond != 0
        ):
            return ()
        accepted.append(point)
        timestamps.append(local)
    if len(set(timestamps)) != len(timestamps):
        return ()
    if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        return ()
    if tuple(timestamps) != expected_complete_minutes(trading_date):
        return ()
    return tuple(accepted)
