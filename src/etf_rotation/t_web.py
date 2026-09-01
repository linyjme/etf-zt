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
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from . import constants
from .etf_metadata import EtfMetadataStore
from .market_data import (
    MarketDataValidator,
    MarketHealthClassifier,
    MinuteHistoryStore,
    SHANGHAI,
    finalized_points,
    load_closed_dates,
)
from .t_backtest import TBacktester
from .t_monitor import (
    AlertHistoryStore,
    JsonQuoteAdapter,
    Quote,
    QuoteHistoryStore,
    TMonitorEngine,
    load_watchlist,
    snapshot_to_dict,
)
from .t_page import PAGE
from .valuation import ValuationStore


_DATA_ROOT = Path(__file__).resolve().parents[2] / "data" / "monitor"
_DEFAULT_METADATA_PATH = _DATA_ROOT / "etf_metadata.json"
_DEFAULT_CALENDAR_PATH = _DATA_ROOT / "market_calendar.json"


@dataclass
class MonitorApplication:
    quotes_path: Path
    watchlist_path: Path
    history_path: Path | None = None
    collector: Any | None = None
    refresh_interval: float = 5.0
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
        with self.refresh_lock:
            published = self._published
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
        all_points = self._current_day_points(published, symbol)
        reset = (
            since == 0
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
            and not {"OUTAGE", "DELAYED"}.intersection(statuses)
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
                current = self._published
                current_revision = int(current.get("revision", 0))
                events = tuple(self._revision_events)
                if after_revision > current_revision:
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
                if selected is not None or self._stop_event.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._publish_condition.wait(remaining)
        return copy.deepcopy(selected) if selected is not None else None

    def refresh_once(self) -> bool:
        return self._refresh_once(generation=None)

    def _refresh_once(self, generation: int | None) -> bool:
        with self.producer_lock:
            if self.collector is None:
                return False
            staging: Path | None = None
            try:
                watchlist = load_watchlist(self.watchlist_path)
                staging = self._staging_quotes_path()
                self.collector.collect_to_file(watchlist, staging)
                payload = json.loads(staging.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("行情文件必须是对象")
                all_quotes = JsonQuoteAdapter().parse(payload)
                metadata: Mapping[str, Any] = {}
                if self.history_store is not None:
                    metadata = self.metadata_store.load()
                    self._validate_quotes(all_quotes, metadata)
            except Exception as error:
                if staging is not None:
                    staging.unlink(missing_ok=True)
                    staging = None
                with self.lifecycle_gate:
                    if self._generation_cancelled(generation):
                        return False
                    self._publish_outage(str(error))
                return False
            try:
                with self.lifecycle_gate:
                    if self._generation_cancelled(generation):
                        return False
                    try:
                        now = self.clock()
                        quotes = self._quotes_for_now(all_quotes, now)
                        health = self._health_by_symbol(quotes, now)
                        published = snapshot_to_dict(self.engine.evaluate(
                            watchlist, quotes, generated_at=now, health=health,
                        ))
                    except Exception as error:
                        self._publish_outage(str(error))
                        return False
                    try:
                        self._commit_staged_quotes(staging)
                        staging = None
                    except Exception as error:
                        self._publish_outage(str(error))
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
        try:
            while True:
                with self.lifecycle_gate:
                    if self._generation_cancelled(generation):
                        break
                self._refresh_once(generation)
                self._stop_event.wait(self.refresh_interval)
        finally:
            with self.lifecycle_gate:
                if self._refresh_thread is thread:
                    self._refresh_thread = None

    def _bootstrap(self, *, increment_revision: bool) -> None:
        watchlist = load_watchlist(self.watchlist_path)
        error: str | None = None
        try:
            raw = json.loads(self.quotes_path.read_text(encoding="utf-8"))
            all_quotes = JsonQuoteAdapter().parse(raw)
            payload = raw if isinstance(raw, dict) else {}
        except (ValueError, OSError) as failure:
            all_quotes = {}
            payload = {}
            error = str(failure)
        if error is None and self.history_store is not None:
            try:
                self._validate_quotes(all_quotes, self.metadata_store.load())
            except (ValueError, OSError) as failure:
                error = str(failure)
        now = self.clock()
        quotes = self._quotes_for_now(all_quotes, now)
        health = self._health_by_symbol(quotes, now, error)
        published = snapshot_to_dict(self.engine.evaluate(
            watchlist, quotes, generated_at=now, health=health,
        ))
        if error is not None:
            published["errors"] = [error]
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
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for symbol, quote in quotes.items():
            completed = finalized_points(quote.points, quote.observed_at)
            result[symbol] = self.health_classifier.classify(
                now, completed[-1].timestamp if completed else None, error,
            )
        return result

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

    def _publish(
        self,
        published: Mapping[str, Any],
        payload: Mapping[str, Any],
        *,
        error: str | None = None,
        persistence_errors: list[str] | None = None,
        increment_revision: bool = True,
    ) -> None:
        with self._publish_condition:
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
            self._revision_events.append(self._revision_delta(previous, result))
            self._publish_condition.notify_all()

    def _publish_outage(self, message: str) -> None:
        with self._publish_condition:
            self._revision += 1
            previous = self._published
            result = copy.deepcopy(self._published)
            result["revision"] = self._revision
            result["generated_at"] = self.clock().isoformat()
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

    def valuation(self, symbol: str) -> dict[str, Any]:
        metadata = EtfMetadataStore(self.metadata_path).get(symbol) if self.metadata_path else None
        if metadata is None:
            return {"symbol": symbol, "status": "MISSING_METADATA", "index": None, "valuation": None, "read_only": True}
        snapshot = ValuationStore(self.valuation_path).get(metadata.index.code) if self.valuation_path else None
        usable = snapshot if snapshot and snapshot.status != "MISSING_VALUATION" else None
        return {"symbol": symbol, "status": snapshot.status if snapshot else "MISSING_VALUATION", "index": metadata.index.to_dict(), "valuation": usable.to_dict() if usable else None, "read_only": True}

    def t_backtest(self) -> dict[str, Any]:
        quotes = JsonQuoteAdapter().load(self.quotes_path)
        if self.history_path is not None:
            store = QuoteHistoryStore(self.history_path)
            quotes = store.merge(quotes)
        watchlist = load_watchlist(self.watchlist_path)
        metadata = self.metadata_store.load()
        items = []
        for item in watchlist:
            if not item.enabled:
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
        quotes = JsonQuoteAdapter().load(self.quotes_path)
        if self.history_path is not None:
            quotes = QuoteHistoryStore(self.history_path).merge(quotes)
        items: list[dict[str, Any]] = []
        for item in load_watchlist(self.watchlist_path):
            if not item.enabled:
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
        with self.watchlist_lock:
            watchlist = load_watchlist(self.watchlist_path)
            if any(current.symbol == normalized_symbol for current in watchlist):
                raise FileExistsError(f"代码已在监控列表中: {normalized_symbol}")
            self._atomic_write_watchlist([*watchlist, item])
        if self.collector is None:
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

    def __init__(self, address: tuple[str, int], application: MonitorApplication):
        super().__init__(address, MonitorRequestHandler)
        self.application = application

    def server_close(self) -> None:
        self.application.stop_refresh()
        super().server_close()


class MonitorRequestHandler(BaseHTTPRequestHandler):
    server: MonitorServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
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
        path = urlsplit(self.path).path
        if path == "/api/watchlist":
            self._add_watch_item()
        else:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {
                "error": "read_only",
                "message": "本服务仅允许写入监控列表，不提供交易接口",
            })

    def log_message(self, format: str, *args: object) -> None:
        return

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
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(error)})
        except OSError as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _snapshot(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.snapshot())
        except (ValueError, OSError) as error:
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
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, payload)

    def _t_backtest(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.t_backtest())
        except (ValueError, OSError) as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _signal_replay(self) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.signal_replay())
        except (ValueError, OSError) as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _backtest(self) -> None:
        try:
            payload = self.server.application.t_backtest()
            payload["deprecated_alias"] = True
            self._json(HTTPStatus.OK, payload)
        except (ValueError, OSError) as error:
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})

    def _valuation(self, symbol: str) -> None:
        try:
            self._json(HTTPStatus.OK, self.server.application.valuation(symbol))
        except (ValueError, OSError) as error:
            self._json(HTTPStatus.OK, {"symbol": symbol, "status": "UNKNOWN", "index": None, "valuation": None, "error": str(error), "read_only": True})

    def _history_dates(self) -> None:
        path = self.server.application.history_path
        try:
            dates = QuoteHistoryStore(path).available_dates() if path is not None else []
        except (ValueError, OSError) as error:
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
            self._json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
            return
        self._json(HTTPStatus.OK, {"items": items, "read_only": True})

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            application = self.server.application
            header = self.headers.get("Last-Event-ID")
            if header is None:
                payload = application.snapshot()
                after_revision = int(payload["revision"])
                self._write_snapshot_event(payload)
            else:
                try:
                    after_revision = int(header)
                except (TypeError, ValueError):
                    payload = application.snapshot()
                    after_revision = int(payload["revision"])
                    self._write_snapshot_event(payload)
            while not application.is_stopping():
                payload = application.wait_for_revision(after_revision, timeout=5.0)
                if payload is None:
                    if application.is_stopping():
                        return
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                after_revision = int(payload["revision"])
                self._write_snapshot_event(payload)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _write_snapshot_event(self, payload: Mapping[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        revision = int(payload["revision"])
        event_name = (
            str(payload["event"])
            if payload.get("event") in {"delta", "reset"}
            else "snapshot"
        )
        self.wfile.write(
            f"id: {revision}\nevent: {event_name}\ndata: {content}\n\n".encode("utf-8"),
        )
        self.wfile.flush()

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
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    quotes_path: Path,
    watchlist_path: Path,
    history_path: Path | None = None,
    collector: Any | None = None,
    refresh_interval: float = 5.0,
    alert_history_path: Path | None = None,
    metadata_path: Path | None = None,
    valuation_path: Path | None = None,
    calendar_path: Path | None = None,
    clock: Callable[[], datetime] | None = None,
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
    server = MonitorServer((host, port), application)
    application.start_refresh()
    return server
