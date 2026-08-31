from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any, Mapping


class MetadataError(ValueError):
    pass


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "index": self.index.to_dict(),
            "trading": self.trading.to_dict(),
        }


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
                self._code(index.get("code"), "指数"),
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
        )

    @staticmethod
    def _code(value: object, label: str) -> str:
        if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
            raise MetadataError(f"{label}代码必须是6位数字")
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
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise MetadataError(f"{label}必须是正数")
        return float(value)
