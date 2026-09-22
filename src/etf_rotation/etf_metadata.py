from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date
import json
import math
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


class MetadataError(ValueError):
    pass


def is_valid_index_code(value: object) -> bool:
    """Accept canonical six-character ASCII index IDs, including CSI H30533."""
    return (
        isinstance(value, str)
        and len(value) == 6
        and value.isascii()
        and value.isalnum()
        and value == value.upper()
    )


@dataclass(frozen=True)
class IndexMetadata:
    code: str
    name: str
    provider: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "name": self.name, "provider": self.provider}


@dataclass(frozen=True)
class TradingMetadata:
    exchange: str
    asset_type: str
    intraday_turnaround: bool
    sellable_delay_days: int
    lot_size: int
    price_tick: float
    price_limit_pct: float
    volume_unit_shares: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EtfMetadata:
    symbol: str
    name: str
    index: IndexMetadata
    trading: TradingMetadata
    category: str | None = None
    environment_index: str | None = None
    correlation_group: str | None = None
    fund_size_cny: float | None = None
    avg_amount20_cny: float | None = None
    dividend_dates: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "symbol": self.symbol,
            "name": self.name,
            "index": self.index.to_dict(),
            "trading": self.trading.to_dict(),
        }
        optional = {
            "category": self.category,
            "environment_index": self.environment_index,
            "correlation_group": self.correlation_group,
            "fund_size_cny": self.fund_size_cny,
            "avg_amount20_cny": self.avg_amount20_cny,
            "dividend_dates": list(self.dividend_dates),
        }
        payload.update({key: value for key, value in optional.items() if value not in (None, [])})
        return payload


class EtfMetadataStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[str, EtfMetadata]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise MetadataError(f"ETF元数据读取失败: {error}") from error
        if (
            not isinstance(payload, Mapping)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 2
        ):
            raise MetadataError("ETF元数据schema_version无效")
        records = payload.get("items")
        if not isinstance(records, list):
            raise MetadataError("ETF元数据items必须是数组")
        result: dict[str, EtfMetadata] = {}
        for record in records:
            item = self._parse(record)
            if item.symbol in result:
                raise MetadataError(f"ETF元数据代码重复: {item.symbol}")
            result[item.symbol] = item
        return result

    def get(self, symbol: str) -> EtfMetadata | None:
        return self.load().get(symbol)

    def _parse(self, record: object) -> EtfMetadata:
        if not isinstance(record, Mapping):
            raise MetadataError("ETF元数据项必须是对象")
        symbol = self._code(record.get("symbol"), "ETF")
        name = self._text(record.get("name"), "ETF名称")
        index = record.get("index")
        if not isinstance(index, Mapping):
            raise MetadataError(f"{symbol}的指数元数据无效")
        trading = record.get("trading")
        if not isinstance(trading, Mapping):
            raise MetadataError(f"{symbol}的交易元数据无效")
        exchange = trading.get("exchange")
        if exchange not in {"SSE", "SZSE"}:
            raise MetadataError(f"{symbol}的交易所必须是SSE或SZSE")
        intraday_turnaround = trading.get("intraday_turnaround")
        if type(intraday_turnaround) is not bool:
            raise MetadataError(f"{symbol}的日内回转标记必须是布尔值")
        return EtfMetadata(
            symbol,
            name,
            IndexMetadata(
                self._index_code(index.get("code")),
                self._text(index.get("name"), "指数名称"),
                self._text(index.get("provider"), "指数提供方"),
            ),
            TradingMetadata(
                exchange,
                self._text(trading.get("asset_type"), "资产类型"),
                intraday_turnaround,
                self._nonnegative_int(trading.get("sellable_delay_days"), "可卖延迟天数"),
                self._positive_int(trading.get("lot_size"), "每手股数"),
                self._positive_number(trading.get("price_tick"), "最小价位"),
                self._positive_number(trading.get("price_limit_pct"), "涨跌幅限制"),
                self._positive_int(trading.get("volume_unit_shares"), "成交量单位股数"),
            ),
            self._optional_text(record.get("category"), "标的类别"),
            self._optional_text(record.get("environment_index"), "环境指数"),
            self._optional_text(record.get("correlation_group"), "相关性分组"),
            self._optional_number(record.get("fund_size_cny"), "基金规模"),
            self._optional_number(record.get("avg_amount20_cny"), "20日平均成交额"),
            self._dates(record.get("dividend_dates"), "分红日期"),
        )

    @staticmethod
    def _code(value: object, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 6
            or not value.isascii()
            or not value.isdigit()
        ):
            raise MetadataError(f"{label}代码必须是6位数字")
        return value

    @staticmethod
    def _index_code(value: object) -> str:
        if not is_valid_index_code(value):
            raise MetadataError("指数代码必须是6位ASCII大写字母或数字")
        return value

    @staticmethod
    def _text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise MetadataError(f"{label}不能为空")
        return value.strip()

    @staticmethod
    def _nonnegative_int(value: object, label: str) -> int:
        if type(value) is not int or value < 0:
            raise MetadataError(f"{label}必须是非负整数")
        return value

    @staticmethod
    def _positive_int(value: object, label: str) -> int:
        if type(value) is not int or value <= 0:
            raise MetadataError(f"{label}必须是正整数")
        return value

    @staticmethod
    def _positive_number(value: object, label: str) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise MetadataError(f"{label}必须是正数")
        return float(value)

    @staticmethod
    def _optional_text(value: object, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise MetadataError(f"{label}必须是非空文本")
        return value.strip()

    @staticmethod
    def _optional_number(value: object, label: str) -> float | None:
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise MetadataError(f"{label}必须是非负有限数")
        return float(value)

    @staticmethod
    def _dates(value: object, label: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise MetadataError(f"{label}必须是数组")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise MetadataError(f"{label}必须使用ISO日期")
            try:
                parsed = date.fromisoformat(item)
            except ValueError as error:
                raise MetadataError(f"{label}必须使用ISO日期") from error
            normalized = parsed.isoformat()
            if normalized in result:
                raise MetadataError(f"{label}不能重复")
            result.append(normalized)
        return tuple(result)


_PENDING_ETF_KEYS = frozenset({
    "symbol", "name", "index", "exchange", "asset_type", "intraday_turnaround",
    "sellable_delay_days", "lot_size", "price_tick", "price_limit_pct",
    "volume_unit_shares", "status", "sources",
})
_PENDING_ASSET_TYPES = frozenset({
    "DOMESTIC_EQUITY_ETF", "CROSS_BORDER_EQUITY_ETF", "QDII_EQUITY_ETF",
})


def _pending_etf_record(record: object) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _PENDING_ETF_KEYS:
        raise MetadataError("观察ETF身份字段无效")
    EtfMetadataStore._code(record["symbol"], "ETF")
    EtfMetadataStore._text(record["name"], "ETF名称")
    index = record["index"]
    if not isinstance(index, dict) or set(index) != {"code", "name", "provider"}:
        raise MetadataError("观察ETF指数字段无效")
    EtfMetadataStore._index_code(index["code"])
    EtfMetadataStore._text(index["name"], "指数名称")
    EtfMetadataStore._text(index["provider"], "指数提供方")
    if record["exchange"] not in {"SSE", "SZSE"}:
        raise MetadataError("观察ETF交易所无效")
    if record["asset_type"] not in _PENDING_ASSET_TYPES:
        raise MetadataError("观察ETF资产类型无效")
    turnaround = record["intraday_turnaround"]
    delay = record["sellable_delay_days"]
    if type(turnaround) is not bool or type(delay) is not int:
        raise MetadataError("观察ETF回转字段类型无效")
    if delay != (0 if turnaround else 1):
        raise MetadataError("观察ETF回转与可卖延迟不一致")
    EtfMetadataStore._positive_int(record["lot_size"], "买入申报单位")
    EtfMetadataStore._positive_number(record["price_tick"], "最小价位")
    limit = EtfMetadataStore._positive_number(record["price_limit_pct"], "涨跌幅限制")
    if limit > 1:
        raise MetadataError("观察ETF涨跌幅限制不能大于1")
    if record["volume_unit_shares"] is not None or record["status"] != "OBSERVATION_ONLY":
        raise MetadataError("观察ETF必须保留未知成交量单位与仅观察状态")
    sources = record["sources"]
    if not isinstance(sources, list) or not sources:
        raise MetadataError("观察ETF必须包含公开资料来源")
    for source in sources:
        if not isinstance(source, str) or any(character.isspace() for character in source):
            raise MetadataError("观察ETF资料链接无效")
        parsed = urlsplit(source)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise MetadataError("观察ETF资料链接必须是无凭据的HTTPS地址")
    try:
        json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (UnicodeError, ValueError):
        raise MetadataError("观察ETF身份文本编码无效") from None
    return deepcopy(record)


def load_pending_etfs(path: Path) -> dict[str, dict[str, Any]]:
    """Read observation-only identities, never constructing trading metadata.

    Missing is optional and empty; malformed data fails closed as a whole.
    Callers must not merge these records into an active collector watchlist.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as error:
        raise MetadataError("观察ETF身份目录读取失败") from error
    try:
        payload = json.loads(text)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema_version", "items"}
            or type(payload["schema_version"]) is not int
            or payload["schema_version"] != 1
            or not isinstance(payload["items"], list)
        ):
            raise MetadataError("观察ETF身份目录结构无效")
        result: dict[str, dict[str, Any]] = {}
        for supplied in payload["items"]:
            record = _pending_etf_record(supplied)
            symbol = record["symbol"]
            if symbol in result:
                raise MetadataError("观察ETF身份代码重复")
            result[symbol] = record
        return result
    except MetadataError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise MetadataError("观察ETF身份目录内容无效") from error
