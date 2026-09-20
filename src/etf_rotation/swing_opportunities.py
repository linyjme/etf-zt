"""Versioned, immutable opportunity events for the swing shadow layer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping


class OpportunityStatus(StrEnum):
    PULLBACK_WATCH = "PULLBACK_WATCH"
    RECOVERY_WATCH = "RECOVERY_WATCH"
    TECHNICAL_CANDIDATE = "TECHNICAL_CANDIDATE"
    PUBLISHED = "PUBLISHED"
    WINDOW_OPEN = "WINDOW_OPEN"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


_TERMINAL_STATUSES = frozenset({
    OpportunityStatus.FILLED,
    OpportunityStatus.PARTIAL,
    OpportunityStatus.SKIPPED,
    OpportunityStatus.EXPIRED,
    OpportunityStatus.CANCELLED,
})


@dataclass(frozen=True)
class OpportunityObservation:
    """One completed-day observation used to advance an opportunity."""

    symbol: str
    trading_date: date
    pullback: bool
    recovery: bool
    conditions: Mapping[str, bool | float | int | str | None]
    data_version: str
    price_basis: str = "adjusted_ohlc"
    atr_algorithm: str = "ATR14_SMA"
    strategy_version: str = "SWING_V2_SHADOW"
    indicator_version: str = "INDICATORS_V1"
    is_final: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.symbol) is not str
            or len(self.symbol) != 6
            or not self.symbol.isascii()
            or not self.symbol.isdigit()
        ):
            raise ValueError("opportunity symbol must be six ASCII digits")
        if type(self.trading_date) is not date:
            raise ValueError("opportunity trading_date must be a date")
        if type(self.pullback) is not bool or type(self.recovery) is not bool:
            raise ValueError("opportunity conditions must be boolean")
        if type(self.is_final) is not bool:
            raise ValueError("opportunity observations must declare is_final")
        if type(self.data_version) is not str or not self.data_version:
            raise ValueError("opportunity data_version is required")
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))


@dataclass(frozen=True)
class OpportunityEvent:
    opportunity_id: str
    symbol: str
    strategy_version: str
    pullback_start_date: date
    recovery_date: date | None
    expiry_date: date
    status: OpportunityStatus
    conditions: Mapping[str, bool | float | int | str | None]
    cancel_reason: str | None
    data_version: str
    indicator_version: str = "INDICATORS_V1"

    def __post_init__(self) -> None:
        if not isinstance(self.status, OpportunityStatus):
            object.__setattr__(self, "status", OpportunityStatus(self.status))
        object.__setattr__(self, "conditions", MappingProxyType(dict(self.conditions)))

    def to_dict(self) -> dict[str, object]:
        return {
            "opportunity_id": self.opportunity_id,
            "symbol": self.symbol,
            "strategy_version": self.strategy_version,
            "indicator_version": self.indicator_version,
            "pullback_start_date": self.pullback_start_date.isoformat(),
            "recovery_date": (
                self.recovery_date.isoformat() if self.recovery_date else None
            ),
            "expiry_date": self.expiry_date.isoformat(),
            "status": self.status.value,
            "conditions": dict(self.conditions),
            "cancel_reason": self.cancel_reason,
            "data_version": self.data_version,
        }


def _expiry_date(start: date, sessions: int) -> date:
    if type(sessions) is not int or sessions <= 0:
        raise ValueError("recovery_window_sessions must be positive")
    current = start
    completed = 0
    while completed < sessions:
        current += timedelta(days=1)
        if current.weekday() < 5:
            completed += 1
    return current


def _opportunity_id(observation: OpportunityObservation) -> str:
    identity = "|".join((
        observation.symbol,
        observation.trading_date.isoformat(),
        observation.price_basis,
        observation.atr_algorithm,
        observation.strategy_version,
        observation.data_version,
    ))
    return sha256(identity.encode("utf-8")).hexdigest()[:24]


def _validate_observation(observation: OpportunityObservation) -> None:
    if type(observation) is not OpportunityObservation:
        raise ValueError("observation must be OpportunityObservation")
    if observation.is_final is not True:
        raise ValueError("opportunity observation must be completed")


def update_opportunity(
    *,
    previous: OpportunityEvent | None,
    observation: OpportunityObservation,
    recovery_window_sessions: int = 5,
) -> OpportunityEvent | None:
    """Advance an event using one completed observation, without look-ahead."""
    _validate_observation(observation)
    if type(previous) not in (OpportunityEvent, type(None)):
        raise ValueError("previous must be OpportunityEvent or None")

    if previous is None:
        if not observation.pullback:
            return None
        return OpportunityEvent(
            opportunity_id=_opportunity_id(observation),
            symbol=observation.symbol,
            strategy_version=observation.strategy_version,
            indicator_version=observation.indicator_version,
            pullback_start_date=observation.trading_date,
            recovery_date=None,
            expiry_date=_expiry_date(
                observation.trading_date, recovery_window_sessions,
            ),
            status=OpportunityStatus.PULLBACK_WATCH,
            conditions=observation.conditions,
            cancel_reason=None,
            data_version=observation.data_version,
        )

    if previous.symbol != observation.symbol:
        raise ValueError("opportunity symbol cannot change")
    if observation.trading_date < previous.pullback_start_date:
        raise ValueError("observation cannot precede pullback start")
    if previous.status in _TERMINAL_STATUSES:
        return previous
    if observation.trading_date <= previous.pullback_start_date:
        return previous
    if observation.trading_date > previous.expiry_date:
        return replace(
            previous,
            status=OpportunityStatus.EXPIRED,
            cancel_reason="RECOVERY_WINDOW_EXPIRED",
        )
    if previous.status is OpportunityStatus.PULLBACK_WATCH and observation.recovery:
        return replace(
            previous,
            status=OpportunityStatus.TECHNICAL_CANDIDATE,
            recovery_date=observation.trading_date,
            conditions=observation.conditions,
        )
    return previous


__all__ = [
    "OpportunityEvent",
    "OpportunityObservation",
    "OpportunityStatus",
    "update_opportunity",
]

