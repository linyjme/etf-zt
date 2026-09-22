from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from . import constants
from .etf_metadata import EtfMetadataStore
from .market_data import (
    MarketDataValidator,
    MarketHealth,
    MarketHealthClassifier,
    MinuteHistoryStore,
    SHANGHAI,
    finalized_points,
    load_closed_dates,
    market_session_state,
)
from .t_backtest import TBacktester
from .t_monitor import (
    AlertHistoryStore,
    JsonQuoteAdapter,
    Quote,
    QuoteHistoryStore,
    TMonitorEngine,
    WatchItem,
    load_watchlist,
    snapshot_to_dict,
)
from .t_page import PAGE
from .pr_page import PR_PAGE
from .industry_page import INDUSTRY_PAGE, INDUSTRY_SWING_PAGE
from .quote_quality import (
    MinuteQuarantineStore,
    prepare_quote_batch,
    read_validation_issues,
)
from .swing_alerts import AlertStoreError
from .swing_page import SWING_PAGE
from .swing_portfolio import PortfolioLedgerError, TradeInput
from .swing_service import SwingPaths, SwingService, SwingServiceError
from .valuation import ValuationStore


_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "monitor"
_DEFAULT_METADATA_PATH = _DATA_ROOT / "etf_metadata.json"
_DEFAULT_CALENDAR_PATH = _DATA_ROOT / "market_calendar.json"
_EXPECTED_CLIENT_DISCONNECTS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
_REQUEST_SOCKET_TIMEOUT_SECONDS = 2.0
_RESPONSE_SOCKET_TIMEOUT_SECONDS = 10.0
_MAX_CURSOR_DIGITS = 19


class _RequestBodyTimeoutError(ValueError):
    """Raised when a declared local JSON body does not arrive in time."""


class _RequestDeadlineExceeded(TimeoutError):
    """Raised when request headers or body exceed one absolute deadline."""


class _DeadlineReader:
    """Buffered request reader that cannot be kept alive by trickled bytes."""

    def __init__(
        self,
        source: Any,
        connection: socket.socket,
        deadline: Callable[[], float | None],
    ) -> None:
        self.source = source
        self.connection = connection
        self.deadline = deadline
        self.buffer = bytearray()

    def _remaining(self) -> float | None:
        deadline = self.deadline()
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise _RequestDeadlineExceeded("absolute request deadline exceeded")
        return remaining

    def _read_once(self, size: int) -> bytes:
        remaining = self._remaining()
        if remaining is not None:
            self.connection.settimeout(remaining)
        try:
            reader = getattr(self.source, "read1", self.source.read)
            return reader(max(1, size))
        except socket.timeout as error:
            raise _RequestDeadlineExceeded(
                "absolute request deadline exceeded",
            ) from error

    def readline(self, limit: int = -1) -> bytes:
        bounded = limit is not None and limit >= 0
        while True:
            search_end = limit if bounded else len(self.buffer)
            newline = self.buffer.find(b"\n", 0, search_end)
            if newline >= 0:
                return self._consume(newline + 1)
            if bounded and len(self.buffer) >= limit:
                return self._consume(limit)
            read_size = 8192
            if bounded:
                read_size = min(read_size, limit - len(self.buffer))
            chunk = self._read_once(read_size)
            if not chunk:
                return self._consume(len(self.buffer))
            self.buffer.extend(chunk)

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        if size < 0:
            chunks = [self._consume(len(self.buffer))]
            while True:
                chunk = self._read_once(8192)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        while len(self.buffer) < size:
            chunk = self._read_once(min(8192, size - len(self.buffer)))
            if not chunk:
                break
            self.buffer.extend(chunk)
        return self._consume(min(size, len(self.buffer)))

    def _consume(self, size: int) -> bytes:
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def close(self) -> None:
        self.source.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.source, name)


class MissingWatchMetadataError(Exception):
    """Raised when a watch item has no verified trading metadata."""


