"""Append-only local portfolio ledger for the swing monitor.

The JSONL event stream is the sole source of truth.  Every mutation holds one
cross-process lock while it reads, validates, and atomically rewrites that
stream; the JSON projection is always disposable and rebuilt from events.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Callable
import tempfile
import uuid

from .etf_metadata import EtfMetadata
from .swing_data import _SiblingFileLock


SHANGHAI = timezone(timedelta(hours=8))
_SCHEMA_VERSION = 1
_MAX_IDEMPOTENCY_LENGTH = 256


class PortfolioLedgerError(ValueError):
    """Raised when portfolio input or its authoritative event log is invalid."""


class PortfolioEventType(StrEnum):
    ACCOUNT_INITIALIZED = "ACCOUNT_INITIALIZED"
    BUY_CONFIRMED = "BUY_CONFIRMED"
    SELL_CONFIRMED = "SELL_CONFIRMED"
    TRADE_REVERSED = "TRADE_REVERSED"


@dataclass(frozen=True)
class TradeInput:
    symbol: str
    side: str
    shares: int
    price: float
    fee: float
    executed_at: datetime
    planned_risk_per_share: float = 0.0


@dataclass(frozen=True)
class InitialPositionInput:
    shares: int
    average_cost: float
    planned_risk_per_share: float = 0.0


@dataclass(frozen=True)
class PortfolioEvent:
    schema_version: int
    event_id: str
    event_type: PortfolioEventType
    idempotency_key: str
    recorded_at: datetime
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise PortfolioLedgerError("event schema_version must be integer 1")
        _require_uuid4(self.event_id, "event_id")
        if type(self.event_type) is not PortfolioEventType:
            raise PortfolioLedgerError("event_type is invalid")
        _validate_idempotency_key(self.idempotency_key)
        object.__setattr__(
            self, "recorded_at", _aware_datetime(self.recorded_at, "recorded_at"),
        )
        if not isinstance(self.payload, Mapping):
            raise PortfolioLedgerError("event payload must be an object")
        try:
            payload = dict(self.payload)
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError) as error:
            raise PortfolioLedgerError("event payload must contain JSON values") from error
        object.__setattr__(self, "payload", MappingProxyType(payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "idempotency_key": self.idempotency_key,
            "recorded_at": self.recorded_at.isoformat(),
            "payload": dict(self.payload),
        }

    @classmethod
    def from_mapping(cls, value: object) -> PortfolioEvent:
        if not isinstance(value, Mapping):
            raise PortfolioLedgerError("event log record must be an object")
        expected = {
            "schema_version", "event_id", "event_type", "idempotency_key",
            "recorded_at", "payload",
        }
        if set(value) != expected:
            raise PortfolioLedgerError("event log record fields are invalid")
        try:
            event_type = PortfolioEventType(value["event_type"])
        except (TypeError, ValueError) as error:
            raise PortfolioLedgerError("event_type is invalid") from error
        recorded_at = _parse_datetime(value["recorded_at"], "recorded_at")
        return cls(
            schema_version=value["schema_version"],
            event_id=value["event_id"],
            event_type=event_type,
            idempotency_key=value["idempotency_key"],
            recorded_at=recorded_at,
            payload=value["payload"],
        )


@dataclass(frozen=True)
class PortfolioPosition:
    symbol: str
    shares: int
    sellable_shares: int
    today_bought_shares: int
    average_cost: float
    market_price: float
    market_value: float
    planned_risk: float

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "shares": self.shares,
            "sellable_shares": self.sellable_shares,
            "today_bought_shares": self.today_bought_shares,
            "average_cost": self.average_cost,
            "market_price": self.market_price,
            "market_value": self.market_value,
            "planned_risk": self.planned_risk,
        }


@dataclass(frozen=True)
class PortfolioProjection:
    schema_version: int
    name: str
    default_risk_per_trade: float
    as_of_trading_date: date
    cash: float
    positions: Mapping[str, PortfolioPosition]
    realized_pnl: float
    etf_market_value: float
    equity: float
    planned_risk: float
    warnings: tuple[str, ...]
    last_event_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "default_risk_per_trade": self.default_risk_per_trade,
            "as_of_trading_date": self.as_of_trading_date.isoformat(),
            "cash": self.cash,
            "positions": {
                symbol: position.to_dict()
                for symbol, position in sorted(self.positions.items())
            },
            "realized_pnl": self.realized_pnl,
            "etf_market_value": self.etf_market_value,
            "equity": self.equity,
            "planned_risk": self.planned_risk,
            "warnings": list(self.warnings),
            "last_event_id": self.last_event_id,
        }


@dataclass
class _Lot:
    shares: int
    bought_on: date
    planned_risk_per_share: Decimal


@dataclass
class _MutablePosition:
    shares: int
    cost_basis: Decimal
    lots: list[_Lot]
    planned_risk: Decimal


class PortfolioLedger:
    """Validate and persist one unleveraged manual cash account."""

    def __init__(
        self,
        path: Path,
        metadata: Mapping[str, EtfMetadata],
        *,
        clock: Callable[[], datetime] | None = None,
        max_portfolio_risk_rate: float = 0.02,
    ):
        self.path = Path(path).resolve(strict=False)
        self.metadata = _snapshot_metadata(metadata)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.max_portfolio_risk_rate = _finite_decimal(
            max_portfolio_risk_rate, "max_portfolio_risk_rate", positive=True,
        )
        if self.max_portfolio_risk_rate > Decimal("1"):
            raise PortfolioLedgerError("max_portfolio_risk_rate must not exceed 1")

    def initialize(
        self,
        name: str,
        cash: float,
        idempotency_key: str,
        *,
        initial_positions: Mapping[str, InitialPositionInput | Mapping[str, object]]
        | None = None,
        default_risk_per_trade: float = 0.0075,
    ) -> PortfolioEvent:
        key = _validate_idempotency_key(idempotency_key)
        account_name = _nonblank_text(name, "account name")
        initial_cash = _finite_decimal(cash, "cash", positive=False)
        positions = self._validate_initial_positions(initial_positions)
        risk_rate = _finite_decimal(
            default_risk_per_trade, "default_risk_per_trade", positive=True,
        )
        if risk_rate > Decimal("1"):
            raise PortfolioLedgerError("default_risk_per_trade must not exceed 1")

        def build(events: tuple[PortfolioEvent, ...]) -> PortfolioEvent:
            if events:
                raise PortfolioLedgerError("portfolio account is already initialized")
            return self._event(
                PortfolioEventType.ACCOUNT_INITIALIZED,
                key,
                {
                    "name": account_name,
                    "cash": float(initial_cash),
                    "initial_positions": {
                        symbol: {
                            "shares": position.shares,
                            "average_cost": position.average_cost,
                            "planned_risk_per_share": position.planned_risk_per_share,
                        }
                        for symbol, position in sorted(positions.items())
                    },
                    "default_risk_per_trade": float(risk_rate),
                },
            )

        return self._mutate_idempotent(key, build)

    def record_trade(
        self,
        trade: TradeInput,
        idempotency_key: str,
    ) -> PortfolioEvent:
        key = _validate_idempotency_key(idempotency_key)
        normalized = self._validate_trade_input(trade)

        def build(events: tuple[PortfolioEvent, ...]) -> PortfolioEvent:
            latest = self._latest_trade_datetime(events)
            if latest is not None and normalized.executed_at < latest:
                raise PortfolioLedgerError("trade execution time is out of order")
            current = self._replay(events, normalized.executed_at.date(), {})
            self._validate_trade_against_projection(current, normalized)
            event_type = (
                PortfolioEventType.BUY_CONFIRMED
                if normalized.side == "BUY"
                else PortfolioEventType.SELL_CONFIRMED
            )
            return self._event(event_type, key, {
                "symbol": normalized.symbol,
                "side": normalized.side,
                "shares": normalized.shares,
                "price": normalized.price,
                "fee": normalized.fee,
                "executed_at": normalized.executed_at.isoformat(),
                "planned_risk_per_share": normalized.planned_risk_per_share,
            })

        return self._mutate_idempotent(key, build)

    def reverse(self, event_id: str, idempotency_key: str) -> PortfolioEvent:
        target_id = _require_uuid4(event_id, "reversal target event_id")
        key = _validate_idempotency_key(idempotency_key)

        def build(events: tuple[PortfolioEvent, ...]) -> PortfolioEvent:
            by_id = {event.event_id: event for event in events}
            target = by_id.get(target_id)
            if target is None:
                raise PortfolioLedgerError("reversal target does not exist")
            if target.event_type not in {
                PortfolioEventType.BUY_CONFIRMED,
                PortfolioEventType.SELL_CONFIRMED,
            }:
                raise PortfolioLedgerError("reversal target is not a trade")
            reversed_ids = _reversed_event_ids(events)
            if target_id in reversed_ids:
                raise PortfolioLedgerError("trade is already reversed")
            candidate = self._event(
                PortfolioEventType.TRADE_REVERSED,
                key,
                {"target_event_id": target_id},
            )
            try:
                self._replay(
                    events + (candidate,), self._latest_trading_date(events), {},
                )
            except PortfolioLedgerError as error:
                raise PortfolioLedgerError(
                    "reversal invalidates later portfolio events",
                ) from error
            return candidate

        return self._mutate_idempotent(key, build)

    def load_events(self) -> tuple[PortfolioEvent, ...]:
        if not self.path.parent.exists():
            return ()
        with _SiblingFileLock(self.path, shared=True):
            return self._load_events_unlocked()

    def project(
        self,
        trading_date: date,
        marks: Mapping[str, float],
    ) -> PortfolioProjection:
        as_of = _strict_date(trading_date, "trading_date")
        normalized_marks = self._validate_marks(marks)
        events = self.load_events()
        return self._replay(events, as_of, normalized_marks)

    def load_or_rebuild_projection(
        self,
        projection_path: Path,
        trading_date: date,
        marks: Mapping[str, float],
    ) -> PortfolioProjection:
        projected = self.project(trading_date, marks)
        destination = Path(projection_path).resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(destination, shared=False):
            _atomic_replace_json(destination, projected.to_dict())
        return projected

    def _mutate_idempotent(
        self,
        key: str,
        build: Callable[[tuple[PortfolioEvent, ...]], PortfolioEvent],
    ) -> PortfolioEvent:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=False):
            events = self._load_events_unlocked()
            matches = tuple(event for event in events if event.idempotency_key == key)
            if len(matches) > 1:
                raise PortfolioLedgerError("event log has duplicate idempotency keys")
            if matches:
                return matches[0]
            event = build(events)
            if any(existing.event_id == event.event_id for existing in events):
                raise PortfolioLedgerError("generated duplicate event_id")
            updated = events + (event,)
            self._validate_event_sequence(updated)
            self._atomic_replace_events(updated)
            return event

    def _event(
        self,
        event_type: PortfolioEventType,
        key: str,
        payload: Mapping[str, object],
    ) -> PortfolioEvent:
        try:
            recorded_at = self.clock()
        except Exception as error:
            raise PortfolioLedgerError("portfolio clock failed") from error
        return PortfolioEvent(
            schema_version=_SCHEMA_VERSION,
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            idempotency_key=key,
            recorded_at=recorded_at,
            payload=payload,
        )

    def _load_events_unlocked(self) -> tuple[PortfolioEvent, ...]:
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise PortfolioLedgerError(f"event log read failed: {error}") from error
        if not content:
            return ()
        if not content.endswith(b"\n"):
            raise PortfolioLedgerError("event log has an incomplete final line")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PortfolioLedgerError("event log is not valid UTF-8") from error
        events: list[PortfolioEvent] = []
        for line_number, line in enumerate(text[:-1].split("\n"), start=1):
            if not line:
                raise PortfolioLedgerError(
                    f"event log contains blank line {line_number}",
                )
            try:
                payload = json.loads(line)
            except (ValueError, OverflowError, RecursionError) as error:
                raise PortfolioLedgerError(
                    f"event log JSON is invalid at line {line_number}",
                ) from error
            try:
                event = PortfolioEvent.from_mapping(payload)
            except PortfolioLedgerError as error:
                raise PortfolioLedgerError(
                    f"event log record is invalid at line {line_number}: {error}",
                ) from error
            if line != self._encode_event(event):
                raise PortfolioLedgerError(
                    f"event log record is not canonical at line {line_number}",
                )
            events.append(event)
        result = tuple(events)
        self._validate_event_sequence(result)
        if result:
            self._replay(result, date.max, {})
        return result

    def _validate_event_sequence(self, events: tuple[PortfolioEvent, ...]) -> None:
        event_ids: set[str] = set()
        keys: set[str] = set()
        initialized = False
        known_trade_ids: set[str] = set()
        reversed_ids: set[str] = set()
        for index, event in enumerate(events):
            if event.event_id in event_ids:
                raise PortfolioLedgerError("event log has duplicate event IDs")
            if event.idempotency_key in keys:
                raise PortfolioLedgerError("event log has duplicate idempotency keys")
            event_ids.add(event.event_id)
            keys.add(event.idempotency_key)
            if event.event_type is PortfolioEventType.ACCOUNT_INITIALIZED:
                if initialized or index != 0:
                    raise PortfolioLedgerError("event log account initialization is invalid")
                self._parse_initialization(event)
                initialized = True
            elif not initialized:
                raise PortfolioLedgerError("event log starts before account initialization")
            elif event.event_type in {
                PortfolioEventType.BUY_CONFIRMED,
                PortfolioEventType.SELL_CONFIRMED,
            }:
                self._trade_from_event(event)
                known_trade_ids.add(event.event_id)
            else:
                target_id = self._reversal_target(event)
                if target_id not in known_trade_ids:
                    raise PortfolioLedgerError("reversal target must be an earlier trade")
                if target_id in reversed_ids:
                    raise PortfolioLedgerError("trade is already reversed")
                reversed_ids.add(target_id)
        if events and not initialized:
            raise PortfolioLedgerError("event log has no account initialization")

    def _atomic_replace_events(self, events: tuple[PortfolioEvent, ...]) -> None:
        serialized = "".join(
            self._encode_event(event) + "\n"
            for event in events
        ).encode("utf-8")
        _atomic_replace_bytes(self.path, serialized)

    @staticmethod
    def _encode_event(event: PortfolioEvent) -> str:
        return json.dumps(
            event.to_dict(), ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        )

    def _replay(
        self,
        events: tuple[PortfolioEvent, ...],
        trading_date: date,
        marks: Mapping[str, Decimal],
    ) -> PortfolioProjection:
        if not events:
            raise PortfolioLedgerError("portfolio account is not initialized")
        self._validate_event_sequence(events)
        name, initial_cash, initial_positions, default_risk_rate = (
            self._parse_initialization(events[0])
        )
        cash = initial_cash
        realized = Decimal("0")
        positions: dict[str, _MutablePosition] = {
            symbol: _MutablePosition(
                shares=position.shares,
                cost_basis=_decimal(position.average_cost) * position.shares,
                lots=[
                    _Lot(
                        position.shares,
                        date.min,
                        _decimal(position.planned_risk_per_share),
                    ),
                ] if position.shares else [],
                planned_risk=(
                    _decimal(position.planned_risk_per_share) * position.shares
                ),
            )
            for symbol, position in initial_positions.items()
            if position.shares
        }
        reversed_ids = _reversed_event_ids(events)
        last_event_id = events[-1].event_id
        for event in events[1:]:
            if event.event_type is PortfolioEventType.TRADE_REVERSED:
                continue
            if event.event_id in reversed_ids:
                continue
            trade = self._trade_from_event(event)
            if trade.executed_at.date() > trading_date:
                continue
            metadata = self.metadata[trade.symbol]
            position = positions.get(trade.symbol)
            if trade.side == "BUY":
                cost = _decimal(trade.price) * trade.shares + _decimal(trade.fee)
                if cost > cash:
                    raise PortfolioLedgerError("event log buy exceeds available cash")
                cash -= cost
                if position is None:
                    position = _MutablePosition(0, Decimal("0"), [], Decimal("0"))
                    positions[trade.symbol] = position
                position.shares += trade.shares
                position.cost_basis += cost
                risk_per_share = _decimal(trade.planned_risk_per_share)
                position.planned_risk += risk_per_share * trade.shares
                position.lots.append(
                    _Lot(trade.shares, trade.executed_at.date(), risk_per_share),
                )
                continue

            if position is None or position.shares < trade.shares:
                raise PortfolioLedgerError("event log sell exceeds owned shares")
            sellable = _sellable_shares(position, trading_date, metadata)
            if trade.shares > sellable:
                raise PortfolioLedgerError("event log sell exceeds sellable shares")
            average_cost = position.cost_basis / position.shares
            average_risk = position.planned_risk / position.shares
            proceeds = _decimal(trade.price) * trade.shares - _decimal(trade.fee)
            realized += proceeds - average_cost * trade.shares
            cash += proceeds
            position.cost_basis -= average_cost * trade.shares
            position.planned_risk -= average_risk * trade.shares
            position.shares -= trade.shares
            _consume_sellable_lots(position, trade.shares, trading_date, metadata)
            if position.shares == 0:
                del positions[trade.symbol]

        output_positions: dict[str, PortfolioPosition] = {}
        total_market_value = Decimal("0")
        total_risk = Decimal("0")
        warnings: list[str] = []
        for symbol in sorted(positions):
            position = positions[symbol]
            average_cost = position.cost_basis / position.shares
            mark = marks.get(symbol)
            if mark is None:
                mark = average_cost
                warnings.append(f"MISSING_MARK:{symbol}")
            market_value = mark * position.shares
            sellable = _sellable_shares(
                position, trading_date, self.metadata[symbol],
            )
            today_bought = sum(
                lot.shares for lot in position.lots if lot.bought_on == trading_date
            )
            output_positions[symbol] = PortfolioPosition(
                symbol=symbol,
                shares=position.shares,
                sellable_shares=sellable,
                today_bought_shares=today_bought,
                average_cost=float(average_cost),
                market_price=float(mark),
                market_value=float(market_value),
                planned_risk=float(position.planned_risk),
            )
            total_market_value += market_value
            total_risk += position.planned_risk
        equity = cash + total_market_value
        if equity > 0 and total_risk > equity * self.max_portfolio_risk_rate:
            warnings.append("RISK_LIMIT_EXCEEDED")
        return PortfolioProjection(
            schema_version=_SCHEMA_VERSION,
            name=name,
            default_risk_per_trade=float(default_risk_rate),
            as_of_trading_date=trading_date,
            cash=float(cash),
            positions=MappingProxyType(output_positions),
            realized_pnl=float(realized),
            etf_market_value=float(total_market_value),
            equity=float(equity),
            planned_risk=float(total_risk),
            warnings=tuple(warnings),
            last_event_id=last_event_id,
        )

    def _validate_trade_input(self, trade: TradeInput) -> TradeInput:
        if type(trade) is not TradeInput:
            raise PortfolioLedgerError("trade must be TradeInput")
        symbol = trade.symbol
        if (
            type(symbol) is not str or len(symbol) != 6
            or not symbol.isascii() or not symbol.isdigit()
        ):
            raise PortfolioLedgerError("trade symbol must be six ASCII digits")
        metadata = self.metadata.get(symbol)
        if metadata is None:
            raise PortfolioLedgerError(f"unknown ETF symbol: {symbol}")
        if type(trade.side) is not str or trade.side not in {"BUY", "SELL"}:
            raise PortfolioLedgerError("trade side must be BUY or SELL")
        if type(trade.shares) is not int or trade.shares <= 0:
            raise PortfolioLedgerError("trade shares must be a positive integer")
        if trade.shares % metadata.trading.lot_size != 0:
            raise PortfolioLedgerError("trade shares must be an exact lot")
        price = _finite_decimal(trade.price, "trade price", positive=True)
        fee = _finite_decimal(trade.fee, "trade fee", positive=False)
        risk = _finite_decimal(
            trade.planned_risk_per_share,
            "planned_risk_per_share",
            positive=False,
        )
        executed_at = _aware_datetime(trade.executed_at, "executed_at")
        return TradeInput(
            symbol, trade.side, trade.shares, float(price), float(fee), executed_at,
            float(risk),
        )

    def _validate_initial_positions(
        self,
        value: Mapping[str, InitialPositionInput | Mapping[str, object]] | None,
    ) -> Mapping[str, InitialPositionInput]:
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise PortfolioLedgerError("initial_positions must be a mapping")
        try:
            items = tuple(value.items())
        except Exception as error:
            raise PortfolioLedgerError(
                "initial_positions mapping could not be read",
            ) from error
        result: dict[str, InitialPositionInput] = {}
        for symbol, raw_position in items:
            if type(symbol) is not str or symbol not in self.metadata:
                raise PortfolioLedgerError(f"unknown initial ETF symbol: {symbol}")
            if type(raw_position) is InitialPositionInput:
                position = raw_position
            elif isinstance(raw_position, Mapping):
                try:
                    raw = dict(raw_position)
                except Exception as error:
                    raise PortfolioLedgerError(
                        f"initial position {symbol} could not be read",
                    ) from error
                allowed = {"shares", "average_cost", "planned_risk_per_share"}
                if not {"shares", "average_cost"} <= set(raw) or not set(raw) <= allowed:
                    raise PortfolioLedgerError(
                        f"initial position {symbol} fields are invalid",
                    )
                position = InitialPositionInput(
                    shares=raw["shares"],
                    average_cost=raw["average_cost"],
                    planned_risk_per_share=raw.get("planned_risk_per_share", 0.0),
                )
            else:
                raise PortfolioLedgerError(
                    f"initial position {symbol} must be an object",
                )
            if type(position.shares) is not int or position.shares < 0:
                raise PortfolioLedgerError(
                    f"initial position {symbol} shares must be a nonnegative integer",
                )
            average_cost = _finite_decimal(
                position.average_cost,
                f"initial position {symbol} average_cost",
                positive=True,
            )
            planned_risk = _finite_decimal(
                position.planned_risk_per_share,
                f"initial position {symbol} planned_risk_per_share",
                positive=False,
            )
            result[symbol] = InitialPositionInput(
                position.shares, float(average_cost), float(planned_risk),
            )
        return MappingProxyType(result)

    @staticmethod
    def _validate_trade_against_projection(
        projection: PortfolioProjection,
        trade: TradeInput,
    ) -> None:
        if trade.side == "BUY":
            required = _decimal(trade.price) * trade.shares + _decimal(trade.fee)
            if required > _decimal(projection.cash):
                raise PortfolioLedgerError("buy exceeds available cash")
            return
        position = projection.positions.get(trade.symbol)
        sellable = 0 if position is None else position.sellable_shares
        if trade.shares > sellable:
            raise PortfolioLedgerError("sell exceeds sellable shares")

    def _validate_marks(self, marks: Mapping[str, float]) -> Mapping[str, Decimal]:
        if not isinstance(marks, Mapping):
            raise PortfolioLedgerError("marks must be a mapping")
        result: dict[str, Decimal] = {}
        try:
            items = tuple(marks.items())
        except Exception as error:
            raise PortfolioLedgerError("marks mapping could not be read") from error
        for symbol, value in items:
            if symbol not in self.metadata:
                raise PortfolioLedgerError(f"mark has unknown ETF symbol: {symbol}")
            result[symbol] = _finite_decimal(value, f"mark {symbol}", positive=True)
        return MappingProxyType(result)

    def _parse_initialization(
        self, event: PortfolioEvent,
    ) -> tuple[
        str, Decimal, Mapping[str, InitialPositionInput], Decimal,
    ]:
        if event.event_type is not PortfolioEventType.ACCOUNT_INITIALIZED:
            raise PortfolioLedgerError("first event is not account initialization")
        legacy = {"name", "cash"}
        current = legacy | {"initial_positions", "default_risk_per_trade"}
        if set(event.payload) not in (legacy, current):
            raise PortfolioLedgerError("account initialization payload is invalid")
        if set(event.payload) == legacy:
            initial_positions = self._validate_initial_positions(None)
            default_risk = Decimal("0.0075")
        else:
            initial_positions = self._validate_initial_positions(
                event.payload["initial_positions"],
            )
            default_risk = _finite_decimal(
                event.payload["default_risk_per_trade"],
                "default_risk_per_trade",
                positive=True,
            )
            if default_risk > Decimal("1"):
                raise PortfolioLedgerError("default_risk_per_trade must not exceed 1")
        return (
            _nonblank_text(event.payload["name"], "account name"),
            _finite_decimal(event.payload["cash"], "cash", positive=False),
            initial_positions,
            default_risk,
        )

    def _trade_from_event(self, event: PortfolioEvent) -> TradeInput:
        expected = {
            "symbol", "side", "shares", "price", "fee", "executed_at",
            "planned_risk_per_share",
        }
        if set(event.payload) != expected:
            raise PortfolioLedgerError("trade event payload is invalid")
        trade = TradeInput(
            symbol=event.payload["symbol"],
            side=event.payload["side"],
            shares=event.payload["shares"],
            price=event.payload["price"],
            fee=event.payload["fee"],
            executed_at=_parse_datetime(event.payload["executed_at"], "executed_at"),
            planned_risk_per_share=event.payload["planned_risk_per_share"],
        )
        normalized = self._validate_trade_input(trade)
        expected_type = (
            PortfolioEventType.BUY_CONFIRMED
            if normalized.side == "BUY"
            else PortfolioEventType.SELL_CONFIRMED
        )
        if event.event_type is not expected_type:
            raise PortfolioLedgerError("trade side does not match event type")
        return normalized

    @staticmethod
    def _reversal_target(event: PortfolioEvent) -> str:
        if event.event_type is not PortfolioEventType.TRADE_REVERSED:
            raise PortfolioLedgerError("event is not a reversal")
        if set(event.payload) != {"target_event_id"}:
            raise PortfolioLedgerError("reversal event payload is invalid")
        return _require_uuid4(
            event.payload["target_event_id"], "reversal target event_id",
        )

    def _latest_trade_datetime(
        self, events: tuple[PortfolioEvent, ...],
    ) -> datetime | None:
        values = [
            self._trade_from_event(event).executed_at
            for event in events
            if event.event_type in {
                PortfolioEventType.BUY_CONFIRMED,
                PortfolioEventType.SELL_CONFIRMED,
            }
        ]
        return max(values) if values else None

    def _latest_trading_date(self, events: tuple[PortfolioEvent, ...]) -> date:
        latest = self._latest_trade_datetime(events)
        return latest.date() if latest is not None else self._now().date()

    def _now(self) -> datetime:
        try:
            return _aware_datetime(self.clock(), "portfolio clock")
        except PortfolioLedgerError:
            raise
        except Exception as error:
            raise PortfolioLedgerError("portfolio clock failed") from error


def _snapshot_metadata(
    metadata: Mapping[str, EtfMetadata],
) -> Mapping[str, EtfMetadata]:
    if not isinstance(metadata, Mapping):
        raise PortfolioLedgerError("ETF metadata must be a mapping")
    try:
        items = tuple(metadata.items())
    except Exception as error:
        raise PortfolioLedgerError("ETF metadata mapping could not be read") from error
    result: dict[str, EtfMetadata] = {}
    for symbol, value in items:
        if type(symbol) is not str or type(value) is not EtfMetadata:
            raise PortfolioLedgerError("ETF metadata entries are invalid")
        if value.symbol != symbol:
            raise PortfolioLedgerError("ETF metadata symbol is inconsistent")
        result[symbol] = value
    return MappingProxyType(result)


def _validate_idempotency_key(value: object) -> str:
    if (
        type(value) is not str or not value.strip()
        or len(value) > _MAX_IDEMPOTENCY_LENGTH
    ):
        raise PortfolioLedgerError("idempotency_key must be a nonblank opaque string")
    return value


def _nonblank_text(value: object, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise PortfolioLedgerError(f"{field} must be a nonblank string")
    return value.strip()


def _finite_decimal(
    value: object,
    field: str,
    *,
    positive: bool,
) -> Decimal:
    if type(value) not in (int, float, Decimal):
        raise PortfolioLedgerError(f"{field} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise PortfolioLedgerError(f"{field} must be a finite number") from error
    if not number.is_finite():
        raise PortfolioLedgerError(f"{field} must be a finite number")
    if positive and number <= 0:
        raise PortfolioLedgerError(f"{field} must be positive")
    if not positive and number < 0:
        raise PortfolioLedgerError(f"{field} must be nonnegative")
    return number


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _strict_date(value: object, field: str) -> date:
    if type(value) is not date:
        raise PortfolioLedgerError(f"{field} must be a date")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise PortfolioLedgerError(f"{field} must be a timezone-aware datetime")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as error:
        raise PortfolioLedgerError(f"{field} timezone is invalid") from error
    if offset is None:
        raise PortfolioLedgerError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(SHANGHAI)


def _parse_datetime(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise PortfolioLedgerError(f"{field} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError) as error:
        raise PortfolioLedgerError(f"{field} is not a valid ISO datetime") from error
    return _aware_datetime(parsed, field)


def _require_uuid4(value: object, field: str) -> str:
    if type(value) is not str:
        raise PortfolioLedgerError(f"{field} must be a canonical lowercase UUID4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise PortfolioLedgerError(f"{field} must be a canonical lowercase UUID4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise PortfolioLedgerError(f"{field} must be a canonical lowercase UUID4")
    return value


def _reversed_event_ids(events: tuple[PortfolioEvent, ...]) -> set[str]:
    result: set[str] = set()
    for event in events:
        if event.event_type is PortfolioEventType.TRADE_REVERSED:
            result.add(PortfolioLedger._reversal_target(event))
    return result


def _is_sellable(lot: _Lot, trading_date: date, metadata: EtfMetadata) -> bool:
    delay = metadata.trading.sellable_delay_days
    if metadata.trading.intraday_turnaround or delay == 0:
        return lot.bought_on <= trading_date
    return (trading_date - lot.bought_on).days >= delay


def _sellable_shares(
    position: _MutablePosition,
    trading_date: date,
    metadata: EtfMetadata,
) -> int:
    return sum(
        lot.shares for lot in position.lots
        if _is_sellable(lot, trading_date, metadata)
    )


def _consume_sellable_lots(
    position: _MutablePosition,
    shares: int,
    trading_date: date,
    metadata: EtfMetadata,
) -> None:
    remaining = shares
    retained: list[_Lot] = []
    for lot in position.lots:
        if remaining and _is_sellable(lot, trading_date, metadata):
            consumed = min(remaining, lot.shares)
            remaining -= consumed
            lot.shares -= consumed
        if lot.shares:
            retained.append(lot)
    if remaining:
        raise PortfolioLedgerError("sell exceeds sellable shares")
    position.lots = retained


def _atomic_replace_json(path: Path, payload: Mapping[str, object]) -> None:
    try:
        content = json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, OverflowError) as error:
        raise PortfolioLedgerError("projection serialization failed") from error
    _atomic_replace_bytes(path, content)


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary_path = Path(raw_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise PortfolioLedgerError(f"atomic portfolio write failed: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
