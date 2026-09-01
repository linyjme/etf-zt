"""Durable alert lifecycle for the swing monitor.

The canonical JSONL event stream is the sole source of truth.  Each mutation
holds the same sibling-file lock used by the market-data stores while it reads,
validates, and atomically rewrites the stream.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time as _time
from types import MappingProxyType
from typing import Callable
import uuid

from .swing_data import _SiblingFileLock


SHANGHAI = timezone(timedelta(hours=8))
_SCHEMA_VERSION = 1
_ALERT_ID = re.compile(r"[0-9a-f]{24}\Z")
_SYMBOL = re.compile(r"[0-9]{6}\Z", re.ASCII)
_ASCII_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9_.:-]*\Z", re.ASCII)
_LEVELS = frozenset({"BLUE", "YELLOW", "GREEN", "RED", "GRAY"})
_MAX_TEXT = 4096
_MAX_KEY = 256
_MAX_JSON_DEPTH = 32
_WINDOWS_REPLACE_RETRY_SECONDS = 0.005
_WINDOWS_REPLACE_MAX_ATTEMPTS = 20
_WINDOWS_REPLACE_TRANSIENT_ERRORS = frozenset((5, 32, 33))


class AlertStoreError(ValueError):
    """Raised for invalid alert input or an invalid authoritative event log."""


class AlertEventType(StrEnum):
    FORMAL_PUBLISHED = "FORMAL_PUBLISHED"
    OVERLAY_PUBLISHED = "OVERLAY_PUBLISHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IGNORED = "IGNORED"
    RETRACTED = "RETRACTED"


@dataclass(frozen=True)
class AlertInput:
    trading_date: date
    symbol: str
    state: str
    strategy_version: str
    level: str
    label: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "trading_date", _strict_date(self.trading_date, "trading_date"),
        )
        object.__setattr__(self, "symbol", _symbol(self.symbol))
        object.__setattr__(self, "state", _ascii_token(self.state, "state"))
        object.__setattr__(
            self,
            "strategy_version",
            _ascii_token(self.strategy_version, "strategy_version"),
        )
        if type(self.level) is not str or self.level not in _LEVELS:
            raise AlertStoreError("level is invalid")
        object.__setattr__(self, "label", _nonblank(self.label, "label"))
        frozen = _freeze_json(self.evidence, "evidence")
        if not isinstance(frozen, Mapping):
            raise AlertStoreError("evidence must be an object")
        object.__setattr__(self, "evidence", frozen)

    def to_dict(self) -> dict[str, object]:
        return {
            "trading_date": self.trading_date.isoformat(),
            "symbol": self.symbol,
            "state": self.state,
            "strategy_version": self.strategy_version,
            "level": self.level,
            "label": self.label,
            "evidence": _thaw_json(self.evidence),
        }

    @classmethod
    def from_mapping(cls, value: object) -> AlertInput:
        if not isinstance(value, Mapping):
            raise AlertStoreError("alert must be an object")
        expected = {
            "trading_date", "symbol", "state", "strategy_version",
            "level", "label", "evidence",
        }
        if set(value) != expected:
            raise AlertStoreError("alert fields are invalid")
        raw_date = value["trading_date"]
        if type(raw_date) is not str:
            raise AlertStoreError("trading_date must be an ISO date string")
        try:
            trading_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise AlertStoreError("trading_date is invalid") from error
        if trading_date.isoformat() != raw_date:
            raise AlertStoreError("trading_date must be canonical")
        return cls(
            trading_date=trading_date,
            symbol=value["symbol"],
            state=value["state"],
            strategy_version=value["strategy_version"],
            level=value["level"],
            label=value["label"],
            evidence=value["evidence"],
        )


@dataclass(frozen=True)
class AlertEvent:
    schema_version: int
    event_id: str
    event_type: AlertEventType
    idempotency_key: str | None
    recorded_at: datetime
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise AlertStoreError("event schema_version must be integer 1")
        object.__setattr__(self, "event_id", _uuid4(self.event_id, "event_id"))
        if type(self.event_type) is not AlertEventType:
            raise AlertStoreError("event_type is invalid")
        if self.idempotency_key is not None:
            object.__setattr__(
                self,
                "idempotency_key",
                _idempotency_key(self.idempotency_key),
            )
        object.__setattr__(
            self, "recorded_at", _aware_datetime(self.recorded_at, "recorded_at"),
        )
        frozen = _freeze_json(self.payload, "payload")
        if not isinstance(frozen, Mapping):
            raise AlertStoreError("event payload must be an object")
        object.__setattr__(self, "payload", frozen)

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
    def from_mapping(cls, value: object) -> AlertEvent:
        if not isinstance(value, Mapping):
            raise AlertStoreError("event log record must be an object")
        expected = {
            "schema_version", "event_id", "event_type", "idempotency_key",
            "recorded_at", "payload",
        }
        if set(value) != expected:
            raise AlertStoreError("event log record fields are invalid")
        try:
            event_type = AlertEventType(value["event_type"])
        except (TypeError, ValueError) as error:
            raise AlertStoreError("event_type is invalid") from error
        return cls(
            schema_version=value["schema_version"],
            event_id=value["event_id"],
            event_type=event_type,
            idempotency_key=value["idempotency_key"],
            recorded_at=_parse_datetime(value["recorded_at"], "recorded_at"),
            payload=value["payload"],
        )


@dataclass(frozen=True)
class AlertProjection:
    alert_id: str
    scope: str
    generation: int
    trading_date: date
    symbol: str
    state: str
    strategy_version: str
    level: str
    label: str
    evidence: Mapping[str, object]
    published_at: datetime
    acknowledged: bool = False
    ignored: bool = False
    retracted: bool = False
    retraction_reason: str | None = None

    def __post_init__(self) -> None:
        _alert_id(self.alert_id)
        if self.scope not in {"FORMAL", "INTRADAY"}:
            raise AlertStoreError("projection scope is invalid")
        generation = _generation(self.generation)
        if self.scope == "FORMAL" and generation != 0:
            raise AlertStoreError("formal projection generation must be zero")
        _strict_date(self.trading_date, "projection trading_date")
        _symbol(self.symbol)
        _ascii_token(self.state, "projection state")
        _ascii_token(self.strategy_version, "projection strategy_version")
        if type(self.level) is not str or self.level not in _LEVELS:
            raise AlertStoreError("projection level is invalid")
        _nonblank(self.label, "projection label")
        frozen = _freeze_json(self.evidence, "projection evidence")
        if not isinstance(frozen, Mapping):
            raise AlertStoreError("projection evidence must be an object")
        object.__setattr__(self, "evidence", frozen)
        object.__setattr__(
            self, "published_at", _aware_datetime(self.published_at, "published_at"),
        )
        for field in ("acknowledged", "ignored", "retracted"):
            if type(getattr(self, field)) is not bool:
                raise AlertStoreError(f"projection {field} must be boolean")
        if self.retracted:
            _nonblank(self.retraction_reason, "retraction_reason")
        elif self.retraction_reason is not None:
            raise AlertStoreError("active alert cannot have a retraction_reason")
        identity = AlertInput(
            trading_date=self.trading_date,
            symbol=self.symbol,
            state=self.state,
            strategy_version=self.strategy_version,
            level=self.level,
            label=self.label,
            evidence=self.evidence,
        )
        expected_id = (
            formal_alert_id(
                identity.trading_date,
                identity.symbol,
                identity.state,
                identity.strategy_version,
            )
            if self.scope == "FORMAL"
            else overlay_alert_id(identity, generation)
        )
        if self.alert_id != expected_id:
            raise AlertStoreError("projection alert_id is inconsistent")

    @property
    def active_notification(self) -> bool:
        return not (self.acknowledged or self.ignored or self.retracted)

    def to_dict(self) -> dict[str, object]:
        return {
            "alert_id": self.alert_id,
            "scope": self.scope,
            "generation": self.generation,
            "trading_date": self.trading_date.isoformat(),
            "symbol": self.symbol,
            "state": self.state,
            "strategy_version": self.strategy_version,
            "level": self.level,
            "label": self.label,
            "evidence": _thaw_json(self.evidence),
            "published_at": self.published_at.isoformat(),
            "acknowledged": self.acknowledged,
            "ignored": self.ignored,
            "retracted": self.retracted,
            "retraction_reason": self.retraction_reason,
            "active_notification": self.active_notification,
        }


def formal_alert_id(
    trading_date: date,
    symbol: str,
    state: str,
    strategy_version: str,
) -> str:
    """Return the exact stable formal identity required by the design."""
    day = _strict_date(trading_date, "trading_date")
    normalized_symbol = _symbol(symbol)
    normalized_state = _ascii_token(state, "state")
    normalized_version = _ascii_token(strategy_version, "strategy_version")
    identity = (
        f"{day.isoformat()}|{normalized_symbol}|{normalized_state}|"
        f"{normalized_version}"
    )
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


def overlay_alert_id(alert: AlertInput, generation: int = 0) -> str:
    """Return a stable intraday identity in a namespace separate from formal IDs."""
    normalized_alert = _alert_input(alert)
    normalized_generation = _generation(generation)
    identity = (
        f"INTRADAY|{normalized_alert.trading_date.isoformat()}|"
        f"{normalized_alert.symbol}|{normalized_alert.state}|"
        f"{normalized_alert.strategy_version}|{normalized_generation}"
    )
    return hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]


class SwingAlertStore:
    """Persist and project formal and intraday swing alerts."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        try:
            self.path = Path(path).resolve(strict=False)
        except (OSError, RuntimeError, TypeError) as error:
            raise AlertStoreError("alert path could not be resolved") from error
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def publish_formal(self, alert: AlertInput) -> AlertProjection:
        normalized = _alert_input(alert)
        return self._publish(normalized, "FORMAL")

    def publish_overlay(self, alert: AlertInput) -> AlertProjection:
        normalized = _alert_input(alert)
        return self._publish(normalized, "INTRADAY")

    def acknowledge(self, alert_id: str, idempotency_key: str) -> AlertProjection:
        return self._transition(
            alert_id, AlertEventType.ACKNOWLEDGED, idempotency_key,
        )

    def ignore(self, alert_id: str, idempotency_key: str) -> AlertProjection:
        return self._transition(alert_id, AlertEventType.IGNORED, idempotency_key)

    def retract_overlays(self, reason: str) -> tuple[AlertProjection, ...]:
        normalized_reason = _nonblank(reason, "retraction reason")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=False):
            events = self._load_events_unlocked()
            projections = self._project(events)
            targets = tuple(
                projection
                for projection in projections.values()
                if projection.scope == "INTRADAY" and not projection.retracted
            )
            if not targets:
                return ()
            new_events = tuple(
                self._event(
                    AlertEventType.RETRACTED,
                    None,
                    {"alert_id": item.alert_id, "reason": normalized_reason},
                )
                for item in targets
            )
            updated = events + new_events
            projected = self._validate_and_project(updated)
            self._atomic_replace_events(updated)
            return tuple(projected[item.alert_id] for item in targets)

    def load_events(self) -> tuple[AlertEvent, ...]:
        if not self.path.parent.exists():
            return ()
        with _SiblingFileLock(self.path, shared=True):
            return self._load_events_unlocked()

    def current(self, include_retracted: bool = False) -> tuple[AlertProjection, ...]:
        """Return alerts in publication order, omitting retracted alerts by default.

        Acknowledged and ignored alerts remain here as durable history.  Use
        :meth:`active_notifications` for the subset that may notify the user.
        """
        if type(include_retracted) is not bool:
            raise AlertStoreError("include_retracted must be boolean")
        projections = self._project(self.load_events())
        return tuple(
            item for item in projections.values()
            if include_retracted or not item.retracted
        )

    def active_notifications(self) -> tuple[AlertProjection, ...]:
        return tuple(item for item in self.current() if item.active_notification)

    def _publish(
        self,
        alert: AlertInput,
        scope: str,
    ) -> AlertProjection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=False):
            events = self._load_events_unlocked()
            projected = self._project(events)
            lifecycle = tuple(
                item for item in projected.values()
                if _same_lifecycle(item, alert, scope)
            )
            if scope == "FORMAL":
                generation = 0
                alert_id = formal_alert_id(
                    alert.trading_date,
                    alert.symbol,
                    alert.state,
                    alert.strategy_version,
                )
                existing = projected.get(alert_id)
                if existing is not None:
                    return existing
            else:
                active = tuple(item for item in lifecycle if not item.retracted)
                if active:
                    return active[0]
                generation = (
                    max(item.generation for item in lifecycle) + 1
                    if lifecycle
                    else 0
                )
                alert_id = overlay_alert_id(alert, generation)
                if alert_id in projected:
                    raise AlertStoreError("generated duplicate overlay alert_id")
            payload = {
                "alert_id": alert_id,
                "scope": scope,
                "generation": generation,
                "alert": alert.to_dict(),
            }
            event_type = (
                AlertEventType.FORMAL_PUBLISHED
                if scope == "FORMAL"
                else AlertEventType.OVERLAY_PUBLISHED
            )
            event = self._event(event_type, None, payload)
            updated = events + (event,)
            projected = self._validate_and_project(updated)
            self._atomic_replace_events(updated)
            return projected[alert_id]

    def _transition(
        self,
        alert_id: str,
        event_type: AlertEventType,
        idempotency_key: str,
    ) -> AlertProjection:
        normalized_id = _alert_id(alert_id)
        key = _idempotency_key(idempotency_key)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(self.path, shared=False):
            events = self._load_events_unlocked()
            matches = tuple(
                event for event in events if event.idempotency_key == key
            )
            if len(matches) > 1:
                raise AlertStoreError("event log has duplicate idempotency keys")
            if matches:
                event = matches[0]
                if (
                    event.event_type is not event_type
                    or event.payload.get("alert_id") != normalized_id
                ):
                    raise AlertStoreError(
                        "idempotency_key was reused for a different request",
                    )
                event_index = events.index(event)
                return self._project(events[:event_index + 1])[normalized_id]
            projected = self._project(events)
            current = projected.get(normalized_id)
            if current is None:
                raise AlertStoreError("alert does not exist")
            if current.retracted:
                raise AlertStoreError("retracted alert is not current")
            event = self._event(
                event_type, key, {"alert_id": normalized_id},
            )
            updated = events + (event,)
            projected = self._validate_and_project(updated)
            self._atomic_replace_events(updated)
            return projected[normalized_id]

    def _event(
        self,
        event_type: AlertEventType,
        idempotency_key: str | None,
        payload: Mapping[str, object],
    ) -> AlertEvent:
        try:
            recorded_at = self.clock()
        except Exception as error:
            raise AlertStoreError("alert clock failed") from error
        return AlertEvent(
            schema_version=_SCHEMA_VERSION,
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            idempotency_key=idempotency_key,
            recorded_at=recorded_at,
            payload=payload,
        )

    def _load_events_unlocked(self) -> tuple[AlertEvent, ...]:
        try:
            content = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise AlertStoreError(f"alert event log read failed: {error}") from error
        if not content:
            return ()
        if not content.endswith(b"\n"):
            raise AlertStoreError("alert event log has an incomplete final line")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AlertStoreError("alert event log is not valid UTF-8") from error
        events: list[AlertEvent] = []
        for line_number, line in enumerate(text[:-1].split("\n"), start=1):
            if not line:
                raise AlertStoreError(
                    f"alert event log contains blank line {line_number}",
                )
            try:
                payload = json.loads(line)
            except (ValueError, OverflowError, RecursionError) as error:
                raise AlertStoreError(
                    f"alert event log JSON is invalid at line {line_number}",
                ) from error
            try:
                event = AlertEvent.from_mapping(payload)
            except AlertStoreError as error:
                raise AlertStoreError(
                    f"alert event log record is invalid at line {line_number}: {error}",
                ) from error
            if line != self._encode_event(event):
                raise AlertStoreError(
                    f"alert event log record is not canonical at line {line_number}",
                )
            events.append(event)
        result = tuple(events)
        self._validate_and_project(result)
        return result

    def _validate_and_project(
        self,
        events: tuple[AlertEvent, ...],
    ) -> dict[str, AlertProjection]:
        event_ids: set[str] = set()
        keys: set[str] = set()
        for event in events:
            if event.event_id in event_ids:
                raise AlertStoreError("alert event log has duplicate event IDs")
            event_ids.add(event.event_id)
            if event.idempotency_key is not None:
                if event.idempotency_key in keys:
                    raise AlertStoreError(
                        "alert event log has duplicate idempotency keys",
                    )
                keys.add(event.idempotency_key)
        return self._project(events)

    def _project(
        self,
        events: tuple[AlertEvent, ...],
    ) -> dict[str, AlertProjection]:
        projections: dict[str, AlertProjection] = {}
        for event in events:
            if event.event_type in {
                AlertEventType.FORMAL_PUBLISHED,
                AlertEventType.OVERLAY_PUBLISHED,
            }:
                if event.idempotency_key is not None:
                    raise AlertStoreError(
                        "publication event cannot have an idempotency_key",
                    )
                expected_scope = (
                    "FORMAL"
                    if event.event_type is AlertEventType.FORMAL_PUBLISHED
                    else "INTRADAY"
                )
                payload = _published_payload(event.payload, expected_scope)
                alert = AlertInput.from_mapping(payload["alert"])
                alert_id = payload["alert_id"]
                generation = payload["generation"]
                expected_id = (
                    formal_alert_id(
                        alert.trading_date,
                        alert.symbol,
                        alert.state,
                        alert.strategy_version,
                    )
                    if expected_scope == "FORMAL"
                    else overlay_alert_id(alert, generation)
                )
                if alert_id != expected_id:
                    raise AlertStoreError("published alert_id is inconsistent")
                if alert_id in projections:
                    raise AlertStoreError("alert was published more than once")
                lifecycle = tuple(
                    item for item in projections.values()
                    if _same_lifecycle(item, alert, expected_scope)
                )
                if expected_scope == "FORMAL":
                    if generation != 0:
                        raise AlertStoreError(
                            "formal alert generation must be zero",
                        )
                else:
                    if generation != len(lifecycle):
                        raise AlertStoreError(
                            "overlay generation sequence is invalid",
                        )
                    if any(not item.retracted for item in lifecycle):
                        raise AlertStoreError(
                            "previous overlay generation must be retracted",
                        )
                projections[alert_id] = AlertProjection(
                    alert_id=alert_id,
                    scope=expected_scope,
                    generation=generation,
                    trading_date=alert.trading_date,
                    symbol=alert.symbol,
                    state=alert.state,
                    strategy_version=alert.strategy_version,
                    level=alert.level,
                    label=alert.label,
                    evidence=alert.evidence,
                    published_at=event.recorded_at,
                )
                continue

            alert_id, reason = _transition_payload(event)
            current = projections.get(alert_id)
            if current is None:
                raise AlertStoreError(
                    "alert transition requires an earlier published alert",
                )
            if current.retracted:
                raise AlertStoreError("alert transition follows retraction")
            changes: dict[str, object] = {}
            if event.event_type is AlertEventType.ACKNOWLEDGED:
                changes["acknowledged"] = True
            elif event.event_type is AlertEventType.IGNORED:
                changes["ignored"] = True
            else:
                if current.scope != "INTRADAY":
                    raise AlertStoreError("formal alerts cannot be retracted")
                changes["retracted"] = True
                changes["retraction_reason"] = reason
            projections[alert_id] = _replace_projection(current, **changes)
        return projections

    @staticmethod
    def _encode_event(event: AlertEvent) -> str:
        try:
            return json.dumps(
                event.to_dict(),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except AlertStoreError:
            raise
        except Exception as error:
            raise AlertStoreError("alert event serialization failed") from error

    def _atomic_replace_events(self, events: tuple[AlertEvent, ...]) -> None:
        try:
            content = "".join(
                self._encode_event(event) + "\n" for event in events
            )
            encoded = content.encode("utf-8", errors="strict")
        except AlertStoreError:
            raise
        except Exception as error:
            raise AlertStoreError("alert event serialization failed") from error
        _atomic_replace_bytes(self.path, encoded)


def _alert_input(value: object) -> AlertInput:
    if type(value) is not AlertInput:
        raise AlertStoreError("alert must be an AlertInput")
    # Read only declared fields and rebuild.  Never dispatch through a possibly
    # shadowed ``to_dict`` method on a mutated frozen instance.
    try:
        fields = (
            value.trading_date,
            value.symbol,
            value.state,
            value.strategy_version,
            value.level,
            value.label,
            value.evidence,
        )
    except Exception as error:
        raise AlertStoreError("alert fields could not be read") from error
    try:
        return AlertInput(
            trading_date=fields[0],
            symbol=fields[1],
            state=fields[2],
            strategy_version=fields[3],
            level=fields[4],
            label=fields[5],
            evidence=fields[6],
        )
    except AlertStoreError:
        raise
    except Exception as error:
        raise AlertStoreError("alert fields could not be normalized") from error


def _published_payload(value: object, expected_scope: str) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"alert_id", "scope", "generation", "alert"}
    ):
        raise AlertStoreError("published event payload fields are invalid")
    alert_id = _alert_id(value["alert_id"])
    generation = _generation(value["generation"])
    if value["scope"] != expected_scope:
        raise AlertStoreError("published event scope is inconsistent")
    return MappingProxyType(
        {
            "alert_id": alert_id,
            "scope": expected_scope,
            "generation": generation,
            "alert": value["alert"],
        },
    )


