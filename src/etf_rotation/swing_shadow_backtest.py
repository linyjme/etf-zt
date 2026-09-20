"""Small, auditable next-session replay for formal and shadow variants."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from .swing_data import DailyBar
from .swing_shadow import ShadowVariant
from .swing_indicators import calculate_indicator_snapshot


@dataclass(frozen=True)
class ExecutionCosts:
    commission_rate: float = 0.00012
    spread_rate: float = 0.0002
    slippage_rate: float = 0.0002
    minimum_fee: float = 5.0
    lot_size: int = 100

    def assumptions(self) -> tuple[tuple[str, float | int | str], ...]:
        return (
            ("commission_rate", self.commission_rate),
            ("spread_rate", self.spread_rate),
            ("slippage_rate", self.slippage_rate),
            ("minimum_fee", self.minimum_fee),
            ("lot_size", self.lot_size),
            ("execution", "next_completed_session"),
        )


@dataclass(frozen=True)
class ShadowTrade:
    symbol: str
    side: str
    signal_date: date
    execution_date: date
    shares: int
    execution_price: float
    fee: float


@dataclass(frozen=True)
class ShadowReplayResult:
    variant: str
    trades: tuple[ShadowTrade, ...]
    execution_assumptions: tuple[tuple[str, float | int | str], ...]
    validation_status: str
    performance_claim_allowed: bool
    net_pnl: float
    max_drawdown: float
    rejected_count: int


def _validate_history(
    history_by_symbol: Mapping[str, Sequence[DailyBar]],
) -> dict[str, tuple[DailyBar, ...]]:
    if not isinstance(history_by_symbol, Mapping) or not history_by_symbol:
        raise ValueError("history_by_symbol must be a non-empty mapping")
    result: dict[str, tuple[DailyBar, ...]] = {}
    for symbol, supplied in history_by_symbol.items():
        bars = tuple(supplied)
        if not bars or type(bars[0]) is not DailyBar or bars[0].symbol != symbol:
            raise ValueError("history key must match the first bar symbol")
        for previous, current in zip(bars, bars[1:]):
            if type(current) is not DailyBar or current.symbol != symbol:
                raise ValueError("history must contain one completed symbol")
            if current.trading_date <= previous.trading_date:
                raise ValueError("history dates must be strictly ascending")
        if any(bar.is_final is not True for bar in bars):
            raise ValueError("history must contain completed DailyBar values")
        result[symbol] = bars
    return result


def _signal(
    bars: tuple[DailyBar, ...], variant: ShadowVariant,
) -> bool:
    if variant is ShadowVariant.V1:
        snapshot = calculate_indicator_snapshot(bars)
        moving = snapshot["moving_averages"]
        close = bars[-1].adjusted_close
        return bool(
            moving["ma20"] is not None and moving["ma60"] is not None
            and close > moving["ma60"] and moving["ma20"] > moving["ma60"]
            and bars[-1].adjusted_low <= moving["ma20"]
            and close > moving["ma20"]
        )
    snapshot = calculate_indicator_snapshot(bars)
    moving = snapshot["moving_averages"]
    close = bars[-1].adjusted_close
    ma20 = moving["ma20"]
    ma60 = moving["ma60"]
    if ma20 is None or ma60 is None or close <= ma60 or ma20 <= ma60:
        return False
    if abs(close - ma20) > max(0.01, (bars[-1].adjusted_high - bars[-1].adjusted_low) * 2.0):
        return False
    macd = snapshot["macd"]
    rsi = snapshot["rsi"]["rsi14"]
    kdj = snapshot["kdj"]
    return bool(
        macd["dif"] >= macd["dea"]
        or (rsi is not None and 45.0 <= rsi <= 65.0)
        or (
            kdj["k"] is not None and kdj["d"] is not None
            and kdj["j"] is not None and kdj["k"] > kdj["d"]
        )
    )


def _fee(price: float, shares: int, costs: ExecutionCosts) -> float:
    return max(costs.minimum_fee, price * shares * costs.commission_rate)


def _trade_pair(
    bars: tuple[DailyBar, ...], index: int, costs: ExecutionCosts,
) -> tuple[ShadowTrade, ShadowTrade] | None:
    execution_index = index + 1
    if execution_index >= len(bars):
        return None
    exit_index = min(execution_index + 10, len(bars) - 1)
    buy_price = bars[execution_index].close * (
        1.0 + costs.spread_rate / 2.0 + costs.slippage_rate
    )
    sell_price = bars[exit_index].close * (
        1.0 - costs.spread_rate / 2.0 - costs.slippage_rate
    )
    shares = costs.lot_size
    return (
        ShadowTrade(
            bars[index].symbol, "BUY", bars[index].trading_date,
            bars[execution_index].trading_date, shares, buy_price,
            _fee(buy_price, shares, costs),
        ),
        ShadowTrade(
            bars[index].symbol, "SELL", bars[index].trading_date,
            bars[exit_index].trading_date, shares, sell_price,
            _fee(sell_price, shares, costs),
        ),
    )


def _result(
    variant: ShadowVariant, costs: ExecutionCosts,
    trades: tuple[ShadowTrade, ...], status: str, rejected_count: int,
) -> ShadowReplayResult:
    pnl = 0.0
    buys: dict[str, ShadowTrade] = {}
    for trade in trades:
        if trade.side == "BUY":
            buys[trade.symbol] = trade
        else:
            buy = buys.pop(trade.symbol, None)
            if buy is not None:
                pnl += (trade.execution_price - buy.execution_price) * trade.shares
                pnl -= buy.fee + trade.fee
    return ShadowReplayResult(
        variant=variant.value,
        trades=trades,
        execution_assumptions=costs.assumptions(),
        validation_status=status,
        performance_claim_allowed=status == "VALIDATED" and len(trades) >= 40,
        net_pnl=pnl,
        max_drawdown=0.0,
        rejected_count=rejected_count,
    )


def replay_variant(
    history_by_symbol: Mapping[str, Sequence[DailyBar]], *,
    variant: ShadowVariant,
    costs: ExecutionCosts,
    initial_equity: float = 100000.0,
) -> ShadowReplayResult:
    """Replay one variant using only the signal prefix and next session fill."""
    if type(costs) is not ExecutionCosts:
        raise ValueError("costs must be ExecutionCosts")
    if type(initial_equity) not in (int, float) or initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    histories = _validate_history(history_by_symbol)
    minimum = min(len(bars) for bars in histories.values())
    status = "VALIDATED" if minimum >= 630 else "INSUFFICIENT_SAMPLE"
    if status != "VALIDATED":
        return _result(variant, costs, (), status, 0)
    all_trades: list[ShadowTrade] = []
    rejected = 0
    for bars in histories.values():
        index = 120
        while index < len(bars) - 1:
            if _signal(bars[:index + 1], variant):
                pair = _trade_pair(bars, index, costs)
                if pair is None:
                    rejected += 1
                else:
                    all_trades.extend(pair)
                    index = min(index + 11, len(bars) - 1)
            index += 1
    return _result(variant, costs, tuple(all_trades), status, rejected)


def replay_all_variants(
    history_by_symbol: Mapping[str, Sequence[DailyBar]], *,
    costs: ExecutionCosts,
    initial_equity: float = 100000.0,
) -> dict[str, ShadowReplayResult]:
    return {
        variant.value: replay_variant(
            history_by_symbol, variant=variant, costs=costs,
            initial_equity=initial_equity,
        )
        for variant in (
            ShadowVariant.V1, ShadowVariant.V2_A,
            ShadowVariant.V2_B, ShadowVariant.V2_C,
        )
    }


__all__ = [
    "ExecutionCosts", "ShadowReplayResult", "ShadowTrade",
    "replay_all_variants", "replay_variant",
]