@dataclass
class MonitorApplication:
    quotes_path: Path
    watchlist_path: Path
    history_path: Path | None = None
    collector: Any | None = None
    refresh_interval: float = constants.DEFAULT_REFRESH_INTERVAL_SECONDS
    alert_history_path: Path | None = None
    metadata_path: Path | None = None
    valuation_path: Path | None = None
    calendar_path: Path | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now().astimezone(), compare=False)
    revision_event_limit: int = 128
    watchlist_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    refresh_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    producer_lock: threading.Lock = field(default_factory=threading.Lock, compare=False)
    lifecycle_gate: Any = field(default_factory=threading.RLock, compare=False)
    refresh_error: str | None = None
    last_refresh_at: str | None = None
    _stop_event: threading.Event = field(default_factory=threading.Event, compare=False)
    _refresh_thread: threading.Thread | None = field(default=None, compare=False)
    _published: dict[str, Any] = field(default_factory=dict, init=False, compare=False)
    _revision: int = field(default=0, init=False, compare=False)
    _generation: int = field(default=0, init=False, compare=False)
    _publish_condition: threading.Condition = field(init=False, compare=False)
    _revision_events: deque[dict[str, Any]] = field(init=False, compare=False)
    metadata_store: EtfMetadataStore = field(init=False, compare=False)
    history_store: MinuteHistoryStore | None = field(init=False, compare=False)
    alert_store: AlertHistoryStore | None = field(init=False, compare=False)
    health_classifier: MarketHealthClassifier = field(init=False, compare=False)
    engine: TMonitorEngine = field(init=False, compare=False)

    def __post_init__(self) -> None:
        self.quotes_path = Path(self.quotes_path)
        self.watchlist_path = Path(self.watchlist_path)
        self.history_path = Path(self.history_path) if self.history_path is not None else None
        self.alert_history_path = (
            Path(self.alert_history_path) if self.alert_history_path is not None else None
        )
        self.metadata_path = Path(
            _DEFAULT_METADATA_PATH if self.metadata_path is None else self.metadata_path,
        )
        self.calendar_path = Path(
            _DEFAULT_CALENDAR_PATH if self.calendar_path is None else self.calendar_path,
        )
        self.metadata_store = EtfMetadataStore(self.metadata_path)
        self.history_store = (
            MinuteHistoryStore(self.history_path) if self.history_path is not None else None
        )
        self.alert_store = (
            AlertHistoryStore(self.alert_history_path)
            if self.alert_history_path is not None else None
        )
        self.health_classifier = MarketHealthClassifier(
            load_closed_dates(self.calendar_path),
        )
        self.engine = TMonitorEngine(self.health_classifier)
        self._publish_condition = threading.Condition(self.refresh_lock)
        self._revision_events = deque(maxlen=max(1, self.revision_event_limit))
        self._published = self._empty_snapshot()
        self._bootstrap(increment_revision=self.collector is None)

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        with self.refresh_lock:
            published = copy.deepcopy(self._published)
        published, _ = self._current_date_view(published, now)
        return self._summary_snapshot(published)

    def quotes(self, symbol: str, since: int) -> dict[str, Any]:
        if not isinstance(symbol, str) or re.fullmatch(r"[0-9]{6}", symbol) is None:
            raise ValueError("ETF代码必须是ASCII 6位数字")
        if isinstance(since, bool) or not isinstance(since, int) or since < 0:
            raise ValueError("since必须是非负整数")
        enabled = {
            item.symbol for item in load_watchlist(self.watchlist_path) if item.enabled
        }
        if symbol not in enabled:
            raise ValueError(f"标的未启用: {symbol}")

        with self._publish_condition:
            revision = self._revision
            published = copy.deepcopy(self._published)
            events = copy.deepcopy(tuple(self._revision_events))
        published, crossed_date = self._current_date_view(published, self.clock())
        all_points = self._current_day_points(published, symbol)
        reset = (
            crossed_date
            or since == 0
            or since > revision
            or (since < revision and not events)
            or (
                bool(events)
                and since < int(events[0]["revision"]) - 1
            )
        )
        if not reset:
            reset = any(
                int(event["revision"]) > since
                and symbol in event.get("resets", [])
                for event in events
            )
        if reset:
            upserts = all_points
        else:
            by_timestamp: dict[str, dict[str, Any]] = {}
            current_date = self._points_trading_date(all_points)
            for event in events:
                if int(event["revision"]) <= since:
                    continue
                for point in event.get("upserts", {}).get(symbol, []):
                    timestamp = str(point.get("timestamp", ""))
                    if current_date is None or timestamp.startswith(current_date):
                        by_timestamp[timestamp] = copy.deepcopy(point)
            upserts = [by_timestamp[key] for key in sorted(by_timestamp)]
        return {
            "symbol": symbol,
            "revision": revision,
            "upserts": upserts,
            "reset": reset,
            "read_only": True,
        }

    def health(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        statuses = sorted({
            str(item.get("health_status", "UNKNOWN"))
            for item in snapshot.get("items", [])
        })
        errors = list(snapshot.get("errors") or [])
        ok = (
            snapshot.get("refresh_error") is None
            and not errors
            and not {"OUTAGE", "DELAYED", "DATA_ERROR"}.intersection(statuses)
        )
        return {
            "status": "ok" if ok else "degraded",
            "ok": ok,
            "mode": "MONITOR_ONLY",
            "revision": snapshot.get("revision", 0),
            "health_statuses": statuses,
            "errors": errors,
        }

    def start_refresh(self) -> None:
        with self.lifecycle_gate:
            if self.collector is None or self._refresh_thread is not None:
                return
            self._stop_event.clear()
            self._generation += 1
            generation = self._generation
            thread = threading.Thread(
                target=self._refresh_loop, args=(generation,), daemon=True,
            )
            self._refresh_thread = thread
            thread.start()

    def stop_refresh(self) -> None:
        with self.lifecycle_gate:
            self._stop_event.set()
            self._generation += 1
            thread = self._refresh_thread
        with self._publish_condition:
            self._publish_condition.notify_all()
        if thread is not None:
            thread.join(timeout=max(self.refresh_interval, 1.0) + 1.0)

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()

    def wait_for_revision(
        self, after_revision: int, timeout: float,
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.0, timeout)
        selected: dict[str, Any] | None = None
        with self._publish_condition:
            while selected is None:
                now = self.clock()
                current = copy.deepcopy(self._published)
                current_revision = int(current.get("revision", 0))
                events = tuple(self._revision_events)
                if not self._published_date_matches(current, now):
                    current_view = self._empty_current_date_view(current, now)
                    self._publish_locked(
                        current_view, {}, force_reset_event=True,
                    )
                    selected = copy.deepcopy(self._revision_events[-1])
                elif after_revision > current_revision:
                    selected = self._reset_summary(current)
                elif (
                    after_revision < current_revision
                    and (
                        not events
                        or after_revision < int(events[0]["revision"]) - 1
                    )
                ):
                    selected = self._reset_summary(current)
                else:
                    selected = next((
                    event for event in self._revision_events
                    if event["revision"] > after_revision
                    ), None)
                if (
                    selected is not None
                    and not self._revision_payload_matches_date(selected, now)
                ):
                    selected = self._reset_summary(current)
                if (
                    selected is not None
                    or self._stop_event.is_set()
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._publish_condition.wait(remaining)
        return copy.deepcopy(selected) if selected is not None else None

    def refresh_once(self) -> bool:
        return self._refresh_once(generation=None)

    def _has_complete_current_day(
        self,
        now: datetime,
        enabled: Sequence[str],
        session_phase: str,
    ) -> bool:
        local = now.astimezone(SHANGHAI)
        endpoint = local.replace(
            hour=11 if session_phase == "LUNCH_BREAK" else 15,
            minute=30 if session_phase == "LUNCH_BREAK" else 0,
            second=0,
            microsecond=0,
        )
        with self.refresh_lock:
            published = copy.deepcopy(self._published)
        published, _ = self._current_date_view(published, now)
        items = {
            str(item.get("symbol", "")): item
            for item in published.get("items", [])
        }
        for symbol in enabled:
            item = items.get(symbol)
            if item is None or item.get("status") != "OK":
                return False
            try:
                timestamp = datetime.fromisoformat(str(item.get("timestamp", "")))
            except ValueError:
                return False
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                return False
            completed_at = timestamp.astimezone(SHANGHAI)
            if completed_at.date() != local.date() or completed_at < endpoint:
                return False
        return True

    def collection_due(self, now: datetime | None = None) -> bool:
        now = self.clock() if now is None else now
        enabled = tuple(
            item.symbol for item in load_watchlist(self.watchlist_path) if item.enabled
        )
        if not enabled:
            return False
        session = market_session_state(
            now, closed_dates=self.health_classifier.closed_dates,
        )
        if session.active:
            return True
        return session.catch_up_allowed and not self._has_complete_current_day(
            now, enabled, session.phase,
        )

    def refresh_delay(self, failure_count: int) -> float:
        exponent = max(0, int(failure_count) - 1)
        return min(
            self.refresh_interval * (2 ** exponent),
            constants.MAX_REFRESH_BACKOFF_SECONDS,
        )

    @staticmethod
    def delay_until_next_minute(now: datetime) -> float:
        local = now.astimezone(SHANGHAI)
        elapsed = local.second + local.microsecond / 1_000_000
        return 60.0 if elapsed == 0 else 60.0 - elapsed

    def _refresh_once(self, generation: int | None) -> bool:
        with self.producer_lock:
            if self.collector is None:
                return False
            staging: Path | None = None
            watchlist: Sequence[WatchItem] | None = None
            try:
                metadata: Mapping[str, Any] = {}
                try:
                    watchlist = load_watchlist(self.watchlist_path)
                    staging = self._staging_quotes_path()
                    if self.history_store is not None:
                        metadata = self.metadata_store.load()
                except Exception as error:
                    with self.lifecycle_gate:
                        if self._generation_cancelled(generation):
                            return False
                        self._publish_outage(str(error), watchlist)
                    return False
                try:
                    self.collector.collect_to_file(watchlist, staging)
                    payload = json.loads(staging.read_text(encoding="utf-8"))
                    if not isinstance(payload, Mapping):
                        raise ValueError("行情文件必须是对象")
                    all_quotes = JsonQuoteAdapter().parse(payload)
                    issues = read_validation_issues(payload)
                    if self.history_store is not None:
                        clean_payload, all_quotes, issues = prepare_quote_batch(payload, metadata)
                        if clean_payload != payload:
                            staging.write_text(json.dumps(
                                clean_payload, ensure_ascii=False, allow_nan=False,
                            ), encoding="utf-8")
                        payload = clean_payload
                    audit_issues = issues
                except Exception as error:
                    with self.lifecycle_gate:
                        if self._generation_cancelled(generation):
                            return False
                        self._publish_collection_failure(str(error))
                    return False
                with self.lifecycle_gate:
                    if self._generation_cancelled(generation):
                        return False
                    try:
                        now = self.clock()
                        day = now.astimezone(SHANGHAI).date().isoformat()
                        evidence = MinuteQuarantineStore(self._quarantine_path()).read()
                        issues = self._unresolved_quality_issues(
                            all_quotes,
                            [issue for issue in [*evidence, *issues] if issue['trading_date'] == day],
                            metadata or None,
                        )
                        if issues != read_validation_issues(payload):
                            if 'quotes' not in payload:
                                payload = {'quotes': [
                                    {'symbol': symbol, **record}
                                    for symbol, record in payload.items()
                                ]}
                            payload['validation_issues'] = issues
                            staging.write_text(json.dumps(
                                payload, ensure_ascii=False, allow_nan=False,
                            ), encoding='utf-8')
                        quotes = self._quotes_for_now(all_quotes, now)
                        health = self._health_by_symbol(quotes, now, issues=issues)
                        published = snapshot_to_dict(self.engine.evaluate(
                            watchlist, quotes, generated_at=now, health=health,
                        ))
                        self._annotate_quality(published, issues)
                    except Exception as error:
                        self._publish_outage(str(error), watchlist)
                        return False
                    try:
                        MinuteQuarantineStore(self._quarantine_path()).append(audit_issues)
                        self._commit_staged_quotes(staging)
                        staging = None
                    except Exception as error:
                        self._publish_outage(str(error), watchlist)
                        return False

                    persistence_errors: list[str] = []
                    if self.history_store is not None:
                        try:
                            self.history_store.upsert(all_quotes, metadata)
                        except Exception as error:
                            persistence_errors.append(str(error))
                    if self.alert_store is not None:
                        try:
                            self.alert_store.append_candidates(published)
                        except Exception as error:
                            persistence_errors.append(str(error))
                    self._publish(
                        published, payload, persistence_errors=persistence_errors,
                    )
                    return True
            finally:
                if staging is not None:
                    staging.unlink(missing_ok=True)

    def _generation_cancelled(self, generation: int | None) -> bool:
        return (
            generation is not None
            and (
                self._stop_event.is_set()
                or generation != self._generation
                or self._refresh_thread is not threading.current_thread()
            )
        )

    def _refresh_loop(self, generation: int) -> None:
        thread = threading.current_thread()
        failure_count = 0
        try:
            while True:
                with self.lifecycle_gate:
                    if self._generation_cancelled(generation):
                        break
                try:
                    cycle_now = self.clock()
                    self._publish_cross_date_reset(cycle_now, generation)
                    attempted = self.collection_due(cycle_now)
                except Exception as error:
                    with self.lifecycle_gate:
                        if self._generation_cancelled(generation):
                            break
                        self._publish_outage(str(error))
                    attempted = True
                    succeeded = False
                else:
                    if attempted:
                        succeeded = self._refresh_once(generation)
                if attempted:
                    failure_count = 0 if succeeded else failure_count + 1
                else:
                    failure_count = 0
                delay = (
                    self.delay_until_next_minute(self.clock())
                    if attempted and succeeded
                    else self.refresh_delay(failure_count if attempted else 0)
                )
                self._stop_event.wait(delay)
        finally:
            with self.lifecycle_gate:
                if self._refresh_thread is thread:
                    self._refresh_thread = None

    def _bootstrap(
        self, *, increment_revision: bool, now: datetime | None = None,
    ) -> None:
        watchlist = load_watchlist(self.watchlist_path)
        error: str | None = None
        issues: list[dict[str, Any]] = []
        try:
            raw = json.loads(self.quotes_path.read_text(encoding="utf-8"))
            all_quotes = JsonQuoteAdapter().parse(raw)
            payload = raw if isinstance(raw, dict) else {}
            issues = read_validation_issues(payload)
        except (ValueError, OSError) as failure:
            all_quotes = {}
            payload = {}
            error = str(failure)
        if error is None and self.history_store is not None:
            try:
                self._validate_quotes(all_quotes, self.metadata_store.load())
            except (ValueError, OSError) as failure:
                error = str(failure)
        now = self.clock() if now is None else now
        if error is None:
            try:
                day = now.astimezone(SHANGHAI).date().isoformat()
                evidence = MinuteQuarantineStore(self._quarantine_path()).read()
                issues = self._unresolved_quality_issues(
                    all_quotes,
                    [issue for issue in [*evidence, *issues] if issue['trading_date'] == day],
                )
            except (ValueError, OSError) as failure:
                error = str(failure)
        quotes = self._quotes_for_now(all_quotes, now)
        health = self._health_by_symbol(quotes, now, error, issues=issues)
        published = snapshot_to_dict(self.engine.evaluate(
            watchlist, quotes, generated_at=now, health=health,
        ))
        if error is not None:
            published["errors"] = [error]
        self._annotate_quality(published, issues)
        self._publish(
            published, payload, error=error, increment_revision=increment_revision,
        )

    @staticmethod
    def _quote_for_date(quote: Quote, trading_date: date) -> Quote | None:
        points = tuple(
            point for point in quote.points
            if point.timestamp.astimezone(SHANGHAI).date() == trading_date
        )
        if not points:
            return None
        latest = points[-1]
        return Quote(
            symbol=quote.symbol,
            name=quote.name,
            price=latest.price,
            average_price=latest.average_price,
            previous_close=quote.previous_close,
            timestamp=latest.timestamp,
            points=points,
            observed_at=quote.observed_at,
            source=quote.source,
        )

    @classmethod
    def _quotes_for_now(
        cls, quotes: Mapping[str, Quote], now: datetime,
    ) -> dict[str, Quote]:
        trading_date = now.astimezone(SHANGHAI).date()
        result: dict[str, Quote] = {}
        for symbol, quote in quotes.items():
            current = cls._quote_for_date(quote, trading_date)
            if current is not None:
                result[symbol] = current
        return result

    def _health_by_symbol(
        self,
        quotes: Mapping[str, Any],
        now: datetime,
        error: str | None = None,
        *,
        issues: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for symbol, quote in quotes.items():
            completed = finalized_points(quote.points, quote.observed_at)
            result[symbol] = self.health_classifier.classify(
                now, completed[-1].timestamp if completed else None, error,
                completed_minute=True,
            )
        current_date = now.astimezone(SHANGHAI).date().isoformat()
        for issue in issues:
            if issue["trading_date"] == current_date:
                result[issue["symbol"]] = MarketHealth(
                    "DATA_ERROR", None, self._quality_reason(issue),
                )
        return result

    def _quarantine_path(self) -> Path:
        return self.quotes_path.with_name("quarantine.jsonl")

    @staticmethod
    def _quality_reason(issue: Mapping[str, Any]) -> str:
        return (
            f"{issue['symbol']} {issue['timestamp']}: {issue['reason']}；"
            "异常分钟已隔离，该标的候选提醒暂停"
        )

    def _replay_quality_symbols(
        self, payload: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> set[str]:
        # Legacy replay fixtures/history may be top-level arrays. They have
        # already passed JsonQuoteAdapter, but cannot carry envelope metadata.
        current_issues = read_validation_issues(payload) if isinstance(payload, dict) else []
        evidence = MinuteQuarantineStore(self._quarantine_path()).read()
        latest_quotes = JsonQuoteAdapter().parse(payload)
        # These endpoints replay all local history. A completely isolated first
        # day has no surviving quote to define its date range, but still counts.
        return {issue['symbol'] for issue in self._unresolved_quality_issues(
            latest_quotes, [*evidence, *current_issues], metadata,
        )}

    def _unresolved_quality_issues(
        self, latest_quotes: Mapping[str, Quote],
        evidence: Sequence[dict[str, Any]],
        metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        pending: dict[tuple[str, str], dict[str, Any]] = {}
        def observed(issue: Mapping[str, Any]) -> datetime:
            return datetime.fromisoformat(issue.get('last_observed_at', issue['observed_at']))
        for issue in evidence:
            key = (issue['symbol'], issue['timestamp'])
            if key not in pending or observed(issue) > observed(pending[key]):
                pending[key] = issue
        unresolved = []
        history_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for issue in pending.values():
            symbol = issue["symbol"]
            day = issue["trading_date"]
            if metadata is None:
                metadata = self.metadata_store.load()
            item_metadata = metadata.get(symbol)
            candidates = []
            current = latest_quotes.get(symbol)
            if current is not None:
                candidates.extend(
                    (point, current.observed_at, current.previous_close)
                    for point in current.points
                    if point.timestamp.isoformat() == issue["timestamp"]
                )
            if self.history_store is not None:
                key = (symbol, day)
                if key not in history_cache:
                    history_cache[key] = self.history_store.query(day, symbol)
                candidates.extend(
                    (JsonQuoteAdapter()._point(symbol, row),
                     datetime.fromisoformat(row["observed_at"]), row["previous_close"])
                    for row in history_cache[key]
                    if row["timestamp"] == issue["timestamp"]
                )
            resolved = False
            if item_metadata is not None:
                validator = MarketDataValidator(item_metadata.trading)
                for point, observed_at, previous_close in candidates:
                    if observed_at <= observed(issue):
                        continue
                    try:
                        validator.validate_point(point, previous_close)
                    except ValueError:
                        continue
                    resolved = True
                    break
            if not resolved:
                unresolved.append(issue)
        return unresolved

    @classmethod
    def _annotate_quality(
        cls, published: dict[str, Any], issues: Sequence[Mapping[str, Any]],
    ) -> None:
        current_date = datetime.fromisoformat(
            published["generated_at"],
        ).astimezone(SHANGHAI).date().isoformat()
        summaries = [
            {key: issue[key] for key in ("symbol", "timestamp", "trading_date", "reason")}
            for issue in issues if issue["trading_date"] == current_date
        ]
        published["validation_issues"] = summaries
        for item in published.get("items", []):
            own = [issue for issue in summaries if issue["symbol"] == item["symbol"]]
            item["validation_issues"] = own
            if not own:
                continue
            # Missing-quote engine branches cannot consume an injected health
            # value. Keep them unsafe too, before any alert can be persisted.
            item["health_status"] = "DATA_ERROR"
            item["health_reason"] = cls._quality_reason(own[0])
            if item["action"] in {"BUY_CANDIDATE", "SELL_CANDIDATE"}:
                item["action"] = "DEVIATION_OBSERVE"
                item["label"] = "偏离观察"
            item["trade_markers"] = []
            item["blocked_reasons"] = list(dict.fromkeys([
                *item.get("blocked_reasons", []), "MARKET_DATA_INVALID",
            ]))

    @staticmethod
    def _validate_quotes(
        quotes: Mapping[str, Any], metadata: Mapping[str, Any],
    ) -> None:
        for symbol, quote in quotes.items():
            item_metadata = metadata.get(symbol)
            if item_metadata is None:
                raise ValueError(f"缺少交易元数据: {symbol}")
            validator = MarketDataValidator(item_metadata.trading)
            for point in finalized_points(quote.points, quote.observed_at):
                validator.validate_point(point, quote.previous_close)

    def _staging_quotes_path(self) -> Path:
        self.quotes_path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "wb", dir=self.quotes_path.parent,
            prefix=f".{self.quotes_path.name}.", suffix=".staging", delete=False,
        )
        try:
            return Path(handle.name)
        finally:
            handle.close()

    def _commit_staged_quotes(self, staging: Path) -> None:
        with staging.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(staging, self.quotes_path)

    def _empty_snapshot(self) -> dict[str, Any]:
        return {
            "generated_at": self.clock().isoformat(),
            "mode": "MONITOR_ONLY",
            "auto_trade": False,
            "errors": [],
            "items": [],
            "revision": 0,
            "source": None,
            "refresh_error": None,
            "persistence_errors": [],
            "last_refresh_at": None,
        }

    @staticmethod
    def _published_date_matches(
        published: Mapping[str, Any], now: datetime,
    ) -> bool:
        try:
            generated = datetime.fromisoformat(str(published.get("generated_at", "")))
        except ValueError:
            return False
        return (
            generated.tzinfo is not None
            and generated.utcoffset() is not None
            and generated.astimezone(SHANGHAI).date()
            == now.astimezone(SHANGHAI).date()
        )

    def _empty_current_date_view(
        self, published: Mapping[str, Any], now: datetime,
    ) -> dict[str, Any]:
        result = snapshot_to_dict(self.engine.evaluate(
            load_watchlist(self.watchlist_path), {}, generated_at=now,
        ))
        result.update({
            "revision": int(published.get("revision", self._revision)),
            "source": None,
            "refresh_error": None,
            "persistence_errors": [],
            "last_refresh_at": None,
        })
        return result

    def _current_date_view(
        self, published: Mapping[str, Any], now: datetime,
    ) -> tuple[dict[str, Any], bool]:
        if self._published_date_matches(published, now):
            return copy.deepcopy(dict(published)), False
        return self._empty_current_date_view(published, now), True

    @staticmethod
    def _revision_payload_matches_date(
        payload: Mapping[str, Any], now: datetime,
    ) -> bool:
        try:
            generated = datetime.fromisoformat(str(payload.get("generated_at", "")))
        except ValueError:
            return False
        if generated.tzinfo is None or generated.utcoffset() is None:
            return False
        current_date = now.astimezone(SHANGHAI).date()
        if generated.astimezone(SHANGHAI).date() != current_date:
            return False
        timestamps = [
            item.get("timestamp") for item in payload.get("items", [])
            if item.get("timestamp") is not None
        ]
        timestamps.extend(
            point.get("timestamp")
            for points in payload.get("upserts", {}).values()
            for point in points
        )
        for value in timestamps:
            try:
                timestamp = datetime.fromisoformat(str(value))
            except ValueError:
                return False
            if (
                timestamp.tzinfo is None
                or timestamp.utcoffset() is None
                or timestamp.astimezone(SHANGHAI).date() != current_date
            ):
                return False
        return all(
            str(point.get("trading_date", "")) == current_date.isoformat()
            for points in payload.get("upserts", {}).values()
            for point in points
        )

    def _publish_cross_date_reset(
        self, now: datetime, generation: int | None,
    ) -> bool:
        with self.producer_lock:
            with self.lifecycle_gate:
                if self._generation_cancelled(generation):
                    return False
                with self.refresh_lock:
                    previous = copy.deepcopy(self._published)
                if self._published_date_matches(previous, now):
                    return False
                current = self._empty_current_date_view(previous, now)
                with self._publish_condition:
                    if self._published_date_matches(self._published, now):
                        return False
                    self._publish_locked(
                        current, {}, force_reset_event=True,
                    )
                    return True

    @staticmethod
    def _summary_snapshot(published: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(published))
        for item in result.get("items", []):
            item.pop("points", None)
        return result

    @classmethod
    def _reset_summary(cls, published: Mapping[str, Any]) -> dict[str, Any]:
        result = cls._summary_snapshot(published)
        result["event"] = "reset"
        result["reset"] = True
        result["upserts"] = {}
        result["resets"] = sorted(
            str(item.get("symbol")) for item in result.get("items", [])
            if item.get("symbol")
        )
        return result

    @staticmethod
    def _normalize_point(point: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(dict(point))
        timestamp = datetime.fromisoformat(str(result.get("timestamp", "")))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("分钟时间必须带时区")
        result["timestamp"] = timestamp.astimezone(SHANGHAI).isoformat()
        return result

    @staticmethod
    def _points_trading_date(points: list[dict[str, Any]]) -> str | None:
        if not points:
            return None
        timestamp = points[-1].get("timestamp")
        return str(timestamp)[:10] if timestamp else None

    @classmethod
    def _current_day_points(
        cls, published: Mapping[str, Any], symbol: str,
    ) -> list[dict[str, Any]]:
        try:
            generated_at = datetime.fromisoformat(
                str(published.get("generated_at", "")),
            )
        except ValueError:
            return []
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            return []
        current_date = generated_at.astimezone(SHANGHAI).date().isoformat()
        for item in published.get("items", []):
            if item.get("symbol") != symbol:
                continue
            points = [
                cls._normalize_point(point)
                for point in item.get("points") or []
                if str(point.get("trading_date") or "") == current_date
            ]
            points.sort(key=lambda point: point["timestamp"])
            return points
        return []

    @classmethod
    def _revision_delta(
        cls,
        previous: Mapping[str, Any],
        current: Mapping[str, Any],
    ) -> dict[str, Any]:
        previous_summary = cls._summary_snapshot(previous)
        current_summary = cls._summary_snapshot(current)
        previous_items = {
            str(item.get("symbol")): item
            for item in previous_summary.get("items", [])
        }
        current_items = {
            str(item.get("symbol")): item
            for item in current_summary.get("items", [])
        }
        current_full_items = {
            str(item.get("symbol")): item
            for item in current.get("items", [])
        }
        changed_items = [
            copy.deepcopy(item)
            for symbol, item in current_items.items()
            if item != previous_items.get(symbol)
        ]
        removed_symbols = sorted(set(previous_items) - set(current_items))

        upserts: dict[str, list[dict[str, Any]]] = {}
        resets: set[str] = set(removed_symbols)
        symbols = set(previous_items) | set(current_items)
        for symbol in symbols:
            old_point_list = cls._current_day_points(previous, symbol)
            new_points = cls._current_day_points(current, symbol)
            old_points = {
                str(point.get("timestamp")): point for point in old_point_list
            }
            new_point_keys = {
                str(point.get("timestamp")) for point in new_points
            }
            current_item = current_full_items.get(symbol)
            current_missing = (
                current_item is None
                or current_item.get("status") == "MISSING_QUOTE"
                or "points" not in current_item
            )
            day_changed = (
                bool(old_point_list)
                and bool(new_points)
                and cls._points_trading_date(old_point_list)
                != cls._points_trading_date(new_points)
            )
            point_removed = bool(set(old_points) - new_point_keys)
            if current_missing or day_changed or point_removed:
                resets.add(symbol)
            changed = [
                copy.deepcopy(point)
                for point in new_points
                if point != old_points.get(str(point.get("timestamp")))
            ]
            if changed and symbol not in resets:
                upserts[symbol] = changed

        event = {
            key: copy.deepcopy(value)
            for key, value in current_summary.items()
            if key != "items"
        }
        event.update({
            "event": "delta",
            "items": changed_items,
            "removed_symbols": removed_symbols,
            "resets": sorted(resets),
            "upserts": upserts,
        })
        return event

    def _publish_locked(
        self,
        published: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        error: str | None = None,
        persistence_errors: list[str] | None = None,
        increment_revision: bool = True,
        force_reset_event: bool = False,
    ) -> None:
        if increment_revision:
            self._revision += 1
        previous = self._published
        result = copy.deepcopy(dict(published))
        result["revision"] = self._revision
        result["source"] = copy.deepcopy(payload.get("source"))
        result["refresh_error"] = error
        result["persistence_errors"] = list(persistence_errors or [])
        if persistence_errors:
            result["errors"] = [
                *list(result.get("errors") or []), *persistence_errors,
            ]
        result["last_refresh_at"] = payload.get("collected_at")
        self.refresh_error = error
        self.last_refresh_at = result["last_refresh_at"]
        self._published = result
        event = (
            self._reset_summary(result)
            if force_reset_event
            else self._revision_delta(previous, result)
        )
        self._revision_events.append(event)
        self._publish_condition.notify_all()

    def _publish(
        self,
        published: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        error: str | None = None,
        persistence_errors: list[str] | None = None,
        increment_revision: bool = True,
        force_reset_event: bool = False,
    ) -> None:
        with self._publish_condition:
            self._publish_locked(
                published,
                payload,
                error=error,
                persistence_errors=persistence_errors,
                increment_revision=increment_revision,
                force_reset_event=force_reset_event,
            )

    def _publish_outage(
        self,
        message: str,
        watchlist: Sequence[WatchItem] | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        now = self.clock() if now is None else now
        with self._publish_condition:
            self._revision += 1
            previous = self._published
            try:
                previous_generated = datetime.fromisoformat(
                    str(previous.get("generated_at", "")),
                )
                same_day = (
                    previous_generated.tzinfo is not None
                    and previous_generated.utcoffset() is not None
                    and previous_generated.astimezone(SHANGHAI).date()
                    == now.astimezone(SHANGHAI).date()
                )
            except ValueError:
                same_day = False
            if same_day:
                result = copy.deepcopy(previous)
            else:
                current_watchlist = watchlist
                if current_watchlist is None:
                    current_watchlist = tuple(
                        WatchItem(
                            str(item.get("symbol", "")),
                            str(item.get("name", "")),
                            float(
                                item.get("grid_width_pct")
                                or constants.DEFAULT_GRID_WIDTH_PCT
                            ),
                        )
                        for item in previous.get("items", [])
                        if item.get("symbol")
                    )
                result = snapshot_to_dict(self.engine.evaluate(
                    current_watchlist, {}, generated_at=now,
                ))
                result["source"] = copy.deepcopy(previous.get("source"))
                result["last_refresh_at"] = previous.get("last_refresh_at")
            result["revision"] = self._revision
            result["generated_at"] = now.isoformat()
            result["errors"] = [message]
            result["refresh_error"] = message
            result["persistence_errors"] = []
            for item in result.get("items", []):
                if item.get("action") in {"BUY_CANDIDATE", "SELL_CANDIDATE"}:
                    item["action"] = "DEVIATION_OBSERVE"
                    item["label"] = "偏离观察"
                item["health_status"] = "OUTAGE"
                item["health_reason"] = message
                reasons = list(item.get("blocked_reasons") or [])
                if "MARKET_NOT_REALTIME" not in reasons:
                    reasons.append("MARKET_NOT_REALTIME")
                item["blocked_reasons"] = reasons
            self.refresh_error = message
            self._published = result
            self._revision_events.append(self._revision_delta(previous, result))
            self._publish_condition.notify_all()

    def _publish_collection_failure(self, message: str) -> None:
        now = self.clock()
        session = market_session_state(
            now, closed_dates=self.health_classifier.closed_dates,
        )
        if session.active:
            self._publish_outage(message, now=now)
            return
        with self.refresh_lock:
            previous = copy.deepcopy(self._published)
        try:
            previous_generated = datetime.fromisoformat(
                str(previous.get("generated_at", "")),
            )
            previous_session = market_session_state(
                previous_generated,
                closed_dates=self.health_classifier.closed_dates,
            )
        except ValueError:
            previous_session = None
        if (
            self._published_date_matches(previous, now)
            and previous_session is not None
            and previous_session.phase == session.phase
            and not previous.get("errors")
            and previous.get("refresh_error") is None
            and all(
                item.get("health_status") == session.health_status
                for item in previous.get("items", [])
            )
        ):
            return
        self._bootstrap(increment_revision=True, now=now)

    def valuations(self) -> dict[str, Any]:
        metadata = self.metadata_store.load()
        store = ValuationStore(self.valuation_path) if self.valuation_path else None
        values = store.load() if store else {}
        items = []
        for symbol, item in metadata.items():
            snapshot = values.get(item.index.code)
            items.append({"symbol": symbol, "name": item.name, "index": item.index.to_dict(), "status": snapshot.status if snapshot else "MISSING_VALUATION", "valuation": snapshot.to_dict() if snapshot else None, "read_only": True})
        return {"generated_at": datetime.now(SHANGHAI).isoformat(), "items": items, "read_only": True}

    def industry_valuations(self) -> dict[str, Any]:
        path = self.metadata_path.parent.parent / "industry" / "watchlist.json"
        try:
            records = json.loads(path.read_text(encoding="utf-8")).get("items", [])
        except (OSError, json.JSONDecodeError):
            records = []
        values = ValuationStore(self.valuation_path).load() if self.valuation_path else {}
        items = []
        for r in records:
            code = str(r.get("index_code", "")); v = values.get(code)
            items.append({"symbol": r.get("symbol"), "name": r.get("name"), "index_code": code, "index_name": r.get("index_name"), "pe": v.pe_ttm if v else None, "pb": v.pb if v else None, "roe": v.roe_ttm if v else None, "pr": v.pr_pe_roe if v else None, "as_of": v.as_of if v else None, "status": "OK" if v and v.pr_pe_roe is not None else "MISSING_VALUATION"})
        return {"generated_at": datetime.now(SHANGHAI).isoformat(), "items": items, "read_only": True}

    def industry_swing(self) -> dict[str, Any]:
        payload = self.swing_application.snapshot()
        allowed = {str(x.get("symbol")) for x in json.loads((self.metadata_path.parent.parent / "industry" / "watchlist.json").read_text(encoding="utf-8")).get("items", [])}
        return {"generated_at": datetime.now(SHANGHAI).isoformat(), "items": [{"symbol": x.get("symbol"), "name": x.get("name"), "state": x.get("formal_state", "MISSING"), "score": x.get("formal_decision", {}).get("trend_score"), "data_status": x.get("execution_status", "MISSING")} for x in payload.get("items", []) if x.get("symbol") in allowed], "read_only": True}

    def valuation(self, symbol: str) -> dict[str, Any]:
        metadata = EtfMetadataStore(self.metadata_path).get(symbol) if self.metadata_path else None
        if metadata is None:
            return {"symbol": symbol, "status": "MISSING_METADATA", "index": None, "valuation": None, "read_only": True}
        snapshot = ValuationStore(self.valuation_path).get(metadata.index.code) if self.valuation_path else None
        usable = snapshot if snapshot and snapshot.status != "MISSING_VALUATION" else None
        return {"symbol": symbol, "status": snapshot.status if snapshot else "MISSING_VALUATION", "index": metadata.index.to_dict(), "valuation": usable.to_dict() if usable else None, "read_only": True}

    def t_backtest(self) -> dict[str, Any]:
        payload = json.loads(self.quotes_path.read_text(encoding="utf-8"))
        quotes = JsonQuoteAdapter().parse(payload)
        if self.history_path is not None:
            store = QuoteHistoryStore(self.history_path)
            quotes = store.merge(quotes)
        watchlist = load_watchlist(self.watchlist_path)
        metadata = self.metadata_store.load()
        invalid_symbols = self._replay_quality_symbols(payload, metadata)
        items = []
        for item in watchlist:
            if not item.enabled:
                continue
            if item.symbol in invalid_symbols:
                invalid = self._empty_backtest_item(item.symbol, "INVALID_DATA")
                invalid["reason"] = "存在尚未重新核验的隔离分钟，暂停该标的收益回测"
                items.append(invalid)
                continue
            quote = quotes.get(item.symbol)
            if quote is None:
                items.append(self._empty_backtest_item(item.symbol, "MISSING_QUOTE"))
                continue
            item_metadata = metadata.get(item.symbol)
            if item_metadata is None:
                items.append(self._empty_backtest_item(item.symbol, "MISSING_METADATA"))
                continue
            try:
                result = TBacktester(engine=self.engine).run(
                    quote, item, item_metadata.trading,
                ).to_dict()
            except ValueError as error:
                invalid = self._empty_backtest_item(item.symbol, "INVALID_DATA")
                invalid["reason"] = str(error)
                items.append(invalid)
                continue
            result["symbol"] = item.symbol
            items.append(result)
        return {
            "mode": "T_BACKTEST",
            "items": items,
            "commission_rate": constants.BUY_COMMISSION_RATE,
            "buy_commission_rate": constants.BUY_COMMISSION_RATE,
            "sell_commission_rate": constants.SELL_COMMISSION_RATE,
            "minimum_commission_cny": constants.MINIMUM_COMMISSION_CNY,
            "commission_minimum_waived": constants.MINIMUM_COMMISSION_CNY == 0,
            "slippage_rate": constants.SLIPPAGE_RATE,
            "volume_participation": constants.DEFAULT_VOLUME_PARTICIPATION,
            "execution_mode": "NEXT_COMPLETED_BAR",
            "read_only": True,
        }

    def backtest(self) -> dict[str, Any]:
        return self.t_backtest()

    def signal_replay(self) -> dict[str, Any]:
        payload = json.loads(self.quotes_path.read_text(encoding="utf-8"))
        quotes = JsonQuoteAdapter().parse(payload)
        if self.history_path is not None:
            quotes = QuoteHistoryStore(self.history_path).merge(quotes)
        invalid_symbols = self._replay_quality_symbols(
            payload,
        )
        items: list[dict[str, Any]] = []
        for item in load_watchlist(self.watchlist_path):
            if not item.enabled:
                continue
            if item.symbol in invalid_symbols:
                items.append({
                    "symbol": item.symbol, "status": "INVALID_DATA",
                    "reason": "存在尚未重新核验的隔离分钟，暂停该标的信号回放",
                    "evaluated_signal_count": 0, "candidate_action_count": 0,
                    "actions": [],
                })
                continue
            quote = quotes.get(item.symbol)
            if quote is None:
                items.append({
                    "symbol": item.symbol,
                    "status": "MISSING_QUOTE",
                    "evaluated_signal_count": 0,
                    "candidate_action_count": 0,
                    "actions": [],
                })
                continue
            completed = finalized_points(quote.points, quote.observed_at)
            actions: list[dict[str, Any]] = []
            for index in range(1, len(completed)):
                point = completed[index]
                previous = completed[index - 1]
                decision_quote = type(quote)(
                    quote.symbol,
                    quote.name,
                    previous.price,
                    previous.average_price,
                    previous.previous_close
                    if previous.previous_close is not None
                    else quote.previous_close,
                    previous.timestamp,
                    completed[:index],
                    point.timestamp,
                    quote.source,
                )
                signal = self.engine.evaluate(
                    (item,),
                    {quote.symbol: decision_quote},
                    generated_at=point.timestamp,
                ).signals[0]
                if signal.action == "BUY_CANDIDATE":
                    actions.append({
                        "action": signal.action,
                        "signal_timestamp": previous.timestamp.isoformat(),
                        "next_completed_timestamp": point.timestamp.isoformat(),
                    })
                elif signal.action == "SELL_CANDIDATE":
                    actions.append({
                        "action": signal.action,
                        "signal_timestamp": previous.timestamp.isoformat(),
                        "next_completed_timestamp": point.timestamp.isoformat(),
                    })
            items.append({
                "symbol": item.symbol,
                "status": "OK",
                "evaluated_signal_count": max(0, len(completed) - 1),
                "candidate_action_count": len(actions),
                "actions": actions,
            })
        return {
            "mode": "SIGNAL_ROUGH_REPLAY",
            "execution_mode": "NEXT_COMPLETED_BAR",
            "items": items,
            "read_only": True,
        }

    @staticmethod
    def _empty_backtest_item(symbol: str, status: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "status": status,
            "baseline_equity_cny": None,
            "strategy_equity_cny": None,
            "t_net_gain_cny": None,
            "completed_pair_count": 0,
            "completed_pairs": [],
            "open_leg_count": 0,
            "open_legs": [],
            "rejections": [],
            "inventory": None,
            "costs": None,
            "execution_mode": "NEXT_COMPLETED_BAR",
            "outperformed_baseline": None,
        }

    def add_watch_item(self, symbol: object, name: object) -> dict[str, Any]:
        if not isinstance(symbol, str) or re.fullmatch(r"\d{6}", symbol.strip()) is None:
            raise ValueError("代码必须是6位数字")
        if name is not None and not isinstance(name, str):
            raise ValueError("名称必须是字符串")
        normalized_symbol = symbol.strip()
        normalized_name = name.strip() if isinstance(name, str) else ""
        if len(normalized_name) > 50:
            raise ValueError("名称不能超过50个字符")
        item = {
            "symbol": normalized_symbol,
            "name": normalized_name or normalized_symbol,
            "grid_width_pct": constants.DEFAULT_GRID_WIDTH_PCT,
            "enabled": True,
        }
        with self.producer_lock:
            with self.watchlist_lock:
                watchlist = load_watchlist(self.watchlist_path)
                if any(current.symbol == normalized_symbol for current in watchlist):
                    raise FileExistsError(f"代码已在监控列表中: {normalized_symbol}")
                if normalized_symbol not in self.metadata_store.load():
                    raise MissingWatchMetadataError(
                        f"缺少交易元数据，无法添加: {normalized_symbol}",
                    )
                self._atomic_write_watchlist([*watchlist, item])
            self._bootstrap(increment_revision=True)
        return item

    def _atomic_write_watchlist(self, records: list[Any]) -> None:
        payload = [
            {
                "symbol": item.symbol,
                "name": item.name,
                "grid_width_pct": item.grid_width_pct,
                "enabled": item.enabled,
                **({"base_notional_cny": item.base_notional_cny} if item.base_notional_cny is not None else {}),
                **({"base_shares": item.base_shares} if item.base_shares is not None else {}),
                **({"t_capacity_ratio": item.t_capacity_ratio} if item.t_capacity_ratio is not None else {}),
                **({"t_capacity_shares": item.t_capacity_shares} if item.t_capacity_shares is not None else {}),
            } if not isinstance(item, dict) else item
            for item in records
        ]
        path = Path(self.watchlist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", newline="\n", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump({"watchlist": payload}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        application: MonitorApplication,
        swing_application: SwingService | None = None,
    ):
        super().__init__(address, MonitorRequestHandler)
        self.application = application
        self.swing_application = swing_application
        self.notifications = None
        self.notification_error = None
        self.request_deadline_seconds = _REQUEST_SOCKET_TIMEOUT_SECONDS
        self.response_socket_timeout_seconds = _RESPONSE_SOCKET_TIMEOUT_SECONDS

    def server_close(self) -> None:
        try:
            if self.notifications is not None:
                self.notifications.stop()
            if self.swing_application is not None:
                self.swing_application.stop_refresh()
        finally:
            try:
                self.application.stop_refresh()
            finally:
                super().server_close()


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        self._request_deadline: float | None = None
        super().setup()
        self.connection.settimeout(
            getattr(
                self.server,
                "response_socket_timeout_seconds",
                _RESPONSE_SOCKET_TIMEOUT_SECONDS,
            ),
        )
        self.rfile = _DeadlineReader(
            self.rfile,
            self.connection,
            lambda: self._request_deadline,
        )

    def send_response(self, code: int, message: str | None = None) -> None:
        self._request_deadline = None
        self.connection.settimeout(
            getattr(
                self.server,
                "response_socket_timeout_seconds",
                _RESPONSE_SOCKET_TIMEOUT_SECONDS,
            ),
        )
        super().send_response(code, message)

    def handle_one_request(self) -> None:
        """Handle one request, quieting only an aborted request-line read."""
        deadline_seconds = float(getattr(
            self.server,
            "request_deadline_seconds",
            _REQUEST_SOCKET_TIMEOUT_SECONDS,
        ))
        self._request_deadline = time.monotonic() + deadline_seconds
        self.requestline = ""
        self.request_version = ""
        self.command = ""
        try:
            try:
                self.raw_requestline = self.rfile.readline(65537)
            except _EXPECTED_CLIENT_DISCONNECTS:
                self.close_connection = True
                return
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            try:
                parsed = self.parse_request()
            except UnicodeError:
                self.close_connection = True
                self._json(HTTPStatus.FORBIDDEN, {
                    "error": "forbidden", "message": "请求头编码无效",
                })
                return
            if not parsed:
                return
            method_name = "do_" + self.command
            if not hasattr(self, method_name):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "Unsupported method (%r)" % self.command,
                )
                return
            method = getattr(self, method_name)
            method()
            self.wfile.flush()
        except _RequestDeadlineExceeded as error:
            self.close_connection = True
            try:
                self._json(HTTPStatus.REQUEST_TIMEOUT, {
                    "error": "request_timeout", "message": str(error),
                })
            except (OSError, _RequestDeadlineExceeded):
                pass
            return
        except socket.timeout as error:
            self.log_error("Request timed out: %r", error)
            self.close_connection = True
            return

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/notifications" or path.startswith("/api/notifications"):
            from .notification_web import handle_get
            handle_get(self, parsed)
        elif path == "/":
            self._send(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/industry":
            if self._reject_unexpected_query(parsed.query):
                return
            self._send(HTTPStatus.OK, INDUSTRY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/industry-swing":
            if self._reject_unexpected_query(parsed.query):
                return
            self._send(HTTPStatus.OK, INDUSTRY_SWING_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/industry/valuations":
            if self._reject_unexpected_query(parsed.query):
                return
            self._json(HTTPStatus.OK, self.server.application.industry_valuations())
        elif path == "/api/industry/swing":
            if self._reject_unexpected_query(parsed.query):
                return
            payload = self.server.swing_application.snapshot() if self.server.swing_application is not None else {"items": []}
            path = self.server.application.metadata_path.parent.parent / "industry" / "watchlist.json"
            try:
                industry_records = json.loads(path.read_text(encoding="utf-8")).get("items", [])
            except (OSError, json.JSONDecodeError):
                industry_records = []
            industry_names = {str(x.get("symbol")): x.get("name", str(x.get("symbol"))) for x in industry_records}
            allowed = set(industry_names)
            snapshot_items = {str(x.get("symbol")): x for x in payload.get("items", [])}
            rows = []
            for symbol in sorted(allowed):
                x = snapshot_items.get(symbol)
                if x is None:
                    rows.append({"symbol": symbol, "name": industry_names[symbol], "state": "DATA_NOT_READY", "score": None, "data_status": "INDUSTRY_HISTORY_PENDING"})
                else:
                    rows.append({"symbol": x.get("symbol"), "name": x.get("name"), "state": x.get("formal_state", "MISSING"), "score": x.get("formal_decision", {}).get("trend_score"), "data_status": x.get("execution_status", "MISSING")})
            self._json(HTTPStatus.OK, {"generated_at": datetime.now(SHANGHAI).isoformat(), "items": rows, "read_only": True})
        elif path == "/pr":
            if self._reject_unexpected_query(parsed.query):
                return
            self._send(HTTPStatus.OK, PR_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/valuations":
            if self._reject_unexpected_query(parsed.query):
                return
            self._json(HTTPStatus.OK, self.server.application.valuations())
        elif path == "/portfolio":
            if self._reject_unexpected_query(parsed.query):
                return
            from .portfolio_page import PORTFOLIO_PAGE

            self._send(
                HTTPStatus.OK,
                PORTFOLIO_PAGE.encode("utf-8"),
                "text/html; charset=utf-8",
            )
        elif path == "/api/portfolio":
            if self._reject_unexpected_query(parsed.query):
                return
            # Reuse the published account view without a second cache or producer.
            self._swing_read(lambda application: application.portfolio())
        elif path == "/swing":
            self._send(
                HTTPStatus.OK,
                SWING_PAGE.encode("utf-8"),
                "text/html; charset=utf-8",
            )
        elif path == "/api/swing/snapshot":
            if self._reject_unexpected_query(parsed.query):
                return
            self._swing_read(lambda application: application.snapshot())
        elif path == "/api/swing/watchlist":
            if self._reject_unexpected_query(parsed.query):
                return
            self._swing_read(lambda application: application.watchlist())
        elif path == "/api/swing/daily-quotes":
            self._swing_daily_quotes(parsed.query)
        elif path == "/api/swing/events":
            self._swing_events(parsed.query)
        elif path == "/api/swing/portfolio":
            if self._reject_unexpected_query(parsed.query):
                return
            self._swing_read(lambda application: application.portfolio())
        elif path == "/api/swing/alerts":
            self._swing_alerts(parsed.query)
        elif path == "/api/swing/backtest":
            self._swing_backtest(parsed.query)
        elif path == "/api/snapshot":
            self._snapshot()
        elif path == "/api/quotes":
            self._quotes()
        elif path == "/api/events":
            self._events()
        elif path == "/api/t-backtest":
            self._t_backtest()
        elif path == "/api/signal-replay":
            self._signal_replay()
        elif path == "/api/backtest":
            self._backtest()
        elif re.fullmatch(r"/api/etf/\d{6}/valuation", path):
            self._valuation(path.split("/")[3])
        elif path == "/api/alerts":
            self._alerts()
        elif path == "/api/history/dates":
            self._history_dates()
        elif path == "/api/history/quotes":
            self._history_quotes()
        elif path == "/health":
            self._json(HTTPStatus.OK, self.server.application.health())
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        self.close_connection = True
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/notifications" or path.startswith("/api/notifications"):
            from .notification_web import handle_post
            handle_post(self, parsed)
            return
        if (path == "/swing" or path.startswith("/api/swing/")) and parsed.query:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_query", "message": "query parameters are not allowed",
            })
            return
        if path == "/api/watchlist":
            self._add_watch_item()
        elif path == "/api/swing/watchlist":
            self._swing_update_watchlist()
        elif path == "/api/swing/portfolio/initialize":
            self._swing_initialize_portfolio()
        elif path == "/api/swing/trades":
            self._swing_record_trade()
        elif re.fullmatch(
            r"/api/swing/trades/[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/reverse",
            path,
        ):
            self._swing_reverse_trade(path.split("/")[4])
        elif re.fullmatch(r"/api/swing/alerts/[0-9a-f]{24}/acknowledge", path):
            self._swing_alert_transition(path.split("/")[4], "acknowledge")
        elif re.fullmatch(r"/api/swing/alerts/[0-9a-f]{24}/ignore", path):
            self._swing_alert_transition(path.split("/")[4], "ignore")
        elif path in {
            "/portfolio",
            "/api/portfolio",
            "/swing",
            "/api/swing/snapshot",
            "/api/swing/daily-quotes",
            "/api/swing/events",
            "/api/swing/portfolio",
            "/api/swing/alerts",
            "/api/swing/backtest",
        }:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {
                "error": "method_not_allowed",
                "message": "resource is read-only",
            })
        elif path == "/api/swing" or path.startswith("/api/swing/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        else:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {
                "error": "read_only",
                "message": "本服务仅允许写入监控列表，不提供交易接口",
            })

    def log_message(self, format: str, *args: object) -> None:
        return

    @staticmethod
    def _propagate_disconnect(error: BaseException) -> None:
        if isinstance(error, _EXPECTED_CLIENT_DISCONNECTS):
            raise error

    def _add_watch_item(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("请求体大小无效")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是对象")
            item = self.server.application.add_watch_item(
                payload.get("symbol"), payload.get("name"),
            )
            self._json(HTTPStatus.CREATED, {"item": item})
        except FileExistsError as error:
            self._json(HTTPStatus.CONFLICT, {"error": "duplicate", "message": str(error)})
        except MissingWatchMetadataError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {
                "error": "missing_metadata",
                "message": str(error),
            })
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(error)})
        except OSError as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _swing_application(self) -> SwingService:
        application = self.server.swing_application
        if application is None:
            raise SwingServiceError("swing service is unavailable")
        return application

    @staticmethod
    def _reject_json_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON value is not allowed: {value}")

    @staticmethod
    def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def _read_json_object(self, max_bytes: int = 16_384) -> dict[str, object]:
        self.close_connection = True
        if self.headers.get_all("Transfer-Encoding", failobj=[]):
            raise ValueError("Transfer-Encoding is not supported")
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if len(content_types) != 1:
            raise ValueError("Content-Type must be application/json")
        media_type, *parameters = [
            value.strip() for value in content_types[0].split(";")
        ]
        if media_type.lower() != "application/json":
            raise ValueError("Content-Type must be application/json")
        parameter_names: set[str] = set()
        for parameter in parameters:
            if "=" not in parameter:
                raise ValueError("Content-Type parameters are invalid")
            key, value = (part.strip().lower() for part in parameter.split("=", 1))
            if key in parameter_names:
                raise ValueError("Content-Type parameters must not be repeated")
            parameter_names.add(key)
            if key != "charset" or value.strip('"') not in {"utf-8", "utf8"}:
                raise ValueError("JSON charset must be UTF-8")

        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1 or re.fullmatch(r"(?:0|[1-9][0-9]*)", lengths[0]) is None:
            raise ValueError("Content-Length must be one canonical nonnegative integer")
        length = int(lengths[0])
        if not 0 < length <= max_bytes:
            raise ValueError("request body size is invalid")
        try:
            raw = self.rfile.read(length)
        except (_RequestDeadlineExceeded, socket.timeout) as error:
            raise _RequestBodyTimeoutError(
                "request body timed out before Content-Length bytes arrived",
            ) from error
        if len(raw) != length:
            raise ValueError("request body is shorter than Content-Length")
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(
            text,
            object_pairs_hook=self._json_object,
            parse_constant=self._reject_json_constant,
        )
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _idempotency_key(self) -> str:
        values = self.headers.get_all("Idempotency-Key", failobj=[])
        if len(values) != 1:
            raise ValueError("Idempotency-Key is required")
        key = values[0]
        if (
            not key or key != key.strip() or len(key) > 256
            or any(ord(character) < 32 for character in key)
        ):
            raise ValueError("Idempotency-Key is invalid")
        return key

    @staticmethod
    def _require_fields(
        payload: Mapping[str, object],
        required: set[str],
        optional: set[str] = frozenset(),
    ) -> None:
        fields = set(payload)
        if not required <= fields or not fields <= required | optional:
            raise ValueError("request fields are invalid")

    def _swing_request_error(self, error: Exception) -> None:
        self._json(HTTPStatus.BAD_REQUEST, {
            "error": "invalid_request", "message": str(error),
        })

    def _swing_write_error(self, error: Exception) -> None:
        if isinstance(error, _RequestBodyTimeoutError):
            self._json(HTTPStatus.REQUEST_TIMEOUT, {
                "error": "request_timeout", "message": str(error),
            })
        elif isinstance(
            error, (SwingServiceError, PortfolioLedgerError, AlertStoreError),
        ):
            self._swing_domain_error(error)
        else:
            self._swing_request_error(error)

    def _swing_domain_error(self, error: Exception) -> None:
        message = str(error)
        conflict = any(fragment in message for fragment in (
            "idempotency_key was reused",
            "idempotency key was reused",
            "already initialized",
            "already reversed",
        ))
        self._json(
            HTTPStatus.CONFLICT if conflict else HTTPStatus.UNPROCESSABLE_ENTITY,
            {"error": "conflict" if conflict else "unprocessable", "message": message},
        )

    def _swing_read(
        self, reader: Callable[[SwingService], dict[str, object]],
    ) -> None:
        try:
            self._json(HTTPStatus.OK, reader(self._swing_application()))
        except (SwingServiceError, PortfolioLedgerError, AlertStoreError) as error:
            self._swing_domain_error(error)
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)

    def _reject_unexpected_query(self, query: str) -> bool:
        if not query:
            return False
        self._json(HTTPStatus.BAD_REQUEST, {
            "error": "invalid_query", "message": "query parameters are not allowed",
        })
        return True

    @staticmethod
    def _strict_query(query: str) -> dict[str, list[str]]:
        return parse_qs(query, keep_blank_values=True, strict_parsing=True)

    @staticmethod
    def _parse_nonnegative_cursor(value: str, label: str) -> int:
        if re.fullmatch(
            rf"(?:0|[1-9][0-9]{{0,{_MAX_CURSOR_DIGITS - 1}}})",
            value,
        ) is None:
            raise ValueError(f"{label} must be a bounded nonnegative integer")
        return int(value)

    def _swing_daily_quotes(self, query_string: str) -> None:
        try:
            query = self._strict_query(query_string)
            if not {"symbol", "since"} <= set(query) or not set(query) <= {
                "symbol", "since", "limit",
            }:
                raise ValueError("symbol and since are required")
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("query parameters must not be repeated")
            symbol = query["symbol"][0]
            since_text = query["since"][0]
            limit_text = query.get("limit", ["500"])[0]
            if re.fullmatch(r"[0-9]{6}", symbol, flags=re.ASCII) is None:
                raise ValueError("symbol must be six ASCII digits")
            since = self._parse_nonnegative_cursor(since_text, "since")
            if re.fullmatch(r"[1-9][0-9]*", limit_text) is None:
                raise ValueError("limit must be a positive integer")
            payload = self._swing_application().daily_quotes(
                symbol, since, int(limit_text),
            )
        except (ValueError, SwingServiceError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_query", "message": str(error),
            })
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.OK, payload)

    def _swing_backtest(self, query_string: str) -> None:
        try:
            query = self._strict_query(query_string)
            if "scope" not in query or len(query["scope"]) != 1:
                raise ValueError("scope is required exactly once")
            scope = query["scope"][0]
            if scope == "symbol":
                if set(query) != {"scope", "symbol"} or len(query["symbol"]) != 1:
                    raise ValueError(
                        "symbol scope requires exactly one symbol",
                    )
                symbol = query["symbol"][0]
                if re.fullmatch(r"[0-9]{6}", symbol, flags=re.ASCII) is None:
                    raise ValueError("symbol must be six ASCII digits")
            elif scope == "portfolio":
                if set(query) != {"scope"}:
                    raise ValueError("portfolio scope accepts no symbol")
                symbol = None
            else:
                raise ValueError("scope must be symbol or portfolio")
            payload = self._swing_application().backtest(symbol, scope)
        except (ValueError, SwingServiceError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_query", "message": str(error),
            })
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.OK, payload)

    def _swing_alerts(self, query_string: str) -> None:
        try:
            query = self._strict_query(query_string) if query_string else {}
            if not set(query) <= {"include_retracted", "limit"}:
                raise ValueError("only include_retracted and limit are supported")
            if any(len(values) != 1 for values in query.values()):
                raise ValueError("query parameters must not be repeated")
            include_retracted = False
            if "include_retracted" in query:
                raw = query["include_retracted"][0]
                if raw not in {"true", "false"}:
                    raise ValueError("include_retracted must be true or false")
                include_retracted = raw == "true"
            limit = None
            if "limit" in query:
                raw_limit = query["limit"][0]
                if re.fullmatch(r"[1-9][0-9]*", raw_limit) is None:
                    raise ValueError("limit must be a positive integer")
                limit = int(raw_limit)
            payload = self._swing_application().alerts(
                include_retracted=include_retracted,
                limit=limit,
            )
        except (ValueError, SwingServiceError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_query", "message": str(error),
            })
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.OK, payload)

    def _swing_events(self, query_string: str) -> None:
        if self._reject_unexpected_query(query_string):
            return
        headers = self.headers.get_all("Last-Event-ID", failobj=[])
        try:
            if len(headers) > 1:
                raise ValueError("Last-Event-ID must not be repeated")
            parsed_revision = (
                self._parse_nonnegative_cursor(headers[0], "Last-Event-ID")
                if headers else None
            )
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_last_event_id",
                "message": str(error),
            })
            return
        application = self._swing_application()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        try:
            self.end_headers()
        except _EXPECTED_CLIENT_DISCONNECTS:
            return
        if not headers:
            payload = application.snapshot()
            after_revision = int(payload["revision"])
            if not self._write_snapshot_event(payload):
                return
        else:
            after_revision = parsed_revision
        while not application._stop_event.is_set():
            payload = application.wait_for_event(after_revision, timeout=5.0)
            if payload is None:
                if application._stop_event.is_set():
                    return
                if not self._write_sse(b": heartbeat\n\n"):
                    return
                continue
            after_revision = int(payload["revision"])
            if not self._write_snapshot_event(payload):
                return

    def _swing_update_watchlist(self) -> None:
        try:
            payload = self._read_json_object()
            self._require_fields(payload, {"symbol", "enabled"})
            result = self._swing_application().update_watchlist(
                payload["symbol"], payload["enabled"],
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._swing_write_error(error)
            return
        except (SwingServiceError, PortfolioLedgerError, AlertStoreError) as error:
            self._swing_domain_error(error)
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.OK, result)

    def _swing_initialize_portfolio(self) -> None:
        try:
            payload = self._read_json_object()
            key = self._idempotency_key()
            self._require_fields(
                payload, {"name", "cash"},
                {"initial_positions", "default_risk_per_trade"},
            )
            result = self._swing_application().initialize_portfolio(
                payload["name"], payload["cash"], key,
                initial_positions=payload.get("initial_positions"),
                default_risk_per_trade=payload.get("default_risk_per_trade"),
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._swing_write_error(error)
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.CREATED, result)

    def _swing_record_trade(self) -> None:
        try:
            payload = self._read_json_object()
            key = self._idempotency_key()
            self._require_fields(
                payload,
                {"symbol", "side", "shares", "price", "fee", "executed_at"},
                {"planned_risk_per_share", "exit_reason"},
            )
            raw_time = payload["executed_at"]
            if type(raw_time) is not str:
                raise ValueError("executed_at must be an ISO datetime string")
            executed_at = datetime.fromisoformat(raw_time)
            trade = TradeInput(
                symbol=payload["symbol"], side=payload["side"],
                shares=payload["shares"], price=payload["price"], fee=payload["fee"],
                executed_at=executed_at,
                planned_risk_per_share=payload.get("planned_risk_per_share", 0.0),
                exit_reason=payload.get("exit_reason"),
            )
            result = self._swing_application().record_trade(trade, key)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._swing_write_error(error)
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.CREATED, result)

    def _swing_reverse_trade(self, event_id: str) -> None:
        try:
            payload = self._read_json_object()
            key = self._idempotency_key()
            self._require_fields(payload, set())
            result = self._swing_application().reverse_trade(event_id, key)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._swing_write_error(error)
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.CREATED, result)

    def _swing_alert_transition(self, alert_id: str, action: str) -> None:
        try:
            payload = self._read_json_object()
            key = self._idempotency_key()
            self._require_fields(payload, set())
            application = self._swing_application()
            result = (
                application.acknowledge_alert(alert_id, key)
                if action == "acknowledge"
                else application.ignore_alert(alert_id, key)
            )
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self._swing_write_error(error)
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._swing_domain_error(error)
            return
        self._json(HTTPStatus.OK, result)

    def _snapshot(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.snapshot())
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _quotes(self) -> None:
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        symbols = query.get("symbol", [])
        cursors = query.get("since", [])
        if (
            len(symbols) != 1
            or len(cursors) != 1
            or re.fullmatch(r"[0-9]{6}", symbols[0]) is None
            or re.fullmatch(r"[0-9]+", cursors[0]) is None
        ):
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_query",
                "message": "symbol必须是启用的ASCII 6位代码，since必须是非负整数",
            })
            return
        try:
            payload = self.server.application.quotes(symbols[0], int(cursors[0]))
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {
                "error": "invalid_query", "message": str(error),
            })
            return
        except OSError as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, payload)

    def _t_backtest(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.t_backtest())
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _signal_replay(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.signal_replay())
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _backtest(self) -> None:
        try:
            payload = self.server.application.t_backtest()
            payload["deprecated_alias"] = True
            self._json(HTTPStatus.OK, payload)
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _valuation(self, symbol: str) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.valuation(symbol))
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.OK, {"symbol": symbol, "status": "UNKNOWN", "index": None, "valuation": None, "error": str(error), "read_only": True})

    def _history_dates(self) -> None:
        path = self.server.application.history_path
        try:
            dates = QuoteHistoryStore(path).available_dates() if path is not None else []
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"dates": dates, "read_only": True})

    def _history_quotes(self) -> None:
        path = self.server.application.history_path
        query = parse_qs(urlsplit(self.path).query)
        trading_date = query.get("date", [None])[0]
        symbol = query.get("symbol", [None])[0]
        if path is None or trading_date is None or re.fullmatch(r"\d{4}-\d{2}-\d{2}", trading_date) is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_date"})
            return
        try:
            records = QuoteHistoryStore(path).query(trading_date, symbol)
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"date": trading_date, "symbol": symbol, "records": records, "read_only": True})

    def _alerts(self) -> None:
        if self.server.application.alert_history_path is None:
            self._json(HTTPStatus.OK, {"items": []})
            return
        query = parse_qs(urlsplit(self.path).query)
        try:
            limit = min(max(int(query.get("limit", ["100"])[0]), 1), 500)
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_limit"})
            return
        try:
            items = AlertHistoryStore(self.server.application.alert_history_path).query(
                query.get("date", [None])[0], query.get("symbol", [None])[0],
                query.get("action", [None])[0], limit,
            )
        except (ValueError, OSError) as error:
            self._propagate_disconnect(error)
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"items": items, "read_only": True})

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        try:
            self.end_headers()
        except _EXPECTED_CLIENT_DISCONNECTS:
            return
        application = self.server.application
        header = self.headers.get("Last-Event-ID")
        if header is None:
            payload = application.snapshot()
            after_revision = int(payload["revision"])
            if not self._write_snapshot_event(payload):
                return
        else:
            try:
                after_revision = int(header)
            except (TypeError, ValueError):
                payload = application.snapshot()
                after_revision = int(payload["revision"])
                if not self._write_snapshot_event(payload):
                    return
        while not application.is_stopping():
            payload = application.wait_for_revision(after_revision, timeout=5.0)
            if payload is None:
                if application.is_stopping():
                    return
                if not self._write_sse(b": heartbeat\n\n"):
                    return
                continue
            after_revision = int(payload["revision"])
            if not self._write_snapshot_event(payload):
                return

    def _write_snapshot_event(self, payload: Mapping[str, Any]) -> bool:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        revision = int(payload["revision"])
        event_name = (
            str(payload["event"])
            if payload.get("event") in {"delta", "reset"}
            else "snapshot"
        )
        return self._write_sse(
            f"id: {revision}\nevent: {event_name}\ndata: {content}\n\n".encode("utf-8"),
        )

    def _write_sse(self, value: bytes) -> bool:
        try:
            self.wfile.write(value)
            self.wfile.flush()
        except _EXPECTED_CLIENT_DISCONNECTS:
            return False
        return True

    def _json(self, status: HTTPStatus, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
        try:
            self.end_headers()
        except _EXPECTED_CLIENT_DISCONNECTS:
            return
        try:
            self.wfile.write(body)
        except _EXPECTED_CLIENT_DISCONNECTS:
            return


def create_server(
    host: str,
    port: int,
    quotes_path: Path,
    watchlist_path: Path,
    history_path: Path | None = None,
    collector: Any | None = None,
    refresh_interval: float = constants.DEFAULT_REFRESH_INTERVAL_SECONDS,
    alert_history_path: Path | None = None,
    metadata_path: Path | None = None,
    valuation_path: Path | None = None,
    calendar_path: Path | None = None,
    clock: Callable[[], datetime] | None = None,
    swing_paths: SwingPaths | None = None,
    swing_collector: Any | None = None,
    swing_clock: Callable[[], datetime] | None = None,
) -> MonitorServer:
    application = MonitorApplication(
        quotes_path=quotes_path,
        watchlist_path=watchlist_path,
        history_path=history_path,
        collector=collector,
        refresh_interval=refresh_interval,
        alert_history_path=alert_history_path,
        metadata_path=metadata_path,
        valuation_path=valuation_path,
        calendar_path=calendar_path,
        **({"clock": clock} if clock is not None else {}),
    )
    if swing_paths is None:
        swing_root = Path(quotes_path).parent / "swing"
        swing_paths = SwingPaths(
            watchlist=swing_root / "watchlist.json",
            strategy=swing_root / "strategy.json",
            daily_history=swing_root / "daily_quotes.jsonl",
            portfolio_snapshot=swing_root / "portfolio.json",
            trades=swing_root / "trades.jsonl",
            alerts=swing_root / "alerts.jsonl",
            metadata=Path(
                _DEFAULT_METADATA_PATH if metadata_path is None else metadata_path,
            ),
            calendar=Path(
                _DEFAULT_CALENDAR_PATH if calendar_path is None else calendar_path,
            ),
            backtests=swing_root / "backtests",
        )
    swing_application = SwingService(
        swing_paths,
        collector=swing_collector,
        intraday_provider=application.snapshot,
        intraday_points_provider=lambda symbol: application.quotes(symbol, 0),
        clock=(swing_clock or clock or application.clock),
        refresh_interval=refresh_interval,
        valuation_path=valuation_path,
    )
    server = MonitorServer((host, port), application, swing_application)
    try:
        application.start_refresh()
        swing_application.start_refresh()
        # Notifications are optional and cannot take the existing market
        # service down if their private configuration/storage is unavailable.
        try:
            from .notification_service import NotificationService
            runtime_parent = Path(quotes_path).parent
            if runtime_parent.name == "monitor":
                runtime_parent = runtime_parent.parent
            server.notifications = NotificationService(
                runtime_parent / "notifications", application, swing_application,
                clock=application.clock, read_only=collector is None,
            )
            server.notifications.start()
        except Exception:
            server.notification_error = "通知模块启动失败；行情服务继续运行"
    except BaseException as start_error:
        try:
            server.server_close()
        except BaseException as cleanup_error:
            start_error.add_note(
                f"server cleanup also failed: {cleanup_error!r}",
            )
        raise
    return server
