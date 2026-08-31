from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import math
from typing import Any, Callable

from . import constants
from .etf_metadata import TradingMetadata
from .market_data import finalized_points
from .t_monitor import Quote, QuotePoint, TMonitorEngine, WatchItem


def floor_to_lot(shares: int, lot_size: int) -> int:
    if type(lot_size) is not int or lot_size <= 0:
        raise ValueError("lot_size必须是正整数")
    if type(shares) is not int:
        raise ValueError("shares必须是整数")
    return max(0, shares // lot_size * lot_size)


def _finite_positive(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{field}必须是有限正数")
    return float(value)


def _finite_nonnegative(value: object, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{field}必须是有限非负数")
    return float(value)


@dataclass(frozen=True)
class FillBar:
    timestamp: str
    price: float
    volume_lots: float

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, str) or not self.timestamp:
            raise ValueError("timestamp不能为空")
        object.__setattr__(self, "price", _finite_positive(self.price, "price"))
        object.__setattr__(
            self, "volume_lots", _finite_nonnegative(self.volume_lots, "volume_lots"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Fill:
    timestamp: str
    signal_action: str
    side: str
    requested_shares: int
    shares: int
    market_price: float
    price: float
    commission_cny: float
    slippage_cny: float
    cash_delta_cny: float
    remaining_t_capacity_shares: int

    @property
    def fee(self) -> float:
        return self.commission_cny

    @property
    def notional_cny(self) -> float:
        return self.price * self.shares

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["notional_cny"] = self.notional_cny
        return result


@dataclass(frozen=True)
class TradePair:
    direction: str
    shares: int
    open_fill: Fill
    close_fill: Fill
    gross_pnl_cny: float
    commission_cny: float
    slippage_cny: float
    net_pnl_cny: float

    @property
    def net_pnl(self) -> float:
        return self.net_pnl_cny

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "shares": self.shares,
            "open_fill": self.open_fill.to_dict(),
            "close_fill": self.close_fill.to_dict(),
            "gross_pnl_cny": self.gross_pnl_cny,
            "commission_cny": self.commission_cny,
            "slippage_cny": self.slippage_cny,
            "net_pnl_cny": self.net_pnl_cny,
        }


@dataclass(frozen=True)
class RejectedFill:
    timestamp: str
    signal_action: str
    requested_shares: int
    rejected_shares: int
    reason: str
    remaining_t_capacity_shares: int

    @property
    def shares(self) -> int:
        return 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BacktestResult:
    status: str
    execution_mode: str
    base_shares: int
    t_capacity_shares: int
    reserve_cash_cny: float
    first_price: float
    last_price: float
    strategy_ending_equity_cny: float
    baseline_ending_equity_cny: float
    t_net_gain_cny: float
    t_net_gain_rate: float
    completed_pairs: tuple[TradePair, ...] = ()
    open_legs: tuple[Fill, ...] = ()
    rejections: tuple[RejectedFill, ...] = ()
    total_shares: int = 0
    overnight_sellable_shares: int = 0
    today_bought_shares: int = 0
    cash_cny: float = 0.0
    paired_net_pnl_cny: float = 0.0
    win_rate: float | None = None
    sell_fly_loss_cny: float = 0.0
    total_commission_cny: float = 0.0
    total_slippage_cny: float = 0.0
    maximum_drawdown: float = 0.0
    outperformed_baseline: bool | None = None

    @property
    def completed_pair_count(self) -> int:
        return len(self.completed_pairs)

    @property
    def open_leg_count(self) -> int:
        return len(self.open_legs)

    @property
    def total_costs_cny(self) -> float:
        return self.total_commission_cny + self.total_slippage_cny

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "execution_mode": self.execution_mode,
            "base_shares": self.base_shares,
            "t_capacity_shares": self.t_capacity_shares,
            "reserve_cash_cny": self.reserve_cash_cny,
            "first_price": self.first_price,
            "last_price": self.last_price,
            "strategy_ending_equity_cny": self.strategy_ending_equity_cny,
            "baseline_ending_equity_cny": self.baseline_ending_equity_cny,
            "strategy_equity_cny": self.strategy_ending_equity_cny,
            "baseline_equity_cny": self.baseline_ending_equity_cny,
            "t_net_gain_cny": self.t_net_gain_cny,
            "t_net_gain_rate": self.t_net_gain_rate,
            "completed_pair_count": self.completed_pair_count,
            "completed_pairs": [item.to_dict() for item in self.completed_pairs],
            "win_rate": self.win_rate,
            "paired_net_pnl_cny": self.paired_net_pnl_cny,
            "open_leg_count": self.open_leg_count,
            "open_legs": [item.to_dict() for item in self.open_legs],
            "sell_fly_loss_cny": self.sell_fly_loss_cny,
            "rejections": [item.to_dict() for item in self.rejections],
            "inventory": {
                "total_shares": self.total_shares,
                "overnight_sellable_shares": self.overnight_sellable_shares,
                "today_bought_shares": self.today_bought_shares,
                "cash_cny": self.cash_cny,
            },
            "costs": {
                "commission_cny": self.total_commission_cny,
                "slippage_cny": self.total_slippage_cny,
                "total_cny": self.total_costs_cny,
            },
            "maximum_drawdown": self.maximum_drawdown,
            "outperformed_baseline": self.outperformed_baseline,
        }


class TAccount:
    def __init__(
        self,
        *,
        base_shares: int,
        t_capacity_shares: int,
        lot_size: int,
        intraday_turnaround: bool,
        volume_unit_shares: int,
        reserve_cash: float,
        volume_participation: float,
    ):
        self.base_shares = base_shares
        self.t_capacity_shares = t_capacity_shares
        self.lot_size = lot_size
        self.intraday_turnaround = intraday_turnaround
        self.volume_unit_shares = volume_unit_shares
        self.reserve_cash = reserve_cash
        self.cash = reserve_cash
        self.volume_participation = volume_participation
        self.overnight_sellable_shares = base_shares
        self.today_bought_shares = 0
        self.open_buy_legs: list[Fill] = []
        self.open_sell_legs: list[Fill] = []
        self.completed_pairs: list[TradePair] = []
        self.rejections: list[RejectedFill] = []
        self.fills: list[Fill] = []
        self.trading_date: date | None = None

    @classmethod
    def create(
        cls,
        *,
        base_shares: int,
        t_capacity_shares: int,
        first_price: float,
        lot_size: int,
        intraday_turnaround: bool,
        volume_unit_shares: int = 100,
        reserve_cash: float | None = None,
        volume_participation: float = constants.DEFAULT_VOLUME_PARTICIPATION,
    ) -> TAccount:
        if type(lot_size) is not int or lot_size <= 0:
            raise ValueError("lot_size必须是正整数")
        for value, field in (
            (base_shares, "base_shares"),
            (t_capacity_shares, "t_capacity_shares"),
        ):
            if type(value) is not int or value < 0 or value % lot_size:
                raise ValueError(f"{field}必须是非负整手")
        if type(volume_unit_shares) is not int or volume_unit_shares <= 0:
            raise ValueError("volume_unit_shares必须是正整数")
        if type(intraday_turnaround) is not bool:
            raise ValueError("intraday_turnaround必须是布尔值")
        first = _finite_positive(first_price, "first_price")
        participation = _finite_positive(volume_participation, "volume_participation")
        if participation > 1:
            raise ValueError("volume_participation不能大于1")
        if reserve_cash is None:
            buy_price = first * (1 + constants.SLIPPAGE_RATE)
            notional = t_capacity_shares * buy_price
            reserve = notional + (
                max(notional * constants.BUY_COMMISSION_RATE, constants.MINIMUM_COMMISSION_CNY)
                if t_capacity_shares else 0.0
            )
        else:
            reserve = _finite_nonnegative(reserve_cash, "reserve_cash")
        return cls(
            base_shares=base_shares,
            t_capacity_shares=t_capacity_shares,
            lot_size=lot_size,
            intraday_turnaround=intraday_turnaround,
            volume_unit_shares=volume_unit_shares,
            reserve_cash=reserve,
            volume_participation=participation,
        )

    @property
    def total_shares(self) -> int:
        return self.overnight_sellable_shares + self.today_bought_shares

    @property
    def used_t_capacity_shares(self) -> int:
        return sum(item.shares for item in (*self.open_buy_legs, *self.open_sell_legs))

    @property
    def remaining_t_capacity_shares(self) -> int:
        return max(0, self.t_capacity_shares - self.used_t_capacity_shares)

    @property
    def total_commission_cny(self) -> float:
        return sum(item.commission_cny for item in self.fills)

    @property
    def total_slippage_cny(self) -> float:
        return sum(item.slippage_cny for item in self.fills)

    def rollover(self, trading_date: date) -> None:
        if not isinstance(trading_date, date):
            raise ValueError("trading_date必须是日期")
        if self.trading_date is None:
            self.trading_date = trading_date
            return
        if trading_date < self.trading_date:
            raise ValueError("trading_date不能倒退")
        if trading_date == self.trading_date:
            return
        self.overnight_sellable_shares = self.total_shares
        self.today_bought_shares = 0
        self.trading_date = trading_date

    def executable_shares(self, bar: FillBar, requested_shares: int) -> int:
        if bar.volume_lots <= 0 or type(requested_shares) is not int:
            return 0
        market_capacity = floor_to_lot(
            int(
                bar.volume_lots
                * self.volume_unit_shares
                * self.volume_participation
            ),
            self.lot_size,
        )
        return min(
            floor_to_lot(requested_shares, self.lot_size),
            market_capacity,
            self.remaining_t_capacity_shares,
        )

    def execute(
        self, signal_action: str, bar: FillBar, requested_shares: int,
    ) -> Fill | RejectedFill:
        if signal_action not in {"BUY_CANDIDATE", "SELL_CANDIDATE"}:
            return self._reject(bar, signal_action, requested_shares, "UNSUPPORTED_ACTION")
        if type(requested_shares) is not int or requested_shares <= 0:
            return self._reject(
                bar, signal_action, requested_shares if type(requested_shares) is int else 0,
                "INVALID_REQUESTED_SHARES",
            )
        requested_lots = floor_to_lot(requested_shares, self.lot_size)
        if requested_lots == 0:
            return self._reject(bar, signal_action, requested_shares, "LOT_SIZE")
        if bar.volume_lots <= 0:
            return self._reject(bar, signal_action, requested_shares, "ZERO_VOLUME")
        market_capacity = floor_to_lot(
            int(bar.volume_lots * self.volume_unit_shares * self.volume_participation),
            self.lot_size,
        )
        if market_capacity == 0:
            return self._reject(
                bar, signal_action, requested_shares, "PARTICIPATION_LIMIT",
            )

        opposing = self.open_sell_legs if signal_action == "BUY_CANDIDATE" else self.open_buy_legs
        closable_shares = sum(item.shares for item in opposing)
        action_capacity = self.remaining_t_capacity_shares + closable_shares
        if action_capacity <= 0:
            return self._reject(bar, signal_action, requested_shares, "T_CAPACITY")
        executable = min(requested_lots, market_capacity, action_capacity)
        limiting_reason = self._limiting_reason(
            requested_shares, requested_lots, market_capacity, action_capacity,
        )

        side = "BUY" if signal_action == "BUY_CANDIDATE" else "SELL"
        fill_price = bar.price * (
            1 + constants.SLIPPAGE_RATE if side == "BUY"
            else 1 - constants.SLIPPAGE_RATE
        )
        if side == "SELL":
            sellable = self.overnight_sellable_shares + (
                self.today_bought_shares if self.intraday_turnaround else 0
            )
            inventory_capacity = floor_to_lot(sellable, self.lot_size)
            if inventory_capacity <= 0:
                return self._reject(
                    bar, signal_action, requested_shares, "INSUFFICIENT_INVENTORY",
                )
            if inventory_capacity < executable:
                executable = inventory_capacity
                limiting_reason = "INSUFFICIENT_INVENTORY"
        else:
            cash_capacity = self._affordable_buy_shares(fill_price)
            if cash_capacity <= 0:
                return self._reject(
                    bar, signal_action, requested_shares, "INSUFFICIENT_CASH",
                )
            if cash_capacity < executable:
                executable = cash_capacity
                limiting_reason = "INSUFFICIENT_CASH"

        executable = floor_to_lot(executable, self.lot_size)
        if executable <= 0:
            return self._reject(bar, signal_action, requested_shares, limiting_reason)
        notional = executable * fill_price
        commission_rate = (
            constants.BUY_COMMISSION_RATE if side == "BUY"
            else constants.SELL_COMMISSION_RATE
        )
        commission = max(
            notional * commission_rate, constants.MINIMUM_COMMISSION_CNY,
        )
        slippage = abs(fill_price - bar.price) * executable
        cash_delta = (
            -(notional + commission) if side == "BUY"
            else notional - commission
        )
        fill = Fill(
            bar.timestamp,
            signal_action,
            side,
            requested_shares,
            executable,
            bar.price,
            fill_price,
            commission,
            slippage,
            cash_delta,
            self._remaining_capacity_after_fill(side, executable),
        )

        if side == "BUY":
            self.cash += cash_delta
            self.today_bought_shares += executable
        else:
            self.cash += cash_delta
            from_overnight = min(executable, self.overnight_sellable_shares)
            self.overnight_sellable_shares -= from_overnight
            if self.intraday_turnaround:
                self.today_bought_shares -= executable - from_overnight
        self._pair(fill)
        self.fills.append(fill)
        if executable < requested_shares:
            self._reject(
                bar,
                signal_action,
                requested_shares,
                limiting_reason,
                rejected_shares=requested_shares - executable,
            )
        return fill

    def _remaining_capacity_after_fill(self, side: str, shares: int) -> int:
        opposing = self.open_sell_legs if side == "BUY" else self.open_buy_legs
        matched = min(shares, sum(item.shares for item in opposing))
        used_after = self.used_t_capacity_shares - matched + (shares - matched)
        return max(0, self.t_capacity_shares - used_after)

    def _affordable_buy_shares(self, fill_price: float) -> int:
        shares = floor_to_lot(int(self.cash / fill_price), self.lot_size)
        while shares > 0:
            notional = shares * fill_price
            commission = max(
                notional * constants.BUY_COMMISSION_RATE,
                constants.MINIMUM_COMMISSION_CNY,
            )
            if notional + commission <= self.cash + 1e-9:
                return shares
            shares -= self.lot_size
        return 0

    @staticmethod
    def _limiting_reason(
        requested_shares: int,
        requested_lots: int,
        market_capacity: int,
        action_capacity: int,
    ) -> str:
        limit = min(requested_lots, market_capacity, action_capacity)
        if requested_shares != requested_lots and limit == requested_lots:
            return "LOT_SIZE"
        if limit == market_capacity and market_capacity < requested_lots:
            return "PARTICIPATION_LIMIT"
        if limit == action_capacity and action_capacity < requested_lots:
            return "T_CAPACITY"
        return "LOT_SIZE"

    def _reject(
        self,
        bar: FillBar,
        signal_action: str,
        requested_shares: int,
        reason: str,
        *,
        rejected_shares: int | None = None,
    ) -> RejectedFill:
        rejected = RejectedFill(
            bar.timestamp,
            signal_action,
            requested_shares,
            requested_shares if rejected_shares is None else rejected_shares,
            reason,
            self.remaining_t_capacity_shares,
        )
        self.rejections.append(rejected)
        return rejected

    def _pair(self, fill: Fill) -> None:
        opposing = self.open_sell_legs if fill.side == "BUY" else self.open_buy_legs
        same_side = self.open_buy_legs if fill.side == "BUY" else self.open_sell_legs
        remaining = fill.shares
        while remaining and opposing:
            open_leg = opposing[0]
            matched = min(remaining, open_leg.shares)
            open_piece = self._portion(open_leg, matched)
            close_piece = self._portion(fill, matched)
            if fill.side == "SELL":
                buy_fill, sell_fill = open_piece, close_piece
                direction = "BUY_THEN_SELL"
            else:
                buy_fill, sell_fill = close_piece, open_piece
                direction = "SELL_THEN_BUY"
            gross = (sell_fill.market_price - buy_fill.market_price) * matched
            commission = open_piece.commission_cny + close_piece.commission_cny
            slippage = open_piece.slippage_cny + close_piece.slippage_cny
            self.completed_pairs.append(TradePair(
                direction,
                matched,
                open_piece,
                close_piece,
                gross,
                commission,
                slippage,
                gross - commission - slippage,
            ))
            if matched == open_leg.shares:
                opposing.pop(0)
            else:
                opposing[0] = self._portion(open_leg, open_leg.shares - matched)
            remaining -= matched
        if remaining:
            same_side.append(self._portion(fill, remaining))

    @staticmethod
    def _portion(fill: Fill, shares: int) -> Fill:
        ratio = shares / fill.shares
        return Fill(
            fill.timestamp,
            fill.signal_action,
            fill.side,
            fill.requested_shares,
            shares,
            fill.market_price,
            fill.price,
            fill.commission_cny * ratio,
            fill.slippage_cny * ratio,
            fill.cash_delta_cny * ratio,
            fill.remaining_t_capacity_shares,
        )


class TBacktester:
    def __init__(
        self,
        engine: TMonitorEngine | None = None,
        signal_callback: Callable[[Quote, WatchItem, QuotePoint], Any] | None = None,
    ):
        self.engine = engine or TMonitorEngine()
        self.signal_callback = signal_callback

    def run(
        self,
        quote: Quote,
        watch_item: WatchItem,
        trading: TradingMetadata,
        *,
        base_shares: int | None = None,
        t_capacity_shares: int | None = None,
        base_notional_cny: float | None = None,
        t_capacity_ratio: float | None = None,
        reserve_cash: float | None = None,
    ) -> BacktestResult:
        completed = finalized_points(quote.points, quote.observed_at)
        if not completed:
            raise ValueError("没有已完成分钟")
        first_price = _finite_positive(completed[0].price, "first_price")
        last_price = _finite_positive(completed[-1].price, "last_price")
        explicit_base = (
            base_shares if base_shares is not None
            else getattr(watch_item, "base_shares", None)
        )
        if explicit_base is not None:
            self._validate_explicit_shares(
                explicit_base, trading.lot_size, "base_shares",
            )
            configured_base = explicit_base
        else:
            configured_notional = (
                base_notional_cny if base_notional_cny is not None
                else getattr(watch_item, "base_notional_cny", None)
            )
            if configured_notional is None:
                configured_notional = constants.DEFAULT_BASE_NOTIONAL_CNY
            configured_notional = _finite_positive(
                configured_notional, "base_notional_cny",
            )
            configured_base = floor_to_lot(
                int(configured_notional / first_price),
                trading.lot_size,
            )
        explicit_capacity = (
            t_capacity_shares if t_capacity_shares is not None
            else getattr(watch_item, "t_capacity_shares", None)
        )
        if explicit_capacity is not None:
            self._validate_explicit_shares(
                explicit_capacity, trading.lot_size, "t_capacity_shares",
            )
            configured_capacity = explicit_capacity
        else:
            configured_ratio = (
                t_capacity_ratio if t_capacity_ratio is not None
                else getattr(watch_item, "t_capacity_ratio", None)
            )
            if configured_ratio is None:
                configured_ratio = constants.DEFAULT_T_CAPACITY_RATIO
            configured_ratio = _finite_positive(
                configured_ratio, "t_capacity_ratio",
            )
            if configured_ratio > 1:
                raise ValueError("t_capacity_ratio必须在(0,1]范围内")
            configured_capacity = floor_to_lot(
                int(configured_base * configured_ratio),
                trading.lot_size,
            )
        account = TAccount.create(
            base_shares=configured_base,
            t_capacity_shares=configured_capacity,
            first_price=first_price,
            lot_size=trading.lot_size,
            intraday_turnaround=trading.intraday_turnaround,
            volume_unit_shares=trading.volume_unit_shares,
            reserve_cash=reserve_cash,
        )
        curve = [account.cash + account.total_shares * first_price]
        for index in range(1, len(completed)):
            execution_point = completed[index]
            decision_point = completed[index - 1]
            account.rollover(execution_point.timestamp.date())
            if decision_point.timestamp.date() != execution_point.timestamp.date():
                curve.append(
                    account.cash + account.total_shares * execution_point.price,
                )
                continue
            decision_quote = Quote(
                quote.symbol,
                quote.name,
                decision_point.price,
                decision_point.average_price,
                decision_point.previous_close
                if decision_point.previous_close is not None
                else quote.previous_close,
                decision_point.timestamp,
                tuple(completed[:index]),
                execution_point.timestamp,
                quote.source,
            )
            action = self._action(decision_quote, watch_item, execution_point)
            if action in {"BUY_CANDIDATE", "SELL_CANDIDATE"}:
                account.execute(
                    action,
                    FillBar(
                        execution_point.timestamp.isoformat(),
                        execution_point.price,
                        execution_point.volume,
                    ),
                    configured_capacity,
                )
            curve.append(account.cash + account.total_shares * execution_point.price)
        maximum_drawdown = self._maximum_drawdown(curve)
        return self.summarize(
            account,
            first_price=first_price,
            last_price=last_price,
            maximum_drawdown=maximum_drawdown,
        )

    @staticmethod
    def _validate_explicit_shares(shares: object, lot_size: int, field: str) -> None:
        if type(shares) is not int or shares < 0 or shares % lot_size != 0:
            raise ValueError(f"{field}配置必须是{lot_size}股整数倍的非负整数")

    def _action(
        self, decision_quote: Quote, watch_item: WatchItem, execution_point: QuotePoint,
    ) -> str:
        if self.signal_callback is not None:
            decision = self.signal_callback(decision_quote, watch_item, execution_point)
            return str(getattr(decision, "action", decision))
        return self.engine.evaluate(
            (watch_item,),
            {decision_quote.symbol: decision_quote},
            generated_at=execution_point.timestamp,
        ).signals[0].action

    def summarize(
        self,
        account: TAccount,
        *,
        first_price: float,
        last_price: float,
        maximum_drawdown: float = 0.0,
    ) -> BacktestResult:
        baseline = account.base_shares * last_price + account.reserve_cash
        strategy = account.cash + account.total_shares * last_price
        completed = tuple(account.completed_pairs)
        open_legs = tuple((*account.open_buy_legs, *account.open_sell_legs))
        if completed:
            status = "OK"
            net_gain = strategy - baseline
            outperformed: bool | None = strategy > baseline
        elif open_legs:
            status = "OPEN_LEG"
            net_gain = strategy - baseline
            outperformed = None
        else:
            status = "NO_COMPLETED_PAIRS"
            net_gain = 0.0
            outperformed = None
        wins = sum(1 for item in completed if item.net_pnl_cny > 0)
        paired_net = sum(item.net_pnl_cny for item in completed)
        sell_fly_loss = sum(
            self._sell_fly_loss(item, last_price)
            for item in account.open_sell_legs
        )
        return BacktestResult(
            status=status,
            execution_mode="NEXT_COMPLETED_BAR",
            base_shares=account.base_shares,
            t_capacity_shares=account.t_capacity_shares,
            reserve_cash_cny=account.reserve_cash,
            first_price=first_price,
            last_price=last_price,
            strategy_ending_equity_cny=strategy,
            baseline_ending_equity_cny=baseline,
            t_net_gain_cny=net_gain,
            t_net_gain_rate=net_gain / baseline if baseline else 0.0,
            completed_pairs=completed,
            open_legs=open_legs,
            rejections=tuple(account.rejections),
            total_shares=account.total_shares,
            overnight_sellable_shares=account.overnight_sellable_shares,
            today_bought_shares=account.today_bought_shares,
            cash_cny=account.cash,
            paired_net_pnl_cny=paired_net,
            win_rate=wins / len(completed) if completed else None,
            sell_fly_loss_cny=sell_fly_loss,
            total_commission_cny=account.total_commission_cny,
            total_slippage_cny=account.total_slippage_cny,
            maximum_drawdown=maximum_drawdown,
            outperformed_baseline=outperformed,
        )

    def summarize_no_trade(
        self,
        *,
        base_shares: int,
        reserve_cash: float,
        first_price: float,
        last_price: float,
    ) -> BacktestResult:
        strategy = base_shares * last_price + reserve_cash
        return BacktestResult(
            status="NO_COMPLETED_PAIRS",
            execution_mode="NEXT_COMPLETED_BAR",
            base_shares=base_shares,
            t_capacity_shares=0,
            reserve_cash_cny=reserve_cash,
            first_price=first_price,
            last_price=last_price,
            strategy_ending_equity_cny=strategy,
            baseline_ending_equity_cny=strategy,
            t_net_gain_cny=0.0,
            t_net_gain_rate=0.0,
            total_shares=base_shares,
            overnight_sellable_shares=base_shares,
            cash_cny=reserve_cash,
            outperformed_baseline=None,
        )

    @staticmethod
    def _maximum_drawdown(curve: list[float]) -> float:
        if not curve:
            return 0.0
        peak = curve[0]
        result = 0.0
        for value in curve:
            peak = max(peak, value)
            if peak > 0:
                result = max(result, 1 - value / peak)
        return result

    @staticmethod
    def _sell_fly_loss(fill: Fill, last_price: float) -> float:
        cover_price = last_price * (1 + constants.SLIPPAGE_RATE)
        cover_notional = fill.shares * cover_price
        cover_fee = max(
            cover_notional * constants.BUY_COMMISSION_RATE,
            constants.MINIMUM_COMMISSION_CNY,
        )
        cover_cost = cover_notional + cover_fee
        sale_proceeds = fill.shares * fill.price - fill.commission_cny
        return max(0.0, cover_cost - sale_proceeds)
