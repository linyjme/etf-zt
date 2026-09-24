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
import hashlib
import hmac
import importlib.util
import math
import os
from pathlib import Path
import tempfile
import threading
import time as monotonic_time
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .constants import DEFAULT_SWING_HISTORY_COUNT, REALTIME_MAX_AGE_SECONDS
from .etf_metadata import EtfMetadata, EtfMetadataStore, load_pending_etfs
from .holdings_snapshot import read_holdings_snapshot
from .market_data import MarketHealthClassifier, load_closed_dates
from .swing_alerts import AlertInput, SwingAlertStore
from .swing_backtest import (
    SwingBacktestError,
    SwingBacktester,
    _curve_metric_values,
)
from .swing_config import (
    SwingStrategyConfig,
    SwingWatchItem,
    load_strategy,
    load_watchlist,
)
from .swing_crosscheck import (
    INDEPENDENT_SOURCE_LABEL,
    CrosscheckReceipt,
    crosscheck_history,
    write_receipts,
)
from .swing_data import (
    DailyBar,
    DailyHistoryStore,
    IndexHistoryStore,
    _SiblingFileLock,
)
from .swing_minutes import expected_complete_minutes, parse_minute_payload
from .valuation import ValuationStore, classify_valuation_stage
from .swing_indicators import (
    DEFAULT_MINIMUM_BARS,
    INDICATOR_SCHEMA_VERSION,
    IndicatorInputError,
    calculate_indicator_context,
    calculate_indicator_snapshot,
)
from .swing_opportunities import (
    opportunity_timeline,
)
from .swing_shadow import (
    ShadowContext,
    ShadowVariant,
    evaluate_hybrid_shadow,
    evaluate_shadow,
    infer_shadow_regime,
    load_shadow_config,
)
from .swing_quality import (
    assess_verified_quality,
    summarize_common_history,
    summarize_history_quality,
    summarize_strategy_diagnostics,
)
from .swing_portfolio import (
    InitialPositionInput,
    PortfolioLedger,
    PortfolioLedgerError,
    PortfolioEvent,
    PortfolioEventType,
    PortfolioPosition,
    PortfolioProjection,
    TradeInput,
    load_projection,
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
from .swing_v11 import (
    V11_F2_CONSECUTIVE_LOSSES,
    V11Context,
    V11Position,
    calculate_relative_strength_20,
    calculate_v11_environment,
    calculate_v11_indicators,
    classify_v11_environment,
    evaluate_v11,
    evaluate_v11_position,
    load_v11_config,
    normalize_v11_indicators,
    parse_v11_observed_at,
    validate_v11_metadata,
)
from .swing_v11_state import (
    V11StateStore,
    advance_environment_state,
    normalize_environment_state,
    normalize_position_state,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_FINAL_DAILY_TIME = time(15, 10)
_ENVIRONMENT_INDEX_CODES = ("000300", "000852")
_BACKTEST_CACHE_SCHEMA_VERSION = 4
_BACKTEST_ENGINE_VERSION = "SWING_BACKTEST_ENGINE_V5"
_DEFAULT_HISTORY_COUNT = DEFAULT_SWING_HISTORY_COUNT
_MAX_DAILY_QUOTE_LIMIT = 10_000
_MAX_ALERT_HISTORY_LIMIT = 500
_REALTIME_FUTURE_SKEW_SECONDS = 5.0
_TRADE_FUTURE_SKEW_SECONDS = 5.0
_READ_MODEL_KEY = "_published_read_model"
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
_OVERLAY_ALERT_STATES = frozenset(
    overlay.value for overlay in _OVERLAY_ALERT_STYLE
)


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
        valuation_path: Path | None = None,
        shadow_config_path: Path | None = None,
        index_history: Mapping[str, Sequence[DailyBar]] | None = None,
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
        self.valuation_path = Path(valuation_path) if valuation_path is not None else self.paths.metadata.with_name("valuation.json")
        self._valuation_store = ValuationStore(self.valuation_path)
        self.shadow_config_path = (
            Path(shadow_config_path)
            if shadow_config_path is not None
            else Path(__file__).resolve().parents[2] / "data/swing/shadow_strategy.json"
        )
        self._shadow_config = None
        self._shadow_config_error: str | None = None
        try:
            self._shadow_config = load_shadow_config(self.shadow_config_path)
        except Exception as error:
            self._shadow_config_error = self._safe_error(error)
        self.producer_lock = threading.Lock()
        self.publish_condition = threading.Condition(threading.Lock())
        self.events: deque[dict[str, object]] = deque(maxlen=event_limit)
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self.revision = 0
        self._watchlist: tuple[SwingWatchItem, ...] = ()
        self._strategy: SwingStrategyConfig | None = None
        self._v11_config = None
        self._v11_state: dict[str, object] = {
            "positions": {}, "environment": normalize_environment_state(None),
        }
        self._metadata: Mapping[str, EtfMetadata] = {}
        self._pending_instruments: dict[str, object] = {}
        self._holdings_snapshot: dict[str, object] = {
            "status": "ABSENT", "snapshot": None, "error": None,
            "read_only": True, "strategy_ready": False,
        }
        self._closed_dates: frozenset[date] | None = None
        self._history_store: DailyHistoryStore | None = None
        self._ledger: PortfolioLedger | None = None
        self._alert_store: SwingAlertStore | None = None
        self._portfolio_projection: PortfolioProjection | None = None
        self._history: tuple[DailyBar, ...] = ()
        self._index_history: dict[str, tuple[DailyBar, ...]] = {
            str(code): tuple(value)
            for code, value in (index_history or {}).items()
        }
        self._formal: dict[str, SwingDecision] = {}
        self._health: dict[str, str] = {
            "service": "STARTING",
            "configuration": "UNKNOWN",
            "calendar": "UNKNOWN",
            "daily": "UNKNOWN",
            "minute_crosscheck": "NOT_RUN",
            "independent_crosscheck": "NOT_RUN",
            "portfolio": "UNKNOWN",
            "alerts": "UNKNOWN",
            "intraday": "UNAVAILABLE",
        }
        self._errors: dict[str, str] = {}
        self.published: dict[str, object] = {}
        self._published_watchlist_view: dict[str, object] = {}
        self._published_portfolio_view: dict[str, object] = {}
        self._published_alerts_current: dict[str, object] = {}
        self._published_alerts_history: dict[str, object] = {}
        self._backtest_registry_lock = threading.Lock()
        self._backtest_key_locks: dict[str, threading.Lock] = {}
        bootstrap = self._bootstrap()
        self._install_published(bootstrap, revision=0)
        self._persist_v11_state(bootstrap)

    # ---- Public read and lifecycle API ---------------------------------

    def snapshot(self) -> dict[str, object]:
        with self.publish_condition:
            return copy.deepcopy(self.published)

    def health(self) -> dict[str, object]:
        snapshot = self.snapshot()
        health = dict(snapshot.get("health", {}))
        ok = all(value in {"OK", "REALTIME", "NOT_RUN"} for value in health.values())
        environment_history = snapshot.get("environment_history")
        if not isinstance(environment_history, Mapping):
            environment_history = {code: None for code in _ENVIRONMENT_INDEX_CODES}
        return {
            "status": "ok" if ok else "degraded",
            "ok": ok,
            "mode": "MONITOR_ONLY",
            "revision": snapshot["revision"],
            "components": health,
            "errors": copy.deepcopy(snapshot.get("errors", {})),
            "environment_history": {
                code: environment_history.get(code) for code in _ENVIRONMENT_INDEX_CODES
            },
        }

    def watchlist(self) -> dict[str, object]:
        with self.publish_condition:
            return copy.deepcopy(self._published_watchlist_view)

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
            if enabled and not self._watch_history_ready(symbol):
                raise SwingServiceError("该标的已完成日线尚未就绪，请先补齐并校验最少日线，再启用监控")
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

    def _watch_history_ready(self, symbol: str) -> bool:
        return bool(
            self._strategy is not None
            and self._health.get("daily") == "OK"
            and sum(bar.symbol == symbol for bar in self._history)
            >= self._strategy.minimum_daily_bars
        )

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
            return copy.deepcopy(self._published_portfolio_view)

    def backtest(
        self, symbol: str | None, scope: str,
    ) -> dict[str, object]:
        """Return one content-addressed real backtest without publishing state."""
        if scope not in {"symbol", "portfolio"}:
            raise SwingServiceError("scope must be symbol or portfolio")
        if scope == "symbol":
            if symbol is None:
                raise SwingServiceError("symbol is required for symbol scope")
            normalized_symbol = self._validated_enabled_symbol(symbol)
            selected_symbols = (normalized_symbol,)
        else:
            if symbol is not None:
                raise SwingServiceError("portfolio scope does not accept symbol")
            selected_symbols = tuple(sorted(
                item.symbol for item in self._watchlist if item.enabled
            ))
            if not selected_symbols:
                raise SwingServiceError("portfolio scope requires enabled symbols")
            normalized_symbol = None

        with self.publish_condition:
            strategy = self._strategy
            history = tuple(
                bar for bar in self._history if bar.symbol in selected_symbols
            )
            metadata = {
                item: self._metadata[item]
                for item in selected_symbols if item in self._metadata
            }
        if strategy is None or len(metadata) != len(selected_symbols):
            return self._backtest_unavailable(
                scope, normalized_symbol, "DATA_UNAVAILABLE",
                "CONFIGURATION_OR_METADATA_UNAVAILABLE",
            )
        histories = {
            item: tuple(bar for bar in history if bar.symbol == item)
            for item in selected_symbols
        }
        canonical_history = [
            bar.to_dict()
            for item in selected_symbols for bar in histories[item]
        ]
        history_digest = self._canonical_digest(canonical_history)
        latest = max(
            (bar.trading_date for bar in history), default=None,
        )
        execution_contract = {
            "strategy": {
                field: getattr(strategy, field)
                for field in sorted(strategy.__dataclass_fields__)
            },
            "trading": {
                item: metadata[item].trading.to_dict()
                for item in selected_symbols
            },
            "engine_version": _BACKTEST_ENGINE_VERSION,
            "cache_schema_version": _BACKTEST_CACHE_SCHEMA_VERSION,
            "execution_assumptions": {
                item: SwingBacktester(
                    strategy, metadata[item].trading,
                )._execution_assumptions()
                for item in selected_symbols
            },
            "portfolio_policies": {
                "cash": "ONE_SHARED_CASH_BALANCE",
                "priority": "EXIT,REDUCE,ADD,TRIAL_ENTRY",
                "common_range": "INTERSECTION_AFTER_WARMUP",
                "walk_forward_variants": 81,
                "walk_forward_selection": None,
            },
            "initial_cash": 100_000.0,
        }
        assumptions_digest = self._canonical_digest(execution_contract)
        strategy_parameters = {
            field: getattr(strategy, field)
            for field in sorted(strategy.__dataclass_fields__)
        }
        first_symbol = selected_symbols[0]
        result_assumptions = dict(SwingBacktester(
            strategy, metadata[first_symbol].trading,
        )._execution_assumptions())
        unavailable_result_assumptions = dict(result_assumptions)
        if scope == "portfolio":
            unavailable_result_assumptions["portfolio_cash_model"] = (
                "ONE_SHARED_CASH_BALANCE"
            )
            result_assumptions.update({
                "portfolio_cash_model": "ONE_SHARED_CASH_BALANCE",
                "action_priority": "EXIT,REDUCE,ADD,TRIAL_ENTRY",
                "common_range_policy": "INTERSECTION_AFTER_WARMUP",
                "trading_metadata_by_symbol": {
                    item: metadata[item].trading.to_dict()
                    for item in selected_symbols
                },
            })
        cache_key = {
            "cache_schema_version": _BACKTEST_CACHE_SCHEMA_VERSION,
            "engine_version": _BACKTEST_ENGINE_VERSION,
            "scope": scope,
            "symbol": normalized_symbol,
            "selected_symbols": list(selected_symbols),
            "strategy_version": strategy.strategy_version,
            "latest_trading_date": (
                None if latest is None else latest.isoformat()
            ),
            "history_digest": history_digest,
            "execution_assumptions_digest": assumptions_digest,
            "strategy_parameters_digest": self._canonical_digest(
                strategy_parameters,
            ),
            "result_assumptions_digest": self._canonical_digest(
                result_assumptions,
            ),
            "unavailable_result_assumptions_digest": self._canonical_digest(
                unavailable_result_assumptions,
            ),
        }
        cache_name = self._canonical_digest(cache_key) + ".json"
        cache_path = self.paths.backtests / cache_name
        self.paths.backtests.mkdir(parents=True, exist_ok=True)
        signing_key = self._backtest_signing_key()
        with self._backtest_registry_lock:
            key_lock = self._backtest_key_locks.setdefault(
                cache_name, threading.Lock(),
            )
        with key_lock:
            with _SiblingFileLock(cache_path, shared=False):
                cached = self._read_backtest_cache(
                    cache_path, cache_key, signing_key,
                )
                if cached is not None:
                    return cached
                result, metric_evidence = self._run_backtest(
                    scope,
                    normalized_symbol,
                    selected_symbols,
                    histories,
                    metadata,
                    strategy,
                )
                self._write_backtest_cache(
                    cache_path, cache_key, result, metric_evidence, signing_key,
                )
                return copy.deepcopy(result)

    @staticmethod
    def _canonical_digest(value: object) -> str:
        return hashlib.sha256(SwingService._canonical_bytes(value)).hexdigest()

    @staticmethod
    def _canonical_bytes(value: object) -> bytes:
        return json.dumps(
            value, ensure_ascii=True, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")

    @classmethod
    def _cache_hmac(
        cls, signing_key: bytes, envelope: Mapping[str, object],
    ) -> str:
        # The persisted key is the authenticity boundary: compromise of both it
        # and metric evidence permits cache forgery, as in the standard HMAC
        # threat model. Ordinary cache-file edits cannot produce a valid tag.
        return hmac.new(
            signing_key, cls._canonical_bytes(envelope), hashlib.sha256,
        ).hexdigest()

    def _backtest_signing_key(self) -> bytes:
        path = self.paths.backtests.with_name(
            f".{self.paths.backtests.name}.signing-key",
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SiblingFileLock(path, shared=False):
            try:
                key = path.read_bytes()
            except OSError:
                key = b""
            if len(key) == 32:
                return key
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "wb", dir=path.parent, prefix=f".{path.name}.",
                    suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    key = os.urandom(32)
                    handle.write(key)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temporary, 0o600)
                except OSError:
                    pass
                os.replace(temporary, path)
                temporary = None
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                return key
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    @staticmethod
    def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @classmethod
    def _read_backtest_cache(
        cls,
        path: Path,
        cache_key: Mapping[str, object],
        signing_key: bytes,
    ) -> dict[str, object] | None:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=cls._strict_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"nonfinite JSON value: {value}")
                ),
            )
            if (
                type(payload) is not dict
                or set(payload) != {
                    "schema_version", "cache_key", "payload_sha256", "result",
                    "metric_evidence", "hmac_sha256",
                }
                or type(payload.get("schema_version")) is not int
                or payload.get("schema_version") != _BACKTEST_CACHE_SCHEMA_VERSION
                or type(payload.get("cache_key")) is not dict
                or payload.get("cache_key") != dict(cache_key)
                or type(payload.get("payload_sha256")) is not str
                or len(payload["payload_sha256"]) != 64
                or any(character not in "0123456789abcdef"
                       for character in payload["payload_sha256"])
                or type(payload.get("result")) is not dict
                or type(payload.get("metric_evidence")) not in (
                    dict, type(None),
                )
                or type(payload.get("hmac_sha256")) is not str
                or len(payload["hmac_sha256"]) != 64
                or any(character not in "0123456789abcdef"
                       for character in payload["hmac_sha256"])
            ):
                return None
            result = payload["result"]
            signed = {
                key: payload[key]
                for key in (
                    "schema_version", "cache_key", "payload_sha256", "result",
                    "metric_evidence",
                )
            }
            if (
                payload["payload_sha256"] != cls._canonical_digest(result)
                or not hmac.compare_digest(
                    payload["hmac_sha256"], cls._cache_hmac(signing_key, signed),
                )
                or not cls._valid_backtest_result(result, cache_key)
                or not cls._valid_metric_evidence(
                    result, payload["metric_evidence"], cache_key,
                )
            ):
                return None
            return copy.deepcopy(result)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _write_backtest_cache(
        cls,
        path: Path,
        cache_key: Mapping[str, object],
        result: Mapping[str, object],
        metric_evidence: Mapping[str, object] | None,
        signing_key: bytes,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                canonical_result = dict(result)
                signed = {
                    "schema_version": _BACKTEST_CACHE_SCHEMA_VERSION,
                    "cache_key": dict(cache_key),
                    "payload_sha256": cls._canonical_digest(canonical_result),
                    "result": canonical_result,
                    "metric_evidence": (
                        None if metric_evidence is None
                        else copy.deepcopy(dict(metric_evidence))
                    ),
                }
                envelope = {
                    **signed,
                    "hmac_sha256": cls._cache_hmac(signing_key, signed),
                }
                json.dump(envelope, handle, ensure_ascii=True, allow_nan=False,
                    sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @classmethod
    def _valid_backtest_result(
        cls,
        result: Mapping[str, object],
        cache_key: Mapping[str, object],
    ) -> bool:
        unavailable_keys = {
            "schema_version", "scope", "symbol", "status", "reason",
            "outperformance", "read_only",
        }
        symbol_keys = {
            "schema_version", "strategy_version", "strategy_parameters",
            "execution_assumptions", "symbol", "status", "reason",
            "initial_cash", "cash", "ending_equity", "start_date", "end_date",
            "trades", "rejections", "completed_round_trips", "round_trips",
            "open_position_shares", "uncompleted_leg_count", "benchmark",
            "outperformance", "metrics", "metric_conventions", "scope",
            "read_only",
        }
        portfolio_keys = {
            "schema_version", "scope", "strategy_version", "status", "reason",
            "symbols", "initial_cash", "cash", "ending_equity",
            "common_start_date", "common_end_date", "event_dates", "trades",
            "rejections", "rejection_counts", "completed_round_trips",
            "round_trips", "open_position_shares", "uncompleted_leg_count",
            "max_equity_weight", "max_planned_risk", "metrics", "baseline",
            "baseline_weights", "outperformance", "strategy_parameters",
            "execution_assumptions", "walk_forward", "symbol", "read_only",
        }
        keys = set(result)
        if keys == unavailable_keys:
            expected_keys = unavailable_keys
        elif cache_key.get("scope") == "symbol":
            expected_keys = symbol_keys
        else:
            expected_keys = portfolio_keys
        if keys != expected_keys:
            return False
        if (
            type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1
            or result.get("scope") != cache_key.get("scope")
            or result.get("symbol") != cache_key.get("symbol")
            or type(result.get("status")) is not str
            or result.get("status") not in {
                "OK", "INSUFFICIENT_SAMPLE", "DATA_UNAVAILABLE",
            }
            or result.get("read_only") is not True
            or type(result.get("reason")) not in (str, type(None))
            or type(result.get("outperformance")) not in (int, float, type(None))
            or (
                result.get("status") != "OK"
                and result.get("outperformance") is not None
            )
            or not cls._strict_json_tree(result)
        ):
            return False
        if expected_keys != unavailable_keys:
            required_mappings = (
                "strategy_parameters", "execution_assumptions",
            )
            required_lists = ("trades", "rejections", "round_trips")
            if any(type(result.get(key)) is not dict for key in required_mappings):
                return False
            if any(type(result.get(key)) is not list for key in required_lists):
                return False
            if not cls._valid_backtest_result_components(result, cache_key):
                return False
        return True

    @classmethod
    def _valid_backtest_result_components(
        cls,
        result: Mapping[str, object],
        cache_key: Mapping[str, object],
    ) -> bool:
        strategy_keys = set(SwingStrategyConfig.__dataclass_fields__)
        assumption_keys = {
            "actual_buy_sizing_policy", "asset_type",
            "benchmark_liquidated_at_end", "benchmark_policy", "buy_fee_rate",
            "buy_fill_price_formula", "corporate_action_policy",
            "default_half_spread_ticks", "default_half_spread_ticks_rationale",
            "entry_execution_policy", "exchange", "execution_cost_order",
            "execution_day_phases", "execution_timing", "execution_volume_gate",
            "fee_formula", "fill_cap_policy", "financing_policy",
            "half_spread_ticks", "intraday_turnaround", "liquidity_budget_policy",
            "lot_size", "mark_to_market_policy", "max_volume_participation",
            "minimum_fee", "price_limit_pct", "price_limit_policy", "price_tick",
            "raw_adjusted_policy", "sell_fee_cash_policy", "sell_fee_rate",
            "sell_fill_price_formula", "sellability_policy", "sellable_delay_days",
            "signal_bar_policy", "slippage_rate", "spread_slippage_attribution",
            "stop_execution_policy", "volume_policy", "volume_unit_shares",
        }
        if set(result["strategy_parameters"]) != strategy_keys:
            return False
        try:
            parsed_strategy = SwingStrategyConfig(
                **dict(result["strategy_parameters"]),
            )
        except (TypeError, ValueError):
            return False
        canonical_strategy = {
            field: getattr(parsed_strategy, field) for field in strategy_keys
        }
        if any(
            type(result["strategy_parameters"][field])
            is not type(canonical_strategy[field])
            or result["strategy_parameters"][field] != canonical_strategy[field]
            for field in strategy_keys
        ):
            return False
        if (
            cls._canonical_digest(result["strategy_parameters"])
            != cache_key.get("strategy_parameters_digest")
            or result.get("strategy_version") != cache_key.get("strategy_version")
            or type(result.get("strategy_version")) is not str
        ):
            return False
        expected_assumptions = set(assumption_keys)
        unavailable_shape = result.get("metrics") is None
        if cache_key.get("scope") == "portfolio":
            full_portfolio_assumptions = expected_assumptions | {
                "portfolio_cash_model", "action_priority", "common_range_policy",
                "trading_metadata_by_symbol",
            }
            unavailable_portfolio_assumptions = expected_assumptions | {
                "portfolio_cash_model",
            }
            required_assumption_keys = (
                unavailable_portfolio_assumptions
                if unavailable_shape else full_portfolio_assumptions
            )
            if set(result["execution_assumptions"]) != required_assumption_keys:
                return False
        elif set(result["execution_assumptions"]) != expected_assumptions:
            return False
        expected_assumptions_digest = cache_key.get(
            "unavailable_result_assumptions_digest"
            if unavailable_shape else "result_assumptions_digest",
        )
        if (
            cls._canonical_digest(result["execution_assumptions"])
            != expected_assumptions_digest
        ):
            return False
        for key in ("initial_cash", "cash"):
            if not cls._strict_number(result.get(key), nonnegative=True):
                return False
        if result.get("initial_cash") != 100_000.0:
            return False
        if not cls._strict_optional_number(
            result.get("ending_equity"), nonnegative=True,
        ) or not cls._strict_optional_number(result.get("outperformance")):
            return False
        if any(
            type(result.get(key)) is not int
            for key in ("completed_round_trips", "uncompleted_leg_count")
        ):
            return False
        fill_keys = {
            "symbol", "side", "requested_shares", "shares", "signal_date",
            "execution_date", "raw_reference_price", "fill_price", "fee",
            "spread_cost", "slippage", "planned_stop", "reason",
        }
        rejection_keys = {
            "symbol", "side", "signal_date", "execution_date",
            "requested_shares", "rejected_shares", "reason",
        }
        round_trip_keys = {
            "entry_date", "exit_date", "net_pnl", "holding_days",
        }
        if cache_key.get("scope") == "portfolio":
            round_trip_keys.add("symbol")
        if not cls._valid_record_list(result["trades"], fill_keys):
            return False
        if not cls._valid_record_list(result["rejections"], rejection_keys):
            return False
        if not cls._valid_record_list(result["round_trips"], round_trip_keys):
            return False
        if not cls._valid_trade_records(result["trades"]):
            return False
        if not cls._valid_rejection_records(result["rejections"]):
            return False
        if not cls._valid_round_trip_records(result["round_trips"]):
            return False
        if result["completed_round_trips"] != len(result["round_trips"]):
            return False
        selected_symbols = cache_key.get("selected_symbols")
        if (
            type(selected_symbols) is not list
            or not selected_symbols
            or any(type(symbol) is not str for symbol in selected_symbols)
        ):
            return False
        allowed_symbols = set(selected_symbols)
        if cache_key.get("scope") == "symbol" and selected_symbols != [
            result.get("symbol"),
        ]:
            return False
        if any(
            item["symbol"] not in allowed_symbols
            for key in ("trades", "rejections") for item in result[key]
        ):
            return False
        if cache_key.get("scope") == "portfolio" and any(
            item["symbol"] not in allowed_symbols for item in result["round_trips"]
        ):
            return False
        expected_cash = cls._cash_after_fills(
            result["initial_cash"], result["trades"],
        )
        if expected_cash is None or result["cash"] != expected_cash:
            return False
        metrics = result.get("metrics")
        if metrics is not None and (
            type(metrics) is not dict
            or set(metrics) != {
                "cumulative_return", "annualized_return", "maximum_drawdown",
                "calmar", "sharpe", "win_rate", "average_profit",
                "average_loss", "payoff_ratio", "average_holding_days",
                "utilization", "longest_losing_streak", "fees", "spread_cost",
                "slippage", "rejection_counts",
            }
            or type(metrics.get("rejection_counts")) is not dict
        ):
            return False
        if metrics is not None and not cls._valid_metrics(metrics):
            return False
        if metrics is not None and (
            not cls._same_cache_number(metrics["fees"], sum(
                item["fee"] for item in result["trades"]
            ))
            or not cls._same_cache_number(metrics["spread_cost"], sum(
                item["spread_cost"] for item in result["trades"]
            ))
            or not cls._same_cache_number(metrics["slippage"], sum(
                item["slippage"] for item in result["trades"]
            ))
            or any(
                metrics["rejection_counts"].get(reason, 0) < count
                for reason, count in cls._reason_counts(
                    result["rejections"],
                ).items()
            )
        ):
            return False
        if metrics is not None and not cls._valid_metric_aggregates(
            metrics, result["round_trips"],
        ):
            return False
        status = result["status"]
        if (status == "OK") != (result.get("reason") is None):
            return False
        if status == "OK" and (
            metrics is None
            or result.get("ending_equity") is None
            or result.get("outperformance") is None
            or result["completed_round_trips"] <= 0
        ):
            return False
        if status == "DATA_UNAVAILABLE" and (
            metrics is not None
            or result.get("ending_equity") is not None
            or result.get("outperformance") is not None
        ):
            return False
        if unavailable_shape and not cls._valid_unavailable_result_state(result):
            return False
        if metrics is not None and metrics["cumulative_return"] != (
            cls._clean_cache_number(
                result["ending_equity"] / result["initial_cash"] - 1.0,
            )
        ):
            return False
        if metrics is not None and (
            (result["ending_equity"] > 0.0)
            != (metrics["annualized_return"] is not None)
        ):
            return False
        if cache_key.get("scope") == "symbol":
            benchmark = result.get("benchmark")
            if benchmark is not None and (
                type(benchmark) is not dict
                or set(benchmark) != {
                    "start_date", "shares", "cash", "ending_equity",
                    "cumulative_return", "fee", "spread_cost", "slippage",
                }
            ):
                return False
            conventions = result.get("metric_conventions")
            if not (
                cls._strict_date_text(result.get("start_date"))
                and cls._strict_date_text(result.get("end_date"))
                and result["start_date"] <= result["end_date"]
                and type(conventions) is dict and set(conventions) == {
                "annualization_sessions", "sharpe_frequency",
                "sharpe_risk_free_rate", "sharpe_zero_variance",
                "drawdown_sign", "holding_days",
                }
                and type(result.get("open_position_shares")) is int
                and result["open_position_shares"] >= 0
            ):
                return False
            position_shares = cls._shares_after_fills(
                result["trades"], [result["symbol"]],
            )
            if position_shares != {result["symbol"]: result["open_position_shares"]}:
                return False
            if benchmark is not None and not cls._valid_symbol_benchmark(benchmark):
                return False
            if benchmark is not None and benchmark["cumulative_return"] != (
                cls._clean_cache_number(
                    benchmark["ending_equity"] / result["initial_cash"] - 1.0,
                )
            ):
                return False
            if status == "OK" and (
                benchmark is None
                or result["outperformance"] != cls._clean_cache_number(
                    metrics["cumulative_return"]
                    - benchmark["cumulative_return"],
                )
            ):
                return False
            return True
        baseline = result.get("baseline")
        if baseline is not None and (
            type(baseline) is not dict
            or set(baseline) != {
                "status", "reason", "initial_cash", "cash", "ending_equity",
                "trades", "fees", "spread_cost", "slippage",
                "shares_by_symbol", "cumulative_return",
            }
            or not cls._valid_record_list(baseline.get("trades"), fill_keys)
            or type(baseline.get("shares_by_symbol")) is not dict
        ):
            return False
        if baseline is not None and baseline["ending_equity"] is not None and (
            baseline["cumulative_return"] != cls._clean_cache_number(
                baseline["ending_equity"] / baseline["initial_cash"] - 1.0,
            )
        ):
            return False
        if status == "OK" and result["outperformance"] != (
            cls._clean_cache_number(
                metrics["cumulative_return"] - baseline["cumulative_return"],
            )
        ):
            return False
        if baseline is not None and not cls._valid_portfolio_benchmark(baseline):
            return False
        for key in (
            "rejection_counts", "open_position_shares", "baseline_weights",
        ):
            if type(result.get(key)) is not dict:
                return False
        symbols = result.get("symbols")
        if (
            type(symbols) is not list or not symbols
            or symbols != selected_symbols
            or symbols != sorted(symbols) or len(set(symbols)) != len(symbols)
            or any(type(item) is not str or len(item) != 6 or not item.isascii()
                   or not item.isdigit() for item in symbols)
            or set(result["open_position_shares"]) != set(symbols)
            or any(type(value) is not int or value < 0
                   for value in result["open_position_shares"].values())
            or any(not cls._strict_number(value, nonnegative=True)
                   for value in result["baseline_weights"].values())
            or any(type(value) is not int or value < 0
                   for value in result["rejection_counts"].values())
            or result["rejection_counts"] != cls._reason_counts(
                result["rejections"],
            )
            or not cls._strict_optional_number(
                result.get("max_equity_weight"), nonnegative=True,
            )
            or not cls._strict_optional_number(
                result.get("max_planned_risk"), nonnegative=True,
            )
        ):
            return False
        if cls._shares_after_fills(result["trades"], symbols) != result[
            "open_position_shares"
        ]:
            return False
        if baseline is not None and (
            set(baseline["shares_by_symbol"]) != set(symbols)
            or set(result["baseline_weights"]) != set(symbols)
            or cls._cash_after_fills(
                baseline["initial_cash"], baseline["trades"],
            ) != baseline["cash"]
            or cls._shares_after_fills(
                baseline["trades"], symbols,
            ) != baseline["shares_by_symbol"]
        ):
            return False
        if not unavailable_shape and set(
            result["execution_assumptions"]["trading_metadata_by_symbol"],
        ) != set(symbols):
            return False
        for key in ("common_start_date", "common_end_date"):
            if not cls._strict_date_text(result.get(key), optional=True):
                return False
        if (
            (result["common_start_date"] is None)
            != (result["common_end_date"] is None)
            or (
                result["common_start_date"] is not None
                and result["common_start_date"] > result["common_end_date"]
            )
            or type(result.get("event_dates")) is not list
            or any(not cls._strict_date_text(item) for item in result["event_dates"])
            or result["event_dates"] != sorted(set(result["event_dates"]))
            or (status == "OK" and baseline is None)
        ):
            return False
        if metrics is not None and result["event_dates"]:
            expected_annualized = (
                (result["ending_equity"] / result["initial_cash"])
                ** (252.0 / len(result["event_dates"])) - 1.0
            )
            if not cls._same_cache_number(
                metrics["annualized_return"], expected_annualized,
            ):
                return False
        return cls._valid_walk_forward(result.get("walk_forward"), metrics_keys={
            "cumulative_return", "annualized_return", "maximum_drawdown",
            "calmar", "sharpe", "win_rate", "average_profit", "average_loss",
            "payoff_ratio", "average_holding_days", "utilization",
            "longest_losing_streak", "fees", "spread_cost", "slippage",
            "rejection_counts",
        }, strategy=parsed_strategy)

    @staticmethod
    def _strict_number(value: object, *, nonnegative: bool = False) -> bool:
        return (
            type(value) in (int, float)
            and math.isfinite(float(value))
            and (not nonnegative or float(value) >= 0.0)
        )

    @staticmethod
    def _clean_cache_number(value: float) -> float:
        rounded = round(float(value), 12)
        return 0.0 if rounded == 0.0 else rounded

    @staticmethod
    def _same_cache_number(actual: object, expected: float) -> bool:
        return (
            type(actual) in (int, float)
            and math.isfinite(float(actual))
            and math.isclose(
                float(actual), float(expected), rel_tol=1e-9, abs_tol=1e-9,
            )
        )

    @classmethod
    def _cash_after_fills(
        cls, initial_cash: object, fills: object,
    ) -> float | None:
        if (
            not cls._strict_number(initial_cash, nonnegative=True)
            or type(fills) is not list
        ):
            return None
        cash = float(initial_cash)
        for fill in fills:
            if type(fill) is not dict:
                return None
            notional = float(fill["fill_price"]) * fill["shares"]
            if fill["side"] == "BUY":
                cash -= notional + float(fill["fee"])
            else:
                cash += notional - float(fill["fee"])
        return cls._clean_cache_number(cash)

    @staticmethod
    def _shares_after_fills(
        fills: list[Mapping[str, object]],
        symbols: Sequence[str],
    ) -> dict[str, int] | None:
        shares = {symbol: 0 for symbol in symbols}
        for fill in fills:
            symbol = fill["symbol"]
            if symbol not in shares:
                return None
            quantity = fill["shares"]
            assert type(quantity) is int
            shares[symbol] += quantity if fill["side"] == "BUY" else -quantity
            if shares[symbol] < 0:
                return None
        return dict(sorted(shares.items()))

    @staticmethod
    def _valid_unavailable_result_state(result: Mapping[str, object]) -> bool:
        common = (
            (
                result.get("status") == "DATA_UNAVAILABLE"
                or (
                    result.get("scope") == "portfolio"
                    and result.get("status") == "INSUFFICIENT_SAMPLE"
                )
            )
            and result.get("cash") == result.get("initial_cash")
            and result.get("ending_equity") is None
            and result.get("outperformance") is None
            and result.get("trades") == []
            and result.get("rejections") == []
            and result.get("round_trips") == []
            and result.get("completed_round_trips") == 0
            and result.get("uncompleted_leg_count") == 0
        )
        if not common:
            return False
        if result.get("scope") == "symbol":
            return (
                result.get("benchmark") is None
                and result.get("open_position_shares") == 0
            )
        return (
            result.get("event_dates") == []
            and result.get("rejection_counts") == {}
            and result.get("baseline") is None
            and result.get("baseline_weights") == {}
            and result.get("max_equity_weight") == 0.0
            and result.get("max_planned_risk") == 0.0
            and all(
                shares == 0
                for shares in result.get("open_position_shares", {}).values()
            )
        )

    @classmethod
    def _strict_optional_number(
        cls, value: object, *, nonnegative: bool = False,
    ) -> bool:
        return value is None or cls._strict_number(
            value, nonnegative=nonnegative,
        )

    @staticmethod
    def _strict_date_text(value: object, *, optional: bool = False) -> bool:
        if value is None:
            return optional
        if type(value) is not str:
            return False
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return False
        return parsed.isoformat() == value

    @classmethod
    def _valid_trade_records(cls, records: object) -> bool:
        if type(records) is not list:
            return False
        for item in records:
            if (
                type(item.get("symbol")) is not str
                or len(item["symbol"]) != 6 or not item["symbol"].isascii()
                or not item["symbol"].isdigit()
                or item.get("side") not in {"BUY", "SELL"}
                or type(item.get("requested_shares")) is not int
                or type(item.get("shares")) is not int
                or item["requested_shares"] <= 0
                or item["shares"] <= 0
                or item["shares"] > item["requested_shares"]
                or not cls._strict_date_text(item.get("signal_date"))
                or not cls._strict_date_text(item.get("execution_date"))
                or item["signal_date"] >= item["execution_date"]
                or any(not cls._strict_number(item.get(key), nonnegative=True)
                       for key in ("fee", "spread_cost", "slippage"))
                or any(not cls._strict_number(item.get(key))
                       or float(item[key]) <= 0.0
                       for key in ("raw_reference_price", "fill_price"))
                or not cls._strict_optional_number(
                    item.get("planned_stop"), nonnegative=True,
                )
                or type(item.get("reason")) is not str or not item["reason"]
            ):
                return False
        return True

    @classmethod
    def _valid_rejection_records(cls, records: object) -> bool:
        if type(records) is not list:
            return False
        for item in records:
            if (
                type(item.get("symbol")) is not str
                or item.get("side") not in {"BUY", "SELL"}
                or type(item.get("requested_shares")) is not int
                or type(item.get("rejected_shares")) is not int
                or item["requested_shares"] <= 0 or item["rejected_shares"] < 0
                or not cls._strict_date_text(item.get("signal_date"))
                or not cls._strict_date_text(item.get("execution_date"))
                or item["signal_date"] >= item["execution_date"]
                or type(item.get("reason")) is not str or not item["reason"]
            ):
                return False
        return True

    @classmethod
    def _valid_round_trip_records(cls, records: object) -> bool:
        if type(records) is not list:
            return False
        for item in records:
            if (
                not cls._strict_date_text(item.get("entry_date"))
                or not cls._strict_date_text(item.get("exit_date"))
                or item["entry_date"] > item["exit_date"]
                or not cls._strict_number(item.get("net_pnl"))
                or type(item.get("holding_days")) is not int
                or item["holding_days"] < 0
            ):
                return False
            if "symbol" in item and (
                type(item["symbol"]) is not str or len(item["symbol"]) != 6
            ):
                return False
        return True

    @classmethod
    def _valid_metrics(cls, metrics: Mapping[str, object]) -> bool:
        optional = {
            "annualized_return", "calmar", "sharpe", "win_rate",
            "average_profit", "average_loss", "payoff_ratio",
            "average_holding_days", "longest_losing_streak",
        }
        numeric = set(metrics) - {"rejection_counts"}
        for key in numeric:
            value = metrics[key]
            if key == "longest_losing_streak":
                if value is not None and (type(value) is not int or value < 0):
                    return False
            elif key in optional:
                if not cls._strict_optional_number(value):
                    return False
            elif not cls._strict_number(value):
                return False
        if not all(
            type(key) is str and type(value) is int and value >= 0
            for key, value in metrics["rejection_counts"].items()
        ):
            return False
        if not -1.0 <= metrics["cumulative_return"]:
            return False
        if not 0.0 <= metrics["maximum_drawdown"] <= 1.0:
            return False
        if not 0.0 <= metrics["utilization"] <= 1.0:
            return False
        if metrics["annualized_return"] is not None and metrics[
            "annualized_return"
        ] < -1.0:
            return False
        if metrics["win_rate"] is not None and not 0.0 <= metrics[
            "win_rate"
        ] <= 1.0:
            return False
        if metrics["average_profit"] is not None and metrics[
            "average_profit"
        ] <= 0.0:
            return False
        if metrics["average_loss"] is not None and metrics["average_loss"] >= 0.0:
            return False
        if metrics["average_holding_days"] is not None and metrics[
            "average_holding_days"
        ] < 0.0:
            return False
        if any(metrics[key] < 0.0 for key in (
            "fees", "spread_cost", "slippage",
        )):
            return False
        expected_calmar = (
            None
            if metrics["annualized_return"] is None
            or metrics["maximum_drawdown"] <= 0.0
            else cls._clean_cache_number(
                metrics["annualized_return"] / metrics["maximum_drawdown"],
            )
        )
        if (
            (metrics["calmar"] is None) != (expected_calmar is None)
            or expected_calmar is not None
            and not cls._same_cache_number(metrics["calmar"], expected_calmar)
        ):
            return False
        expected_payoff = (
            None
            if metrics["average_profit"] is None
            or metrics["average_loss"] is None
            else cls._clean_cache_number(
                metrics["average_profit"] / abs(metrics["average_loss"]),
            )
        )
        return not (
            (metrics["payoff_ratio"] is None) != (expected_payoff is None)
            or expected_payoff is not None
            and not cls._same_cache_number(
                metrics["payoff_ratio"], expected_payoff,
            )
        )

    @classmethod
    def _valid_metric_aggregates(
        cls,
        metrics: Mapping[str, object],
        round_trips: list[Mapping[str, object]],
    ) -> bool:
        pnls = [float(item["net_pnl"]) for item in round_trips]
        profits = [value for value in pnls if value > 0.0]
        losses = [value for value in pnls if value < 0.0]
        expected_win_rate = (
            None if not pnls
            else cls._clean_cache_number(len(profits) / len(pnls))
        )
        expected_profit = (
            None if not profits
            else cls._clean_cache_number(sum(profits) / len(profits))
        )
        expected_loss = (
            None if not losses
            else cls._clean_cache_number(sum(losses) / len(losses))
        )
        expected_holding = (
            None if not round_trips
            else cls._clean_cache_number(
                sum(item["holding_days"] for item in round_trips)
                / len(round_trips),
            )
        )
        expected_streak: int | None = None
        if pnls:
            current = longest = 0
            for pnl in pnls:
                current = current + 1 if pnl < 0.0 else 0
                longest = max(longest, current)
            expected_streak = longest
        for key, expected in (
            ("win_rate", expected_win_rate),
            ("average_profit", expected_profit),
            ("average_loss", expected_loss),
            ("average_holding_days", expected_holding),
        ):
            if (metrics[key] is None) != (expected is None):
                return False
            if expected is not None and not cls._same_cache_number(
                metrics[key], expected,
            ):
                return False
        return metrics["longest_losing_streak"] == expected_streak

    @classmethod
    def _valid_metric_evidence_node(
        cls,
        metrics: object,
        evidence: object,
        *,
        initial_cash: float,
        ending_equity: object = None,
    ) -> bool:
        if metrics is None:
            return evidence is None
        if (
            type(metrics) is not dict
            or type(evidence) is not dict
            or set(evidence) != {
                "session_count", "equity_curve", "utilization",
                "blocked_counts", "order_rejection_counts",
            }
            or type(evidence.get("session_count")) is not int
            or evidence["session_count"] < 0
            or type(evidence.get("equity_curve")) is not list
            or type(evidence.get("utilization")) is not list
            or type(evidence.get("blocked_counts")) is not dict
            or type(evidence.get("order_rejection_counts")) is not dict
            or evidence["session_count"] != len(evidence["equity_curve"])
            or evidence["session_count"] != len(evidence["utilization"])
            or any(
                not cls._strict_number(value, nonnegative=True)
                for value in evidence["equity_curve"]
            )
            or any(
                not cls._strict_number(value, nonnegative=True)
                or value > 1.0
                for value in evidence["utilization"]
            )
            or any(
                type(reason) is not str or not reason
                or type(count) is not int or count <= 0
                for counts in (
                    evidence["blocked_counts"],
                    evidence["order_rejection_counts"],
                )
                for reason, count in counts.items()
            )
        ):
            return False
        try:
            derived = _curve_metric_values(
                initial_cash,
                evidence["equity_curve"],
                evidence["utilization"],
            )
        except (ArithmeticError, OverflowError, ValueError, ZeroDivisionError):
            return False
        for key, expected in derived.items():
            actual = metrics.get(key)
            if (actual is None) != (expected is None):
                return False
            if expected is not None and not cls._same_cache_number(
                actual, expected,
            ):
                return False
        combined_counts = dict(evidence["order_rejection_counts"])
        for reason, count in evidence["blocked_counts"].items():
            combined_counts[reason] = combined_counts.get(reason, 0) + count
        if metrics.get("rejection_counts") != dict(
            sorted(combined_counts.items())
        ):
            return False
        if ending_equity is not None:
            if not evidence["equity_curve"]:
                return False
            if not cls._same_cache_number(
                evidence["equity_curve"][-1], float(ending_equity),
            ):
                return False
        return True

    @classmethod
    def _valid_metric_evidence(
        cls,
        result: Mapping[str, object],
        evidence: object,
        cache_key: Mapping[str, object],
    ) -> bool:
        if set(result) == {
            "schema_version", "scope", "symbol", "status", "reason",
            "outperformance", "read_only",
        }:
            return evidence is None
        scope = cache_key.get("scope")
        expected_keys = {"root"} if scope == "symbol" else {
            "root", "walk_forward",
        }
        if type(evidence) is not dict or set(evidence) != expected_keys:
            return False
        if not cls._valid_metric_evidence_node(
            result.get("metrics"),
            evidence["root"],
            initial_cash=float(result["initial_cash"]),
            ending_equity=result.get("ending_equity"),
        ):
            return False
        root_evidence = evidence["root"]
        if root_evidence is not None and root_evidence[
            "order_rejection_counts"
        ] != cls._reason_counts(result["rejections"]):
            return False
        if scope == "symbol":
            return True
        if root_evidence is not None and (
            root_evidence["session_count"] != len(result["event_dates"])
            or not cls._same_cache_number(
                result["max_equity_weight"],
                max(root_evidence["utilization"], default=0.0),
            )
        ):
            return False
        report = result.get("walk_forward")
        report_evidence = evidence.get("walk_forward")
        if (
            type(report) is not dict
            or type(report_evidence) is not dict
            or set(report_evidence) != {"variants"}
            or type(report_evidence.get("variants")) is not list
            or len(report_evidence["variants"]) != len(report["variants"])
        ):
            return False
        for variant, variant_evidence in zip(
            report["variants"], report_evidence["variants"], strict=True,
        ):
            if (
                type(variant_evidence) is not dict
                or set(variant_evidence) != {"folds"}
                or type(variant_evidence.get("folds")) is not list
                or len(variant_evidence["folds"]) != len(variant["folds"])
            ):
                return False
            for fold, fold_evidence in zip(
                variant["folds"], variant_evidence["folds"], strict=True,
            ):
                if (
                    type(fold_evidence) is not dict
                    or set(fold_evidence) != {"train", "test"}
                ):
                    return False
                for phase_name in ("train", "test"):
                    if not cls._valid_metric_evidence_node(
                        fold[phase_name].get("metrics"),
                        fold_evidence[phase_name],
                        initial_cash=float(result["initial_cash"]),
                    ):
                        return False
                    phase_evidence = fold_evidence[phase_name]
                    if phase_evidence is not None:
                        expected_sessions = (
                            fold["train_bar_count"]
                            - max(
                                result["strategy_parameters"][
                                    "minimum_daily_bars"
                                ],
                                variant["parameters"]["long_ma_days"]
                                + result["strategy_parameters"][
                                    "long_ma_slope_lookback"
                                ],
                            )
                            if phase_name == "train"
                            else fold["test_bar_count"]
                        )
                        if (
                            phase_evidence["session_count"]
                            != expected_sessions
                        ):
                            return False
        return True

    @classmethod
    def _valid_symbol_benchmark(cls, value: Mapping[str, object]) -> bool:
        return (
            cls._strict_date_text(value.get("start_date"))
            and type(value.get("shares")) is int and value["shares"] > 0
            and all(cls._strict_number(value.get(key)) for key in (
                "cash", "ending_equity", "cumulative_return", "fee",
                "spread_cost", "slippage",
            ))
            and value["cash"] >= 0 and value["ending_equity"] >= 0
            and value["fee"] >= 0 and value["spread_cost"] >= 0
            and value["slippage"] >= 0
        )

    @classmethod
    def _valid_portfolio_benchmark(cls, value: Mapping[str, object]) -> bool:
        return (
            value.get("status") in {"OK", "INSUFFICIENT_SAMPLE"}
            and type(value.get("reason")) in (str, type(None))
            and cls._strict_number(value.get("initial_cash"), nonnegative=True)
            and cls._strict_number(value.get("cash"), nonnegative=True)
            and cls._strict_optional_number(
                value.get("ending_equity"), nonnegative=True,
            )
            and cls._strict_optional_number(value.get("cumulative_return"))
            and all(cls._strict_number(value.get(key), nonnegative=True) for key in (
                "fees", "spread_cost", "slippage",
            ))
            and cls._valid_trade_records(value.get("trades"))
            and type(value.get("shares_by_symbol")) is dict
            and all(type(shares) is int and shares >= 0
                    for shares in value["shares_by_symbol"].values())
        )

    @staticmethod
    def _reason_counts(records: list[Mapping[str, object]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in records:
            reason = item["reason"]
            assert type(reason) is str
            counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    @classmethod
    def _valid_record_list(cls, value: object, keys: set[str]) -> bool:
        return type(value) is list and all(
            type(item) is dict and set(item) == keys
            for item in value
        )

    @classmethod
    def _valid_walk_forward(
        cls,
        value: object,
        *,
        metrics_keys: set[str],
        strategy: SwingStrategyConfig,
    ) -> bool:
        if type(value) is not dict or set(value) != {
            "status", "reason", "train_days", "test_days", "step_days",
            "selected_variant", "variants",
        } or type(value.get("variants")) is not list:
            return False
        summary_keys = {
            "status", "reason", "metrics", "cumulative_return",
            "maximum_drawdown", "completed_round_trips", "outperformance",
        }
        if any(
            type(value.get(key)) is not int or value[key] <= 0
            for key in ("train_days", "test_days", "step_days")
        ) or (
            value.get("status") not in {
                "OK", "INSUFFICIENT_SAMPLE", "DATA_UNAVAILABLE",
            }
            or type(value.get("reason")) not in (str, type(None))
            or (value["status"] == "OK") != (value["reason"] is None)
            or value["train_days"] != strategy.walk_forward_train_days
            or value["test_days"] != strategy.walk_forward_test_days
            or value["step_days"] != strategy.walk_forward_step_days
            or value.get("selected_variant") is not None
        ):
            return False
        expected_parameters = [
            {
                "short_ma_days": short,
                "long_ma_days": long,
                "initial_stop_atr": initial,
                "trailing_stop_atr": trailing,
            }
            for short in (18, 20, 22)
            for long in (55, 60, 65)
            for initial in (1.75, 2.0, 2.25)
            for trailing in (2.75, 3.0, 3.25)
        ]
        if value.get("status") == "OK":
            if len(value["variants"]) != 81:
                return False
        elif value["variants"]:
            return False
        for variant_index, variant in enumerate(value["variants"]):
            if type(variant) is not dict or set(variant) != {
                "parameters", "folds", "stability",
            }:
                return False
            if (
                type(variant.get("parameters")) is not dict
                or set(variant["parameters"]) != {
                    "short_ma_days", "long_ma_days", "initial_stop_atr",
                    "trailing_stop_atr",
                }
                or type(variant.get("stability")) is not dict
                or set(variant["stability"]) != {
                    "fold_count", "test_ok_count", "mean_test_return",
                    "positive_test_fold_count",
                }
                or type(variant.get("folds")) is not list
            ):
                return False
            if variant["parameters"] != expected_parameters[variant_index]:
                return False
            if (
                type(variant["stability"].get("fold_count")) is not int
                or variant["stability"]["fold_count"] != len(variant["folds"])
                or any(
                    type(variant["stability"].get(key)) is not int
                    or variant["stability"][key] < 0
                    for key in (
                        "test_ok_count", "positive_test_fold_count",
                    )
                )
                or not cls._strict_optional_number(
                    variant["stability"].get("mean_test_return"),
                )
            ):
                return False
            test_returns: list[float] = []
            test_ok_count = 0
            for expected_fold_index, fold in enumerate(variant["folds"]):
                if type(fold) is not dict or set(fold) != {
                    "fold_index", "train_start_date", "train_end_date",
                    "test_start_date", "test_end_date", "train_bar_count",
                    "test_bar_count", "train", "test",
                }:
                    return False
                if (
                    any(type(fold.get(key)) is not int or fold[key] < 0 for key in (
                        "fold_index", "train_bar_count", "test_bar_count",
                    ))
                    or fold["fold_index"] != expected_fold_index
                    or fold["train_bar_count"] != strategy.walk_forward_train_days
                    or fold["test_bar_count"] != strategy.walk_forward_test_days
                    or any(not cls._strict_date_text(fold.get(key)) for key in (
                        "train_start_date", "train_end_date", "test_start_date",
                        "test_end_date",
                    ))
                    or not (
                        fold["train_start_date"] <= fold["train_end_date"]
                        < fold["test_start_date"] <= fold["test_end_date"]
                    )
                ):
                    return False
                for phase in (fold.get("train"), fold.get("test")):
                    if type(phase) is not dict or set(phase) != summary_keys:
                        return False
                    nested_metrics = phase.get("metrics")
                    if nested_metrics is not None and (
                        type(nested_metrics) is not dict
                        or set(nested_metrics) != metrics_keys
                        or not cls._valid_metrics(nested_metrics)
                    ):
                        return False
                    completed = phase.get("completed_round_trips")
                    if (
                        phase.get("status") not in {
                            "OK", "INSUFFICIENT_SAMPLE", "DATA_UNAVAILABLE",
                        }
                        or type(completed) is not int or completed < 0
                        or (phase["status"] == "OK")
                        != (phase.get("reason") is None)
                        or type(phase.get("outperformance"))
                        not in (int, float, type(None))
                        or (
                            phase["status"] != "OK"
                            and phase.get("outperformance") is not None
                        )
                    ):
                        return False
                    if nested_metrics is None:
                        if (
                            completed != 0
                            or phase.get("cumulative_return") is not None
                            or phase.get("maximum_drawdown") is not None
                        ):
                            return False
                    elif (
                        not cls._same_cache_number(
                            phase.get("cumulative_return"),
                            nested_metrics["cumulative_return"],
                        )
                        or not cls._same_cache_number(
                            phase.get("maximum_drawdown"),
                            nested_metrics["maximum_drawdown"],
                        )
                        or (completed == 0) != (
                            nested_metrics["win_rate"] is None
                        )
                        or phase["status"] == "OK" and completed <= 0
                    ):
                        return False
                test_phase = fold["test"]
                if test_phase["status"] == "OK":
                    test_ok_count += 1
                test_return = test_phase["cumulative_return"]
                if type(test_return) in (int, float):
                    test_returns.append(float(test_return))
            expected_mean = (
                None if not test_returns
                else cls._clean_cache_number(
                    sum(test_returns) / len(test_returns),
                )
            )
            stability = variant["stability"]
            if (
                stability["test_ok_count"] != test_ok_count
                or stability["positive_test_fold_count"] != sum(
                    item > 0.0 for item in test_returns
                )
                or (stability["mean_test_return"] is None)
                != (expected_mean is None)
                or expected_mean is not None
                and not cls._same_cache_number(
                    stability["mean_test_return"], expected_mean,
                )
            ):
                return False
        return True

    @classmethod
    def _strict_json_tree(cls, value: object) -> bool:
        if value is None or type(value) in (str, bool, int):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if type(value) is list:
            return all(cls._strict_json_tree(item) for item in value)
        if type(value) is dict:
            return all(
                type(key) is str and cls._strict_json_tree(item)
                for key, item in value.items()
            )
        return False

    @staticmethod
    def _backtest_unavailable(
        scope: str,
        symbol: str | None,
        status: str,
        reason: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "scope": scope,
            "symbol": symbol,
            "status": status,
            "reason": reason,
            "outperformance": None,
            "read_only": True,
        }

    def _run_backtest(
        self,
        scope: str,
        symbol: str | None,
        selected_symbols: tuple[str, ...],
        histories: Mapping[str, tuple[DailyBar, ...]],
        metadata: Mapping[str, EtfMetadata],
        strategy: SwingStrategyConfig,
    ) -> tuple[dict[str, object], dict[str, object] | None]:
        initial_cash = 100_000.0
        if scope == "symbol":
            assert symbol is not None
            bars = histories[symbol]
            if len(bars) < strategy.minimum_daily_bars + 1:
                return (
                    self._backtest_unavailable(
                        scope, symbol, "INSUFFICIENT_SAMPLE",
                        "INSUFFICIENT_COMPLETED_DAILY_BARS",
                    ),
                    None,
                )
            try:
                computed = SwingBacktester(
                    strategy, metadata[symbol].trading,
                ).run_symbol(bars, initial_cash)
            except SwingBacktestError as error:
                return (
                    self._backtest_unavailable(
                        scope, symbol, "DATA_UNAVAILABLE", str(error),
                    ),
                    None,
                )
            result = computed.to_dict()
            result["scope"] = "symbol"
            result["read_only"] = True
            return result, {"root": computed.cache_evidence()}

        first = selected_symbols[0]
        backtester = SwingBacktester(strategy, metadata[first].trading)
        trading_map = {
            item: metadata[item].trading for item in selected_symbols
        }
        computed = backtester.run_portfolio(
            histories,
            initial_cash,
            trading_by_symbol=trading_map,
        )
        result = computed.to_dict()
        stability_report = backtester.walk_forward(
            histories,
            initial_cash,
            trading_by_symbol=trading_map,
        )
        result["walk_forward"] = stability_report.to_dict()
        result["symbol"] = None
        result["read_only"] = True
        return result, {
            "root": computed.cache_evidence(),
            "walk_forward": stability_report.cache_evidence(),
        }

    def alerts(
        self,
        *,
        include_retracted: bool = False,
        limit: int | None = None,
    ) -> dict[str, object]:
        if type(include_retracted) is not bool:
            raise SwingServiceError("include_retracted must be boolean")
        if limit is not None and (
            type(limit) is not int or not 1 <= limit <= _MAX_ALERT_HISTORY_LIMIT
        ):
            raise SwingServiceError(
                f"limit must be an integer from 1 to {_MAX_ALERT_HISTORY_LIMIT}",
            )
        with self.publish_condition:
            source = (
                self._published_alerts_history
                if include_retracted else self._published_alerts_current
            )
            if limit is None:
                return copy.deepcopy(source)
            raw_items = source.get("items", [])
            items = raw_items if isinstance(raw_items, list) else []
            active = [
                item for item in items
                if isinstance(item, Mapping)
                and item.get("currently_active") is True
            ]
            history = [
                item for item in items
                if not (
                    isinstance(item, Mapping)
                    and item.get("currently_active") is True
                )
            ]
            selected = active + history[-limit:]
            result = {
                key: copy.deepcopy(value)
                for key, value in source.items()
                if key != "items"
            }
            result["items"] = copy.deepcopy(selected)
            return result

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
            used_clock = now is None
            try:
                cycle_time = self._local_time(self.clock() if now is None else now)
            except Exception as error:
                self._reload_holdings_snapshot(self._fallback_now())
                self._publish_component_failure(
                    "service", "CLOCK_FAILED", error, now=self._fallback_now(),
                )
                return False
            recovered = self._mark_clock_success() if used_clock else False
            holdings_changed = self._reload_holdings_snapshot(cycle_time)
            environment_before = self._environment_history_summary()
            self._reload_index_history()
            revision_before_refresh = self.revision
            result = self._refresh_completed_daily(cycle_time)
            environment_changed = environment_before != self._environment_history_summary()
            if (
                (recovered or holdings_changed or environment_changed)
                and self.revision == revision_before_refresh
            ):
                self._publish(self._build_snapshot(cycle_time))
            return result

    def refresh_intraday(self) -> dict[str, object]:
        with self.producer_lock:
            try:
                cycle_time = self._local_time(self.clock())
            except Exception as error:
                fallback_time = self._fallback_now()
                self._reload_holdings_snapshot(fallback_time)
                self._health["service"] = "CLOCK_FAILED"
                self._errors["service"] = self._safe_error(error)
                self._withdraw_intraday("CLOCK_FAILED", fallback_time, error)
                return self.snapshot()
            self._mark_clock_success()
            self._reload_holdings_snapshot(cycle_time)
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
            self._require_no_holdings_snapshot()
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
            self._require_no_holdings_snapshot()
            ledger = self._require_ledger()
            normalized = trade
            events = ledger.load_events()
            prior = tuple(
                event for event in events
                if event.idempotency_key == idempotency_key
            )
            if len(prior) > 1:
                raise SwingServiceError("duplicate trade idempotency keys")
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
                event = ledger.record_trade(
                    normalized, idempotency_key,
                )
                repair_now = self._trade_retry_reference_time(
                    events, trade,
                )
                if self._trade_derivations_are_current(repair_now):
                    self._ensure_current_formal_alerts(repair_now)
                else:
                    self._rebuild_after_portfolio_mutation(repair_now)
                return event.to_dict()

            inference_now = self._trusted_trade_now()
            self._validate_trade_execution_time(trade, inference_now)
            if (
                type(trade) is TradeInput
                and trade.side == "SELL"
                and trade.exit_reason is None
                and self._sell_completes_position(trade)
                and inference_now is not None
                and self._trade_executes_today(trade, inference_now)
                and self._stop_exit_is_current(
                    trade.symbol, inference_now, trade.executed_at,
                )
            ):
                normalized = replace(trade, exit_reason="STOP_EXIT")
            event = ledger.record_trade(
                normalized, idempotency_key,
            )
            self._rebuild_after_portfolio_mutation(inference_now)
            return event.to_dict()

    def _trusted_trade_now(self) -> datetime:
        try:
            return self._local_time(self.clock())
        except Exception as error:
            raise SwingServiceError("trusted clock is unavailable") from error

    def _trade_retry_reference_time(
        self,
        events: Sequence[PortfolioEvent],
        trade: TradeInput,
    ) -> datetime:
        candidates = [
            trade.executed_at.astimezone(SHANGHAI),
            self._fallback_now(),
        ]
        for ledger_event in events:
            if ledger_event.event_type not in {
                PortfolioEventType.BUY_CONFIRMED,
                PortfolioEventType.SELL_CONFIRMED,
            }:
                continue
            raw_executed_at = ledger_event.payload.get("executed_at")
            if type(raw_executed_at) is not str:
                raise SwingServiceError(
                    "authoritative ledger trade time is unavailable",
                )
            try:
                executed_at = datetime.fromisoformat(raw_executed_at)
                if (
                    executed_at.tzinfo is None
                    or executed_at.utcoffset() is None
                ):
                    raise ValueError("ledger trade time is timezone-naive")
                candidates.append(executed_at.astimezone(SHANGHAI))
            except (TypeError, ValueError, OverflowError) as error:
                raise SwingServiceError(
                    "authoritative ledger trade time is invalid",
                ) from error
        raw_generated = self.published.get("generated_at")
        if type(raw_generated) is str:
            try:
                generated = datetime.fromisoformat(raw_generated)
                if (
                    generated.tzinfo is not None
                    and generated.utcoffset() is not None
                ):
                    candidates.append(generated.astimezone(SHANGHAI))
            except (TypeError, ValueError, OverflowError):
                pass
        return max(candidates)

    def _trade_derivations_are_current(self, now: datetime) -> bool:
        projection, portfolio_status, portfolio_error = (
            self._calculate_portfolio_projection(
                self._history, now, persist=False,
            )
        )
        formal = self._calculate_formal(
            self._history,
            projection,
            portfolio_status,
            self._health["daily"],
            now,
        )
        projection_payload = (
            projection.to_dict() if projection is not None else None
        )
        expected_formal = {
            symbol: decision.to_dict() for symbol, decision in formal.items()
        }
        published_items = self.published.get("items")
        if not isinstance(published_items, list):
            return False
        actual_formal = {
            item.get("symbol"): item.get("formal_decision")
            for item in published_items if isinstance(item, Mapping)
        }
        published_health = self.published.get("health")
        published_errors = self.published.get("errors")
        if not isinstance(published_health, Mapping):
            return False
        if not isinstance(published_errors, Mapping):
            return False
        if (
            self._portfolio_projection != projection
            or self._formal != formal
            or self._health.get("portfolio") != portfolio_status
            or self._errors.get("portfolio") != portfolio_error
            or published_health != self._health
            or published_errors != self._errors
            or self.published.get("portfolio") != projection_payload
            or actual_formal != expected_formal
            or self._published_portfolio_view.get("status") != portfolio_status
            or self._published_portfolio_view.get("projection")
            != projection_payload
            or self._published_portfolio_view.get("error") != portfolio_error
            or not self._projection_file_matches(projection)
        ):
            return False
        alert_history, active_alerts = self._alert_snapshot(
            now, include_retracted=True,
        )
        alert_current = [
            item for item in alert_history if not item.get("retracted")
        ]
        return bool(
            self.published.get("active_alerts") == active_alerts
            and self.published.get("alerts") == active_alerts
            and self._published_alerts_current.get("items") == alert_current
            and self._published_alerts_history.get("items") == alert_history
        )

    def _projection_file_matches(
        self,
        expected: PortfolioProjection | None,
    ) -> bool:
        if expected is None:
            return False
        try:
            actual = load_projection(self.paths.portfolio_snapshot)
        except PortfolioLedgerError:
            return False
        return actual == expected

    @staticmethod
    def _validate_trade_execution_time(
        trade: TradeInput,
        trusted_now: datetime,
    ) -> None:
        if type(trade) is not TradeInput:
            return
        try:
            executed_at = trade.executed_at.astimezone(SHANGHAI)
        except Exception as error:
            raise SwingServiceError(
                "trade executed_at cannot be compared with trusted clock",
            ) from error
        if executed_at > trusted_now + timedelta(
            seconds=_TRADE_FUTURE_SKEW_SECONDS,
        ):
            raise SwingServiceError("trade executed_at exceeds trusted clock")

    def _sell_completes_position(self, trade: TradeInput) -> bool:
        projection = self._portfolio_projection
        if projection is None:
            return False
        position = projection.positions.get(trade.symbol)
        return position is not None and trade.shares == position.shares

    @staticmethod
    def _trade_executes_today(trade: TradeInput, now: datetime) -> bool:
        try:
            executed_at = trade.executed_at
            return bool(
                type(executed_at) is datetime
                and executed_at.tzinfo is not None
                and executed_at.utcoffset() is not None
                and executed_at.astimezone(SHANGHAI).date() == now.date()
            )
        except Exception:
            return False

    def _stop_exit_is_current(
        self,
        symbol: str,
        now: datetime,
        executed_at: datetime,
    ) -> bool:
        if not self._is_trading_date(now.date()):
            return False
        formal = self._formal.get(symbol)
        expected = self._last_completed_trading_date(now)
        if (
            formal is not None
            and formal.state is SwingState.EXIT_CANDIDATE
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
            trigger_time = timestamp.astimezone(SHANGHAI)
            execution_time = executed_at.astimezone(SHANGHAI)
            return bool(
                published_item.get("intraday_overlay")
                == IntradayOverlay.PREDEFINED_STOP_TOUCHED.value
                and published_item.get("intraday_health_status") == "REALTIME"
                and trigger_time.date() == now.date()
                and execution_time >= trigger_time
            )
        except Exception:
            return False

    def reverse_trade(
        self, event_id: str, idempotency_key: str,
    ) -> dict[str, object]:
        with self.producer_lock:
            self._require_no_holdings_snapshot()
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

        self._read_holdings_source()

        try:
            v11_path = self.paths.strategy.parent / "v11_strategy.json"
            if not v11_path.exists():
                v11_path = Path(__file__).resolve().parents[2] / "data" / "swing" / "v11_strategy.json"
            self._v11_config = load_v11_config(v11_path)
        except Exception as error:
            self._v11_config = None
            self._errors["v11"] = self._safe_error(error)

        try:
            self._v11_state = self._v11_state_store().load()
            self._errors.pop("v11_state", None)
        except Exception as error:
            self._v11_state = {
                "positions": {}, "environment": normalize_environment_state(None),
            }
            self._errors["v11_state"] = self._safe_error(error)

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
        self._reload_index_history()

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
        if self._health["service"] != "CLOCK_FAILED":
            self._health["service"] = (
                "OK" if self._health["configuration"] == "OK" else "BLOCKED"
            )
        return self._build_snapshot(now)

    def _read_holdings_source(self) -> None:
        # Pending registrations identify reports, never strategy-tradable metadata.
        try:
            self._pending_instruments = load_pending_etfs(
                self.paths.metadata.with_name("pending_etfs.json"),
            )
            self._errors.pop("pending_instruments", None)
        except Exception:
            self._pending_instruments = {}
            self._errors["pending_instruments"] = "待接入标的登记不可用，请核对本地文件"
        self._holdings_snapshot = read_holdings_snapshot(
            self.paths.portfolio_snapshot.with_name("holdings_snapshot.json"),
            {**self._metadata, **self._pending_instruments},
        )

    def _holdings_snapshot_present(self) -> bool:
        return self._holdings_snapshot.get("status") != "ABSENT"

    def _snapshot_position(self, symbol: str) -> Mapping[str, object] | None:
        snapshot = self._holdings_snapshot.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return None
        for position in snapshot.get("positions", []):
            if isinstance(position, Mapping) and position.get("symbol") == symbol:
                return position
        return None

    def _reload_holdings_snapshot(self, now: datetime | None = None) -> datetime | None:
        previous = (self._holdings_snapshot, self._pending_instruments,
                    self._errors.get("pending_instruments"))
        self._read_holdings_source()
        changed = previous != (self._holdings_snapshot, self._pending_instruments,
                               self._errors.get("pending_instruments"))
        if changed:
            changed_at = now if now is not None else self._safe_now()
            self._load_portfolio_projection(changed_at)
            self._recompute_formal(changed_at, publish_alerts=False)
            return changed_at
        return None

    def _require_no_holdings_snapshot(self) -> None:
        # Re-read at the write boundary: a report can arrive after bootstrapping.
        changed_at = self._reload_holdings_snapshot()
        if changed_at is not None:
            self._publish(self._build_snapshot(changed_at))
        if self._holdings_snapshot_present():
            raise SwingServiceError("已导入持仓快照（或文件待修复）；需完成明确迁移后才能初始化、记成交或冲正")

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
        if self._holdings_snapshot_present():
            return (
                None,
                "SNAPSHOT_ONLY" if self._holdings_snapshot.get("status") == "SNAPSHOT_ONLY" else "BLOCKED",
                "已导入持仓快照：买入日期和止损未知，仅记录不激活策略；需迁移后启用账本",
            )
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
        snapshot_only = self._holdings_snapshot_present()
        if snapshot_only:
            projection, portfolio_health = None, "SNAPSHOT_ONLY"
        account_risk_active = portfolio_health == "OK" and projection is not None
        effective_strategy = (
            replace(self._strategy, risk_per_trade=projection.default_risk_per_trade)
            if account_risk_active else self._strategy
        )
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
            decision = evaluate_swing(
                bars, effective_strategy, context,
            )
            if snapshot_only:
                position = self._snapshot_position(item.symbol)
                # Keep market/trend evidence, including technical entry zone.
                evidence = {
                    key: value for key, value in decision.evidence.items()
                    if not any(token in key for token in (
                        "risk", "shares", "cash", "cap_",
                        "minimum_lot", "exit_", "reduce_", "add_",
                    ))
                }
                evidence.update({
                    "snapshot_only": True,
                    "snapshot_has_position": bool(position and position["shares"] > 0),
                    "snapshot_shares": position["shares"] if position else None,
                    "snapshot_sellable_shares": position["sellable_shares"] if position else None,
                    "position_risk_amount": None,
                    "entry_date_known": False,
                    "stop_loss_known": False,
                })
                published_state = decision.state
                technical_trial = bool(
                    evidence.get("pullback_low_touched")
                    and evidence.get("reclaim_close_above_ma20")
                    and evidence.get("confirmation_above_previous_high")
                    and evidence.get("anti_chase_ok")
                    and evidence.get("trend_close_above_ma60")
                    and evidence.get("trend_ma20_above_ma60")
                    and evidence.get("trend_ma60_rising")
                )
                if technical_trial or published_state is SwingState.TRIAL_ENTRY_CANDIDATE:
                    published_state = SwingState.TRIAL_ENTRY_OBSERVE
                    evidence["technical_trial_ready"] = True
                elif published_state in {
                    SwingState.ADD_CANDIDATE,
                    SwingState.REDUCE_CANDIDATE,
                    SwingState.EXIT_CANDIDATE,
                }:
                    published_state = (
                        SwingState.HOLDING
                        if position and position["shares"] > 0
                        else SwingState.UPTREND_WATCH
                    )
                decision = replace(
                    decision,
                    state=published_state,
                    evidence=evidence,
                    blocked_reasons=tuple(dict.fromkeys((
                        *(reason for reason in decision.blocked_reasons if reason not in {
                            "ledger_healthy", "cash_cap", "single_symbol_cap",
                            "total_exposure_cap", "trade_risk_cap", "portfolio_risk_cap",
                            "minimum_lot",
                        }),
                        "holdings_snapshot_only",
                    ))),
                    planned_stop=None, planned_shares=0, planned_risk_rate=0.0,
                    first_reduce_price=None,
                    valid_for_trading_date=None,
                )
            result[item.symbol] = replace(
                decision,
                evidence={
                    **decision.evidence,
                    "effective_risk_per_trade": None if snapshot_only else effective_strategy.risk_per_trade,
                    "risk_setting_source": (
                        "SNAPSHOT_UNKNOWN" if snapshot_only else
                        "ACCOUNT" if account_risk_active else "STRATEGY_DEFAULT"
                    ),
                },
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
        symbol_planned_risk = 0.0
        risk_known = True
        position = None
        last_stop_trading_date = (
            self._last_stop_trading_date(symbol, bars) if ledger_healthy else None
        )
        if ledger_healthy and projection is not None:
            projected = projection.positions.get(symbol)
            if projected is not None:
                symbol_planned_risk = projected.planned_risk
                risk_known = projected.shares <= 0 or projected.planned_risk > 0.0
            if (
                projected is not None
                and projected.shares > 0
                and bars
                and risk_known
            ):
                position = self._position_context(symbol, projected, bars)
        return PortfolioContext(
            equity=equity,
            cash=cash,
            current_etf_market_value=market_value,
            current_planned_risk_amount=planned_risk,
            current_symbol_planned_risk_amount=symbol_planned_risk,
            lot_size=lot_size,
            data_healthy=data_healthy,
            metadata_complete=metadata is not None,
            ledger_healthy=ledger_healthy,
            risk_known=risk_known,
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
            return None
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
        if target is not None and not self._index_history_current(target):
            self._collect_index_history(target)
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
            strategy_count = (
                0 if self._strategy is None
                else self._strategy.walk_forward_train_days
                + self._strategy.walk_forward_test_days
                + self._strategy.walk_forward_step_days
            )
            raw_records = self.collector.collect(
                enabled, target, max(_DEFAULT_HISTORY_COUNT, strategy_count),
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
        crosscheck_health, crosscheck_error = self._record_crosscheck_receipts(
            merged, enabled, target, max(_DEFAULT_HISTORY_COUNT, strategy_count), now,
        )
        next_health["independent_crosscheck"] = crosscheck_health
        if crosscheck_error is None:
            next_errors.pop("independent_crosscheck", None)
        else:
            next_errors["independent_crosscheck"] = crosscheck_error
        self._rebuild_research_manifest()

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
        return parse_minute_payload(payload, trading_date)

    @staticmethod
    def _expected_complete_minutes(trading_date: date) -> tuple[datetime, ...]:
        return expected_complete_minutes(trading_date)

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
            return self._compose_intraday_overlay(now)
        except Exception as error:
            self._withdraw_intraday("INTRADAY_FEED_UNAVAILABLE", now, error)
            return self.snapshot()

    def _compose_intraday_overlay(self, now: datetime) -> dict[str, object]:
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

        overlays: dict[str, str | None] = {}
        current: dict[str, tuple[float | None, str | None, str, str]] = {}
        desired_alerts: list[AlertInput] = []
        for symbol, formal in self._formal.items():
            raw = by_symbol.get(symbol)
            healthy, timestamp, validated_health = (
                self._validated_realtime_quote(raw, now)
            )
            price = raw.get("price") if raw is not None else None
            intraday = evaluate_intraday_overlay(
                formal,
                price,
                feed_healthy=healthy,
                has_position=self._has_position(symbol),
            )
            resolved_status = self._execution_status(
                formal, now, market_realtime=healthy,
            )
            if self._holdings_snapshot_present():
                overlays[symbol] = None
                current[symbol] = (
                    intraday.price, timestamp, resolved_status, validated_health,
                )
                continue
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
                current[symbol] = (
                    normalized_price, timestamp, status, validated_health,
                )
                continue
            if intraday.overlay is IntradayOverlay.INTRADAY_FEED_UNAVAILABLE:
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
            current[symbol] = (
                normalized_price, timestamp, status, validated_health,
            )

        validated_statuses = tuple(value[3] for value in current.values())
        if validated_statuses and all(
            status == "REALTIME" for status in validated_statuses
        ):
            aggregate_health = "REALTIME"
        elif "UNAVAILABLE" in validated_statuses or not validated_statuses:
            aggregate_health = "UNAVAILABLE"
        elif "OUTAGE" in validated_statuses:
            aggregate_health = "OUTAGE"
        elif "DATA_ERROR" in validated_statuses:
            aggregate_health = "DATA_ERROR"
        elif "STALE" in validated_statuses:
            aggregate_health = "STALE"
        elif "DELAYED" in validated_statuses:
            aggregate_health = "DELAYED"
        elif "LUNCH_BREAK" in validated_statuses:
            aggregate_health = "LUNCH_BREAK"
        else:
            aggregate_health = "CLOSED"
        self._health["intraday"] = aggregate_health
        if aggregate_health == "REALTIME":
            self._errors.pop("intraday", None)
        else:
            self._errors["intraday"] = (
                f"intraday quotes are {aggregate_health.lower()}"
            )
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

    def _validated_realtime_quote(
        self,
        raw: Mapping[str, object] | None,
        now: datetime,
    ) -> tuple[bool, str | None, str]:
        if raw is not None and raw.get("health_status") == "DATA_ERROR":
            timestamp = raw.get("timestamp")
            return False, timestamp if type(timestamp) is str else None, "DATA_ERROR"
        if self._closed_dates is None:
            return False, None, "UNAVAILABLE"
        now_time = now.timetz().replace(tzinfo=None)
        if (
            self._is_trading_date(now.date())
            and time(11, 30) < now_time < time(13, 0)
        ):
            timestamp = raw.get("timestamp") if raw is not None else None
            return (
                False,
                timestamp if type(timestamp) is str else None,
                "LUNCH_BREAK",
            )
        if (
            not self._is_trading_date(now.date())
            or not self._in_continuous_session(now_time)
        ):
            timestamp = raw.get("timestamp") if raw is not None else None
            return (
                False,
                timestamp if type(timestamp) is str else None,
                "CLOSED",
            )
        if raw is None:
            return False, None, "UNAVAILABLE"
        status = raw.get("health_status")
        raw_timestamp = raw.get("timestamp")
        if type(raw_timestamp) is not str:
            return False, None, "UNAVAILABLE"
        if type(status) is not str:
            return False, raw_timestamp, "UNAVAILABLE"
        minute_basis = raw.get("timestamp_basis") == "MINUTE_START"
        if "timestamp_basis" in raw and not minute_basis:
            return False, raw_timestamp, "UNAVAILABLE"
        try:
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return False, raw_timestamp, "UNAVAILABLE"
            local_timestamp = timestamp.astimezone(SHANGHAI)
            age = (now - local_timestamp).total_seconds()
        except Exception:
            return False, raw_timestamp, "UNAVAILABLE"
        if status in {"DELAYED", "STALE", "OUTAGE"}:
            return False, raw_timestamp, status
        if status != "REALTIME":
            return False, raw_timestamp, "UNAVAILABLE"
        if minute_basis:
            if (
                local_timestamp.date() != now.date()
                or not self._in_continuous_session(
                    local_timestamp.timetz().replace(tzinfo=None),
                )
            ):
                return False, raw_timestamp, "STALE"
            minute_health = MarketHealthClassifier(self._closed_dates).classify(
                now, local_timestamp, None, completed_minute=True,
            )
            return minute_health.status == "REALTIME", raw_timestamp, minute_health.status
        if (
            local_timestamp.date() != now.date()
            or not self._in_continuous_session(
                local_timestamp.timetz().replace(tzinfo=None),
            )
            or age > REALTIME_MAX_AGE_SECONDS
        ):
            return False, raw_timestamp, "STALE"
        if age < -_REALTIME_FUTURE_SKEW_SECONDS:
            return False, raw_timestamp, "UNAVAILABLE"
        return True, raw_timestamp, "REALTIME"

    @staticmethod
    def _in_continuous_session(value: time) -> bool:
        return bool(
            time(9, 30) <= value <= time(11, 30)
            or time(13, 0) <= value <= time(15, 0)
        )

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
        effective = self._fallback_now() if now is None else now
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
            raise

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

    def _build_snapshot(
        self,
        now: datetime,
        *,
        quality_now: datetime | None = None,
    ) -> dict[str, object]:
        current = self.published if hasattr(self, "published") else {}
        history_by_symbol = self._bars_by_symbol()
        enabled_histories = {
            watch.symbol: history_by_symbol.get(watch.symbol, ())
            for watch in self._watchlist if watch.enabled
        }
        valuation_by_index = self._valuation_store.load(
            today=now.astimezone(SHANGHAI).date(),
        )
        current_items = {
            str(item.get("symbol")): item
            for item in current.get("items", [])
            if isinstance(item, Mapping)
        }
        # ``now`` is supplied by the producer boundary and is already the
        # trusted observation time for this snapshot.  Do not call the clock
        # again here: doing so can make a staged failure consume a new clock
        # value and can produce a partially advanced quality timestamp.
        quality_reference = now if quality_now is None else quality_now
        v11_shared: dict[str, object] | None = None
        if self._v11_config is not None:
            try:
                v11_shared = self._v11_shared_context()
            except Exception:
                # Each symbol recomputes and reports V11_DATA_ERROR itself.
                v11_shared = None
        items: list[dict[str, object]] = []
        for watch in self._watchlist:
            if not watch.enabled:
                continue
            formal = self._formal.get(watch.symbol)
            if formal is None:
                continue
            previous = current_items.get(watch.symbol, {})
            metadata = self._metadata.get(watch.symbol)
            bars_for_symbol = tuple(
                bar for bar in enabled_histories[watch.symbol]
                if bar.symbol == watch.symbol
            )
            receipt = self._research_receipt(watch.symbol)
            quality = (
                summarize_history_quality(
                    enabled_histories[watch.symbol], self._strategy, receipt=receipt,
                )
                if self._strategy is not None else None
            )
            if quality is not None and self._strategy is not None:
                metadata_errors = validate_v11_metadata(
                    {watch.symbol: metadata} if metadata is not None else {},
                    (watch.symbol,),
                )
                verified_quality = assess_verified_quality(
                    bars_for_symbol,
                    today=quality_reference,
                    closed_dates=self._closed_dates or frozenset(),
                    metadata_status="PASSED" if not metadata_errors else "FAILED",
                    environment_histories=self._index_history,
                    receipt=receipt,
                    minimum_daily_bars=250,
                )
                quality = {**quality, **verified_quality}
            valuation = self._valuation_snapshot(metadata, valuation_by_index)
            v11 = self._v11_snapshot(
                watch.symbol,
                bars_for_symbol,
                metadata,
                previous,
                data_quality=quality,
                valuation=valuation,
                shared=v11_shared,
            )
            items.append({
                "symbol": watch.symbol,
                "name": metadata.name if metadata is not None else watch.symbol,
                "formal_state": formal.state.value,
                "formal_decision": formal.to_dict(),
                "v11": v11,
                "data_quality": quality,
                "valuation": valuation,
                "indicators": self._indicator_snapshot(
                    watch.symbol, enabled_histories[watch.symbol],
                ),
                "formal": {
                    "strategy_version": formal.strategy_version,
                    "state": formal.state.value,
                    "decision": formal.to_dict(),
                },
                "shadow": self._shadow_snapshot(
                    watch.symbol,
                    enabled_histories[watch.symbol],
                    quality,
                    valuation,
                    formal,
                ),
                "signal_data_date": (
                    formal.as_of_trading_date.isoformat()
                    if formal.as_of_trading_date is not None else None
                ),
                "blocked_reasons": list(formal.blocked_reasons),
                "execution_status": self._execution_status(
                    formal, now,
                    market_realtime=self._health["intraday"] == "REALTIME",
                ),
                "intraday_overlay": (
                    None if self._holdings_snapshot_present() else previous.get("intraday_overlay")
                ),
                "reported_holding": copy.deepcopy(self._snapshot_position(watch.symbol)),
                "current_price": previous.get("current_price"),
                "current_price_time": previous.get("current_price_time"),
                "intraday_health_status": previous.get(
                    "intraday_health_status", "UNAVAILABLE",
                ),
            })
        history_coverage = (
            summarize_common_history(enabled_histories, self._strategy)
            if self._strategy is not None else None
        )
        diagnostics = summarize_strategy_diagnostics(items, history_coverage, self._health)
        alert_history, active_alerts = self._alert_snapshot(
            now, include_retracted=True,
        )
        if self._holdings_snapshot_present():
            active_alerts = []
            for alert in alert_history:
                alert.update(currently_active=False, active=False, active_notification=False)
        alert_items = [
            copy.deepcopy(item) for item in alert_history
            if not item.get("retracted")
        ]
        v11_state_counts: dict[str, int] = {}
        v11_candidate_count = 0
        v11_data_unavailable = 0
        v11_position_actions: dict[str, int] = {}
        for item in items:
            decision = item.get("v11", {}).get("decision") or {}
            state = str(decision.get("state") or item.get("v11", {}).get("status") or "UNKNOWN")
            v11_state_counts[state] = v11_state_counts.get(state, 0) + 1
            if state in {"TECHNICAL_CANDIDATE", "ACTION_CANDIDATE", "POSITION_ACTION"}:
                v11_candidate_count += 1
            if state == "DATA_UNAVAILABLE":
                v11_data_unavailable += 1
            if state == "POSITION_ACTION":
                action = str(decision.get("action") or "UNKNOWN")
                v11_position_actions[action] = v11_position_actions.get(action, 0) + 1
        v11_environment_history = (
            dict(v11_shared["environment_history"])
            if v11_shared is not None
            and isinstance(v11_shared.get("environment_history"), Mapping)
            else normalize_environment_state(self._v11_state.get("environment"))
        )
        v11_summary = {
            "strategy_version": "SWING_V11_SHADOW",
            "enabled_count": len(items),
            "state_counts": v11_state_counts,
            "candidate_count": v11_candidate_count,
            "position_action_counts": v11_position_actions,
            "data_unavailable_count": v11_data_unavailable,
            "environment": (
                dict(v11_shared["environment"])
                if v11_shared is not None and isinstance(v11_shared.get("environment"), Mapping)
                else None
            ),
            "environment_history": copy.deepcopy(v11_environment_history),
            "defense_recovery_sessions": (
                v11_shared.get("defense_recovery_sessions") if v11_shared is not None else None
            ),
            "executable": False,
        }
        watchlist_view = {
            "items": [
                {"symbol": item.symbol, "enabled": item.enabled}
                for item in self._watchlist
            ],
            "read_only": False,
            "revision": self.revision,
        }
        portfolio_view = {
            "status": self._health["portfolio"],
            "projection": (
                self._portfolio_projection.to_dict()
                if self._portfolio_projection is not None else None
            ),
            "error": copy.deepcopy(self._errors.get("portfolio")),
            "revision": self.revision,
            "local_only": True,
            "holdings_snapshot": copy.deepcopy(self._holdings_snapshot),
        }
        alert_current_view = {
            "status": self._health["alerts"],
            "items": alert_items,
            "revision": self.revision,
            "local_only": True,
        }
        alert_history_view = {
            "status": self._health["alerts"],
            "items": alert_history,
            "revision": self.revision,
            "local_only": True,
        }
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
            "daily_history_digest": self._enabled_history_digest(),
            "history_coverage": history_coverage,
            "diagnostics": diagnostics,
            "health": copy.deepcopy(self._health),
            "environment_history": self._environment_history_summary(),
            "errors": copy.deepcopy(self._errors),
            "portfolio": (
                self._portfolio_projection.to_dict()
                if self._portfolio_projection is not None else None
            ),
            "holdings_snapshot": copy.deepcopy(self._holdings_snapshot),
            "available_symbols": [
                {
                    "symbol": symbol, "name": metadata.name,
                    "can_enable": self._watch_history_ready(symbol),
                    "daily_count": len(history_by_symbol.get(symbol, ())),
                    "minimum_daily_bars": (
                        self._strategy.minimum_daily_bars if self._strategy is not None else None
                    ),
                    "latest_daily_date": (
                        history_by_symbol[symbol][-1].trading_date.isoformat()
                        if history_by_symbol.get(symbol) else None
                    ),
                    "enable_block_reason": (
                        None if self._watch_history_ready(symbol)
                        else "策略配置不可用，请先恢复配置" if self._strategy is None
                        else "日线校验未通过，请修复数据后再启用"
                        if self._health.get("daily") != "OK"
                        else "先独立补齐并校验已完成日线，再启用监控；等待或刷新不会自动补齐"
                    ),
                }
                for symbol, metadata in self._metadata.items()
            ],
            "pending_instruments": copy.deepcopy(list(self._pending_instruments.values())),
            "items": items,
            "alerts": copy.deepcopy(active_alerts),
            "active_alerts": active_alerts,
            "v11_summary": v11_summary,
            "v11_environment_history": copy.deepcopy(v11_environment_history),
            "alert_counts": {
                "active": len(active_alerts),
                "current": len(alert_items),
                "history": len(alert_history),
            },
            "watchlist": watchlist_view,
            "read_only_market_data": True,
            _READ_MODEL_KEY: {
                "watchlist": watchlist_view,
                "portfolio": portfolio_view,
                "alerts_current": alert_current_view,
                "alerts_history": alert_history_view,
            },
        }

    def _research_receipt(self, symbol: str) -> Mapping[str, object] | None:
        """Load an optional per-symbol research receipt without trusting it.

        The receipt is only evidence for ``assess_verified_quality``; its
        crosscheck, adjustment and warning fields are independently checked by
        that gate.  Missing manifests therefore remain a normal, safe state.
        """
        manifest_path = self.paths.strategy.with_name("research_manifest.json")
        if not manifest_path.exists():
            fallback = Path(__file__).resolve().parents[2] / "data" / "swing" / "research_manifest.json"
            manifest_path = fallback
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        generated_at = payload.get("generated_at")
        for item in payload.get("items", ()):
            if not isinstance(item, Mapping) or item.get("symbol") != symbol:
                continue
            source = item.get("source")
            if isinstance(source, list):
                source = "; ".join(str(value) for value in source)
            return {
                "source": source,
                "checked_at": generated_at,
                "sample_start": item.get("history_start"),
                "sample_end": item.get("history_end"),
                "crosscheck_status": item.get("crosscheck_status"),
                "adjustment_status": item.get("adjustment_status"),
                "amount_quality": item.get("amount_quality"),
                "calculation_version": item.get("data_version"),
                "warnings": item.get("warnings", ()),
            }
        return None

    def _v11_snapshot(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
        metadata: EtfMetadata | None,
        previous: Mapping[str, object],
        data_quality: Mapping[str, object] | None = None,
        valuation: Mapping[str, object] | None = None,
        shared: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        quasi_close_available = False
        raw_time = previous.get("current_price_time")
        quasi_close_date: str | None = None
        try:
            observed_at = parse_v11_observed_at(raw_time)
            if observed_at is None:
                raise ValueError("quasi-close timestamp must be timezone-aware")
            now = self.clock()
            quasi_close_date = observed_at.date().isoformat()
            quasi_close_available = bool(
                previous.get("current_price") is not None
                and self._health.get("intraday") == "REALTIME"
                and observed_at.date() == now.astimezone(SHANGHAI).date()
                and time(14, 45) <= observed_at.timetz().replace(tzinfo=None) <= time(15, 0)
            )
        except Exception:
            quasi_close_available = False
        quality_status = (
            str(data_quality.get("status"))
            if isinstance(data_quality, Mapping)
            and data_quality.get("status") in {"VERIFIED", "UNVERIFIED"}
            else "UNKNOWN"
        )
        quality_reasons = [
            reason for reason in (
                data_quality.get("reasons", ())
                if isinstance(data_quality, Mapping) else ()
            )
            if isinstance(reason, str)
        ]
        base = {
            "strategy_version": "SWING_V11_SHADOW",
            "status": "DATA_UNAVAILABLE",
            "data_quality_status": quality_status,
            "executable": False,
            "as_of_kind": "QUASI_CLOSE_1445" if quasi_close_available else "COMPLETED_DAILY",
            "as_of_trading_date": (
                quasi_close_date if quasi_close_available and quasi_close_date else
                bars[-1].trading_date.isoformat() if bars else None
            ),
            "blocked_reasons": [*quality_reasons, "V11_CONFIG_UNAVAILABLE"],
            "decision": None,
            "quasi_close_available": quasi_close_available,
        }
        config = self._v11_config
        if config is None:
            return base
        if not bars:
            base["blocked_reasons"] = [*quality_reasons, "NO_COMPLETED_BARS"]
            return base
        try:
            indicators = calculate_v11_indicators(
                bars, closed_dates=self._closed_dates,
                pullback_window_max=config.pullback_window_max,
                box_days=config.box_days,
            )
            indicator = normalize_v11_indicators(indicators)
            latest = bars[-1]
            if quasi_close_available and previous.get("current_price") is not None:
                # Moving averages/weekly evidence remain based on completed
                # daily bars, while the entry price gate uses the verified
                # 14:45 observation.  This prevents a stale prior close from
                # producing a live candidate.
                indicator = {
                    **indicator,
                    "price": float(previous["current_price"]),
                    "quasi_close_price": float(previous["current_price"]),
                    "price_source": "QUASI_CLOSE_1445",
                }
            if shared is None:
                shared = self._v11_shared_context()
            environment = shared["environment"]
            environment_bars = shared["environment_bars"]
            rs = None
            if metadata is not None and metadata.environment_index:
                # Brokerage ETFs may use either index; CSI 300 is the
                # relative-strength benchmark in that case.
                rs_index = (
                    "000300" if metadata.environment_index == "ANY"
                    else metadata.environment_index
                )
                rs = calculate_relative_strength_20(
                    bars, environment_bars.get(rs_index, ()),
                )
            as_of_date = (
                date.fromisoformat(quasi_close_date)
                if quasi_close_available and quasi_close_date
                else latest.trading_date
            )
            projection = shared.get("projection")
            held = shared.get("held") if isinstance(shared.get("held"), Mapping) else {}
            projected = held.get(symbol)
            held_groups = shared.get("held_groups")
            other_groups = tuple(sorted({
                group for other, group in held_groups.items() if other != symbol
            })) if isinstance(held_groups, Mapping) else ()
            ledger_summary = shared.get("ledger_summary")
            if not isinstance(ledger_summary, Mapping):
                ledger_summary = {}
            consecutive_losses = int(ledger_summary.get("consecutive_losses", 0) or 0)
            last_loss_date = ledger_summary.get("last_loss_date")
            entry_pause = 0
            if consecutive_losses >= V11_F2_CONSECUTIVE_LOSSES and isinstance(last_loss_date, date):
                entry_pause = self._v11_remaining_sessions(
                    last_loss_date, as_of_date, V11_F2_CONSECUTIVE_LOSSES,
                )
            last_stop_date = (
                self._last_stop_trading_date(symbol, bars)
                if shared.get("ledger_healthy") else None
            )
            reentry_cooldown = self._v11_remaining_sessions(
                last_stop_date, as_of_date, config.cooldown_sessions,
            )
            metadata_errors = validate_v11_metadata(
                {symbol: metadata} if metadata is not None else {}, (symbol,),
            )
            quality_reasons = tuple(
                reason for reason in (
                    data_quality.get("reasons", ())
                    if isinstance(data_quality, Mapping) else ()
                )
                if isinstance(reason, str)
            )
            context = V11Context(
                # Metadata completeness does not prove provider/history quality;
                # the V11 service remains fail-closed until its quality gate
                # supplies an explicit VERIFIED status.
                data_quality=quality_status,
                environment_state=str(environment.get("state", "UNKNOWN")),
                category=(
                    metadata.category if metadata is not None else None
                ),
                correlation_group=(
                    metadata.correlation_group if metadata is not None else None
                ),
                relative_strength_20=rs,
                account_known=self._health.get("portfolio") == "OK",
                equity_cny=(
                    float(self._portfolio_projection.equity)
                    if self._portfolio_projection is not None else 0.0
                ),
                cash_cny=(
                    float(self._portfolio_projection.cash)
                    if self._portfolio_projection is not None else 0.0
                ),
                has_position=projected is not None,
                sellable_shares=(
                    int(projected.sellable_shares) if projected is not None else 0
                ),
                environment_index=(
                    metadata.environment_index if metadata is not None else None
                ),
                environment_states=dict(environment.get("latest_states") or {}),
                defense_recovery_sessions=shared.get("defense_recovery_sessions"),
                open_position_count=len(held),
                etf_market_value_cny=(
                    float(projection.etf_market_value)
                    if isinstance(projection, PortfolioProjection) else 0.0
                ),
                symbol_market_value_cny=(
                    float(projected.market_value) if projected is not None else 0.0
                ),
                held_correlation_groups=other_groups,
                reentry_cooldown_sessions=reentry_cooldown,
                consecutive_losses=consecutive_losses,
                entry_pause_sessions=entry_pause,
                long_holiday_sessions_ahead=self._v11_long_holiday_sessions_ahead(as_of_date),
                ex_dividend_window=self._v11_ex_dividend_window(metadata, as_of_date),
                premium_pct=None,
                indicator=indicator,
                as_of_kind=str(base["as_of_kind"]),
                as_of_trading_date=(
                    quasi_close_date if quasi_close_available and quasi_close_date
                    else latest.trading_date.isoformat()
                ),
                quasi_close={
                    "price": previous.get("current_price"),
                    "observed_at": previous.get("current_price_time"),
                    "health": self._health.get("intraday"),
                },
                metadata=(metadata.to_dict() if metadata is not None else {}),
                valuation_stage=(
                    valuation.get("valuation_stage")
                    if isinstance(valuation, Mapping) else None
                ),
            )
            context = replace(context, metadata={
                **context.metadata,
                "v11_metadata_errors": metadata_errors,
                "environment": environment,
                "relative_strength_20": rs,
                "data_quality_reasons": quality_reasons,
                "valuation": dict(valuation) if isinstance(valuation, Mapping) else {},
                "ledger_error": shared.get("ledger_error"),
            })
            decision = evaluate_v11(bars, config=config, context=context)
            raw_scale = latest.close / latest.adjusted_close
            current_price_adjusted = (
                float(previous["current_price"]) / raw_scale
                if quasi_close_available and previous.get("current_price") is not None
                and math.isfinite(raw_scale) and raw_scale > 0.0
                else latest.adjusted_close
            )
            position_snapshot = self._v11_position_snapshot(
                symbol, bars, metadata,
                config=config, context=context, shared=shared,
                indicator=indicator, previous=previous,
                current_price_adjusted=current_price_adjusted,
                as_of=as_of_date,
            )
            base.update({
                "status": "AVAILABLE",
                "data_quality_status": context.data_quality,
                "as_of_trading_date": (
                    quasi_close_date if quasi_close_available and quasi_close_date
                    else latest.trading_date.isoformat()
                ),
                "decision": decision.to_dict(),
                "entry_decision": decision.to_dict(),
                "blocked_reasons": list(decision.blocked_reasons),
                "evidence": dict(decision.evidence),
                "position_decision": None,
                "position_state": None,
            })
            if position_snapshot is not None:
                base["position_decision"] = position_snapshot
                base["position_state"] = position_snapshot.get("state")
                if position_snapshot.get("status") == "AVAILABLE":
                    # A held ETF's daily action is the position decision;
                    # the entry evaluation stays available as evidence.
                    base["decision"] = position_snapshot["decision"]
                    base["blocked_reasons"] = list(position_snapshot["blocked_reasons"])
                    base["evidence"] = dict(position_snapshot["evidence"])
            return base
        except Exception as error:
            base["blocked_reasons"] = ["V11_DATA_ERROR"]
            base["error"] = self._safe_error(error)
            return base

    def _index_history_path(self) -> Path:
        return self.paths.daily_history.with_name("index_quotes.jsonl")

    def _environment_history_summary(self) -> dict[str, str | None]:
        summary: dict[str, str | None] = {}
        for code in _ENVIRONMENT_INDEX_CODES:
            bars = self._index_history.get(code, ())
            summary[code] = bars[-1].trading_date.isoformat() if bars else None
        return summary

    def _index_history_current(self, target: date) -> bool:
        for code in _ENVIRONMENT_INDEX_CODES:
            bars = self._index_history.get(code, ())
            if not bars or bars[-1].trading_date != target:
                return False
        return True

    def _reload_index_history(self) -> None:
        """Reload CSI 300/1000 bars from the index file when it exists.

        A missing file keeps constructor-injected history so tests can supply
        evidence without a runtime collector.  A present file replaces that
        cache, including an empty file.
        """
        path = self._index_history_path()
        if not path.exists():
            return
        try:
            bars = IndexHistoryStore(path).load()
        except Exception as error:
            self._errors["environment_history"] = self._safe_error(error)
            return
        grouped: dict[str, list[DailyBar]] = {}
        for bar in bars:
            if bar.symbol in _ENVIRONMENT_INDEX_CODES:
                grouped.setdefault(bar.symbol, []).append(bar)
        self._index_history = {
            code: tuple(sorted(items, key=lambda item: item.trading_date))
            for code, items in grouped.items()
        }
        self._errors.pop("environment_history", None)

    def _collect_index_history(self, target: date) -> None:
        collector = self.collector
        collect_indices = getattr(collector, "collect_indices", None)
        if collector is None or not callable(collect_indices):
            return
        try:
            strategy_count = (
                0 if self._strategy is None
                else self._strategy.walk_forward_train_days
                + self._strategy.walk_forward_test_days
                + self._strategy.walk_forward_step_days
            )
            records = collect_indices(target, max(_DEFAULT_HISTORY_COUNT, strategy_count))
            IndexHistoryStore(self._index_history_path()).upsert(records)
            self._reload_index_history()
        except Exception as error:
            self._errors["environment_history"] = self._safe_error(error)

    def _crosscheck_receipts_path(self) -> Path:
        return self.paths.strategy.with_name("crosscheck_receipts.json")

    def _record_crosscheck_receipts(
        self,
        merged: Sequence[DailyBar],
        enabled: Sequence[SwingWatchItem],
        target: date,
        count: int,
        now: datetime,
    ) -> tuple[str, str | None]:
        """Compare the stored history with the independent provider.

        Receipts are evidence for the research manifest, which the verified
        gate reads.  A collector without ``collect_independent`` or a failed
        fetch leaves the receipt for that symbol absent, so the manifest falls
        back to ``PENDING`` and the gate stays closed.  Nothing here changes
        canonical bars.  Returns the component health and error text for the
        staged snapshot.
        """
        collect_independent = getattr(self.collector, "collect_independent", None)
        if not callable(collect_independent):
            return "NOT_RUN", None
        by_symbol: dict[str, list[DailyBar]] = {}
        for bar in merged:
            by_symbol.setdefault(bar.symbol, []).append(bar)
        receipts: list[CrosscheckReceipt] = []
        failures: dict[str, str] = {}
        for item in enabled:
            bars = tuple(sorted(
                by_symbol.get(item.symbol, ()), key=lambda bar: bar.trading_date,
            ))
            metadata = self._metadata.get(item.symbol)
            if not bars or metadata is None:
                failures[item.symbol] = "NO_HISTORY" if not bars else "NO_METADATA"
                continue
            try:
                independent = collect_independent(item.symbol, target, count)
                receipts.append(crosscheck_history(
                    bars, independent,
                    source=INDEPENDENT_SOURCE_LABEL,
                    checked_at=now,
                    price_tick=float(metadata.trading.price_tick),
                ))
            except Exception as error:
                # Transport errors may quote the remote reply; keep the type.
                failures[item.symbol] = type(error).__name__
        try:
            write_receipts(self._crosscheck_receipts_path(), receipts, generated_at=now)
        except Exception as error:
            return "WRITE_FAILED", self._safe_error(error)
        if failures:
            return "PARTIAL", "; ".join(
                f"{symbol}: {reason}" for symbol, reason in sorted(failures.items())
            )
        return "OK", None

    def _rebuild_research_manifest(self) -> None:
        """Refresh the research receipt after a completed daily collection.

        Failure stays on the research manifest and does not roll back bars.
        The verified gate still requires a provider amount, a passed crosscheck
        and a verified adjustment; rebuilding only keeps the sample digest
        aligned with the history that was just stored.
        """
        try:
            builder = _load_research_manifest_builder()
            output = self.paths.strategy.with_name("research_manifest.json")
            builder(
                self.paths.daily_history,
                self.paths.watchlist,
                metadata_path=self.paths.metadata,
                output_path=output,
                calendar_path=self.paths.calendar,
            )
            self._errors.pop("research_manifest", None)
        except Exception as error:
            self._errors["research_manifest"] = self._safe_error(error)

    @staticmethod
    def _shadow_valuation_stage(valuation: Mapping[str, object] | None) -> str:
        if not isinstance(valuation, Mapping):
            return "UNKNOWN"
        stage_payload = valuation.get("valuation_stage")
        if isinstance(stage_payload, Mapping):
            stage = stage_payload.get("stage")
            if isinstance(stage, str) and stage:
                return stage
        return "UNKNOWN"

    def _v11_environment_context(
        self,
    ) -> tuple[dict[str, object], dict[str, tuple[DailyBar, ...]]]:
        """Build CSI 300/1000 environment evidence from completed history."""
        by_code: dict[str, tuple[DailyBar, ...]] = {}
        for code in ("000300", "000852"):
            selected = self._index_history.get(code, ())
            if selected:
                by_code[code] = tuple(sorted(selected, key=lambda item: item.trading_date))
        indicators: dict[str, tuple[Mapping[str, object], ...]] = {}
        for code, bars in by_code.items():
            if len(bars) < 2:
                continue
            previous = normalize_v11_indicators(calculate_v11_indicators(
                bars[:-1], closed_dates=self._closed_dates,
            ))
            latest = normalize_v11_indicators(calculate_v11_indicators(
                bars, closed_dates=self._closed_dates,
            ))
            indicators[code] = (previous, latest)
        confirmation_days = (
            self._v11_config.environment_confirmation_days
            if self._v11_config is not None else 1
        )
        return (
            calculate_v11_environment(indicators, confirmation_days=confirmation_days),
            by_code,
        )

    # ---- V11 portfolio / calendar evidence ------------------------------

    def _v11_state_store(self) -> V11StateStore:
        return V11StateStore(self.paths.strategy.with_name("v11_positions.json"))

    def _v11_sessions_between(self, start: date, end: date) -> int | None:
        """Count trading sessions in ``[start, end]``; ``None`` without a calendar."""
        if self._closed_dates is None or end < start:
            return None
        count = 0
        candidate = start
        for _ in range(400):
            if candidate > end:
                break
            if self._is_trading_date(candidate):
                count += 1
            candidate += timedelta(days=1)
        return count

    def _v11_remaining_sessions(
        self, anchor: date | None, as_of: date | None, total: int,
    ) -> int:
        """Sessions of a ``total``-session pause that remain after ``as_of``.

        The anchor session itself does not count; the pause covers the next
        ``total`` sessions after it.
        """
        if anchor is None or as_of is None or total <= 0 or as_of <= anchor:
            return total if anchor is not None and as_of is not None else 0
        elapsed = self._v11_sessions_between(anchor + timedelta(days=1), as_of)
        if elapsed is None:
            return 0
        return max(0, total - elapsed)

    def _v11_long_holiday_sessions_ahead(self, as_of: date | None) -> int | None:
        """1 on the last session before a long holiday, 2 the session before."""
        if as_of is None:
            return None
        first = self._next_trading_date(as_of)
        if first is None:
            return None
        if (first - as_of).days - 1 >= 4:
            return 1
        second = self._next_trading_date(first)
        if second is not None and (second - first).days - 1 >= 4:
            return 2
        return None

    def _v11_ex_dividend_window(
        self, metadata: EtfMetadata | None, as_of: date | None,
    ) -> bool:
        if metadata is None or as_of is None or not metadata.dividend_dates:
            return False
        window = {as_of}
        first = self._next_trading_date(as_of)
        if first is not None:
            window.add(first)
            second = self._next_trading_date(first)
            if second is not None:
                window.add(second)
        for raw in metadata.dividend_dates:
            try:
                if date.fromisoformat(raw) in window:
                    return True
            except ValueError:
                continue
        return False

    def _v11_ledger_summary(self) -> dict[str, object]:
        """Derive closed-cycle results from the ledger for handbook F1/F2.

        Average-cost accounting per symbol; a cycle closes when shares reach
        zero.  Consecutive losses count trailing closed cycles across the
        whole account ordered by closing time.
        """
        result: dict[str, object] = {
            "consecutive_losses": 0,
            "last_loss_date": None,
            "open_cycle_buys": {},
        }
        if self._ledger is None:
            return result
        events = self._ledger.load_events()
        reversed_ids = {
            str(event.payload["target_event_id"])
            for event in events
            if event.event_type is PortfolioEventType.TRADE_REVERSED
        }
        shares: dict[str, int] = {}
        cost: dict[str, float] = {}
        cycle_pnl: dict[str, float] = {}
        cycle_buys: dict[str, int] = {}
        closed: list[tuple[datetime, float]] = []
        if events:
            initial = events[0].payload.get("initial_positions", {})
            if isinstance(initial, Mapping):
                for symbol, raw in initial.items():
                    if not isinstance(raw, Mapping) or type(raw.get("shares")) is not int:
                        continue
                    count = int(raw["shares"])
                    average = raw.get("average_cost")
                    if count <= 0 or type(average) not in (int, float):
                        continue
                    shares[str(symbol)] = count
                    cost[str(symbol)] = float(average) * count
                    cycle_buys[str(symbol)] = 1
        trades: list[tuple[datetime, Any]] = []
        for event in events:
            if event.event_type not in {
                PortfolioEventType.BUY_CONFIRMED, PortfolioEventType.SELL_CONFIRMED,
            } or event.event_id in reversed_ids:
                continue
            payload = event.payload
            raw_time = payload.get("executed_at")
            if type(raw_time) is not str:
                continue
            try:
                executed_at = datetime.fromisoformat(raw_time)
            except ValueError:
                continue
            trades.append((executed_at, event))
        trades.sort(key=lambda item: item[0])
        for executed_at, event in trades:
            payload = event.payload
            symbol = str(payload.get("symbol"))
            count = payload.get("shares")
            price = payload.get("price")
            fee = payload.get("fee", 0.0)
            if type(count) is not int or type(price) not in (int, float):
                continue
            fee_value = float(fee) if type(fee) in (int, float) else 0.0
            held = shares.get(symbol, 0)
            if event.event_type is PortfolioEventType.BUY_CONFIRMED:
                if held == 0:
                    cost[symbol] = 0.0
                    cycle_pnl[symbol] = 0.0
                    cycle_buys[symbol] = 0
                shares[symbol] = held + count
                cost[symbol] = cost.get(symbol, 0.0) + float(price) * count + fee_value
                cycle_buys[symbol] = cycle_buys.get(symbol, 0) + 1
                continue
            if held <= 0:
                continue
            average = cost.get(symbol, 0.0) / held
            sold = min(count, held)
            cycle_pnl[symbol] = (
                cycle_pnl.get(symbol, 0.0) + (float(price) - average) * sold - fee_value
            )
            shares[symbol] = held - sold
            cost[symbol] = average * shares[symbol]
            if shares[symbol] == 0:
                closed.append((executed_at, cycle_pnl.pop(symbol, 0.0)))
                cycle_buys.pop(symbol, None)
        closed.sort(key=lambda item: item[0])
        losses = 0
        last_loss: date | None = None
        for executed_at, pnl in reversed(closed):
            if pnl >= 0.0:
                break
            losses += 1
            if last_loss is None:
                last_loss = executed_at.astimezone(SHANGHAI).date()
        result["consecutive_losses"] = losses
        result["last_loss_date"] = last_loss
        result["open_cycle_buys"] = {
            symbol: count for symbol, count in cycle_buys.items()
            if shares.get(symbol, 0) > 0
        }
        return result

    def _v11_shared_context(self) -> dict[str, object]:
        """Evidence shared by every symbol in one snapshot."""
        environment, environment_bars = self._v11_environment_context()
        environment_as_of: str | None = None
        for bars in environment_bars.values():
            if bars:
                latest = bars[-1].trading_date.isoformat()
                if environment_as_of is None or latest > environment_as_of:
                    environment_as_of = latest
        environment_history = advance_environment_state(
            self._v11_state.get("environment"),
            state=environment.get("state"),
            as_of_trading_date=environment_as_of,
        )
        recovery_sessions: int | None = None
        started = environment_history.get("defense_recovery_started")
        if (
            isinstance(started, str) and environment_history.get("state") == "NEUTRAL"
            and environment_as_of is not None
        ):
            recovery_sessions = self._v11_sessions_between(
                date.fromisoformat(started), date.fromisoformat(environment_as_of),
            )
        ledger_healthy = (
            self._health.get("portfolio") == "OK"
            and self._portfolio_projection is not None
        )
        projection = self._portfolio_projection if ledger_healthy else None
        held: dict[str, PortfolioPosition] = {}
        if projection is not None:
            held = {
                symbol: position for symbol, position in projection.positions.items()
                if position.shares > 0
            }
        held_groups: dict[str, str] = {}
        for symbol in held:
            metadata = self._metadata.get(symbol)
            if metadata is not None and metadata.correlation_group:
                held_groups[symbol] = metadata.correlation_group
        ledger_summary: dict[str, object] = {
            "consecutive_losses": 0, "last_loss_date": None, "open_cycle_buys": {},
        }
        ledger_error: str | None = None
        if ledger_healthy:
            try:
                ledger_summary = self._v11_ledger_summary()
            except Exception as error:
                ledger_error = self._safe_error(error)
        return {
            "environment": environment,
            "environment_bars": environment_bars,
            "environment_history": environment_history,
            "defense_recovery_sessions": recovery_sessions,
            "ledger_healthy": ledger_healthy,
            "projection": projection,
            "held": held,
            "held_groups": held_groups,
            "ledger_summary": ledger_summary,
            "ledger_error": ledger_error,
        }

    def _v11_position_snapshot(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
        metadata: EtfMetadata | None,
        *,
        config: Any,
        context: V11Context,
        shared: Mapping[str, object],
        indicator: Mapping[str, object],
        previous: Mapping[str, object],
        current_price_adjusted: float,
        as_of: date | None,
    ) -> dict[str, object] | None:
        """Evaluate the held position and project its next persisted state."""
        held = shared.get("held")
        projected = held.get(symbol) if isinstance(held, Mapping) else None
        if projected is None or not bars:
            return None
        latest = bars[-1]
        raw_scale = latest.close / latest.adjusted_close
        if not math.isfinite(raw_scale) or raw_scale <= 0.0:
            return {"status": "UNAVAILABLE", "blocked_reasons": ["ADJUSTMENT_SCALE_INVALID"]}
        try:
            position_context = self._position_context(symbol, projected, bars)
        except PortfolioLedgerError as error:
            return {
                "status": "UNAVAILABLE",
                "blocked_reasons": ["POSITION_LIFECYCLE_INVALID"],
                "error": self._safe_error(error),
            }
        if position_context is None:
            return {"status": "UNAVAILABLE", "blocked_reasons": ["POSITION_RISK_UNKNOWN"]}
        entry_date = position_context.entry_trading_date
        stored_positions = self._v11_state.get("positions")
        stored = (
            normalize_position_state(stored_positions.get(symbol))
            if isinstance(stored_positions, Mapping) else None
        )
        if stored is not None and stored.get("entry_trading_date") != entry_date.isoformat():
            stored = None
        since_entry = [bar for bar in bars if bar.trading_date >= entry_date]
        holding_session = len(since_entry)
        if as_of is not None and as_of > latest.trading_date:
            holding_session += 1
        holding_session = max(1, holding_session)
        entry_session_high = (
            since_entry[0].adjusted_high if since_entry else latest.adjusted_high
        )
        later_closes = [bar.adjusted_close for bar in since_entry[1:]]
        peak_price = max(
            position_context.highest_completed_adjusted_close, current_price_adjusted,
        )
        new_high = bool(
            (later_closes and max(later_closes) > entry_session_high)
            or (as_of is not None and as_of > latest.trading_date
                and current_price_adjusted > entry_session_high)
        )
        reduced = position_context.first_reduction_completed
        stop_price = position_context.hard_stop_adjusted
        if stored is not None and stored.get("stop_price_raw") is not None:
            stop_price = max(stop_price, float(stored["stop_price_raw"]) / raw_scale)
        if reduced:
            stop_price = max(stop_price, position_context.average_cost_adjusted)
        tracking_price = None
        if stored is not None and stored.get("tracking_price_raw") is not None:
            tracking_price = float(stored["tracking_price_raw"]) / raw_scale
        tracking_started = bool(reduced or (stored is not None and stored.get("tracking_started")))
        environment = str(context.environment_state)
        entry_environment = (
            stored.get("entry_environment") if stored is not None else None
        ) or (environment if environment in {"ATTACK", "NEUTRAL", "DEFENSE"} else None)
        setup = stored.get("setup") if stored is not None else None
        if not setup:
            previous_decision = (
                previous.get("v11", {}).get("entry_decision")
                if isinstance(previous.get("v11"), Mapping) else None
            )
            if isinstance(previous_decision, Mapping):
                candidate = previous_decision.get("setup")
                if isinstance(candidate, str) and candidate != "NONE":
                    setup = candidate
        setup = setup or "NONE"
        open_cycle_buys = shared.get("ledger_summary", {}).get("open_cycle_buys", {})
        topup_done = bool(
            (stored is not None and stored.get("topup_done"))
            or (isinstance(open_cycle_buys, Mapping) and open_cycle_buys.get(symbol, 0) > 1)
        )
        try:
            position = V11Position(
                shares=projected.shares,
                sellable_shares=projected.sellable_shares,
                entry_price=position_context.average_cost_adjusted,
                stop_price=stop_price,
                current_price=current_price_adjusted,
                initial_risk_per_share=position_context.initial_risk_per_share_adjusted,
                reduced=reduced,
                tracking_price=tracking_price,
                holding_session=holding_session,
                setup=setup,
                peak_price=peak_price,
                new_high=new_high,
                topup_done=topup_done,
                tracking_started=tracking_started,
                entry_environment=entry_environment,
            )
        except ValueError as error:
            return {
                "status": "UNAVAILABLE",
                "blocked_reasons": ["POSITION_STATE_INVALID"],
                "error": self._safe_error(error),
            }
        position_indicator = {
            **indicator,
            "close": current_price_adjusted,
            "long_holiday_preclose": context.long_holiday_sessions_ahead == 1,
            "reentry_cooldown_sessions": 0,
        }
        if metadata is not None and metadata.fund_size_cny is not None:
            position_indicator["fund_size_cny"] = metadata.fund_size_cny
        turnover = indicator.get("avg_amount20_cny")
        if turnover is None and metadata is not None:
            turnover = metadata.avg_amount20_cny
        if turnover is not None:
            position_indicator["avg_turnover_20d_cny"] = turnover
        position_context_v11 = replace(
            context, indicator=position_indicator, has_position=True,
            sellable_shares=projected.sellable_shares,
        )
        decision = evaluate_v11_position(
            position, config=config, context=position_context_v11,
        )
        next_stop = stop_price
        if decision.action in {"TRACK", "MOVE_STOP"} and decision.stop_price is not None:
            next_stop = max(next_stop, float(decision.stop_price))
        next_tracking_price = tracking_price
        if decision.action == "TRACK":
            entered = decision.evidence.get("tracking_price")
            if type(entered) in (int, float) and math.isfinite(float(entered)) and entered > 0:
                next_tracking_price = float(entered)
        next_state = {
            "entry_trading_date": entry_date.isoformat(),
            "as_of_trading_date": as_of.isoformat() if as_of is not None else None,
            "stop_price_raw": next_stop * raw_scale,
            "tracking_price_raw": (
                next_tracking_price * raw_scale if next_tracking_price is not None else None
            ),
            "peak_price_raw": peak_price * raw_scale,
            "tracking_started": bool(tracking_started or decision.action == "TRACK"),
            "topup_done": topup_done,
            "entry_environment": entry_environment,
            "setup": setup,
            "last_action": decision.action,
        }
        return {
            "status": "AVAILABLE",
            "decision": decision.to_dict(),
            "blocked_reasons": list(decision.blocked_reasons),
            "evidence": dict(decision.evidence),
            "position": {
                "shares": position.shares,
                "sellable_shares": position.sellable_shares,
                "entry_price": position.entry_price,
                "stop_price": position.stop_price,
                "current_price": position.current_price,
                "initial_risk_per_share": position.initial_risk_per_share,
                "holding_session": holding_session,
                "reduced": reduced,
                "tracking_started": tracking_started,
                "tracking_price": tracking_price,
                "peak_price": peak_price,
                "new_high": new_high,
                "entry_environment": entry_environment,
                "setup": setup,
                "topup_done": topup_done,
                "raw_scale": raw_scale,
            },
            "state": next_state,
        }

    def _persist_v11_state(self, snapshot: Mapping[str, object]) -> None:
        """Persist projected V11 position/environment state after publishing."""
        positions: dict[str, dict[str, object]] = {}
        items = snapshot.get("items")
        if isinstance(items, Sequence):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                v11 = item.get("v11")
                if not isinstance(v11, Mapping):
                    continue
                state = normalize_position_state(v11.get("position_state"))
                symbol = item.get("symbol")
                if state is not None and isinstance(symbol, str):
                    positions[symbol] = state
        environment = normalize_environment_state(snapshot.get("v11_environment_history"))
        if environment.get("state") is None:
            environment = normalize_environment_state(self._v11_state.get("environment"))
        if positions == self._v11_state.get("positions") and environment == self._v11_state.get("environment"):
            return
        try:
            self._v11_state_store().save(positions=positions, environment=environment)
            self._v11_state = {"positions": positions, "environment": environment}
            self._errors.pop("v11_state", None)
        except Exception as error:
            self._errors["v11_state"] = self._safe_error(error)

    def _shadow_snapshot(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
        data_quality: Mapping[str, object] | None,
        valuation: Mapping[str, object] | None,
        formal: SwingDecision,
    ) -> dict[str, object]:
        """Build the read-only V2 shadow layer without changing formal state."""
        base = {
            "strategy_version": "SWING_V2_SHADOW",
            "status": "UNAVAILABLE",
            "data_quality_status": "UNKNOWN",
            "data_healthy": False,
            "account_known": False,
            "cost_ok": False,
            "risk_ok": False,
            "snapshot_only": self._holdings_snapshot_present(),
            "variants": {},
            "opportunity": None,
            "blocked_reasons": [],
            "executable": False,
            "error": None,
        }
        if self._shadow_config is None:
            base["blocked_reasons"] = ["SHADOW_CONFIG_UNAVAILABLE"]
            base["error"] = self._shadow_config_error
            return base

        quality_status = (
            str(data_quality.get("status"))
            if isinstance(data_quality, Mapping)
            and data_quality.get("status") in {"VERIFIED", "UNVERIFIED"}
            else "UNKNOWN"
        )
        quality_reasons = tuple(
            reason for reason in (
                data_quality.get("reasons", ())
                if isinstance(data_quality, Mapping) else ()
            )
            if isinstance(reason, str)
        )
        opportunity = self._shadow_opportunity_snapshot(symbol, bars)
        trend_state, regime_evidence = infer_shadow_regime(bars)
        evidence = formal.evidence
        cost_gate_keys = (
            "entry_sizing_gates_ok", "cash_cap_ok", "single_symbol_cap_ok",
            "total_exposure_cap_ok", "trade_risk_cap_ok",
            "portfolio_risk_cap_ok", "minimum_lot_ok", "calendar_validity_ok",
        )
        cost_ok = all(evidence.get(key) is True for key in cost_gate_keys)
        stop = formal.planned_stop
        risk_ok = (
            evidence.get("health_gates_ok") is True
            and evidence.get("entry_hard_gates_ok") is True
            and evidence.get("cooldown_ok") is True
            and isinstance(stop, (int, float))
            and math.isfinite(float(stop))
            and float(stop) > 0.0
        )
        symbol_data_healthy = evidence.get("data_healthy") is True
        account_known = (
            self._health.get("portfolio") == "OK"
            and evidence.get("ledger_healthy") is True
        )
        context = ShadowContext(
            snapshot_only=self._holdings_snapshot_present(),
            data_quality=quality_status,
            quality_reasons=quality_reasons,
            data_healthy=symbol_data_healthy,
            account_known=account_known,
            cost_ok=cost_ok,
            risk_ok=risk_ok,
            valuation_status=self._shadow_valuation_stage(valuation),
            trend_state=trend_state,
            range_confirmed=trend_state == "RANGE",
            uncertain=trend_state == "UNCERTAIN",
            opportunity_id=(
                str(opportunity.get("opportunity_id"))
                if isinstance(opportunity, Mapping)
                else None
            ),
            opportunity_status=(
                str(opportunity.get("status"))
                if isinstance(opportunity, Mapping)
                else None
            ),
            data_version=self._history_digest_for_symbol(symbol, bars),
        )
        variants: dict[str, object] = {}
        blocked: set[str] = set()
        for variant in self._shadow_config.variants:
            try:
                decision = evaluate_shadow(
                    bars, variant=variant, config=self._shadow_config,
                    context=context,
                )
                value = decision.to_dict()
            except Exception as error:
                value = {
                    "strategy_version": "SWING_V2_SHADOW",
                    "variant": variant.value,
                    "state": "DATA_UNAVAILABLE",
                    "executable": False,
                    "blocked_reasons": ["DATA_ERROR"],
                    "evidence": {},
                    "data_version": context.data_version,
                    "indicator_version": "INDICATORS_V1",
                    "opportunity_id": None,
                    "error": self._safe_error(error),
                }
            variants[variant.value] = value
            reasons = value.get("blocked_reasons", [])
            if isinstance(reasons, list):
                blocked.update(reason for reason in reasons if isinstance(reason, str))
        try:
            hybrid = evaluate_hybrid_shadow(bars, context=context)
            hybrid_value = hybrid.to_dict()
        except Exception as error:
            hybrid_value = {
                "strategy_version": "SWING_HYBRID_SHADOW",
                "variant": ShadowVariant.HYBRID.value,
                "state": "DATA_UNAVAILABLE",
                "executable": False,
                "blocked_reasons": ["DATA_ERROR"],
                "evidence": {},
                "data_version": context.data_version,
                "indicator_version": "INDICATORS_V1",
                "opportunity_id": None,
                "error": self._safe_error(error),
            }
        variants[ShadowVariant.HYBRID.value] = hybrid_value
        reasons = hybrid_value.get("blocked_reasons", [])
        if isinstance(reasons, list):
            blocked.update(reason for reason in reasons if isinstance(reason, str))
        base.update({
            "status": "AVAILABLE",
            "data_quality_status": quality_status,
            "data_healthy": symbol_data_healthy,
            "account_known": account_known,
            "cost_ok": cost_ok,
            "risk_ok": risk_ok,
            "regime": {"state": trend_state, **dict(regime_evidence)},
            "variants": variants,
            "opportunity": opportunity,
            "blocked_reasons": sorted(blocked),
        })
        return base

    def _shadow_opportunity_snapshot(
        self,
        symbol: str,
        bars: Sequence[DailyBar],
    ) -> dict[str, object] | None:
        """Rebuild the read-only five-session opportunity from completed bars."""
        if self._shadow_config is None or not bars:
            return None
        try:
            context = calculate_indicator_context(bars, lookback=len(bars))
            recent = context.get("recent")
            if not isinstance(recent, Sequence) or len(recent) != len(bars):
                return None
            timeline = opportunity_timeline(
                bars, recent,
                window_sessions=self._shadow_config.event_window_sessions,
                atr_distance_max=self._shadow_config.atr_distance_max,
                closed_dates=self._closed_dates or frozenset(),
            )
            event = timeline.get(bars[-1].trading_date)
            return event.to_dict() if event is not None else None
        except (IndicatorInputError, ValueError, TypeError, IndexError):
            return None

    def _valuation_snapshot(
        self,
        metadata: EtfMetadata | None,
        valuation_by_index: Mapping[str, object],
    ) -> dict[str, object] | None:
        if metadata is None:
            return None
        snapshot = valuation_by_index.get(metadata.index.code)
        if snapshot is not None:
            payload = snapshot.to_dict()
            payload["valuation_stage"] = classify_valuation_stage(
                snapshot, category=metadata.category or "UNAVAILABLE",
            ).to_dict()
            return payload
        payload = {
            "index_code": metadata.index.code,
            "index_name": metadata.index.name,
            "as_of": None,
            "pe_ttm": None,
            "pb": None,
            "dividend_yield": None,
            "pe_percentile_5y": None,
            "pe_percentile_10y": None,
            "pb_percentile_5y": None,
            "pb_percentile_10y": None,
            "roe_ttm": None,
            "pr_pe_roe": None,
            "pr_pe_pb": None,
            "roe_period": None,
            "roe_annualized": None,
            "roe_consistent": False,
            "percentile_horizon_used": None,
            "generated_at": None,
            "level": "UNKNOWN",
            "status": "MISSING_VALUATION",
            "source": None,
        }
        payload["valuation_stage"] = classify_valuation_stage(
            None, category=metadata.category or "UNAVAILABLE",
        ).to_dict()
        return payload

    @staticmethod
    def _indicator_snapshot(
        symbol: str,
        bars: Sequence[DailyBar],
    ) -> dict[str, object]:
        try:
            return calculate_indicator_snapshot(
                bars, minimum_bars=DEFAULT_MINIMUM_BARS,
            )
        except IndicatorInputError as error:
            latest = bars[-1].trading_date.isoformat() if bars else None
            return {
                "schema_version": INDICATOR_SCHEMA_VERSION,
                "symbol": symbol,
                "as_of_trading_date": latest,
                "bar_count": len(bars),
                "minimum_bars": DEFAULT_MINIMUM_BARS,
                "status": "DATA_ERROR",
                "reason": "INVALID_COMPLETED_BARS",
                "error": str(error),
                "macd": {
                    "ema12": None, "ema26": None, "dif": None,
                    "dea": None, "histogram": None,
                },
                "kdj": {"k": None, "d": None, "j": None},
                "rsi": {"rsi14": None},
                "moving_averages": {
                    "ma5": None, "ma10": None,
                    "ma20": None, "ma60": None,
                },
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
        next_revision = self.revision + 1
        self._install_published(value, revision=next_revision)
        self.revision = next_revision
        self._persist_v11_state(snapshot)
        event = copy.deepcopy(self.published)
        event.update({
            "event": "reset" if force_reset else "update",
            "reset": bool(force_reset),
            "force_reset": bool(force_reset),
            "daily_upserts": copy.deepcopy(dict(daily_upserts or {})),
        })
        self.events.append(event)
        self.publish_condition.notify_all()

    def _install_published(
        self,
        snapshot: Mapping[str, object],
        *,
        revision: int,
    ) -> None:
        """Install the public snapshot and all private GET views as one revision."""
        value = copy.deepcopy(dict(snapshot))
        read_model = value.pop(_READ_MODEL_KEY)
        if not isinstance(read_model, Mapping):
            raise SwingServiceError("published read model is unavailable")

        def endpoint(name: str) -> dict[str, object]:
            raw = read_model.get(name)
            if not isinstance(raw, Mapping):
                raise SwingServiceError(f"published {name} view is unavailable")
            result = copy.deepcopy(dict(raw))
            result["revision"] = revision
            return result

        value["revision"] = revision
        watchlist = endpoint("watchlist")
        value["watchlist"] = copy.deepcopy(watchlist)
        portfolio = endpoint("portfolio")
        alerts_current = endpoint("alerts_current")
        alerts_history = endpoint("alerts_history")
        self.published = value
        self._published_watchlist_view = watchlist
        self._published_portfolio_view = portfolio
        self._published_alerts_current = alerts_current
        self._published_alerts_history = alerts_history

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
        effective_now = self._safe_now() if now is None else now
        # A failed staged recomputation must not advance per-item quality
        # timestamps.  Reuse the last published snapshot's generated time
        # when available while still updating the service-level error state.
        quality_now = self._published_quality_time()
        self._publish(self._build_snapshot(effective_now, quality_now=quality_now))

    def _published_quality_time(self) -> datetime | None:
        raw = self.published.get("generated_at")
        if type(raw) is not str:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(SHANGHAI)

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
                    and item.state in _OVERLAY_ALERT_STATES
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
            value = self._local_time(self.clock())
        except Exception as error:
            self._health["service"] = "CLOCK_FAILED"
            self._errors["service"] = self._safe_error(error)
            return self._fallback_now()
        self._mark_clock_success()
        return value

    def _mark_clock_success(self) -> bool:
        if self._health.get("service") != "CLOCK_FAILED":
            return False
        before = (
            self._health.get("service"), self._errors.get("service"),
        )
        self._errors.pop("service", None)
        configuration = self._health.get("configuration", "UNKNOWN")
        if configuration == "OK":
            self._health["service"] = "OK"
        elif configuration == "BLOCKED":
            self._health["service"] = "BLOCKED"
        elif self._health.get("service") == "CLOCK_FAILED":
            self._health["service"] = "STARTING"
        return before != (
            self._health.get("service"), self._errors.get("service"),
        )

    def _mark_producer_success(self) -> bool:
        if self._health.get("service") != "PRODUCER_FAILED":
            return False
        self._errors.pop("service", None)
        self._health["service"] = (
            "OK" if self._health.get("configuration") == "OK" else "BLOCKED"
        )
        return True

    def _fallback_now(self) -> datetime:
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

    def _enabled_history_digest(self) -> str:
        """Fingerprint every enabled symbol's complete persisted daily history."""
        enabled = {item.symbol for item in self._watchlist if item.enabled}
        canonical_history = [
            bar.to_dict()
            for bar in sorted(
                (bar for bar in self._history if bar.symbol in enabled),
                key=lambda item: (item.symbol, item.trading_date),
            )
        ]
        return self._canonical_digest(canonical_history)

    def _history_digest_for_symbol(
        self, symbol: str, bars: Sequence[DailyBar],
    ) -> str:
        """Fingerprint only the completed bars used for one ETF's shadow decision."""
        canonical_history = [
            bar.to_dict()
            for bar in sorted(
                (bar for bar in bars if bar.symbol == symbol),
                key=lambda item: (item.trading_date, item.observed_at),
            )
        ]
        return self._canonical_digest(canonical_history) or "sha256:unknown"

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
        if self._holdings_snapshot_present():
            position = self._snapshot_position(symbol)
            return bool(position and position["shares"] > 0)
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
        if self._holdings_snapshot_present():
            return "PAUSED_HOLDINGS_SNAPSHOT"
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
                with self.producer_lock:
                    now = self._safe_now()
                    if self._mark_producer_success():
                        self._publish(self._build_snapshot(now))
            except Exception as error:
                with self.producer_lock:
                    self._publish_component_failure(
                        "service", "PRODUCER_FAILED", error, now=None,
                    )
            self._stop_event.wait(self.refresh_interval)


def _load_research_manifest_builder():
    path = Path(__file__).resolve().parents[2] / "scripts" / "build_swing_research_manifest.py"
    spec = importlib.util.spec_from_file_location("build_swing_research_manifest", path)
    if spec is None or spec.loader is None:
        raise SwingServiceError("research manifest builder is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    build = getattr(module, "build_manifest", None)
    if not callable(build):
        raise SwingServiceError("research manifest builder is unavailable")
    return build


__all__ = ["DailyCollector", "SwingPaths", "SwingService", "SwingServiceError"]