def _transition_payload(event: AlertEvent) -> tuple[str, str | None]:
    if event.event_type in {AlertEventType.ACKNOWLEDGED, AlertEventType.IGNORED}:
        if event.idempotency_key is None:
            raise AlertStoreError("user transition requires an idempotency_key")
        if set(event.payload) != {"alert_id"}:
            raise AlertStoreError("transition event payload fields are invalid")
        return _alert_id(event.payload["alert_id"]), None
    if event.event_type is not AlertEventType.RETRACTED:
        raise AlertStoreError("unsupported alert event type")
    if event.idempotency_key is not None:
        raise AlertStoreError("system retraction cannot have an idempotency_key")
    if set(event.payload) != {"alert_id", "reason"}:
        raise AlertStoreError("retraction event payload fields are invalid")
    return (
        _alert_id(event.payload["alert_id"]),
        _nonblank(event.payload["reason"], "retraction reason"),
    )


def _replace_projection(
    item: AlertProjection,
    **changes: object,
) -> AlertProjection:
    values = {
        "alert_id": item.alert_id,
        "scope": item.scope,
        "generation": item.generation,
        "trading_date": item.trading_date,
        "symbol": item.symbol,
        "state": item.state,
        "strategy_version": item.strategy_version,
        "level": item.level,
        "label": item.label,
        "evidence": item.evidence,
        "published_at": item.published_at,
        "acknowledged": item.acknowledged,
        "ignored": item.ignored,
        "retracted": item.retracted,
        "retraction_reason": item.retraction_reason,
    }
    values.update(changes)
    return AlertProjection(**values)


def _alert_id(value: object) -> str:
    if type(value) is not str or _ALERT_ID.fullmatch(value) is None:
        raise AlertStoreError("alert_id must be canonical lowercase 24 hex")
    return value


def _generation(value: object) -> int:
    if type(value) is not int or value < 0:
        raise AlertStoreError("generation must be a nonnegative integer")
    return value


def _same_lifecycle(
    item: AlertProjection,
    alert: AlertInput,
    scope: str,
) -> bool:
    return (
        item.scope == scope
        and item.trading_date == alert.trading_date
        and item.symbol == alert.symbol
        and item.state == alert.state
        and item.strategy_version == alert.strategy_version
    )


def _symbol(value: object) -> str:
    if type(value) is not str or _SYMBOL.fullmatch(value) is None:
        raise AlertStoreError("symbol must be 6 ASCII digits")
    return value


def _ascii_token(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) > 128
        or _ASCII_TOKEN.fullmatch(value) is None
    ):
        raise AlertStoreError(f"{field} must be a canonical ASCII token")
    return value


def _nonblank(value: object, field: str) -> str:
    if type(value) is not str or not value.strip() or len(value) > _MAX_TEXT:
        raise AlertStoreError(f"{field} must be nonblank text")
    _validate_utf8(value, field)
    return value


