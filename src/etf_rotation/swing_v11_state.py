"""Persisted V11 shadow state: per-position tracking and market transitions.

The V11 position evaluator is a pure projection; the service records what it
needs to remember between snapshots here.  Prices are stored in raw (ledger)
units so the file stays consistent with the trade ledger; the service scales
them into adjusted units when it builds a ``V11Position``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import json
import math
import os
from pathlib import Path
import tempfile


V11_STATE_SCHEMA_VERSION = 1

_POSITION_NUMBER_KEYS = ("stop_price_raw", "tracking_price_raw", "peak_price_raw")
_POSITION_BOOL_KEYS = ("tracking_started", "topup_done")
_POSITION_TEXT_KEYS = (
    "entry_trading_date", "as_of_trading_date", "entry_environment", "setup",
    "last_action",
)
_ENVIRONMENT_TEXT_KEYS = (
    "state", "previous_state", "as_of_trading_date", "defense_recovery_started",
)


def _finite_positive(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _iso_date(value: object) -> str | None:
    if type(value) is not str:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def normalize_position_state(raw: object) -> dict[str, object] | None:
    """Return a validated position state or ``None`` when it is unusable."""
    if not isinstance(raw, Mapping):
        return None
    entry = _iso_date(raw.get("entry_trading_date"))
    if entry is None:
        return None
    result: dict[str, object] = {"entry_trading_date": entry}
    for key in _POSITION_TEXT_KEYS[1:]:
        value = raw.get(key)
        if key == "as_of_trading_date":
            value = _iso_date(value)
        result[key] = value if isinstance(value, str) and value else None
    for key in _POSITION_NUMBER_KEYS:
        result[key] = _finite_positive(raw.get(key))
    for key in _POSITION_BOOL_KEYS:
        result[key] = raw.get(key) is True
    return result


def normalize_environment_state(raw: object) -> dict[str, object]:
    result: dict[str, object] = {key: None for key in _ENVIRONMENT_TEXT_KEYS}
    if not isinstance(raw, Mapping):
        return result
    for key in _ENVIRONMENT_TEXT_KEYS:
        value = raw.get(key)
        if key.endswith(("_date", "_started")):
            value = _iso_date(value)
        result[key] = value if isinstance(value, str) and value else None
    return result


def advance_environment_state(
    previous: Mapping[str, object] | None,
    *,
    state: object,
    as_of_trading_date: str | None,
) -> dict[str, object]:
    """Record the market state transition observed on ``as_of_trading_date``.

    Handbook section three only cares about two transitions: attack ->
    neutral (profitable stops move to entry, handled per position) and
    defense -> neutral (a five-session broad-half window).  The recovery
    marker survives while the market stays neutral and clears on any other
    state.  An unknown state leaves the recorded history untouched so a data
    outage never fabricates a transition.
    """
    current = normalize_environment_state(previous)
    if state not in {"ATTACK", "NEUTRAL", "DEFENSE"} or as_of_trading_date is None:
        return current
    if current["as_of_trading_date"] == as_of_trading_date and current["state"] == state:
        return current
    recorded_state = current["state"]
    started = current["defense_recovery_started"]
    if state == "NEUTRAL":
        if recorded_state == "DEFENSE":
            started = as_of_trading_date
        elif recorded_state != "NEUTRAL":
            started = None
    else:
        started = None
    return {
        "state": str(state),
        "previous_state": (
            recorded_state if recorded_state != state else current["previous_state"]
        ),
        "as_of_trading_date": as_of_trading_date,
        "defense_recovery_started": started,
    }


class V11StateStore:
    """Atomic JSON persistence for the V11 shadow state."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, object]:
        empty: dict[str, object] = {"positions": {}, "environment": normalize_environment_state(None)}
        if not self.path.exists():
            return empty
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("v11 state payload must be an object")
        if payload.get("schema_version") != V11_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported v11 state schema_version")
        raw_positions = payload.get("positions", {})
        positions: dict[str, dict[str, object]] = {}
        if isinstance(raw_positions, Mapping):
            for symbol, raw in raw_positions.items():
                normalized = normalize_position_state(raw)
                if isinstance(symbol, str) and normalized is not None:
                    positions[symbol] = normalized
        return {
            "positions": positions,
            "environment": normalize_environment_state(payload.get("environment")),
        }

    def save(
        self,
        *,
        positions: Mapping[str, Mapping[str, object]],
        environment: Mapping[str, object],
    ) -> None:
        payload = {
            "schema_version": V11_STATE_SCHEMA_VERSION,
            "positions": {
                symbol: dict(state) for symbol, state in sorted(positions.items())
            },
            "environment": dict(environment),
        }
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
