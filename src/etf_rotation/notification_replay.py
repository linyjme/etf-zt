"""Strictly read-only notification replay over the monitor's local history."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from .market_data import MinuteHistoryStore, SHANGHAI
from .notification_rules import replay_anomalies
from .quote_quality import MinuteQuarantineStore, read_validation_issues
from .t_monitor import MarketDataError


def audited_replay(
    monitor: Any,
    symbol: str,
    trading_date: str,
    threshold_pct: float = 1.0,
    cooldown_minutes: float = 30,
) -> dict[str, Any]:
    """Audit one symbol/day and return observation counts, never repair or send.

    The service remains responsible for held-ETF eligibility. A usable history
    row must pass the existing schema, OHLC/volume, price-limit and previous-close
    checks, as well as this replay's completion and quarantine checks. Gaps stay
    in the input and count as unavailable windows; rows are never sorted or
    deduplicated. Failures raise ValueError (including MarketDataError).
    """
    try:
        if type(trading_date) is not str:
            raise ValueError
        day = date.fromisoformat(trading_date)
        if day.isoformat() != trading_date:
            raise ValueError
    except ValueError:
        raise ValueError('回放日期无效') from None
    if (type(symbol) is not str or len(symbol) != 6
            or not symbol.isascii() or not symbol.isdigit()):
        raise ValueError('回放标的代码无效')

    clock = getattr(monitor, 'clock', lambda: datetime.now(SHANGHAI))
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('回放当前时间必须包含时区')
    now = now.astimezone(SHANGHAI)
    if day > now.date():
        raise ValueError('不能回放未来日期')
    closed_dates = monitor.health_classifier.closed_dates
    if day.weekday() >= 5 or day in closed_dates:
        raise ValueError('回放日期不是交易日或为已配置休市日')

    store = monitor.history_store
    if not isinstance(store, MinuteHistoryStore):
        raise ValueError('缺少可审计的分钟历史')
    metadata = monitor.metadata_store.load()
    if symbol not in metadata:
        raise ValueError(f'缺少交易元数据: {symbol}')

    # Public history reads call transaction recovery and are therefore not
    # read-only. Reuse its strict parser/validators under the same store lock,
    # explicitly refusing a pending transaction instead of recovering it.
    with store._lock:
        if store._journal_path().exists():
            raise MarketDataError('历史事务尚未完成，不能只读回放')
        rows = [row for row in store._read_path(store.path, strict=True)
                if row['symbol'] == symbol and row['trading_date'] == trading_date]
        if not rows:
            raise ValueError('该日没有通过校验的分钟历史')
        store._validate_records(rows, metadata)
        store._validate_previous_closes(rows, metadata)

        previous_time = None
        by_timestamp = {}
        for row in rows:
            timestamp = datetime.fromisoformat(row['timestamp'])
            observed = datetime.fromisoformat(row['observed_at'])
            if timestamp.second or timestamp.microsecond:
                raise MarketDataError('历史分钟时间未对齐分钟起点')
            if timestamp + timedelta(minutes=1) > now or observed > now:
                raise MarketDataError('历史包含未来观测时间或未完成分钟')
            if previous_time is not None and timestamp <= previous_time:
                raise MarketDataError('历史分钟包含重复或顺序异常，不能回放')
            previous_time = timestamp
            by_timestamp[timestamp] = row

        quotes_path = Path(monitor.quotes_path)
        evidence = MinuteQuarantineStore(quotes_path.with_name('quarantine.jsonl')).read()
        try:
            payload = json.loads(quotes_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            payload = {}
        except (OSError, ValueError, UnicodeError):
            raise MarketDataError('行情质量记录读取失败，不能回放') from None
        evidence.extend(read_validation_issues(payload))
        for issue in evidence:
            if issue['symbol'] != symbol or issue['trading_date'] != trading_date:
                continue
            row = by_timestamp.get(datetime.fromisoformat(issue['timestamp']))
            latest_rejection = datetime.fromisoformat(
                issue.get('last_observed_at', issue['observed_at']),
            )
            # A current quote is not enough: replay must use a later *persisted*
            # correction already included in the strict audit above.
            if row is None or datetime.fromisoformat(row['observed_at']) <= latest_rejection:
                raise MarketDataError('存在未解除的分钟质量隔离问题，不能回放')

        points = [{'timestamp': row['timestamp'], 'price': row['price']} for row in rows]

    return replay_anomalies(points, threshold_pct, cooldown_minutes, closed_dates)


__all__ = ['audited_replay']
