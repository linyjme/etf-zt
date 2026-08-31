from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

from .constants import BUY_COMMISSION_RATE, SELL_COMMISSION_RATE, SLIPPAGE_RATE

if TYPE_CHECKING:
    from .market_data import MarketHealth
    from .t_monitor import Quote


@dataclass(frozen=True)
class CandidateContext:
    quote: Quote
    regime_state: str
    health: MarketHealth
    grid_width_pct: float


@dataclass(frozen=True)
class CandidateDecision:
    action: str
    label: str
    expected_gross_edge_pct: float
    round_trip_cost_pct: float
    expected_net_edge_pct: float
    blocked_reasons: tuple[str, ...]


class TStrategy:
    def evaluate(self, context: CandidateContext) -> CandidateDecision:
        cost = _round_trip_cost_pct()
        base_reasons: list[str] = []
        if context.health.status != "REALTIME":
            base_reasons.append("MARKET_NOT_REALTIME")
        if context.regime_state != "RANGE":
            base_reasons.append("REGIME_NOT_RANGE")
        points = context.quote.points
        if len(points) < 2:
            return _blocked_wait(cost, *base_reasons, "INSUFFICIENT_FINALIZED_POINTS")
        if not _finite_positive(context.grid_width_pct):
            return _blocked_wait(cost, *base_reasons, "INVALID_GRID_WIDTH")

        previous, latest = points[-2:]
        prices = (
            previous.price,
            previous.average_price,
            latest.price,
            latest.average_price,
            context.quote.previous_close,
        )
        if not all(_finite_positive(value) for value in prices):
            return _blocked_wait(cost, *base_reasons, "INVALID_PRICE_DATA")

        current_deviation = latest.price / latest.average_price - 1
        previous_deviation = previous.price / previous.average_price - 1
        grid_size = latest.average_price * context.grid_width_pct
        if not _finite_positive(grid_size):
            return _blocked_wait(cost, *base_reasons, "INVALID_GRID_WIDTH")

        deviation_grids = abs(latest.price - latest.average_price) / grid_size
        close_grids = abs(latest.price - context.quote.previous_close) / grid_size
        gross = abs(latest.price - latest.average_price) / latest.price
        reasons = base_reasons
        if deviation_grids + 1e-9 < 3:
            reasons.append("DEVIATION_BELOW_3_GRIDS")
        if close_grids + 1e-9 < 5:
            reasons.append("PREVIOUS_CLOSE_DISTANCE_BELOW_5_GRIDS")
        if (
            current_deviation * previous_deviation <= 0
            or abs(current_deviation) >= abs(previous_deviation)
        ):
            reasons.append("DEVIATION_NOT_NARROWING")
        if gross <= cost:
            reasons.append("COST_NOT_COVERED")
        if fast_rise_grids(context.quote, grid_size) >= 5:
            reasons.append("FAST_RISE")

        net = gross - cost
        if reasons:
            action = "DEVIATION_OBSERVE" if deviation_grids + 1e-9 >= 3 else "WAIT"
            label = "偏离观察" if action == "DEVIATION_OBSERVE" else "等待"
            return CandidateDecision(action, label, gross, cost, net, tuple(reasons))

        action = "SELL_CANDIDATE" if current_deviation > 0 else "BUY_CANDIDATE"
        return CandidateDecision(action, "做T候选", gross, cost, net, ())


def fast_rise_grids(quote: Quote, grid_size: float) -> float:
    if len(quote.points) < 2 or not _finite_positive(grid_size):
        return 0.0
    previous, latest = quote.points[-2:]
    if not _finite_positive(previous.price) or not _finite_positive(latest.price):
        return 0.0
    try:
        seconds = (latest.timestamp - previous.timestamp).total_seconds()
    except (AttributeError, TypeError):
        return 0.0
    if not math.isfinite(seconds) or not 0 < seconds <= 300:
        return 0.0
    return round(max(0.0, (latest.price - previous.price) / grid_size), 6)


def _round_trip_cost_pct() -> float:
    return BUY_COMMISSION_RATE + SELL_COMMISSION_RATE + 2 * SLIPPAGE_RATE


def _blocked_wait(cost: float, *reasons: str) -> CandidateDecision:
    return CandidateDecision("WAIT", "等待", 0.0, cost, -cost, tuple(reasons))


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )
