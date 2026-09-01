"""Append-only local portfolio ledger for the swing monitor.

The JSONL event stream is the sole source of truth.  Every mutation holds one
cross-process lock while it reads, validates, and atomically rewrites that
stream; the JSON projection is always disposable and rebuilt from events.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import json
import math
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
            payload = _freeze_json(self.payload)
        except PortfolioLedgerError:
            raise
        except Exception as error:
            raise PortfolioLedgerError("event payload must contain JSON values") from error
        if not isinstance(payload, Mapping):
            raise PortfolioLedgerError("event payload must be an object")
        object.__setattr__(self, "payload", payload)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "idempotency_key": self.idempotency_key,
            "recorded_at": self.recorded_at.isoformat(),
            "payload": _thaw_json(self.payload),
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
        closed_dates: Iterable[date] = (),
    ):
        self.path = Path(path).resolve(strict=False)
        self.metadata = _snapshot_metadata(metadata)
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.max_portfolio_risk_rate = _finite_decimal(
            max_portfolio_risk_rate, "max_portfolio_risk_rate", positive=True,
        )
        if self.max_portfolio_risk_rate > Decimal("1"):
            raise PortfolioLedgerError("max_portfolio_risk_rate must not exceed 1")
        self.closed_dates = _snapshot_closed_dates(closed_dates)

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
        request_payload = {
            "name": account_name,
            "cash": _public_float(initial_cash, "cash"),
            "initial_positions": {
                symbol: {
                    "shares": position.shares,
                    "average_cost": position.average_cost,
                    "planned_risk_per_share": position.planned_risk_per_share,
                }
                for symbol, position in sorted(positions.items())
            },
            "default_risk_per_trade": _public_float(
                risk_rate, "default_risk_per_trade",
            ),
        }

        def build(events: tuple[PortfolioEvent, ...]) -> PortfolioEvent:
            if events:
                raise PortfolioLedgerError("portfolio account is already initialized")
            return self._event(
                PortfolioEventType.ACCOUNT_INITIALIZED,
                key,
                request_payload,
            )

        return self._mutate_idempotent(
            key,
            lambda event: (
                event.event_type is PortfolioEventType.ACCOUNT_INITIALIZED
                and dict(event.payload) == request_payload
            ),
            build,
        )

    def record_trade(
        self,
        trade: TradeInput,
        idempotency_key: str,
    ) -> PortfolioEvent:
        key = _validate_idempotency_key(idempotency_key)
        normalized = self._validate_trade_input(trade)
        event_type = (
            PortfolioEventType.BUY_CONFIRMED
            if normalized.side == "BUY"
            else PortfolioEventType.SELL_CONFIRMED
        )
        request_payload = self._trade_request_payload(normalized)

        def build(events: tuple[PortfolioEvent, ...]) -> PortfolioEvent:
            effective_risk = _decimal(normalized.planned_risk_per_share)
            if normalized.side == "BUY" and effective_risk == 0:
                before = self._replay(
                    events, normalized.executed_at.date(), {},
                )
                effective_risk = (
                    _decimal(before.equity)
                    * _decimal(before.default_risk_per_trade)
                    / normalized.shares
                )
            payload = dict(request_payload)
            payload["effective_planned_risk_per_share"] = _public_float(
                effective_risk, "effective_planned_risk_per_share",
            )
            candidate = self._event(event_type, key, payload)
            self._replay(events + (candidate,), date.max, {})
            return candidate

        return self._mutate_idempotent(
            key,
            lambda event: (
                event.event_type is event_type
                and self._trade_request_payload_from_event(event) == request_payload
            ),
            build,
        )

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
                self._replay(events + (candidate,), date.max, {})
            except PortfolioLedgerError as error:
                raise PortfolioLedgerError(
                    "reversal invalidates later portfolio events",
                ) from error
            return candidate

        return self._mutate_idempotent(
            key,
            lambda event: (
                event.event_type is PortfolioEventType.TRADE_REVERSED
                and self._reversal_target(event) == target_id
            ),
            build,
        )

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
        as_of = _strict_date(trading_date, "trading_date")
        normalized_marks = self._validate_marks(marks)
        destination = _resolve_path(projection_path, "projection_path")
        self._reject_projection_alias(destination)
        if not self.path.parent.exists():
            raise PortfolioLedgerError("portfolio account is not initialized")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=True):
            events = self._load_events_unlocked()
            projected = self._replay(events, as_of, normalized_marks)
            with _SiblingFileLock(destination, shared=False):
                if not _existing_projection_is_newer(destination, projected):
                    _atomic_replace_json(destination, projected.to_dict())
            return projected

    def _reject_projection_alias(self, destination: Path) -> None:
        if destination == self.path:
            raise PortfolioLedgerError("projection_path must not alias event path")
        if not self.path.exists() or not destination.exists():
            return
        try:
            aliases = os.path.samefile(self.path, destination)
        except OSError as error:
            raise PortfolioLedgerError(
                "could not verify projection_path does not alias event path",
            ) from error
        if aliases:
            raise PortfolioLedgerError("projection_path must not alias event path")

    def _mutate_idempotent(
        self,
        key: str,
        request_matches: Callable[[PortfolioEvent], bool],
        build: Callable[[tuple[PortfolioEvent, ...]], PortfolioEvent],
    ) -> PortfolioEvent:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=False):
            events = self._load_events_unlocked()
            matches = tuple(event for event in events if event.idempotency_key == key)
            if len(matches) > 1:
                raise PortfolioLedgerError("event log has duplicate idempotency keys")
            if matches:
                if not request_matches(matches[0]):
                    raise PortfolioLedgerError(
                        "idempotency_key was reused for a different request",
                    )
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
        active_trades = [
            self._trade_from_event(event)
            for event in events[1:]
            if (
                event.event_type is not PortfolioEventType.TRADE_REVERSED
                and event.event_id not in reversed_ids
            )
        ]
        active_trades.sort(key=lambda trade: trade.executed_at)
        for trade in active_trades:
            execution_date = trade.executed_at.date()
            if execution_date > trading_date:
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
                raise PortfolioLedgerError("event log sell exceeds sellable shares")
            sellable = _sellable_shares(
                position, execution_date, metadata, self.closed_dates,
            )
            if trade.shares > sellable:
                raise PortfolioLedgerError("event log sell exceeds sellable shares")
            average_cost = position.cost_basis / position.shares
            proceeds = _decimal(trade.price) * trade.shares - _decimal(trade.fee)
            if cash + proceeds < 0:
                raise PortfolioLedgerError("event log sell would leave negative cash")
            realized += proceeds - average_cost * trade.shares
            cash += proceeds
            position.cost_basis -= average_cost * trade.shares
            position.shares -= trade.shares
            consumed_risk = _consume_sellable_lots(
                position, trade.shares, execution_date, metadata,
                self.closed_dates,
            )
            position.planned_risk -= consumed_risk
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
                position, trading_date, self.metadata[symbol], self.closed_dates,
            )
            today_bought = sum(
                lot.shares for lot in position.lots if lot.bought_on == trading_date
            )
            output_positions[symbol] = PortfolioPosition(
                symbol=symbol,
                shares=position.shares,
                sellable_shares=sellable,
                today_bought_shares=today_bought,
                average_cost=_public_float(average_cost, "average_cost"),
                market_price=_public_float(mark, "market_price"),
                market_value=_public_float(market_value, "market_value"),
                planned_risk=_public_float(position.planned_risk, "planned_risk"),
            )
            total_market_value += market_value
            total_risk += position.planned_risk
        equity = cash + total_market_value
        single_trade_risk_exceeded = equity > 0 and any(
            lot.planned_risk_per_share * lot.shares
            > equity * default_risk_rate
            for position in positions.values()
            for lot in position.lots
        )
        portfolio_risk_exceeded = (
            equity > 0
            and total_risk > equity * self.max_portfolio_risk_rate
        )
        if single_trade_risk_exceeded or portfolio_risk_exceeded:
            warnings.append("RISK_LIMIT_EXCEEDED")
        return PortfolioProjection(
            schema_version=_SCHEMA_VERSION,
            name=name,
            default_risk_per_trade=_public_float(
                default_risk_rate, "default_risk_per_trade",
            ),
            as_of_trading_date=trading_date,
            cash=_public_float(cash, "cash"),
            positions=MappingProxyType(output_positions),
            realized_pnl=_public_float(realized, "realized_pnl"),
            etf_market_value=_public_float(
                total_market_value, "etf_market_value",
            ),
            equity=_public_float(equity, "equity"),
            planned_risk=_public_float(total_risk, "planned_risk"),
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
        if not _is_trading_date(executed_at.date(), self.closed_dates):
            raise PortfolioLedgerError("trade executed_at must be a trading day")
        return TradeInput(
            symbol,
            trade.side,
            trade.shares,
            _public_float(price, "trade price"),
            _public_float(fee, "trade fee"),
            executed_at,
            _public_float(risk, "planned_risk_per_share"),
        )

    @staticmethod
    def _trade_request_payload(trade: TradeInput) -> dict[str, object]:
        return {
            "symbol": trade.symbol,
            "side": trade.side,
            "shares": trade.shares,
            "price": trade.price,
            "fee": trade.fee,
            "executed_at": trade.executed_at.isoformat(),
            "planned_risk_per_share": trade.planned_risk_per_share,
        }

    def _trade_request_payload_from_event(
        self, event: PortfolioEvent,
    ) -> dict[str, object]:
        trade = self._trade_from_event(event)
        payload = self._trade_request_payload(trade)
        payload["planned_risk_per_share"] = event.payload[
            "planned_risk_per_share"
        ]
        return payload

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
                position.shares,
                _public_float(average_cost, f"initial position {symbol} average_cost"),
                _public_float(
                    planned_risk,
                    f"initial position {symbol} planned_risk_per_share",
                ),
            )
        return MappingProxyType(result)

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
        legacy = {
            "symbol", "side", "shares", "price", "fee", "executed_at",
            "planned_risk_per_share",
        }
        current = legacy | {"effective_planned_risk_per_share"}
        if set(event.payload) not in (legacy, current):
            raise PortfolioLedgerError("trade event payload is invalid")
        requested_risk = _finite_decimal(
            event.payload["planned_risk_per_share"],
            "planned_risk_per_share",
            positive=False,
        )
        effective_risk = _finite_decimal(
            event.payload.get(
                "effective_planned_risk_per_share",
                event.payload["planned_risk_per_share"],
            ),
            "effective_planned_risk_per_share",
            positive=False,
        )
        if requested_risk > 0 and effective_risk != requested_risk:
            raise PortfolioLedgerError(
                "trade event requested and effective risk fields are inconsistent",
            )
        trade = TradeInput(
            symbol=event.payload["symbol"],
            side=event.payload["side"],
            shares=event.payload["shares"],
            price=event.payload["price"],
            fee=event.payload["fee"],
            executed_at=_parse_datetime(event.payload["executed_at"], "executed_at"),
            planned_risk_per_share=_public_float(
                effective_risk, "effective_planned_risk_per_share",
            ),
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


def _freeze_json(value: object, depth: int = 0) -> object:
    if depth > 64:
        raise PortfolioLedgerError("event payload nesting is too deep")
    if isinstance(value, Mapping):
        try:
            items = tuple(value.items())
        except Exception as error:
            raise PortfolioLedgerError("event payload mapping could not be read") from error
        frozen: dict[str, object] = {}
        for key, item in items:
            if type(key) is not str:
                raise PortfolioLedgerError("event payload keys must be strings")
            frozen[key] = _freeze_json(item, depth + 1)
        return MappingProxyType(frozen)
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item, depth + 1) for item in value)
    if type(value) in (str, int, bool, type(None)):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise PortfolioLedgerError("event payload must contain finite JSON values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


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


def _snapshot_closed_dates(value: Iterable[date]) -> frozenset[date]:
    try:
        dates = tuple(value)
    except Exception as error:
        raise PortfolioLedgerError("closed_dates could not be read") from error
    if any(type(item) is not date for item in dates):
        raise PortfolioLedgerError("closed_dates must contain dates")
    return frozenset(dates)


def _is_trading_date(value: date, closed_dates: frozenset[date]) -> bool:
    return value.weekday() < 5 and value not in closed_dates


def _trading_days_between(
    start: date,
    end: date,
    closed_dates: frozenset[date],
) -> int:
    """Count authoritative trading days in the interval ``(start, end]``."""
    span = (end - start).days
    if span <= 0:
        return 0
    full_weeks, remainder = divmod(span, 7)
    count = full_weeks * 5
    remainder_start = start + timedelta(days=full_weeks * 7)
    for offset in range(1, remainder + 1):
        if (remainder_start + timedelta(days=offset)).weekday() < 5:
            count += 1
    count -= sum(
        1
        for closed in closed_dates
        if start < closed <= end and closed.weekday() < 5
    )
    return max(0, count)


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


def _public_float(value: Decimal, field: str) -> float:
    try:
        number = float(value)
    except (OverflowError, ValueError) as error:
        raise PortfolioLedgerError(
            f"{field} must be representable as a finite float",
        ) from error
    if not math.isfinite(number):
        raise PortfolioLedgerError(
            f"{field} must be representable as a finite float",
        )
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


def _is_sellable(
    lot: _Lot,
    trading_date: date,
    metadata: EtfMetadata,
    closed_dates: frozenset[date],
) -> bool:
    if lot.bought_on == date.min:
        return True
    delay = metadata.trading.sellable_delay_days
    if metadata.trading.intraday_turnaround or delay == 0:
        return lot.bought_on <= trading_date
    return _trading_days_between(
        lot.bought_on, trading_date, closed_dates,
    ) >= delay


def _sellable_shares(
    position: _MutablePosition,
    trading_date: date,
    metadata: EtfMetadata,
    closed_dates: frozenset[date],
) -> int:
    return sum(
        lot.shares for lot in position.lots
        if _is_sellable(lot, trading_date, metadata, closed_dates)
    )


def _consume_sellable_lots(
    position: _MutablePosition,
    shares: int,
    trading_date: date,
    metadata: EtfMetadata,
    closed_dates: frozenset[date],
) -> Decimal:
    remaining = shares
    consumed_risk = Decimal("0")
    retained: list[_Lot] = []
    for lot in position.lots:
        if remaining and _is_sellable(
            lot, trading_date, metadata, closed_dates,
        ):
            consumed = min(remaining, lot.shares)
            remaining -= consumed
            lot.shares -= consumed
            consumed_risk += lot.planned_risk_per_share * consumed
        if lot.shares:
            retained.append(lot)
    if remaining:
        raise PortfolioLedgerError("sell exceeds sellable shares")
    position.lots = retained
    return consumed_risk


def _atomic_replace_json(path: Path, payload: Mapping[str, object]) -> None:
    try:
        content = json.dumps(
            payload, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, OverflowError) as error:
        raise PortfolioLedgerError("projection serialization failed") from error
    _atomic_replace_bytes(path, content)


def _resolve_path(value: Path, field: str) -> Path:
    try:
        return Path(value).resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as error:
        raise PortfolioLedgerError(f"{field} could not be resolved") from error


def _existing_projection_is_newer(
    path: Path,
    projected: PortfolioProjection,
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, ValueError, RecursionError):
        return False
    try:
        existing = _parse_projection(payload)
    except PortfolioLedgerError:
        return False
    return (
        existing.last_event_id == projected.last_event_id
        and existing.as_of_trading_date > projected.as_of_trading_date
    )


def _parse_projection(value: object) -> PortfolioProjection:
    if not isinstance(value, Mapping):
        raise PortfolioLedgerError("projection must be an object")
    expected = {
        "schema_version", "name", "default_risk_per_trade",
        "as_of_trading_date", "cash", "positions", "realized_pnl",
        "etf_market_value", "equity", "planned_risk", "warnings",
        "last_event_id",
    }
    if set(value) != expected:
        raise PortfolioLedgerError("projection fields are invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PortfolioLedgerError("projection schema_version must be integer 1")
    name = _nonblank_text(value["name"], "projection name")
    default_risk = _projection_number(
        value["default_risk_per_trade"],
        "projection default_risk_per_trade",
        positive=True,
    )
    if default_risk > Decimal("1"):
        raise PortfolioLedgerError(
            "projection default_risk_per_trade must not exceed 1",
        )
    raw_date = value["as_of_trading_date"]
    if type(raw_date) is not str:
        raise PortfolioLedgerError(
            "projection as_of_trading_date must be an ISO date string",
        )
    try:
        as_of = date.fromisoformat(raw_date)
    except ValueError as error:
        raise PortfolioLedgerError(
            "projection as_of_trading_date must be an ISO date string",
        ) from error
    if as_of.isoformat() != raw_date:
        raise PortfolioLedgerError(
            "projection as_of_trading_date must be canonical",
        )
    cash = _projection_number(value["cash"], "projection cash")
    realized = _projection_number(
        value["realized_pnl"], "projection realized_pnl", signed=True,
    )
    market_value = _projection_number(
        value["etf_market_value"], "projection etf_market_value",
    )
    equity = _projection_number(value["equity"], "projection equity")
    planned_risk = _projection_number(
        value["planned_risk"], "projection planned_risk",
    )
    positions = _parse_projection_positions(value["positions"])
    position_market_value = sum(
        (_decimal(position.market_value) for position in positions.values()),
        Decimal("0"),
    )
    position_planned_risk = sum(
        (_decimal(position.planned_risk) for position in positions.values()),
        Decimal("0"),
    )
    if not _projection_numbers_equal(market_value, position_market_value):
        raise PortfolioLedgerError("projection market value is inconsistent")
    if not _projection_numbers_equal(equity, cash + market_value):
        raise PortfolioLedgerError("projection equity is inconsistent")
    if not _projection_numbers_equal(planned_risk, position_planned_risk):
        raise PortfolioLedgerError("projection planned risk is inconsistent")
    raw_warnings = value["warnings"]
    if type(raw_warnings) is not list or any(
        type(warning) is not str or not warning
        for warning in raw_warnings
    ):
        raise PortfolioLedgerError("projection warnings must be a string list")
    last_event_id = value["last_event_id"]
    _require_uuid4(last_event_id, "projection last_event_id")
    return PortfolioProjection(
        schema_version=1,
        name=name,
        default_risk_per_trade=_public_float(
            default_risk, "projection default_risk_per_trade",
        ),
        as_of_trading_date=as_of,
        cash=_public_float(cash, "projection cash"),
        positions=MappingProxyType(positions),
        realized_pnl=_public_float(realized, "projection realized_pnl"),
        etf_market_value=_public_float(
            market_value, "projection etf_market_value",
        ),
        equity=_public_float(equity, "projection equity"),
        planned_risk=_public_float(planned_risk, "projection planned_risk"),
        warnings=tuple(raw_warnings),
        last_event_id=last_event_id,
    )


def _parse_projection_positions(value: object) -> dict[str, PortfolioPosition]:
    if type(value) is not dict:
        raise PortfolioLedgerError("projection positions must be an object")
    expected = {
        "symbol", "shares", "sellable_shares", "today_bought_shares",
        "average_cost", "market_price", "market_value", "planned_risk",
    }
    positions: dict[str, PortfolioPosition] = {}
    for symbol, raw_position in value.items():
        if (
            type(symbol) is not str or len(symbol) != 6
            or not symbol.isascii() or not symbol.isdigit()
        ):
            raise PortfolioLedgerError("projection position symbol is invalid")
        if not isinstance(raw_position, Mapping) or set(raw_position) != expected:
            raise PortfolioLedgerError("projection position fields are invalid")
        if raw_position["symbol"] != symbol:
            raise PortfolioLedgerError("projection position symbol is inconsistent")
        shares = _projection_integer(raw_position["shares"], "position shares")
        if shares <= 0:
            raise PortfolioLedgerError("projection position shares must be positive")
        sellable = _projection_integer(
            raw_position["sellable_shares"], "position sellable_shares",
        )
        today_bought = _projection_integer(
            raw_position["today_bought_shares"], "position today_bought_shares",
        )
        if sellable + today_bought > shares:
            raise PortfolioLedgerError("projection position inventory is inconsistent")
        average_cost = _projection_number(
            raw_position["average_cost"], "position average_cost", positive=True,
        )
        market_price = _projection_number(
            raw_position["market_price"], "position market_price", positive=True,
        )
        position_market_value = _projection_number(
            raw_position["market_value"], "position market_value", positive=True,
        )
        position_risk = _projection_number(
            raw_position["planned_risk"], "position planned_risk",
        )
        if not _projection_numbers_equal(
            position_market_value, market_price * shares,
        ):
            raise PortfolioLedgerError("projection position value is inconsistent")
        positions[symbol] = PortfolioPosition(
            symbol=symbol,
            shares=shares,
            sellable_shares=sellable,
            today_bought_shares=today_bought,
            average_cost=_public_float(average_cost, "position average_cost"),
            market_price=_public_float(market_price, "position market_price"),
            market_value=_public_float(
                position_market_value, "position market_value",
            ),
            planned_risk=_public_float(position_risk, "position planned_risk"),
        )
    return positions


def _projection_integer(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise PortfolioLedgerError(f"{field} must be a nonnegative integer")
    return value


def _projection_number(
    value: object,
    field: str,
    *,
    positive: bool = False,
    signed: bool = False,
) -> Decimal:
    if type(value) not in (int, float):
        raise PortfolioLedgerError(f"{field} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise PortfolioLedgerError(f"{field} must be a finite number") from error
    if not number.is_finite():
        raise PortfolioLedgerError(f"{field} must be a finite number")
    _public_float(number, field)
    if positive and number <= 0:
        raise PortfolioLedgerError(f"{field} must be positive")
    if not positive and not signed and number < 0:
        raise PortfolioLedgerError(f"{field} must be nonnegative")
    return number


def _projection_numbers_equal(left: Decimal, right: Decimal) -> bool:
    difference = abs(left - right)
    scale = max(abs(left), abs(right), Decimal("1"))
    return difference <= scale * Decimal("1e-12")


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
