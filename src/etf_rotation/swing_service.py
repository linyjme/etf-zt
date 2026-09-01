"""Isolated single-producer orchestration for the swing monitor.

HTTP consumers only read deep-copied published state.  Completed daily bars,
portfolio projections, formal decisions, and alert transitions are composed here
from their focused modules; none of their business rules are reimplemented.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import copy
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time as monotonic_time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .etf_metadata import EtfMetadata, EtfMetadataStore
from .market_data import load_closed_dates
from .swing_alerts import AlertInput, SwingAlertStore
from .swing_config import (
    SwingStrategyConfig,
    SwingWatchItem,
    load_strategy,
    load_watchlist,
)
from .swing_data import (
    DailyBar,
    DailyHistoryStore,
    _SiblingFileLock,
)
from .swing_portfolio import (
    InitialPositionInput,
    PortfolioLedger,
    PortfolioLedgerError,
    PortfolioEventType,
    PortfolioPosition,
    PortfolioProjection,
    TradeInput,
)
from .swing_strategy import (
    IntradayOverlay,
    PortfolioContext,
    PositionContext,
    SwingDecision,
    SwingState,
    evaluate_intraday_overlay,
    evaluate_swing,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_FINAL_DAILY_TIME = time(15, 10)
_DEFAULT_HISTORY_COUNT = 260
_MAX_DAILY_QUOTE_LIMIT = 10_000
_ACTION_STATES = frozenset({
    SwingState.TRIAL_ENTRY_CANDIDATE,
    SwingState.ADD_CANDIDATE,
    SwingState.REDUCE_CANDIDATE,
    SwingState.EXIT_CANDIDATE,
})
_PORTFOLIO_DEPENDENT_STATES = frozenset({
    SwingState.TRIAL_ENTRY_CANDIDATE,
    SwingState.ADD_CANDIDATE,
    SwingState.REDUCE_CANDIDATE,
})
_FORMAL_ALERT_STYLE: Mapping[SwingState, tuple[str, str]] = {
    SwingState.TRIAL_ENTRY_CANDIDATE: ("YELLOW", "试仓候选"),
    SwingState.ADD_CANDIDATE: ("YELLOW", "加仓候选"),
    SwingState.REDUCE_CANDIDATE: ("YELLOW", "减仓候选"),
    SwingState.EXIT_CANDIDATE: ("RED", "退出候选"),
}
_OVERLAY_ALERT_STYLE: Mapping[IntradayOverlay, tuple[str, str]] = {
    IntradayOverlay.APPROACHING_ENTRY_ZONE: ("BLUE", "接近计划买入区"),
    IntradayOverlay.PREDEFINED_STOP_TOUCHED: ("RED", "盘中触及预设止损"),
}


class SwingServiceError(ValueError):
    """Raised for invalid public service input."""


class DailyCollector(Protocol):
    def collect(
        self,
        watchlist: Sequence[SwingWatchItem],
        last_completed_date: date,
        count: int = _DEFAULT_HISTORY_COUNT,
    ) -> Sequence[DailyBar]: ...


@dataclass(frozen=True)
class SwingPaths:
    watchlist: Path
    strategy: Path
    daily_history: Path
    portfolio_snapshot: Path
    trades: Path
    alerts: Path
    metadata: Path
    calendar: Path
    backtests: Path

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            try:
                normalized = Path(value)
            except (TypeError, ValueError) as error:
                raise SwingServiceError(f"{field} must be a path") from error
            object.__setattr__(self, field, normalized)


class SwingService:
    """Publish authoritative swing state from one serialized producer."""

    def __init__(
        self,
        paths: SwingPaths,
        collector: DailyCollector | None,
        intraday_provider: Callable[[], Mapping[str, object]],
        intraday_points_provider: Callable[[str], Mapping[str, object]],
        clock: Callable[[], datetime],
        refresh_interval: float = 60.0,
        event_limit: int = 128,
    ) -> None:
        if type(paths) is not SwingPaths:
            raise SwingServiceError("paths must be SwingPaths")
        if collector is not None and not callable(getattr(collector, "collect", None)):
            raise SwingServiceError("collector must provide collect")
        for value, label in (
            (intraday_provider, "intraday_provider"),
            (intraday_points_provider, "intraday_points_provider"),
            (clock, "clock"),
        ):
            if not callable(value):
                raise SwingServiceError(f"{label} must be callable")
        if (
            type(refresh_interval) not in (int, float)
            or not math.isfinite(float(refresh_interval))
            or float(refresh_interval) <= 0.0
        ):
            raise SwingServiceError("refresh_interval must be a finite positive number")
        if type(event_limit) is not int or event_limit <= 0:
            raise SwingServiceError("event_limit must be a positive integer")

        self.paths = paths
        self.collector = collector
        self.intraday_provider = intraday_provider
        self.intraday_points_provider = intraday_points_provider
        self.clock = clock
        self.refresh_interval = float(refresh_interval)
        self.producer_lock = threading.Lock()
        self.publish_condition = threading.Condition(threading.Lock())
        self.events: deque[dict[str, object]] = deque(maxlen=event_limit)
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self.revision = 0
        self._watchlist: tuple[SwingWatchItem, ...] = ()
        self._strategy: SwingStrategyConfig | None = None
        self._metadata: Mapping[str, EtfMetadata] = {}
        self._closed_dates: frozenset[date] | None = None
        self._history_store: DailyHistoryStore | None = None
        self._ledger: PortfolioLedger | None = None
        self._alert_store: SwingAlertStore | None = None
        self._portfolio_projection: PortfolioProjection | None = None
        self._history: tuple[DailyBar, ...] = ()
        self._formal: dict[str, SwingDecision] = {}
        self._health: dict[str, str] = {
            "service": "STARTING",
            "configuration": "UNKNOWN",
            "calendar": "UNKNOWN",
            "daily": "UNKNOWN",
            "minute_crosscheck": "NOT_RUN",
            "portfolio": "UNKNOWN",
            "alerts": "UNKNOWN",
            "intraday": "UNAVAILABLE",
        }
        self._errors: dict[str, str] = {}
        self.published = self._bootstrap()

    # ---- Public read and lifecycle API ---------------------------------

    def snapshot(self) -> dict[str, object]:
        with self.publish_condition:
            return copy.deepcopy(self.published)

    def health(self) -> dict[str, object]:
        snapshot = self.snapshot()
        health = dict(snapshot.get("health", {}))
        ok = all(value in {"OK", "REALTIME", "NOT_RUN"} for value in health.values())
        return {
            "status": "ok" if ok else "degraded",
            "ok": ok,
            "mode": "MONITOR_ONLY",
            "revision": snapshot["revision"],
            "components": health,
            "errors": copy.deepcopy(snapshot.get("errors", {})),
        }

    def watchlist(self) -> dict[str, object]:
        with self.publish_condition:
            items = tuple(self._watchlist)
        return {
            "items": [
                {"symbol": item.symbol, "enabled": item.enabled}
                for item in items
            ],
            "read_only": False,
        }

    def update_watchlist(self, symbol: str, enabled: bool) -> dict[str, object]:
        """Atomically persist and publish one validated watchlist toggle."""
        if (
            not isinstance(symbol, str)
            or len(symbol) != 6
            or not symbol.isascii()
            or not symbol.isdigit()
            or symbol not in self._metadata
        ):
            raise SwingServiceError("symbol must identify a metadata-verified ETF")
        if type(enabled) is not bool:
            raise SwingServiceError("enabled must be boolean")
        with self.producer_lock:
            candidate = list(self._watchlist)
            index = next(
                (position for position, item in enumerate(candidate)
                 if item.symbol == symbol),
                None,
            )
            if index is not None and candidate[index].enabled is enabled:
                if enabled:
                    self._ensure_current_formal_alerts(self._safe_now())
                return self.watchlist()
            if index is None:
                candidate.append(SwingWatchItem(symbol, enabled))
            else:
                candidate[index] = SwingWatchItem(symbol, enabled)
            next_watchlist = tuple(candidate)
            now = self._safe_now()
            try:
                self._retract_symbol_overlays(
                    symbol, "WATCHLIST_MEMBERSHIP_CHANGED",
                )
                next_health = dict(self._health)
                next_errors = dict(self._errors)
                next_health["alerts"] = "OK"
                next_errors.pop("alerts", None)
                next_formal = self._calculate_formal(
                    self._history,
                    self._portfolio_projection,
                    self._health["portfolio"],
                    self._health["daily"],
                    now,
                    watchlist=next_watchlist,
                )
                next_by_symbol = {
                    formal_symbol: (
                        self._next_trading_date(decision.as_of_trading_date)
                        if decision.as_of_trading_date is not None else None
                    )
                    for formal_symbol, decision in next_formal.items()
                }
                alert_error = self._persist_formal_alerts(
                    next_formal,
                    next_by_symbol,
                    self._health["portfolio"],
                )
                if alert_error is None:
                    next_health["alerts"] = "OK"
                    next_errors.pop("alerts", None)
                else:
                    next_health["alerts"] = "BLOCKED"
                    next_errors["alerts"] = alert_error
                snapshot = self._candidate_snapshot(
                    now,
                    watchlist=next_watchlist,
                    formal=next_formal,
                    health=next_health,
                    errors=next_errors,
                )
                self._write_watchlist(next_watchlist)
            except Exception as error:
                self._health["alerts"] = "BLOCKED"
                self._errors["alerts"] = self._safe_error(error)
                failed_snapshot = self._build_snapshot(now)
                for item in failed_snapshot["items"]:  # type: ignore[index]
                    if item.get("symbol") == symbol:
                        item["intraday_overlay"] = None
                self._publish(failed_snapshot)
                raise SwingServiceError("watchlist update failed") from error
            with self.publish_condition:
                self._watchlist = next_watchlist
                self._formal = next_formal
                self._health = next_health
                self._errors = next_errors
                self._publish_locked(snapshot)
            return self.watchlist()

    def _ensure_current_formal_alerts(self, now: datetime) -> None:
        store = self._alert_store
        try:
            before_events = store.load_events() if store is not None else ()
        except Exception:
            before_events = ()
        next_by_symbol = {
            symbol: (
                self._next_trading_date(decision.as_of_trading_date)
                if decision.as_of_trading_date is not None else None
            )
            for symbol, decision in self._formal.items()
        }
        alert_error = self._persist_formal_alerts(
            self._formal, next_by_symbol, self._health["portfolio"],
        )
        next_health = dict(self._health)
        next_errors = dict(self._errors)
        if alert_error is None:
            next_health["alerts"] = "OK"
            next_errors.pop("alerts", None)
        else:
            next_health["alerts"] = "BLOCKED"
            next_errors["alerts"] = alert_error
        try:
            after_events = store.load_events() if store is not None else ()
        except Exception:
            after_events = ()
        if (
            before_events == after_events
            and next_health == self._health
            and next_errors == self._errors
        ):
            return
        snapshot = self._candidate_snapshot(
            now, health=next_health, errors=next_errors,
        )
        with self.publish_condition:
            self._health = next_health
            self._errors = next_errors
            self._publish_locked(snapshot)

    def _retract_symbol_overlays(self, symbol: str, reason: str) -> None:
        store = self._require_alert_store()
        active = tuple(
            item for item in store.current()
            if item.scope == "INTRADAY"
            and item.symbol == symbol
            and not item.retracted
        )
        for item in active:
            store.retract_overlay(item.alert_id, reason)

    def portfolio(self) -> dict[str, object]:
        with self.publish_condition:
            projection = self._portfolio_projection
            status = self._health["portfolio"]
            error = self._errors.get("portfolio")
        return {
            "status": status,
            "projection": projection.to_dict() if projection is not None else None,
            "error": error,
            "local_only": True,
        }

    def alerts(self, *, include_retracted: bool = False) -> dict[str, object]:
        if type(include_retracted) is not bool:
            raise SwingServiceError("include_retracted must be boolean")
        store = self._alert_store
        if store is None:
            return {"status": "BLOCKED", "items": [], "local_only": True}
        now = self._safe_now()
        with self.publish_condition:
            items, _ = self._alert_snapshot(
                now, include_retracted=include_retracted,
            )
            status = self._health["alerts"]
        return {
            "status": status,
            "items": items,
            "local_only": True,
        }

    def daily_quotes(
        self,
        symbol: str,
        since: int,
        limit: int = 500,
    ) -> dict[str, object]:
        normalized_symbol = self._validated_enabled_symbol(symbol)
        if type(since) is not int or since < 0:
            raise SwingServiceError("since must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= _MAX_DAILY_QUOTE_LIMIT:
            raise SwingServiceError(
                f"limit must be an integer from 1 to {_MAX_DAILY_QUOTE_LIMIT}",
            )
        with self.publish_condition:
            revision = self.revision
            as_of = self.published.get("as_of_trading_date")
            retained = copy.deepcopy(tuple(self.events))
            history = tuple(
                bar for bar in self._history if bar.symbol == normalized_symbol
            )

        reset = since == 0 or since > revision
        if since < revision:
            if not retained or since < int(retained[0]["revision"]) - 1:
                reset = True
            for event in retained:
                if int(event["revision"]) <= since:
                    continue
                if (
                    bool(event.get("force_reset"))
                    or event.get("as_of_trading_date") != as_of
                ):
                    reset = True
                    break
        upserts: list[dict[str, object]]
        if reset:
            upserts = [bar.to_dict() for bar in history]
        else:
            indexed: dict[str, dict[str, object]] = {}
            for event in retained:
                if int(event["revision"]) <= since:
                    continue
                daily = event.get("daily_upserts", {})
                if not isinstance(daily, Mapping):
                    reset = True
                    break
                raw_values = daily.get(normalized_symbol, ())
                if not isinstance(raw_values, (tuple, list)):
                    reset = True
                    break
                for value in raw_values:
                    if isinstance(value, Mapping):
                        payload = copy.deepcopy(dict(value))
                        key = str(payload.get("trading_date", ""))
                        indexed[key] = payload
            if reset:
                upserts = [bar.to_dict() for bar in history]
            else:
                upserts = [indexed[key] for key in sorted(indexed)]
        truncated = len(upserts) > limit
        if truncated:
            upserts = upserts[-limit:]
            reset = True
        return {
            "symbol": normalized_symbol,
            "revision": revision,
            "as_of_trading_date": as_of,
            "upserts": copy.deepcopy(upserts),
            "reset": reset,
            "truncated": truncated,
            "read_only": True,
        }

    def wait_for_event(
        self,
        after_revision: int,
        timeout: float = 30.0,
    ) -> dict[str, object] | None:
        if type(after_revision) is not int or after_revision < 0:
            raise SwingServiceError("after_revision must be a nonnegative integer")
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(float(timeout))
            or float(timeout) < 0.0
        ):
            raise SwingServiceError("timeout must be a finite nonnegative number")
        deadline = monotonic_time.monotonic() + float(timeout)
        with self.publish_condition:
            while True:
                selected = self._select_event_locked(after_revision)
                if selected is not None:
                    return copy.deepcopy(selected)
                if self._stop_event.is_set():
                    return None
                remaining = deadline - monotonic_time.monotonic()
                if remaining <= 0.0:
                    return None
                self.publish_condition.wait(remaining)

    def events_since(
        self, after_revision: int, timeout: float = 30.0,
    ) -> dict[str, object] | None:
        return self.wait_for_event(after_revision, timeout)

    def start_refresh(self) -> None:
        if self._refresh_thread is not None:
            return
        with self.producer_lock:
            if self._refresh_thread is not None:
                return
            self._stop_event.clear()
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name="swing-monitor-producer",
                daemon=True,
            )
            self._refresh_thread.start()

    def stop_refresh(self) -> None:
        self._stop_event.set()
        with self.publish_condition:
            self.publish_condition.notify_all()
        thread = self._refresh_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(5.0, self.refresh_interval + 1.0))
        if thread is None or not thread.is_alive():
            self._refresh_thread = None

    # ---- Producer entry points ----------------------------------------

    def refresh_once(self, now: datetime | None = None) -> bool:
        with self.producer_lock:
            try:
                cycle_time = self._local_time(self.clock() if now is None else now)
            except Exception as error:
                self._publish_component_failure(
                    "service", "CLOCK_FAILED", error, now=None,
                )
                return False
            return self._refresh_completed_daily(cycle_time)

    def refresh_intraday(self) -> dict[str, object]:
        with self.producer_lock:
            try:
                cycle_time = self._local_time(self.clock())
            except Exception as error:
                self._withdraw_intraday("CLOCK_FAILED", None, error)
                return self.snapshot()
            return self._refresh_intraday_overlay(cycle_time)

    # ---- Local mutation boundary (used by HTTP integration) -----------

    def initialize_portfolio(
        self,
        name: str,
        cash: float,
        idempotency_key: str,
        *,
        initial_positions: Mapping[
            str, InitialPositionInput | Mapping[str, object]
        ] | None = None,
        default_risk_per_trade: float | None = None,
    ) -> dict[str, object]:
        with self.producer_lock:
            ledger = self._require_ledger()
            risk = (
                self._strategy.risk_per_trade
                if default_risk_per_trade is None and self._strategy is not None
                else default_risk_per_trade
            )
            if risk is None:
                raise SwingServiceError("strategy configuration is unavailable")
            event = ledger.initialize(
                name, cash, idempotency_key,
                initial_positions=initial_positions,
                default_risk_per_trade=risk,
            )
            self._rebuild_after_portfolio_mutation(self._safe_now())
            return event.to_dict()

    def record_trade(
        self, trade: TradeInput, idempotency_key: str,
    ) -> dict[str, object]:
        with self.producer_lock:
            ledger = self._require_ledger()
            normalized = trade
            prior = tuple(
                event for event in ledger.load_events()
                if event.idempotency_key == idempotency_key
            )
            if len(prior) == 1:
                existing = prior[0]
                if (
                    type(trade) is TradeInput
                    and trade.side == "SELL"
                    and trade.exit_reason is None
                    and existing.event_type is PortfolioEventType.SELL_CONFIRMED
                    and existing.payload.get("exit_reason") == "STOP_EXIT"
                ):
                    normalized = replace(trade, exit_reason="STOP_EXIT")
            elif not prior and (
                type(trade) is TradeInput
                and trade.side == "SELL"
                and trade.exit_reason is None
                and self._sell_completes_position(trade)
                and self._stop_exit_is_current(
                    trade.symbol, self._safe_now(),
                )
            ):
                normalized = replace(trade, exit_reason="STOP_EXIT")
            event = ledger.record_trade(
                normalized, idempotency_key,
            )
            self._rebuild_after_portfolio_mutation(self._safe_now())
            return event.to_dict()

    def _sell_completes_position(self, trade: TradeInput) -> bool:
        projection = self._portfolio_projection
        if projection is None:
            return False
        position = projection.positions.get(trade.symbol)
        return position is not None and trade.shares == position.shares

    def _stop_exit_is_current(self, symbol: str, now: datetime) -> bool:
        formal = self._formal.get(symbol)
        expected = self._last_completed_trading_date(now)
        if (
            formal is not None
            and self._health["daily"] == "OK"
            and self._health["portfolio"] == "OK"
            and expected is not None
            and formal.as_of_trading_date == expected
            and (
                formal.evidence.get("exit_hard_stop") is True
                or formal.evidence.get("exit_trailing_stop") is True
            )
        ):
            return True
        try:
            published_item = next(
                item for item in self.published.get("items", ())
                if isinstance(item, Mapping) and item.get("symbol") == symbol
            )
            raw_timestamp = published_item.get("current_price_time")
            if type(raw_timestamp) is not str:
                return False
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return False
            return bool(
                self._is_trading_date(now.date())
                and published_item.get("intraday_overlay")
                == IntradayOverlay.PREDEFINED_STOP_TOUCHED.value
                and published_item.get("intraday_health_status") == "REALTIME"
                and timestamp.astimezone(SHANGHAI).date() == now.date()
            )
        except Exception:
            return False

    def reverse_trade(
        self, event_id: str, idempotency_key: str,
    ) -> dict[str, object]:
        with self.producer_lock:
            event = self._require_ledger().reverse(event_id, idempotency_key)
            self._rebuild_after_portfolio_mutation(self._safe_now())
            return event.to_dict()

    def acknowledge_alert(
        self, alert_id: str, idempotency_key: str,
    ) -> dict[str, object]:
        with self.producer_lock:
            store = self._require_alert_store()
            result = store.acknowledge(alert_id, idempotency_key)
            self._refresh_alert_snapshot(self._safe_now())
            return result.to_dict()

    def ignore_alert(
        self, alert_id: str, idempotency_key: str,
    ) -> dict[str, object]:
        with self.producer_lock:
            store = self._require_alert_store()
            result = store.ignore(alert_id, idempotency_key)
            self._refresh_alert_snapshot(self._safe_now())
            return result.to_dict()

    # ---- Bootstrap and formal recomputation ---------------------------

    def _bootstrap(self) -> dict[str, object]:
        now = self._safe_now()
        try:
            self._metadata = EtfMetadataStore(self.paths.metadata).load()
            self._watchlist = load_watchlist(
                self.paths.watchlist, self.paths.metadata,
            )
            self._strategy = load_strategy(self.paths.strategy)
            self._health["configuration"] = "OK"
        except Exception as error:
            self._health["configuration"] = "BLOCKED"
            self._errors["configuration"] = self._safe_error(error)

        try:
            self._closed_dates = frozenset(load_closed_dates(self.paths.calendar))
            self._health["calendar"] = "OK"
        except Exception as error:
            self._closed_dates = None
            self._health["calendar"] = "BLOCKED"
            self._errors["calendar"] = self._safe_error(error)

        if self._metadata and self._closed_dates is not None:
            try:
                self._history_store = DailyHistoryStore(
                    self.paths.daily_history,
                    self._metadata,
                    self._closed_dates,
                )
                self._history = self._history_store.load()
                self._health["daily"] = "OK"
            except Exception as error:
                self._history_store = None
                self._history = ()
                self._health["daily"] = "BLOCKED"
                self._errors["daily"] = self._safe_error(error)
        else:
            self._health["daily"] = "BLOCKED"

        if self._metadata:
            try:
                self._ledger = PortfolioLedger(
                    self.paths.trades,
                    self._metadata,
                    clock=self.clock,
                    max_portfolio_risk_rate=(
                        self._strategy.max_portfolio_risk
                        if self._strategy is not None else 0.02
                    ),
                    closed_dates=self._closed_dates,
                )
                if self._closed_dates is None:
                    self._portfolio_projection = None
                    self._health["portfolio"] = "BLOCKED"
                    self._errors["portfolio"] = (
                        "authoritative market calendar is unavailable"
                    )
                else:
                    self._load_portfolio_projection(now)
            except Exception as error:
                self._portfolio_projection = None
                self._health["portfolio"] = "BLOCKED"
                self._errors["portfolio"] = self._safe_error(error)
        else:
            self._health["portfolio"] = "BLOCKED"

        try:
            self._alert_store = SwingAlertStore(self.paths.alerts, clock=self.clock)
            self._alert_store.current(include_retracted=True)
            self._health["alerts"] = "OK"
        except Exception as error:
            self._alert_store = None
            self._health["alerts"] = "BLOCKED"
            self._errors["alerts"] = self._safe_error(error)

        self._recompute_formal(now, publish_alerts=False)
        self._health["service"] = (
            "OK" if self._health["configuration"] == "OK" else "BLOCKED"
        )
        return self._build_snapshot(now)

    def _load_portfolio_projection(self, now: datetime) -> None:
        projection, status, error = self._calculate_portfolio_projection(
            self._history, now,
        )
        self._portfolio_projection = projection
        self._health["portfolio"] = status
        if error is None:
            self._errors.pop("portfolio", None)
        else:
            self._errors["portfolio"] = error

    def _calculate_portfolio_projection(
        self,
        history: Sequence[DailyBar],
        now: datetime,
        *,
        persist: bool = True,
    ) -> tuple[PortfolioProjection | None, str, str | None]:
        ledger = self._require_ledger()
        if self._closed_dates is None:
            return (
                None,
                "BLOCKED",
                "authoritative market calendar is unavailable",
            )
        as_of = self._portfolio_as_of(now)
        marks = self._latest_marks(history)
        try:
            projection = (
                ledger.load_or_rebuild_projection(
                    self.paths.portfolio_snapshot, as_of, marks,
                )
                if persist
                else ledger.project(as_of, marks)
            )
        except PortfolioLedgerError as error:
            text = str(error).lower()
            return (
                None,
                "UNINITIALIZED" if "not initialized" in text else "BLOCKED",
                self._safe_error(error),
            )
        return projection, "OK", None

    def _recompute_formal(self, now: datetime, *, publish_alerts: bool) -> None:
        self._formal = self._calculate_formal(
            self._history,
            self._portfolio_projection,
            self._health["portfolio"],
            self._health["daily"],
            now,
        )
        if publish_alerts:
            next_by_symbol = {
                symbol: self._next_trading_date(decision.as_of_trading_date)
                for symbol, decision in self._formal.items()
            }
            self._publish_formal_alerts(now, next_by_symbol)

    def _calculate_formal(
        self,
        history: Sequence[DailyBar],
        projection: PortfolioProjection | None,
        portfolio_health: str,
        daily_health: str,
        now: datetime,
        *,
        watchlist: Sequence[SwingWatchItem] | None = None,
    ) -> dict[str, SwingDecision]:
        if self._strategy is None:
            return {}
        grouped = self._bars_by_symbol(history)
        expected = self._last_completed_trading_date(now)
        result: dict[str, SwingDecision] = {}
        for item in self._watchlist if watchlist is None else watchlist:
            if not item.enabled:
                continue
            bars = grouped.get(item.symbol, ())
            latest = bars[-1].trading_date if bars else None
            data_healthy = bool(
                daily_health == "OK"
                and expected is not None
                and latest == expected
            )
            next_date = (
                self._next_trading_date(latest)
                if latest is not None and self._closed_dates is not None else None
            )
            context = self._portfolio_context(
                item.symbol, bars, data_healthy=data_healthy,
                next_trading_date=next_date,
                projection=projection,
                portfolio_health=portfolio_health,
            )
            result[item.symbol] = evaluate_swing(
                bars, self._strategy, context,
            )
        return result

    def _portfolio_context(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
        *,
        data_healthy: bool,
        next_trading_date: date | None,
        projection: PortfolioProjection | None,
        portfolio_health: str,
    ) -> PortfolioContext:
        metadata = self._metadata.get(symbol)
        lot_size = metadata.trading.lot_size if metadata is not None else 100
        ledger_healthy = portfolio_health == "OK" and projection is not None
        equity = projection.equity if ledger_healthy else 1.0
        cash = projection.cash if ledger_healthy else 0.0
        market_value = projection.etf_market_value if ledger_healthy else 0.0
        planned_risk = projection.planned_risk if ledger_healthy else 0.0
        position = None
        last_stop_trading_date = (
            self._last_stop_trading_date(symbol, bars) if ledger_healthy else None
        )
        if ledger_healthy and projection is not None:
            projected = projection.positions.get(symbol)
            if projected is not None and projected.shares > 0 and bars:
                position = self._position_context(symbol, projected, bars)
        return PortfolioContext(
            equity=equity,
            cash=cash,
            current_etf_market_value=market_value,
            current_planned_risk_amount=planned_risk,
            lot_size=lot_size,
            data_healthy=data_healthy,
            metadata_complete=metadata is not None,
            ledger_healthy=ledger_healthy,
            tradable=True,
            next_trading_date=next_trading_date,
            last_stop_trading_date=last_stop_trading_date,
            position=position,
        )

    def _position_context(
        self,
        symbol: str,
        position: PortfolioPosition,
        bars: Sequence[DailyBar],
    ) -> PositionContext | None:
        latest = bars[-1]
        raw_scale = latest.close / latest.adjusted_close
        if not math.isfinite(raw_scale) or raw_scale <= 0.0:
            return None
        average = position.average_cost / raw_scale
        risk = position.planned_risk / max(position.shares, 1) / raw_scale
        if risk <= 0.0:
            risk = max(average * 0.01, math.ulp(average))
        hard_stop = max(average - risk, math.ulp(average))
        entry_date, first_reduction, _ = self._event_lifecycle(symbol, bars)
        if entry_date is None:
            raise PortfolioLedgerError("projected position has no open event lifecycle")
        completed_since_entry = tuple(
            bar for bar in bars if bar.trading_date >= entry_date
        )
        if not completed_since_entry:
            completed_since_entry = (latest,)
        return PositionContext(
            shares=position.shares,
            sellable_shares=position.sellable_shares,
            average_cost_adjusted=average,
            initial_risk_per_share_adjusted=risk,
            entry_trading_date=entry_date,
            highest_completed_adjusted_close=max(
                bar.adjusted_close for bar in completed_since_entry
            ),
            hard_stop_adjusted=hard_stop,
            first_reduction_completed=first_reduction,
        )

    def _last_stop_trading_date(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
    ) -> date | None:
        if self._ledger is None:
            return None
        _, _, stopped = self._event_lifecycle(symbol, bars)
        return stopped

    def _event_lifecycle(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
    ) -> tuple[date | None, bool, date | None]:
        """Derive the open holding cycle solely from authoritative ledger events."""
        ledger = self._require_ledger()
        events = ledger.load_events()
        reversed_ids = {
            str(event.payload["target_event_id"])
            for event in events
            if event.event_type is PortfolioEventType.TRADE_REVERSED
        }
        shares = 0
        entry_date: date | None = None
        first_reduction = False
        last_stop_date: date | None = None
        if events:
            initial = events[0].payload.get("initial_positions", {})
            if isinstance(initial, Mapping):
                raw = initial.get(symbol)
                if isinstance(raw, Mapping) and type(raw.get("shares")) is int:
                    shares = int(raw["shares"])
                    if shares > 0:
                        initialized = events[0].recorded_at.astimezone(SHANGHAI).date()
                        entry_date = self._trading_date_on_or_before(initialized)
        raw_trades = [
            event for event in events
            if (
                event.event_type in {
                    PortfolioEventType.BUY_CONFIRMED,
                    PortfolioEventType.SELL_CONFIRMED,
                }
                and event.event_id not in reversed_ids
                and event.payload.get("symbol") == symbol
            )
        ]
        trades: list[tuple[datetime, Any]] = []
        for event in raw_trades:
            raw_shares = event.payload.get("shares")
            raw_time = event.payload.get("executed_at")
            if type(raw_shares) is not int or type(raw_time) is not str:
                raise PortfolioLedgerError("portfolio trade lifecycle is invalid")
            try:
                executed_at = datetime.fromisoformat(raw_time)
                if executed_at.tzinfo is None or executed_at.utcoffset() is None:
                    raise ValueError("naive trade timestamp")
                executed_at = executed_at.astimezone(SHANGHAI)
            except Exception as error:
                raise PortfolioLedgerError(
                    "portfolio trade lifecycle timestamp is invalid",
                ) from error
            trades.append((executed_at, event))
        trades.sort(key=lambda item: item[0])
        for executed_at, event in trades:
            raw_shares = event.payload["shares"]
            executed = executed_at.date()
            if event.event_type is PortfolioEventType.BUY_CONFIRMED:
                if shares == 0:
                    entry_date = executed
                    first_reduction = False
                shares += raw_shares
            else:
                shares -= raw_shares
                if shares < 0:
                    raise PortfolioLedgerError("portfolio lifecycle shares are negative")
                if shares == 0:
                    if event.payload.get("exit_reason") == "STOP_EXIT":
                        last_stop_date = executed
                    entry_date = None
                    first_reduction = False
                else:
                    first_reduction = True
        return entry_date, first_reduction, last_stop_date

    def _trading_date_on_or_before(self, value: date) -> date:
        candidate = value
        for _ in range(370):
            if self._is_trading_date(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise PortfolioLedgerError("initialization date cannot map to a trading day")

    # ---- Daily producer ------------------------------------------------

    def _refresh_completed_daily(self, now: datetime) -> bool:
        target = self._last_completed_trading_date(now)
        enabled = tuple(item for item in self._watchlist if item.enabled)
        if target is None or not enabled:
            return False
        latest = {
            symbol: bars[-1].trading_date
            for symbol, bars in self._bars_by_symbol().items() if bars
        }
        if all(latest.get(item.symbol) == target for item in enabled):
            return False
        if self.collector is None:
            self._publish_component_failure(
                "daily", "COLLECTOR_UNAVAILABLE",
                SwingServiceError("daily collector is unavailable"), now=now,
            )
            return False
        if self._history_store is None:
            self._publish_component_failure(
                "daily", "BLOCKED",
                SwingServiceError("daily history store is unavailable"), now=now,
            )
            return False
        try:
            raw_records = self.collector.collect(
                enabled, target, _DEFAULT_HISTORY_COUNT,
            )
            records = self._materialize_collected(raw_records)
            self._validate_complete_batch(records, enabled, target)
        except Exception as error:
            self._publish_component_failure(
                "daily", "COLLECTION_FAILED", error, now=now,
            )
            return False

        crosscheck_health = "OK"
        try:
            for item in enabled:
                target_bar = next(
                    bar for bar in records
                    if bar.symbol == item.symbol and bar.trading_date == target
                )
                outcome = self._crosscheck_minutes(target_bar)
                if outcome == "UNAVAILABLE":
                    crosscheck_health = "MINUTE_CROSSCHECK_UNAVAILABLE"
        except Exception as error:
            self._health["minute_crosscheck"] = "CROSSCHECK_FAILED"
            self._publish_component_failure(
                "daily", "CROSSCHECK_FAILED", error, now=now,
            )
            return False

        before = {(bar.symbol, bar.trading_date): bar for bar in self._history}
        try:
            merged = self._candidate_history(records)
            next_health = dict(self._health)
            next_errors = dict(self._errors)
            next_health["daily"] = "OK"
            next_health["minute_crosscheck"] = crosscheck_health
            next_errors.pop("daily", None)
            next_errors.pop("minute_crosscheck", None)
            projection, portfolio_status, portfolio_error = (
                self._calculate_portfolio_projection(
                    merged, now, persist=False,
                )
            )
            next_health["portfolio"] = portfolio_status
            if portfolio_error is None:
                next_errors.pop("portfolio", None)
            else:
                next_errors["portfolio"] = portfolio_error
            formal = self._calculate_formal(
                merged, projection, portfolio_status, "OK", now,
            )
            # Exercise every public serialization path before the primary
            # history file is replaced.  The second build below only reflects
            # the alert-store outcome, which is isolated as its own component.
            self._candidate_snapshot(
                now,
                history=merged,
                projection=projection,
                projection_is_explicit=True,
                formal=formal,
                health=next_health,
                errors=next_errors,
            )
        except Exception as error:
            self._publish_component_failure(
                "daily", "STRATEGY_FAILED", error, now=now,
            )
            return False
        changed = tuple(
            bar for bar in merged
            if before.get((bar.symbol, bar.trading_date)) != bar
        )
        old_as_of = self._snapshot_as_of()
        try:
            persisted = self._history_store.upsert(records)
            if persisted != merged:
                raise SwingServiceError("persisted daily history differs from staged batch")
        except Exception as error:
            self._publish_component_failure(
                "daily", "PERSISTENCE_FAILED", error, now=now,
            )
            return False

        try:
            persisted_portfolio = self._calculate_portfolio_projection(
                merged, now, persist=True,
            )
        except Exception as error:
            self._publish_component_failure(
                "portfolio", "PERSISTENCE_FAILED", error, now=now,
            )
            return False
        candidate_portfolio = (
            projection, portfolio_status, portfolio_error,
        )
        if persisted_portfolio != candidate_portfolio:
            self._publish_component_failure(
                "portfolio",
                "PERSISTENCE_FAILED",
                SwingServiceError(
                    "persisted portfolio projection differs from staged projection",
                ),
                now=now,
            )
            return False
        projection, portfolio_status, portfolio_error = persisted_portfolio

        next_by_symbol = {
            symbol: self._next_trading_date(decision.as_of_trading_date)
            for symbol, decision in formal.items()
        }
        alert_error = self._persist_formal_alerts(
            formal, next_by_symbol, portfolio_status,
        )
        if alert_error is None:
            next_health["alerts"] = "OK"
            next_errors.pop("alerts", None)
        else:
            next_health["alerts"] = "BLOCKED"
            next_errors["alerts"] = alert_error
        snapshot = self._candidate_snapshot(
            now,
            history=merged,
            projection=projection,
            projection_is_explicit=True,
            formal=formal,
            health=next_health,
            errors=next_errors,
        )
        new_as_of = max(
            (bar.trading_date for bar in merged), default=None,
        )
        with self.publish_condition:
            self._history = merged
            self._portfolio_projection = projection
            self._formal = formal
            self._health = next_health
            self._errors = next_errors
            self._publish_locked(
                snapshot,
                daily_upserts=self._group_bar_payloads(changed),
                force_reset=(
                    old_as_of
                    != (new_as_of.isoformat() if new_as_of is not None else None)
                ),
            )
        return True

    @staticmethod
    def _materialize_collected(value: object) -> tuple[DailyBar, ...]:
        if isinstance(value, (str, bytes, bytearray)):
            raise SwingServiceError("collector result must be a daily-bar sequence")
        try:
            records = tuple(value)  # type: ignore[arg-type]
        except Exception as error:
            raise SwingServiceError("collector result could not be read") from error
        if any(type(record) is not DailyBar for record in records):
            raise SwingServiceError("collector result contains invalid daily bars")
        return records

    def _validate_complete_batch(
        self,
        records: tuple[DailyBar, ...],
        enabled: tuple[SwingWatchItem, ...],
        target: date,
    ) -> None:
        expected_symbols = {item.symbol for item in enabled}
        if any(bar.symbol not in expected_symbols for bar in records):
            raise SwingServiceError("collector returned an unrequested symbol")
        keys = tuple((bar.symbol, bar.trading_date) for bar in records)
        if len(set(keys)) != len(keys):
            raise SwingServiceError("collector returned a duplicate daily primary key")
        if any(bar.trading_date > target for bar in records):
            raise SwingServiceError("collector returned a future or incomplete daily bar")
        target_counts = {
            symbol: sum(
                bar.symbol == symbol and bar.trading_date == target
                for bar in records
            )
            for symbol in expected_symbols
        }
        missing = sorted(symbol for symbol, count in target_counts.items() if count != 1)
        if missing:
            raise SwingServiceError(
                f"completed daily batch is missing or duplicates target bars: {missing}",
            )
        self._candidate_history(records)

    def _candidate_history(
        self, records: Sequence[DailyBar],
    ) -> tuple[DailyBar, ...]:
        if self._history_store is None:
            raise SwingServiceError("daily history store is unavailable")
        combined = {
            (bar.symbol, bar.trading_date): bar for bar in self._history
        }
        for bar in records:
            key = (bar.symbol, bar.trading_date)
            existing = combined.get(key)
            if existing is None or bar.observed_at >= existing.observed_at:
                combined[key] = bar
        merged = tuple(combined[key] for key in sorted(combined))
        self._history_store.validator.validate_sequence(merged, self._metadata)
        return merged

    def _crosscheck_minutes(self, bar: DailyBar) -> str:
        try:
            payload = self.intraday_points_provider(bar.symbol)
        except Exception:
            return "UNAVAILABLE"
        points = self._minute_points(payload, bar.trading_date)
        if not points:
            return "UNAVAILABLE"
        aggregate = self._aggregate_minute_ohlc(points)
        metadata = self._metadata[bar.symbol]
        tick = float(metadata.trading.price_tick)
        for field in ("open", "high", "low", "close"):
            actual = aggregate[field]
            expected = getattr(bar, field)
            tolerance = tick + 8.0 * max(math.ulp(actual), math.ulp(expected))
            if abs(actual - expected) > tolerance:
                raise SwingServiceError(
                    f"{bar.symbol} minute aggregate {field} differs by more than one tick",
                )
        return "OK"

    @staticmethod
    def _minute_points(
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
        if tuple(timestamps) != SwingService._expected_complete_minutes(trading_date):
            return ()
        return tuple(accepted)

    @staticmethod
    def _expected_complete_minutes(trading_date: date) -> tuple[datetime, ...]:
        """Return Eastmoney/T-monitor's 241 completed minute timestamps."""
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

    @staticmethod
    def _aggregate_minute_ohlc(
        points: Sequence[Mapping[str, object]],
    ) -> dict[str, float]:
        def number(value: object, field: str) -> float:
            if type(value) not in (int, float):
                raise SwingServiceError(f"minute {field} must be numeric")
            result = float(value)
            if not math.isfinite(result) or result <= 0.0:
                raise SwingServiceError(f"minute {field} must be positive")
            return result

        first = points[0]
        last = points[-1]
        first_price = first.get("price", first.get("close"))
        last_price = last.get("price", last.get("close"))
        opens = number(first.get("open", first_price), "open")
        closes = number(last.get("close", last_price), "close")
        highs = [
            number(point.get("high", point.get("price", point.get("close"))), "high")
            for point in points
        ]
        lows = [
            number(point.get("low", point.get("price", point.get("close"))), "low")
            for point in points
        ]
        return {"open": opens, "high": max(highs), "low": min(lows), "close": closes}

    # ---- Intraday overlay ---------------------------------------------

    def _refresh_intraday_overlay(self, now: datetime) -> dict[str, object]:
        try:
            payload = self.intraday_provider()
            if not isinstance(payload, Mapping):
                raise SwingServiceError("intraday snapshot must be a mapping")
            raw_items = payload.get("items")
            if not isinstance(raw_items, (tuple, list)):
                raise SwingServiceError("intraday snapshot items are unavailable")
            by_symbol: dict[str, Mapping[str, object]] = {}
            for raw in raw_items:
                if isinstance(raw, Mapping) and type(raw.get("symbol")) is str:
                    by_symbol[str(raw["symbol"])] = raw
        except Exception as error:
            self._withdraw_intraday("INTRADAY_FEED_UNAVAILABLE", now, error)
            return self.snapshot()

        overlays: dict[str, str | None] = {}
        current: dict[str, tuple[float | None, str | None, str, str]] = {}
        desired_alerts: list[AlertInput] = []
        all_realtime = True
        for symbol, formal in self._formal.items():
            raw = by_symbol.get(symbol)
            healthy = raw is not None and raw.get("health_status") == "REALTIME"
            price = raw.get("price") if raw is not None else None
            timestamp = raw.get("timestamp") if raw is not None else None
            if type(timestamp) is not str:
                timestamp = None
            intraday = evaluate_intraday_overlay(
                formal,
                price,
                feed_healthy=healthy,
                has_position=self._has_position(symbol),
            )
            resolved_status = self._execution_status(
                formal, now, market_realtime=healthy,
            )
            if (
                intraday.overlay is IntradayOverlay.APPROACHING_ENTRY_ZONE
                and (
                    formal.state is not SwingState.TRIAL_ENTRY_CANDIDATE
                    or resolved_status != "READY_TO_EXECUTE"
                )
            ):
                overlays[symbol] = None
                normalized_price = intraday.price
                status = resolved_status
                raw_health = (
                    str(raw.get("health_status"))
                    if raw is not None and type(raw.get("health_status")) is str
                    else "UNAVAILABLE"
                )
                current[symbol] = (
                    normalized_price, timestamp, status, raw_health,
                )
                continue
            if intraday.overlay is IntradayOverlay.INTRADAY_FEED_UNAVAILABLE:
                all_realtime = False
                overlays[symbol] = None
                normalized_price = intraday.price
                status = "PAUSED_MARKET_NOT_REALTIME"
            else:
                overlays[symbol] = (
                    None if intraday.overlay is IntradayOverlay.NONE
                    else intraday.overlay.value
                )
                normalized_price = intraday.price
                status = resolved_status
                style = _OVERLAY_ALERT_STYLE.get(intraday.overlay)
                if style is not None and formal.as_of_trading_date is not None:
                    desired_alerts.append(AlertInput(
                        trading_date=now.date(),
                        symbol=symbol,
                        state=intraday.overlay.value,
                        strategy_version=formal.strategy_version,
                        level=style[0],
                        label=style[1],
                        evidence=intraday.to_dict()["evidence"],
                    ))
            raw_health = (
                str(raw.get("health_status"))
                if raw is not None and type(raw.get("health_status")) is str
                else "UNAVAILABLE"
            )
            current[symbol] = (normalized_price, timestamp, status, raw_health)

        self._health["intraday"] = "REALTIME" if all_realtime else "UNAVAILABLE"
        if all_realtime:
            self._errors.pop("intraday", None)
        else:
            self._errors["intraday"] = "one or more intraday quotes are not realtime"
        self._sync_overlay_alerts(desired_alerts)
        snapshot = self._build_snapshot(now)
        for item in snapshot["items"]:  # type: ignore[index]
            symbol = str(item["symbol"])
            price, timestamp, status, health_status = current.get(
                symbol, (None, None, "PAUSED_MARKET_NOT_REALTIME", "UNAVAILABLE"),
            )
            item["current_price"] = price
            item["current_price_time"] = timestamp
            item["intraday_health_status"] = health_status
            item["intraday_overlay"] = overlays.get(symbol)
            item["execution_status"] = status
        self._publish(snapshot)
        return self.snapshot()

    def _withdraw_intraday(
        self,
        status: str,
        now: datetime | None,
        error: Exception,
    ) -> None:
        self._health["intraday"] = "UNAVAILABLE"
        self._errors["intraday"] = self._safe_error(error)
        if self._alert_store is not None:
            try:
                self._alert_store.retract_overlays(status)
            except Exception as alert_error:
                self._health["alerts"] = "BLOCKED"
                self._errors["alerts"] = self._safe_error(alert_error)
        effective = self._safe_now() if now is None else now
        snapshot = self._build_snapshot(effective)
        for item in snapshot["items"]:  # type: ignore[index]
            item["intraday_overlay"] = None
            item["current_price"] = None
            item["current_price_time"] = None
            item["intraday_health_status"] = "UNAVAILABLE"
            item["execution_status"] = "PAUSED_MARKET_NOT_REALTIME"
        self._publish(snapshot)

    def _sync_overlay_alerts(self, desired: Sequence[AlertInput]) -> None:
        store = self._alert_store
        if store is None:
            return
        try:
            active = tuple(
                item for item in store.current()
                if item.scope == "INTRADAY" and not item.retracted
            )
            def projection_key(item: object) -> tuple[date, str, str, str]:
                return (
                    item.trading_date, item.symbol, item.state,
                    item.strategy_version,
                )

            def input_key(item: AlertInput) -> tuple[date, str, str, str]:
                return (
                    item.trading_date, item.symbol, item.state,
                    item.strategy_version,
                )

            active_by_key = {projection_key(item): item for item in active}
            desired_by_key = {input_key(item): item for item in desired}
            for key in active_by_key.keys() - desired_by_key.keys():
                store.retract_overlay(
                    active_by_key[key].alert_id, "INTRADAY_STATE_CHANGED",
                )
            for key in desired_by_key.keys() - active_by_key.keys():
                store.publish_overlay(desired_by_key[key])
            self._health["alerts"] = "OK"
            self._errors.pop("alerts", None)
        except Exception as error:
            self._health["alerts"] = "BLOCKED"
            self._errors["alerts"] = self._safe_error(error)

    # ---- Publishing helpers ------------------------------------------

    def _candidate_snapshot(
        self,
        now: datetime,
        *,
        watchlist: tuple[SwingWatchItem, ...] | None = None,
        history: tuple[DailyBar, ...] | None = None,
        projection: PortfolioProjection | None = None,
        formal: dict[str, SwingDecision] | None = None,
        health: dict[str, str] | None = None,
        errors: dict[str, str] | None = None,
        projection_is_explicit: bool = False,
    ) -> dict[str, object]:
        """Build a snapshot from staged state without exposing that state."""
        self.publish_condition.acquire()
        old = (
            self._watchlist,
            self._history,
            self._portfolio_projection,
            self._formal,
            self._health,
            self._errors,
        )
        try:
            if watchlist is not None:
                self._watchlist = watchlist
            if history is not None:
                self._history = history
            if projection_is_explicit:
                self._portfolio_projection = projection
            if formal is not None:
                self._formal = formal
            if health is not None:
                self._health = health
            if errors is not None:
                self._errors = errors
            return self._build_snapshot(now)
        finally:
            (
                self._watchlist,
                self._history,
                self._portfolio_projection,
                self._formal,
                self._health,
                self._errors,
            ) = old
            self.publish_condition.release()

    def _write_watchlist(
        self, watchlist: Sequence[SwingWatchItem],
    ) -> None:
        payload = {
            "schema_version": 1,
            "items": [
                {"symbol": item.symbol, "enabled": item.enabled}
                for item in watchlist
            ],
        }
        destination = self.paths.watchlist
        destination.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=False,
        ) + "\n").encode("utf-8")
        with _SiblingFileLock(destination, shared=False):
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=destination.parent,
                    prefix=f".{destination.name}.", suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
                temporary = None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink()
                    except FileNotFoundError:
                        pass

    def _build_snapshot(self, now: datetime) -> dict[str, object]:
        current = self.published if hasattr(self, "published") else {}
        current_items = {
            str(item.get("symbol")): item
            for item in current.get("items", [])
            if isinstance(item, Mapping)
        }
        items: list[dict[str, object]] = []
        for watch in self._watchlist:
            if not watch.enabled:
                continue
            formal = self._formal.get(watch.symbol)
            if formal is None:
                continue
            previous = current_items.get(watch.symbol, {})
            metadata = self._metadata.get(watch.symbol)
            items.append({
                "symbol": watch.symbol,
                "name": metadata.name if metadata is not None else watch.symbol,
                "formal_state": formal.state.value,
                "formal_decision": formal.to_dict(),
                "signal_data_date": (
                    formal.as_of_trading_date.isoformat()
                    if formal.as_of_trading_date is not None else None
                ),
                "blocked_reasons": list(formal.blocked_reasons),
                "execution_status": self._execution_status(
                    formal, now,
                    market_realtime=self._health["intraday"] == "REALTIME",
                ),
                "intraday_overlay": previous.get("intraday_overlay"),
                "current_price": previous.get("current_price"),
                "current_price_time": previous.get("current_price_time"),
                "intraday_health_status": previous.get(
                    "intraday_health_status", "UNAVAILABLE",
                ),
            })
        alert_items, active_alerts = self._alert_snapshot(now)
        return {
            "mode": "MONITOR_ONLY",
            "auto_trade": False,
            "strategy": (
                self._strategy.strategy_version if self._strategy is not None
                else "UNAVAILABLE"
            ),
            "revision": self.revision,
            "generated_at": now.isoformat(),
            "as_of_trading_date": self._history_as_of(),
            "health": copy.deepcopy(self._health),
            "errors": copy.deepcopy(self._errors),
            "portfolio": (
                self._portfolio_projection.to_dict()
                if self._portfolio_projection is not None else None
            ),
            "items": items,
            "alerts": alert_items,
            "active_alerts": active_alerts,
            "read_only_market_data": True,
        }

    def _publish(
        self,
        snapshot: Mapping[str, object],
        *,
        daily_upserts: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        force_reset: bool = False,
    ) -> None:
        value = copy.deepcopy(dict(snapshot))
        with self.publish_condition:
            self._publish_locked(
                value, daily_upserts=daily_upserts, force_reset=force_reset,
            )

    def _publish_locked(
        self,
        snapshot: Mapping[str, object],
        *,
        daily_upserts: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
        force_reset: bool = False,
    ) -> None:
        value = copy.deepcopy(dict(snapshot))
        self.revision += 1
        value["revision"] = self.revision
        self.published = value
        event = copy.deepcopy(value)
        event.update({
            "event": "reset" if force_reset else "update",
            "reset": bool(force_reset),
            "force_reset": bool(force_reset),
            "daily_upserts": copy.deepcopy(dict(daily_upserts or {})),
        })
        self.events.append(event)
        self.publish_condition.notify_all()

    def _publish_component_failure(
        self,
        component: str,
        status: str,
        error: Exception,
        *,
        now: datetime | None,
    ) -> None:
        self._health[component] = status
        self._errors[component] = self._safe_error(error)
        self._publish(self._build_snapshot(self._safe_now() if now is None else now))

    def _select_event_locked(self, after_revision: int) -> dict[str, object] | None:
        if after_revision > self.revision:
            return self._reset_event_locked()
        if after_revision == self.revision:
            return None
        if not self.events or after_revision < int(self.events[0]["revision"]) - 1:
            return self._reset_event_locked()
        selected = next(
            (
                event for event in self.events
                if int(event["revision"]) > after_revision
            ),
            None,
        )
        if selected is None:
            return None
        if (
            selected.get("as_of_trading_date")
            != self.published.get("as_of_trading_date")
        ):
            return self._reset_event_locked()
        return selected

    def _reset_event_locked(self) -> dict[str, object]:
        result = copy.deepcopy(self.published)
        result.update({
            "event": "reset",
            "reset": True,
            "force_reset": True,
            "daily_upserts": {},
        })
        return result

    # ---- Portfolio/alert mutation recomputation -----------------------

    def _rebuild_after_portfolio_mutation(self, now: datetime) -> None:
        self._load_portfolio_projection(now)
        self._recompute_formal(now, publish_alerts=True)
        self._publish(self._build_snapshot(now))

    def _refresh_alert_snapshot(self, now: datetime) -> None:
        self._publish(self._build_snapshot(now))

    def _publish_formal_alerts(
        self,
        now: datetime,
        next_by_symbol: Mapping[str, date | None],
    ) -> None:
        error = self._persist_formal_alerts(
            self._formal, next_by_symbol, self._health["portfolio"],
        )
        if error is None:
            self._health["alerts"] = "OK"
            self._errors.pop("alerts", None)
        else:
            self._health["alerts"] = "BLOCKED"
            self._errors["alerts"] = error

    def _persist_formal_alerts(
        self,
        formal_by_symbol: Mapping[str, SwingDecision],
        next_by_symbol: Mapping[str, date | None],
        portfolio_health: str,
    ) -> str | None:
        store = self._alert_store
        if store is None:
            return "alert store is unavailable"
        try:
            for symbol, formal in formal_by_symbol.items():
                style = _FORMAL_ALERT_STYLE.get(formal.state)
                if style is None or formal.as_of_trading_date is None:
                    continue
                if (
                    formal.state in _PORTFOLIO_DEPENDENT_STATES
                    and portfolio_health != "OK"
                ):
                    continue
                if formal.state is SwingState.TRIAL_ENTRY_CANDIDATE and (
                    formal.valid_for_trading_date != next_by_symbol.get(symbol)
                ):
                    continue
                store.publish_formal(AlertInput(
                    trading_date=formal.as_of_trading_date,
                    symbol=symbol,
                    state=formal.state.value,
                    strategy_version=formal.strategy_version,
                    level=style[0],
                    label=style[1],
                    evidence=formal.to_dict()["evidence"],
                ))
        except Exception as error:
            return self._safe_error(error)
        return None

    def _alert_snapshot(
        self, now: datetime, *, include_retracted: bool = False,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        store = self._alert_store
        if store is None or self._health["alerts"] != "OK":
            return [], []
        try:
            current = store.current(include_retracted=include_retracted)
        except Exception:
            return [], []
        items: list[dict[str, object]] = []
        active: list[dict[str, object]] = []
        enabled_symbols = {
            item.symbol for item in self._watchlist if item.enabled
        }
        for item in current:
            currently_active = item.active_notification
            if item.scope == "FORMAL":
                formal = self._formal.get(item.symbol)
                currently_active = bool(
                    currently_active
                    and formal is not None
                    and formal.as_of_trading_date == item.trading_date
                    and formal.state.value == item.state
                    and formal.strategy_version == item.strategy_version
                    and formal.state in _ACTION_STATES
                )
                expected = self._last_completed_trading_date(now)
                if formal is None or formal.as_of_trading_date != expected:
                    currently_active = False
                elif formal.state is SwingState.TRIAL_ENTRY_CANDIDATE:
                    currently_active = bool(
                        currently_active
                        and formal.valid_for_trading_date == now.date()
                        and now.timetz().replace(tzinfo=None) <= time(15, 0)
                        and self._health["portfolio"] == "OK"
                    )
                elif formal.state in {
                    SwingState.ADD_CANDIDATE,
                    SwingState.REDUCE_CANDIDATE,
                } and self._health["portfolio"] != "OK":
                    currently_active = False
            else:
                formal = self._formal.get(item.symbol)
                currently_active = bool(
                    currently_active
                    and item.symbol in enabled_symbols
                    and item.trading_date == now.date()
                    and self._health["intraday"] == "REALTIME"
                    and formal is not None
                    and formal.strategy_version == item.strategy_version
                )
                if (
                    currently_active
                    and item.state
                    == IntradayOverlay.APPROACHING_ENTRY_ZONE.value
                ):
                    currently_active = bool(
                        formal is not None
                        and formal.state is SwingState.TRIAL_ENTRY_CANDIDATE
                        and self._execution_status(
                            formal, now, market_realtime=True,
                        ) == "READY_TO_EXECUTE"
                    )
                elif (
                    currently_active
                    and item.state
                    == IntradayOverlay.PREDEFINED_STOP_TOUCHED.value
                ):
                    currently_active = self._has_position(item.symbol)
            payload = item.to_dict()
            payload["currently_active"] = currently_active
            payload["active_notification"] = currently_active
            items.append(payload)
            if currently_active:
                active.append(copy.deepcopy(payload))
        return items, active

    # ---- Calendar, history, and validation utilities ------------------

    def _safe_now(self) -> datetime:
        try:
            return self._local_time(self.clock())
        except Exception as error:
            self._health["service"] = "CLOCK_FAILED"
            self._errors["service"] = self._safe_error(error)
            latest = max(
                (bar.observed_at for bar in self._history),
                default=datetime(1970, 1, 1, tzinfo=SHANGHAI),
            )
            return latest.astimezone(SHANGHAI)

    @staticmethod
    def _local_time(value: object) -> datetime:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise SwingServiceError("clock must return a timezone-aware datetime")
        try:
            return value.astimezone(SHANGHAI)
        except Exception as error:
            raise SwingServiceError("clock timezone could not be converted") from error

    def _last_completed_trading_date(self, now: datetime) -> date | None:
        if self._closed_dates is None:
            return None
        candidate = now.date()
        if (
            not self._is_trading_date(candidate)
            or now.timetz().replace(tzinfo=None) < _FINAL_DAILY_TIME
        ):
            candidate -= timedelta(days=1)
        for _ in range(370):
            if self._is_trading_date(candidate):
                return candidate
            candidate -= timedelta(days=1)
        return None

    def _next_trading_date(self, value: date | None) -> date | None:
        if value is None or self._closed_dates is None:
            return None
        candidate = value + timedelta(days=1)
        for _ in range(370):
            if self._is_trading_date(candidate):
                return candidate
            candidate += timedelta(days=1)
        return None

    def _is_trading_date(self, value: date) -> bool:
        return (
            self._closed_dates is not None
            and value.weekday() < 5
            and value not in self._closed_dates
        )

    def _portfolio_as_of(self, now: datetime) -> date:
        if self._is_trading_date(now.date()):
            return now.date()
        return self._last_completed_trading_date(now) or self._history_as_of_date() or now.date()

    def _history_as_of_date(self) -> date | None:
        enabled = {item.symbol for item in self._watchlist if item.enabled}
        latest = [
            bar.trading_date for bar in self._history if bar.symbol in enabled
        ]
        return max(latest, default=None)

    def _history_as_of(self) -> str | None:
        value = self._history_as_of_date()
        return value.isoformat() if value is not None else None

    def _snapshot_as_of(self) -> str | None:
        with self.publish_condition:
            value = self.published.get("as_of_trading_date")
        return value if type(value) is str else None

    def _bars_by_symbol(
        self, history: Sequence[DailyBar] | None = None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        result: dict[str, list[DailyBar]] = {}
        for bar in self._history if history is None else history:
            result.setdefault(bar.symbol, []).append(bar)
        return {symbol: tuple(bars) for symbol, bars in result.items()}

    def _latest_marks(
        self, history: Sequence[DailyBar] | None = None,
    ) -> dict[str, float]:
        marks: dict[str, float] = {}
        for symbol, bars in self._bars_by_symbol(history).items():
            if bars:
                marks[symbol] = bars[-1].close
        return marks

    @staticmethod
    def _group_bar_payloads(
        bars: Sequence[DailyBar],
    ) -> dict[str, list[dict[str, object]]]:
        result: dict[str, list[dict[str, object]]] = {}
        for bar in bars:
            result.setdefault(bar.symbol, []).append(bar.to_dict())
        return result

    def _has_position(self, symbol: str) -> bool:
        projection = self._portfolio_projection
        return bool(
            projection is not None
            and symbol in projection.positions
            and projection.positions[symbol].shares > 0
        )

    def _execution_status(
        self,
        formal: SwingDecision,
        now: datetime,
        *,
        market_realtime: bool,
    ) -> str:
        if not market_realtime:
            return "PAUSED_MARKET_NOT_REALTIME"
        if formal.state in _PORTFOLIO_DEPENDENT_STATES:
            expected = self._last_completed_trading_date(now)
            if (
                self._health["daily"] != "OK"
                or expected is None
                or formal.as_of_trading_date != expected
            ):
                return "PAUSED_DAILY_DATA"
        if formal.state in _PORTFOLIO_DEPENDENT_STATES and (
            self._health["portfolio"] != "OK"
        ):
            return "PAUSED_PORTFOLIO_BLOCKED"
        if formal.state is SwingState.TRIAL_ENTRY_CANDIDATE:
            if formal.valid_for_trading_date is None:
                return "PAUSED_PLAN_INVALID"
            if now.date() > formal.valid_for_trading_date:
                return "PAUSED_PLAN_EXPIRED"
            if now.date() < formal.valid_for_trading_date:
                return "WAITING_NEXT_TRADING_DAY"
            if now.timetz().replace(tzinfo=None) > time(15, 0):
                return "PAUSED_PLAN_EXPIRED"
        if formal.state in _ACTION_STATES:
            return "READY_TO_EXECUTE"
        return "OBSERVE_ONLY"

    def _validated_enabled_symbol(self, symbol: object) -> str:
        if (
            type(symbol) is not str
            or len(symbol) != 6
            or not symbol.isascii()
            or not symbol.isdigit()
        ):
            raise SwingServiceError("symbol must be six ASCII digits")
        if symbol not in {item.symbol for item in self._watchlist if item.enabled}:
            raise SwingServiceError(f"symbol is not enabled: {symbol}")
        return symbol

    def _require_ledger(self) -> PortfolioLedger:
        if self._ledger is None:
            raise SwingServiceError("portfolio ledger is unavailable")
        return self._ledger

    def _require_alert_store(self) -> SwingAlertStore:
        if self._alert_store is None:
            raise SwingServiceError("alert store is unavailable")
        return self._alert_store

    @staticmethod
    def _safe_error(error: Exception) -> str:
        try:
            text = str(error)
        except Exception:
            text = type(error).__name__
        text = " ".join(text.split())
        return (text or type(error).__name__)[:1024]

    def _refresh_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.refresh_once()
                self.refresh_intraday()
            except Exception as error:
                with self.producer_lock:
                    self._publish_component_failure(
                        "service", "PRODUCER_FAILED", error, now=None,
                    )
            self._stop_event.wait(self.refresh_interval)


__all__ = ["DailyCollector", "SwingPaths", "SwingService", "SwingServiceError"]
