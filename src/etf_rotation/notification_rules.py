"""Pure, observation-only checks over authoritative completed-minute prices."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
import math
from typing import Any

from .market_data import MarketHealthClassifier, SHANGHAI, market_session_state


_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class ShadowResearchAlert:
    """A non-trading research notification emitted by the shadow layer."""

    kind: str
    symbol: str
    strategy_version: str
    state: str
    opportunity_id: str | None
    blocked_reasons: tuple[str, ...]
    executable: bool = False


@dataclass(frozen=True)
class AlertBundle:
    """Separate formal alerts from shadow research-only notifications."""

    formal: list[object]
    shadow: list[ShadowResearchAlert]


def build_alerts(
    *,
    symbol: str = "",
    formal_state: str | None = None,
    shadow_state: str | None = None,
    data_status: str = "VERIFIED",
    snapshot_only: bool = False,
    data_healthy: bool = True,
    account_known: bool = True,
    cost_ok: bool = True,
    risk_ok: bool = True,
    opportunity_id: str | None = None,
    blocked_reasons: Sequence[str] = (),
) -> AlertBundle:
    """Build isolated formal/shadow notification candidates.

    The formal list is intentionally untouched here; existing SWING_V1 alert
    generation remains owned by the service. A shadow candidate is eligible
    only after every research gate is explicitly healthy. It can never become
    an executable alert or a trade instruction.
    """
    if type(symbol) is not str or (symbol and (len(symbol) != 6 or not symbol.isdigit())):
        raise ValueError("symbol must be six digits")
    reasons = tuple(
        reason for reason in blocked_reasons
        if type(reason) is str and reason
    )
    if shadow_state != "TECHNICAL_CANDIDATE":
        return AlertBundle(formal=[], shadow=[])
    if (
        data_status != "VERIFIED"
        or snapshot_only
        or data_healthy is not True
        or account_known is not True
        or cost_ok is not True
        or risk_ok is not True
    ):
        return AlertBundle(formal=[], shadow=[])
    return AlertBundle(
        formal=[],
        shadow=[ShadowResearchAlert(
            kind="SHADOW_RESEARCH",
            symbol=symbol,
            strategy_version="SWING_V2_SHADOW",
            state=shadow_state,
            opportunity_id=opportunity_id,
            blocked_reasons=reasons,
            executable=False,
        )],
    )


def _positive_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("数值必须是有限正数")
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        raise ValueError("数值必须是有限正数") from None
    if not math.isfinite(converted) or converted <= 0:
        raise ValueError("数值必须是有限正数")
    return converted


def _local_time(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return value.astimezone(SHANGHAI)


def _minute_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("分钟时间格式无效")
    result = _local_time(datetime.fromisoformat(value))
    if result.second or result.microsecond:
        raise ValueError("分钟时间必须对齐分钟起点")
    return result


def _result(
    status: str, reason: str, *, timestamp: datetime | None = None,
    price: float | None = None, change_pct: float | None = None,
    direction: str = "NONE",
) -> dict[str, Any]:
    return {
        "status": status, "change_pct": change_pct, "direction": direction,
        "reason": reason, "timestamp": timestamp.isoformat() if timestamp else None,
        "price": price,
    }


def evaluate_anomaly(
    item: Mapping[str, Any], now: datetime, threshold_pct: float = 1.0,
    closed_dates: set[date] | frozenset[date] = frozenset(),
) -> dict[str, Any]:
    """Compare the latest close with five minutes earlier, without repairing data.

    Only the last six raw rows form the current window. The caller supplies the
    authoritative eligibility, health and completed-minute timestamp; each is
    rechecked here. READY means the calculation is usable, not that it crossed
    the threshold. Percent values use UI units: 1.0 means one percent.
    """
    try:
        threshold = _positive_number(threshold_pct)
        local_now = _local_time(now)
    except (ValueError, TypeError, OverflowError):
        return _result("UNAVAILABLE", "当前时间或异动阈值无效")
    session = market_session_state(local_now, closed_dates)
    if not session.active:
        return _result("INACTIVE", "午间休市" if session.phase == "LUNCH_BREAK" else "非连续交易时段")
    if not isinstance(item, Mapping):
        return _result("UNAVAILABLE", "缺少有效行情记录")
    if item.get("eligible") is not True:
        return _result("UNAVAILABLE", "标的身份、持有范围或行情接入尚未就绪")
    if item.get("health_status") != "REALTIME":
        return _result("UNAVAILABLE", "行情非实时或存在未解除的数据质量异常")
    if item.get("timestamp_basis") != "MINUTE_START":
        return _result("UNAVAILABLE", "行情未声明已完成分钟的起点时间")
    points = item.get("points")
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes, bytearray)):
        return _result("UNAVAILABLE", "分钟样本格式无效")
    if not points:
        return _result("INSUFFICIENT", "不足六个连续已完成分钟")

    window: list[tuple[datetime, float]] = []
    try:
        for point in points[-6:]:
            if not isinstance(point, Mapping):
                raise ValueError("分钟样本格式无效")
            timestamp = _minute_time(point.get("timestamp"))
            price = _positive_number(point.get("price"))
            point_session = market_session_state(timestamp, closed_dates)
            if timestamp.date() != local_now.date() or point_session.phase != session.phase:
                raise ValueError("分钟样本跨交易日或交易时段")
            if timestamp + _MINUTE > local_now:
                raise ValueError("分钟尚未完成或时间异常")
            if window and timestamp - window[-1][0] != _MINUTE:
                raise ValueError("分钟样本存在重复、乱序或缺口")
            window.append((timestamp, price))
        latest_time, latest_price = window[-1]
        if _minute_time(item.get("timestamp")) != latest_time:
            raise ValueError("最新分钟与已发布行情时间不一致")
    except (ValueError, TypeError, OverflowError):
        return _result("UNAVAILABLE", "分钟时间、价格、顺序或交易时段校验未通过")

    health = MarketHealthClassifier(closed_dates).classify(
        local_now, latest_time, None, completed_minute=True,
    )
    if health.status != "REALTIME":
        return _result("UNAVAILABLE", health.reason, timestamp=latest_time, price=latest_price)
    if len(window) < 6:
        return _result("INSUFFICIENT", "不足六个连续已完成分钟", timestamp=latest_time, price=latest_price)

    # Decimal textual values preserve exact decimal threshold equality without
    # treating a genuinely below-threshold float as a crossing via an epsilon.
    base = Decimal(str(window[0][1]))
    change = (Decimal(str(latest_price)) - base) / base * 100
    change_pct = float(change)
    if not math.isfinite(change_pct):
        return _result("UNAVAILABLE", "五分钟涨跌幅数值无效", timestamp=latest_time, price=latest_price)
    limit = Decimal(str(threshold))
    direction = "UP" if change >= limit else "DOWN" if change <= -limit else "NONE"
    return _result(
        "READY", "五分钟涨跌幅达到观察阈值" if direction != "NONE" else "五分钟涨跌幅未达到观察阈值",
        timestamp=latest_time, price=latest_price, change_pct=change_pct, direction=direction,
    )


def replay_anomalies(
    points: Sequence[Mapping[str, Any]], threshold_pct: float = 1.0,
    cooldown_minutes: float = 30,
    closed_dates: set[date] | frozenset[date] = frozenset(),
) -> dict[str, Any]:
    """Replay already-audited rows; never send mail or modify caller data.

    raw_crossings counts all usable above-threshold minutes before merging.
    invalid_samples counts every non-READY sample, including initial warm-up,
    malformed rows and windows interrupted by a gap. Raw rows are never sorted,
    dropped or deduplicated to fabricate continuity. A crossing inside cooldown
    starts an active lifecycle but is not queued for delayed notification.
    """
    threshold = _positive_number(threshold_pct)
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes, bytearray)):
        raise ValueError("回放样本必须是分钟记录序列")
    if isinstance(cooldown_minutes, bool) or not isinstance(cooldown_minutes, (int, float)):
        raise ValueError("冷却分钟数必须是有限非负数")
    try:
        cooldown_value = float(cooldown_minutes)
        if not math.isfinite(cooldown_value) or cooldown_value < 0:
            raise ValueError("冷却分钟数必须是有限非负数")
        cooldown = timedelta(minutes=cooldown_value)
    except (ValueError, OverflowError):
        raise ValueError("冷却分钟数必须是有效有限非负数") from None

    events: list[dict[str, Any]] = []
    raw_crossings = 0
    invalid_samples = 0
    active = {"UP": False, "DOWN": False}
    last_event: dict[str, datetime] = {}
    release_level = Decimal(str(threshold)) * Decimal("0.8")

    for index, point in enumerate(points):
        try:
            if not isinstance(point, Mapping):
                raise ValueError("分钟样本格式无效")
            observed = _minute_time(point.get("timestamp")) + _MINUTE
        except (ValueError, TypeError, OverflowError):
            invalid_samples += 1
            active = {"UP": False, "DOWN": False}
            continue
        result = evaluate_anomaly(
            {
                "eligible": True, "health_status": "REALTIME",
                "timestamp_basis": "MINUTE_START", "timestamp": point.get("timestamp"),
                "points": points[max(0, index - 5):index + 1],
            },
            observed, threshold, closed_dates,
        )
        if result["status"] != "READY":
            invalid_samples += 1
            active = {"UP": False, "DOWN": False}
            continue

        change = Decimal(str(result["change_pct"]))
        for direction, sign in (("UP", 1), ("DOWN", -1)):
            if change * sign < release_level:
                active[direction] = False
        direction = result["direction"]
        if direction == "NONE":
            continue
        raw_crossings += 1
        if active[direction]:
            continue
        active[direction] = True
        previous_event = last_event.get(direction)
        if previous_event is not None and observed - previous_event < cooldown:
            continue
        last_event[direction] = observed
        events.append(dict(result))

    return {
        "raw_crossings": raw_crossings, "merged_events": len(events),
        "invalid_samples": invalid_samples, "sample_count": len(points), "events": events,
    }