def _idempotency_key(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_KEY
        or any(ord(character) < 32 for character in value)
    ):
        raise AlertStoreError("idempotency_key is invalid")
    _validate_utf8(value, "idempotency_key")
    return value


def _strict_date(value: object, field: str) -> date:
    if type(value) is not date:
        raise AlertStoreError(f"{field} must be a date")
    return value


def _aware_datetime(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise AlertStoreError(f"{field} must be a timezone-aware datetime")
    try:
        offset = value.utcoffset()
        if offset is None:
            raise AlertStoreError(
                f"{field} must be a timezone-aware datetime",
            )
        return value.astimezone(SHANGHAI)
    except AlertStoreError:
        raise
    except Exception as error:
        raise AlertStoreError(f"{field} timezone is invalid") from error


def _parse_datetime(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise AlertStoreError(f"{field} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, OverflowError) as error:
        raise AlertStoreError(f"{field} is invalid") from error
    normalized = _aware_datetime(parsed, field)
    if normalized.isoformat() != value:
        raise AlertStoreError(f"{field} must be canonical Shanghai time")
    return normalized


def _uuid4(value: object, field: str) -> str:
    if type(value) is not str:
        raise AlertStoreError(f"{field} must be a canonical lowercase UUID4")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise AlertStoreError(f"{field} must be a canonical lowercase UUID4") from error
    if parsed.version != 4 or str(parsed) != value:
        raise AlertStoreError(f"{field} must be a canonical lowercase UUID4")
    return value


def _freeze_json(value: object, field: str, depth: int = 0) -> object:
    if depth > _MAX_JSON_DEPTH:
        raise AlertStoreError(f"{field} exceeds maximum nesting depth")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        _validate_utf8(value, field)
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise AlertStoreError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        try:
            items = value.items()
            for key, item in items:
                if type(key) is not str:
                    raise AlertStoreError(f"{field} keys must be strings")
                _validate_utf8(key, f"{field} key")
                result[key] = _freeze_json(item, field, depth + 1)
        except AlertStoreError:
            raise
        except Exception as error:
            raise AlertStoreError(f"{field} must contain JSON values") from error
        return MappingProxyType(result)
    if type(value) in (list, tuple):
        return tuple(_freeze_json(item, field, depth + 1) for item in value)
    raise AlertStoreError(f"{field} must contain JSON values")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


def _validate_utf8(value: str, field: str) -> None:
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise AlertStoreError(f"{field} must be valid UTF-8 text") from error


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
        _replace_file(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise AlertStoreError(f"atomic alert write failed: {error}") from error
    finally:
        if temporary_path is not None:
            _safe_unlink(temporary_path)


def _replace_file(source: Path, destination: Path) -> None:
    for attempt in range(_WINDOWS_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            retryable = (
                os.name == "nt"
                and getattr(error, "winerror", None)
                in _WINDOWS_REPLACE_TRANSIENT_ERRORS
                and attempt + 1 < _WINDOWS_REPLACE_MAX_ATTEMPTS
            )
            if not retryable:
                raise
        _time.sleep(_WINDOWS_REPLACE_RETRY_SECONDS)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except BaseException:
        pass
