from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import gc
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import weakref

from etf_rotation.etf_metadata import EtfMetadata, EtfMetadataStore
from etf_rotation.swing_data import (
    DailyBar,
    DailyHistoryStore,
    DailyBarValidator,
    SwingDataError,
    _SiblingFileLock,
    _safe_product,
)
from tests.swing_helpers import daily_bar_mapping, metadata_fixture


SHANGHAI = timezone(timedelta(hours=8))


class BrokenMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("broken mapping")

    def __iter__(self) -> Iterator[str]:
        yield "schema_version"

    def __len__(self) -> int:
        return 1


class HostileMessageError(OSError):
    def __str__(self) -> str:
        raise RuntimeError("exception stringification must not run")


class OSErrorMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise HostileMessageError()

    def __iter__(self) -> Iterator[str]:
        yield "schema_version"

    def __len__(self) -> int:
        return 1


class OSErrorMetadataMapping(Mapping[str, EtfMetadata]):
    def __getitem__(self, key: str) -> EtfMetadata:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0

    def get(self, key: str, default: object = None) -> EtfMetadata | None:
        raise HostileMessageError()


class HostileKey:
    def __repr__(self) -> str:
        raise OSError("repr unavailable")


class HostileStr(str):
    def __len__(self) -> int:
        raise HostileMessageError()

    def strip(self, chars: str | None = None) -> str:
        raise HostileMessageError()

    def isascii(self) -> bool:
        raise HostileMessageError()

    def isdigit(self) -> bool:
        raise HostileMessageError()


class HostileFloat(float):
    def __float__(self) -> float:
        raise HostileMessageError()


class HostileInt(int):
    def __float__(self) -> float:
        raise HostileMessageError()


class HostileDate(date):
    def isoformat(self) -> str:
        raise HostileMessageError()


class HostileSwingDataDate(date):
    def isoformat(self) -> str:
        raise SwingDataError("UNTRUSTED_TEMPORAL_MESSAGE")


class FatalDate(date):
    def isoformat(self) -> str:
        raise KeyboardInterrupt()


class HostileDateTime(datetime):
    def utcoffset(self) -> timedelta | None:
        raise HostileMessageError()

    def isoformat(self, sep: str = "T", timespec: str = "auto") -> str:
        raise HostileMessageError()


class NonStringDate(date):
    def isoformat(self) -> object:
        return object()


class NonStringDateTime(datetime):
    def isoformat(self, sep: str = "T", timespec: str = "auto") -> object:
        return {"not": "an ISO string"}


class HostileMultiplier:
    def __mul__(self, other: object) -> object:
        raise HostileMessageError()


class DailyBarTests(unittest.TestCase):
    def test_parses_valid_final_bar_and_is_frozen(self) -> None:
        bar = DailyBar.from_mapping(daily_bar_mapping())

        self.assertEqual(bar.symbol, "510300")
        self.assertEqual(bar.trading_date, date(2026, 8, 28))
        self.assertEqual(bar.observed_at.utcoffset(), timedelta(hours=8))
        with self.assertRaises(FrozenInstanceError):
            bar.close = 11.0  # type: ignore[misc]

    def test_rejects_incomplete_bar(self) -> None:
        payload = daily_bar_mapping()
        payload["is_final"] = False
        with self.assertRaisesRegex(SwingDataError, "is_final"):
            DailyBar.from_mapping(payload)

    def test_rejects_naive_and_too_early_observation(self) -> None:
        for observed_at in (
            "2026-08-28T15:10:00",
            "2026-08-28T07:09:59+00:00",
        ):
            with self.subTest(observed_at=observed_at), self.assertRaisesRegex(
                SwingDataError, "观测时间|时区|15:10",
            ):
                DailyBar.from_mapping(daily_bar_mapping(observed_at=observed_at))

        later_backfill = daily_bar_mapping(
            observed_at="2026-09-01T09:00:00+08:00",
        )
        self.assertEqual(
            DailyBar.from_mapping(later_backfill).observed_at.date(),
            date(2026, 9, 1),
        )

    def test_rejects_invalid_iso_date_and_time(self) -> None:
        invalid = (
            ("trading_date", "20260828"),
            ("trading_date", "2026-02-30"),
            ("observed_at", "not-a-time"),
        )
        for field, value in invalid:
            payload = daily_bar_mapping()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SwingDataError):
                DailyBar.from_mapping(payload)

    def test_requires_exact_keys_and_wraps_broken_mappings(self) -> None:
        payload = daily_bar_mapping()
        payload.pop("amount")
        with self.assertRaisesRegex(SwingDataError, "字段"):
            DailyBar.from_mapping(payload)
        payload = daily_bar_mapping()
        payload["extra"] = 1
        with self.assertRaisesRegex(SwingDataError, "字段"):
            DailyBar.from_mapping(payload)
        with self.assertRaisesRegex(SwingDataError, "映射"):
            DailyBar.from_mapping(BrokenMapping())

        payload = daily_bar_mapping()
        payload[None] = 1  # type: ignore[index]
        payload[1] = 2  # type: ignore[index]
        with self.assertRaisesRegex(SwingDataError, "字段"):
            DailyBar.from_mapping(payload)

    def test_wraps_oserror_mapping_and_avoids_hostile_key_repr(self) -> None:
        with self.assertRaises(SwingDataError) as raised:
            DailyBar.from_mapping(OSErrorMapping())
        self.assertIsInstance(raised.exception.__cause__, OSError)

        payload = daily_bar_mapping()
        payload[HostileKey()] = 1  # type: ignore[index]
        with self.assertRaisesRegex(SwingDataError, "字段名"):
            DailyBar.from_mapping(payload)

    def test_rejects_raw_and_adjusted_ohlc_shape_violations(self) -> None:
        payload = daily_bar_mapping()
        payload["low"] = 10.01
        with self.assertRaisesRegex(SwingDataError, "raw OHLC"):
            DailyBar.from_mapping(payload)

        payload = daily_bar_mapping()
        payload["adjusted_high"] = float(payload["adjusted_close"]) - 0.01
        with self.assertRaisesRegex(SwingDataError, "adjusted OHLC"):
            DailyBar.from_mapping(payload)

    def test_rejects_bad_numeric_fields_including_bool(self) -> None:
        cases = (
            ("open", 0.0),
            ("high", math.inf),
            ("low", math.nan),
            ("close", True),
            ("previous_close", -1.0),
            ("adjusted_open", False),
            ("adjusted_close", 0.0),
            ("volume", -1.0),
            ("volume", math.inf),
            ("amount", True),
            ("amount", math.nan),
        )
        for field, value in cases:
            payload = daily_bar_mapping()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SwingDataError):
                DailyBar.from_mapping(payload)

    def test_rejects_invalid_schema_symbol_source_and_boolean_types(self) -> None:
        cases = (
            ("schema_version", True),
            ("schema_version", 2),
            ("symbol", "５１０３００"),
            ("symbol", "51030"),
            ("source", "  "),
            ("source", 1),
            ("is_final", 1),
        )
        for field, value in cases:
            payload = daily_bar_mapping()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(SwingDataError):
                DailyBar.from_mapping(payload)

    def test_rejects_string_subclasses_without_invoking_their_methods(self) -> None:
        for field, value in (
            ("symbol", HostileStr("510300")),
            ("trading_date", HostileStr("2026-08-28")),
            ("observed_at", HostileStr("2026-08-28T15:10:00+08:00")),
            ("source", HostileStr("TEST_DAILY")),
        ):
            payload = daily_bar_mapping()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(SwingDataError):
                DailyBar.from_mapping(payload)

    def test_rejects_numeric_subclasses_without_invoking_their_methods(self) -> None:
        for field, value in (
            ("open", HostileFloat(10.0)),
            ("volume", HostileInt(1_000)),
        ):
            payload = daily_bar_mapping()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(SwingDataError):
                DailyBar.from_mapping(payload)

    def test_rejects_inconsistent_adjustment_scale(self) -> None:
        payload = daily_bar_mapping(adjustment_scale=1.2345)
        payload["adjusted_close"] = float(payload["adjusted_close"]) + 0.01
        with self.assertRaisesRegex(SwingDataError, "复权比例"):
            DailyBar.from_mapping(payload)

    def test_rejects_nonfinite_or_zero_derived_adjustment_scale(self) -> None:
        for raw, adjusted in ((1e-308, 1e308), (1e308, 1e-308)):
            payload = daily_bar_mapping(adjustment_scale=1.0)
            for field in ("open", "high", "low", "close"):
                payload[field] = raw
                payload[f"adjusted_{field}"] = adjusted
            with self.subTest(raw=raw, adjusted=adjusted), self.assertRaisesRegex(
                SwingDataError, "复权比例",
            ):
                DailyBar.from_mapping(payload)

    def test_rejects_large_relative_mismatch_at_tiny_adjustment_scale(self) -> None:
        payload = daily_bar_mapping(adjustment_scale=1e-10)
        payload["adjusted_low"] = float(payload["low"]) * 1e-20
        with self.assertRaisesRegex(SwingDataError, "复权比例"):
            DailyBar.from_mapping(payload)

    def test_to_dict_round_trip_uses_json_safe_primitives(self) -> None:
        original = DailyBar.from_mapping(daily_bar_mapping())
        payload = original.to_dict()

        self.assertEqual(payload["trading_date"], "2026-08-28")
        self.assertEqual(payload["observed_at"], "2026-08-28T15:10:00+08:00")
        self.assertEqual(DailyBar.from_mapping(payload), original)

    def test_normalizes_utc_observation_to_canonical_shanghai_time(self) -> None:
        bar = DailyBar.from_mapping(daily_bar_mapping(
            observed_at="2026-08-28T07:10:00+00:00",
        ))
        self.assertEqual(bar.observed_at.isoformat(), "2026-08-28T15:10:00+08:00")
        self.assertEqual(bar.to_dict()["observed_at"], "2026-08-28T15:10:00+08:00")

        direct_utc = replace(
            DailyBar.from_mapping(daily_bar_mapping()),
            observed_at=datetime(2026, 8, 28, 7, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(
            direct_utc.to_dict()["observed_at"],
            "2026-08-28T15:10:00+08:00",
        )

    def test_to_dict_wraps_serialization_errors_but_not_base_exceptions(self) -> None:
        valid = DailyBar.from_mapping(daily_bar_mapping())
        for malformed_scalar in (
            replace(valid, symbol=HostileStr("510300")),
            replace(valid, open=HostileFloat(10.0)),
        ):
            with self.assertRaises(SwingDataError):
                malformed_scalar.to_dict()

        malformed = (
            replace(valid, trading_date=object()),  # type: ignore[arg-type]
            replace(valid, observed_at=object()),  # type: ignore[arg-type]
            replace(valid, trading_date=HostileDate(2026, 8, 28)),
            replace(
                valid,
                observed_at=HostileDateTime(
                    2026, 8, 28, 15, 10, tzinfo=timezone.utc,
                ),
            ),
        )
        for item in malformed:
            with self.assertRaises(SwingDataError) as raised:
                item.to_dict()
            self.assertIsInstance(raised.exception.__cause__, Exception)

        with self.assertRaises(KeyboardInterrupt):
            replace(valid, trading_date=FatalDate(2026, 8, 28)).to_dict()

    def test_to_dict_wraps_untrusted_swing_data_error_from_temporal_method(self) -> None:
        valid = DailyBar.from_mapping(daily_bar_mapping())
        malformed = replace(
            valid,
            trading_date=HostileSwingDataDate(2026, 8, 28),
        )

        with self.assertRaises(SwingDataError) as raised:
            malformed.to_dict()

        self.assertEqual(str(raised.exception), "日线记录序列化失败")
        self.assertIsInstance(raised.exception.__cause__, SwingDataError)
        self.assertEqual(str(raised.exception.__cause__), "UNTRUSTED_TEMPORAL_MESSAGE")

    def test_to_dict_rejects_non_string_temporal_serialization_results(self) -> None:
        valid = DailyBar.from_mapping(daily_bar_mapping())
        malformed = (
            replace(valid, trading_date=NonStringDate(2026, 8, 28)),
            replace(
                valid,
                observed_at=NonStringDateTime(
                    2026, 8, 28, 15, 10, tzinfo=timezone.utc,
                ),
            ),
        )
        for item in malformed:
            with self.assertRaises(SwingDataError):
                item.to_dict()

    def test_to_dict_rejects_nonfinite_direct_numeric_fields(self) -> None:
        valid = DailyBar.from_mapping(daily_bar_mapping())
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value), self.assertRaises(SwingDataError):
                replace(valid, amount=value).to_dict()

    def test_arithmetic_wrapper_does_not_stringify_hostile_exception(self) -> None:
        with self.assertRaises(SwingDataError) as raised:
            _safe_product(HostileMultiplier(), 1.0, "测试运算")
        self.assertIsInstance(raised.exception.__cause__, HostileMessageError)


class DailyBarValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        metadata_path = Path(self.temporary.name) / "metadata.json"
        import json
        metadata_path.write_text(
            json.dumps(metadata_fixture(("510300", "510500"))),
            encoding="utf-8",
        )
        self.metadata = EtfMetadataStore(metadata_path).load()
        self.validator = DailyBarValidator(set())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bar(self, **changes: object) -> DailyBar:
        payload = daily_bar_mapping()
        payload.update(changes)
        for field in ("open", "high", "low", "close"):
            if field in changes and f"adjusted_{field}" not in changes:
                payload[f"adjusted_{field}"] = float(payload[field]) * 1.1
        return DailyBar.from_mapping(payload)

    def metadata_for(self, symbol: str = "510300") -> EtfMetadata:
        return self.metadata[symbol]

    def metadata_with(self, **trading_changes: object) -> EtfMetadata:
        metadata = self.metadata_for()
        return replace(
            metadata,
            trading=replace(metadata.trading, **trading_changes),
        )

    def test_accepts_valid_bar_and_all_price_limit_boundaries(self) -> None:
        metadata = self.metadata_for()
        for field, value in (
            ("open", 12.001),
            ("high", 12.001),
            ("low", 7.999),
            ("close", 12.001),
        ):
            payload = daily_bar_mapping(
                open_price=10.0,
                high=12.001,
                low=7.999,
                close=10.0,
                adjustment_scale=1.0,
            )
            payload[field] = value
            payload[f"adjusted_{field}"] = value
            self.validator.validate(DailyBar.from_mapping(payload), metadata)

    def test_rejects_beyond_one_tick_on_any_raw_ohlc(self) -> None:
        metadata = self.metadata_for()
        for field, value in (
            ("open", 12.00101),
            ("high", 12.00101),
            ("low", 7.99899),
            ("close", 12.00101),
        ):
            payload = daily_bar_mapping(
                open_price=10.0,
                high=12.00101 if value > 10 else 12.0,
                low=7.99899 if value < 10 else 8.0,
                close=10.0,
                adjustment_scale=1.0,
            )
            payload[field] = value
            payload[f"adjusted_{field}"] = value
            with self.subTest(field=field), self.assertRaisesRegex(SwingDataError, "涨跌幅"):
                self.validator.validate(DailyBar.from_mapping(payload), metadata)

    def test_price_limit_uses_one_tick_plus_only_local_ulps(self) -> None:
        metadata = self.metadata_for()
        boundary = 12.001
        exact = daily_bar_mapping(
            open_price=boundary,
            high=boundary,
            low=boundary,
            close=boundary,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        )
        self.validator.validate(DailyBar.from_mapping(exact), metadata)

        beyond = math.nextafter(boundary, math.inf)
        for _ in range(32):
            beyond = math.nextafter(beyond, math.inf)
        payload = daily_bar_mapping(
            open_price=beyond,
            high=beyond,
            low=beyond,
            close=beyond,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        )
        with self.assertRaisesRegex(SwingDataError, "涨跌幅"):
            self.validator.validate(DailyBar.from_mapping(payload), metadata)

    def test_tiny_prices_cannot_use_fixed_epsilon_for_many_tick_violations(self) -> None:
        metadata = self.metadata_with(price_tick=1e-15)
        previous = 1e-9
        beyond_limit = previous * 1.2 + 100e-15
        limit_payload = daily_bar_mapping(
            previous_close=previous,
            open_price=beyond_limit,
            high=beyond_limit,
            low=beyond_limit,
            close=beyond_limit,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        )
        with self.assertRaisesRegex(SwingDataError, "涨跌幅"):
            self.validator.validate(DailyBar.from_mapping(limit_payload), metadata)

        price = 1e-9
        volume = 1_000_000_000.0
        amount_payload = daily_bar_mapping(
            previous_close=price,
            open_price=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            amount=(price + 100e-15) * volume * 100.0,
            adjustment_scale=1.0,
        )
        with self.assertRaisesRegex(SwingDataError, "量价"):
            self.validator.validate(DailyBar.from_mapping(amount_payload), metadata)

    def test_rejects_unrepresentable_tick_and_nonfinite_price_limit_products(self) -> None:
        unrepresentable = self.metadata_with(price_tick=1e-10)
        huge_payload = daily_bar_mapping(
            previous_close=1e308,
            open_price=1e308,
            high=1e308,
            low=1e308,
            close=1e308,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        )
        with self.assertRaisesRegex(SwingDataError, "最小价位|精度"):
            self.validator.validate(DailyBar.from_mapping(huge_payload), unrepresentable)

        overflowing = self.metadata_with(price_tick=1e293)
        overflow_payload = daily_bar_mapping(
            previous_close=1.7e308,
            open_price=1.7e308,
            high=1.7e308,
            low=1.7e308,
            close=1.7e308,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        )
        with self.assertRaisesRegex(SwingDataError, "有限|溢出"):
            self.validator.validate(DailyBar.from_mapping(overflow_payload), overflowing)

    def test_rejects_zero_mismatch_and_impossible_amount_price_relation(self) -> None:
        metadata = self.metadata_for()
        for volume, amount in ((0.0, 1.0), (1.0, 0.0)):
            with self.subTest(volume=volume), self.assertRaisesRegex(SwingDataError, "同时"):
                self.validator.validate(self.bar(volume=volume, amount=amount), metadata)

        self.validator.validate(self.bar(volume=0.0, amount=0.0), metadata)
        with self.assertRaisesRegex(SwingDataError, "量价"):
            self.validator.validate(self.bar(amount=500_000.0), metadata)

    def test_wraps_huge_volume_unit_arithmetic_overflow(self) -> None:
        metadata = self.metadata_with(volume_unit_shares=10**400)
        with self.assertRaises(SwingDataError) as raised:
            self.validator.validate(self.bar(), metadata)
        self.assertIsNotNone(raised.exception.__cause__)

    def test_rejects_closed_date_and_weekend(self) -> None:
        closed = date(2026, 8, 28)
        with self.assertRaisesRegex(SwingDataError, "休市|交易日"):
            DailyBarValidator({closed}).validate(self.bar(), self.metadata_for())
        weekend = self.bar(
            trading_date="2026-08-29",
            observed_at="2026-08-29T15:10:00+08:00",
        )
        with self.assertRaisesRegex(SwingDataError, "周末|交易日"):
            self.validator.validate(weekend, self.metadata_for())

    def test_validate_defensively_rechecks_directly_constructed_records(self) -> None:
        valid = self.bar()
        for invalid in (
            replace(valid, is_final=False),
            replace(valid, schema_version=2),
            replace(valid, observed_at=datetime(2026, 8, 28, 15, 9, tzinfo=SHANGHAI)),
            replace(valid, high=9.0),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SwingDataError):
                self.validator.validate(invalid, self.metadata_for())

    def test_validate_wraps_invalid_direct_date_time_types(self) -> None:
        valid = self.bar()
        for invalid in (
            replace(valid, trading_date="2026-08-28"),  # type: ignore[arg-type]
            replace(valid, observed_at="2026-08-28T15:10:00+08:00"),  # type: ignore[arg-type]
        ):
            with self.subTest(invalid=invalid), self.assertRaises(SwingDataError):
                self.validator.validate(invalid, self.metadata_for())

    def test_validate_rejects_direct_scalar_subclasses_before_methods_run(self) -> None:
        valid = self.bar()
        malformed = (
            ("schema", replace(valid, schema_version=HostileInt(1))),
            ("symbol", replace(valid, symbol=HostileStr("510300"))),
            ("source", replace(valid, source=HostileStr("TEST_DAILY"))),
            ("date", replace(valid, trading_date=HostileDate(2026, 8, 28))),
            ("datetime", replace(
                valid,
                observed_at=HostileDateTime(
                    2026, 8, 28, 15, 10, tzinfo=timezone.utc,
                ),
            )),
            ("number", replace(valid, open=HostileFloat(10.0))),
        )
        for label, item in malformed:
            with self.subTest(label=label), self.assertRaises(SwingDataError) as raised:
                self.validator.validate(item, self.metadata_for())
            self.assertIsNone(raised.exception.__cause__)

    def test_rejects_unsorted_duplicate_and_missing_expected_weekday(self) -> None:
        monday = self.bar(
            trading_date="2026-08-24",
            observed_at="2026-08-24T15:10:00+08:00",
            close=10.0,
        )
        tuesday = self.bar(
            trading_date="2026-08-25",
            observed_at="2026-08-25T15:10:00+08:00",
            previous_close=10.0,
        )
        wednesday = self.bar(
            trading_date="2026-08-26",
            observed_at="2026-08-26T15:10:00+08:00",
            previous_close=10.0,
        )
        metadata = {"510300": self.metadata_for()}
        with self.assertRaisesRegex(SwingDataError, "排序"):
            self.validator.validate_sequence((tuesday, monday), metadata)
        with self.assertRaisesRegex(SwingDataError, "重复"):
            self.validator.validate_sequence((monday, monday), metadata)
        with self.assertRaisesRegex(SwingDataError, "缺少交易日"):
            self.validator.validate_sequence((monday, wednesday), metadata)

    def test_sequence_skips_weekends_and_configured_closures(self) -> None:
        friday = self.bar(
            trading_date="2026-08-28",
            observed_at="2026-08-28T15:10:00+08:00",
            close=10.05,
        )
        tuesday = self.bar(
            trading_date="2026-09-01",
            observed_at="2026-09-01T15:10:00+08:00",
            previous_close=10.05,
        )
        metadata = {"510300": self.metadata_for()}
        DailyBarValidator({date(2026, 8, 31)}).validate_sequence(
            (friday, tuesday), metadata,
        )

    def test_tiny_tick_previous_close_continuity_rejects_many_ticks(self) -> None:
        metadata = self.metadata_with(price_tick=1e-15)
        first = DailyBar.from_mapping(daily_bar_mapping(
            trading_date="2026-08-27",
            observed_at="2026-08-27T15:10:00+08:00",
            previous_close=1e-9,
            open_price=1e-9,
            high=1e-9,
            low=1e-9,
            close=1e-9,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        ))
        second = DailyBar.from_mapping(daily_bar_mapping(
            previous_close=1e-9 + 100e-15,
            open_price=1e-9,
            high=1e-9,
            low=1e-9,
            close=1e-9,
            volume=0.0,
            amount=0.0,
            adjustment_scale=1.0,
        ))
        with self.assertRaisesRegex(SwingDataError, "昨收"):
            self.validator.validate_sequence(
                (first, second), {"510300": metadata},
            )

    def test_previous_close_continuity_allows_one_tick_only(self) -> None:
        first = self.bar(
            trading_date="2026-08-27",
            observed_at="2026-08-27T15:10:00+08:00",
            close=10.05,
        )
        at_tick = self.bar(previous_close=10.051)
        beyond = self.bar(previous_close=10.05101)
        metadata = {"510300": self.metadata_for()}
        self.validator.validate_sequence((first, at_tick), metadata)
        with self.assertRaisesRegex(SwingDataError, "昨收"):
            self.validator.validate_sequence((first, beyond), metadata)

    def test_rejects_unknown_metadata_and_accepts_ordered_symbol_groups(self) -> None:
        first_symbol = self.bar()
        second_symbol = DailyBar.from_mapping(daily_bar_mapping(symbol="510500"))
        with self.assertRaisesRegex(SwingDataError, "元数据"):
            self.validator.validate_sequence((first_symbol,), {})

        self.validator.validate_sequence(
            (first_symbol, second_symbol), self.metadata,
        )
        with self.assertRaisesRegex(SwingDataError, "排序"):
            self.validator.validate_sequence(
                (second_symbol, first_symbol), self.metadata,
            )

    def test_wraps_oserror_from_metadata_mapping_get(self) -> None:
        with self.assertRaises(SwingDataError) as raised:
            self.validator.validate_sequence((self.bar(),), OSErrorMetadataMapping())
        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_sequence_wraps_malformed_direct_fields_before_ordering(self) -> None:
        valid = self.bar()
        malformed = replace(valid, symbol=1)  # type: ignore[arg-type]
        with self.assertRaises(SwingDataError):
            self.validator.validate_sequence(
                (valid, malformed), {"510300": self.metadata_for()},
            )

    def test_wraps_oserror_from_closed_dates_iterable(self) -> None:
        def broken_dates() -> Iterator[date]:
            raise HostileMessageError()
            yield date(2026, 8, 28)

        with self.assertRaises(SwingDataError) as raised:
            DailyBarValidator(broken_dates())
        self.assertIsInstance(raised.exception.__cause__, OSError)


class DailyHistoryStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.metadata_path = self.root / "metadata.json"
        self.metadata_path.write_text(
            json.dumps(metadata_fixture(("510300", "510500"))),
            encoding="utf-8",
        )
        self.metadata = EtfMetadataStore(self.metadata_path).load()
        self.path = self.root / "daily.jsonl"
        self.store = DailyHistoryStore(self.path, self.metadata, ())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def bar(
        self,
        symbol: str = "510300",
        trading_date: str = "2026-08-28",
        observed_at: str | None = None,
        **changes: object,
    ) -> DailyBar:
        observed = observed_at or f"{trading_date}T15:10:00+08:00"
        payload = daily_bar_mapping(
            symbol=symbol,
            trading_date=trading_date,
            observed_at=observed,
        )
        payload.update(changes)
        for field in ("open", "high", "low", "close"):
            if field in changes and f"adjusted_{field}" not in changes:
                payload[f"adjusted_{field}"] = float(payload[field]) * 1.1
        return DailyBar.from_mapping(payload)

    def temp_artifacts(self) -> list[Path]:
        return list(self.root.glob(f".{self.path.name}.*.tmp"))

    def assert_history_rejected_without_rewrite(self, content: bytes) -> None:
        self.path.write_bytes(content)
        with self.assertRaises(SwingDataError):
            self.store.load()
        with self.assertRaises(SwingDataError):
            self.store.upsert((self.bar(source="NEW"),))
        self.assertEqual(self.path.read_bytes(), content)

    def test_absent_empty_and_query_validation(self) -> None:
        self.assertEqual(self.store.load(), ())
        self.assertEqual(self.store.query("510300"), ())
        self.assertEqual(self.store.upsert(()), ())
        self.assertFalse(self.path.exists())

        self.path.touch()
        self.assertEqual(self.store.load(), ())
        before = self.path.read_bytes()
        self.assertEqual(self.store.upsert(()), ())
        self.assertEqual(self.path.read_bytes(), before)
        for invalid in ("", "51030", "510300 ", "５１０３００", 510300):
            with self.subTest(symbol=invalid), self.assertRaises(SwingDataError):
                self.store.query(invalid)  # type: ignore[arg-type]

    def test_upsert_uses_newest_observation_and_equal_time_later_wins(self) -> None:
        original = self.bar(source="FIRST")
        newer = self.bar(
            observed_at="2026-08-28T15:12:00+08:00", source="NEWER",
        )
        older = self.bar(
            observed_at="2026-08-28T15:11:00+08:00", source="OLDER",
        )
        equal_later = self.bar(
            observed_at="2026-08-28T15:12:00+08:00", source="EQUAL_LATER",
        )

        self.assertEqual(self.store.upsert((original, newer, older, equal_later)), (equal_later,))
        self.assertEqual(self.store.upsert((older,)), (equal_later,))

    def test_primary_keys_are_unique_and_output_is_canonically_ordered(self) -> None:
        records = (
            self.bar("510500", "2026-08-28"),
            self.bar("510300", "2026-08-28"),
            self.bar("510300", "2026-08-27", close=10.0),
        )
        result = self.store.upsert(records)

        self.assertEqual(
            [(item.symbol, item.trading_date.isoformat()) for item in result],
            [
                ("510300", "2026-08-27"),
                ("510300", "2026-08-28"),
                ("510500", "2026-08-28"),
            ],
        )
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        self.assertEqual(
            lines[0],
            json.dumps(result[0].to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        )

    def test_invalid_mixed_batch_makes_zero_byte_changes(self) -> None:
        self.store.upsert((self.bar(),))
        before = self.path.read_bytes()
        invalid = self.bar(symbol="999999")

        with self.assertRaises(SwingDataError):
            self.store.upsert((self.bar(source="REPLACEMENT"), invalid))

        self.assertEqual(self.path.read_bytes(), before)

    def test_direct_valid_integer_fields_are_retained_in_normalized_form(self) -> None:
        valid = self.bar()
        direct = replace(
            valid,
            open=10,
            high=10,
            low=10,
            close=10,
            previous_close=10,
            volume=0,
            amount=0,
            adjusted_open=11,
            adjusted_high=11,
            adjusted_low=11,
            adjusted_close=11,
        )

        result = self.store.upsert((direct,))

        self.assertIs(type(result[0].open), float)
        self.assertIs(type(result[0].volume), float)
        self.assertEqual(self.store.load(), result)

    def test_lone_surrogate_source_is_rejected_before_persistence(self) -> None:
        original = self.store.upsert((self.bar(source="ORIGINAL"),))
        before = self.path.read_bytes()
        invalid = self.bar(
            observed_at="2026-08-28T15:11:00+08:00",
            source="BROKEN\ud800SOURCE",
        )

        with self.assertRaises(SwingDataError):
            self.store.upsert((invalid,))

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.temp_artifacts(), [])
        self.assertEqual(self.store.load(), original)

    def test_corrupt_or_noncanonical_existing_history_is_never_overwritten(self) -> None:
        bad_histories = (
            b"{not json}\n",
            b"\xff\n",
            b"\n",
            b"[]\n",
            json.dumps(self.bar().to_dict()).encode() + b"\n" + json.dumps(self.bar().to_dict()).encode() + b"\n",
        )
        for content in bad_histories:
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(SwingDataError):
                    self.store.upsert((self.bar(source="NEW"),))
                self.assertEqual(self.path.read_bytes(), content)

    def test_deeply_nested_json_recursion_is_wrapped_without_rewriting(self) -> None:
        nested = b'{"nested":' + (b"[" * 10_000) + b"0" + (b"]" * 10_000) + b"}\n"
        self.assert_history_rejected_without_rewrite(nested)

    def test_json_integer_digit_limit_value_error_is_wrapped_without_rewriting(self) -> None:
        oversized_integer = b'{"schema_version":' + (b"9" * 10_001) + b"}\n"
        self.path.write_bytes(oversized_integer)

        for operation in (self.store.load, lambda: self.store.upsert((self.bar(),))):
            with self.assertRaises(SwingDataError) as raised:
                operation()
            self.assertIn("JSON", str(raised.exception))
            self.assertIs(type(raised.exception.__cause__), ValueError)
            self.assertEqual(self.path.read_bytes(), oversized_integer)

    def test_round_trip_load_validates_every_line(self) -> None:
        records = self.store.upsert((
            self.bar("510300", "2026-08-27", close=10.0),
            self.bar("510300", "2026-08-28"),
        ))
        self.assertEqual(self.store.load(), records)
        self.assertEqual(self.store.query("510300"), records)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        malformed = json.loads(lines[1])
        malformed["is_final"] = False
        self.path.write_text(lines[0] + "\n" + json.dumps(malformed) + "\n", encoding="utf-8")
        with self.assertRaises(SwingDataError):
            self.store.load()

    def test_round_trip_accepts_unicode_line_separator_inside_source(self) -> None:
        record = self.bar(source="FEED\u2028SECONDARY\u2029SOURCE")

        self.assertEqual(self.store.upsert((record,)), (record,))
        self.assertEqual(self.store.load(), (record,))

    def test_load_rejects_spaces_and_unsorted_key_order_without_rewriting(self) -> None:
        payload = self.bar().to_dict()
        noncanonical = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.assert_history_rejected_without_rewrite(noncanonical)

    def test_load_rejects_crlf_without_rewriting(self) -> None:
        canonical = json.dumps(
            self.bar().to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.assert_history_rejected_without_rewrite((canonical + "\r\n").encode("utf-8"))

    def test_load_rejects_duplicate_json_key_without_rewriting(self) -> None:
        canonical = json.dumps(
            self.bar().to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        duplicate = '{"source":"DUPLICATE",' + canonical[1:] + "\n"
        self.assert_history_rejected_without_rewrite(duplicate.encode("utf-8"))

    def test_load_rejects_alternate_numeric_spelling_without_rewriting(self) -> None:
        record = self.bar(
            previous_close=4.6,
            open=4.6,
            high=4.7,
            low=4.5,
            close=4.6,
            amount=460_000.0,
        )
        canonical = json.dumps(
            record.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        alternate = canonical.replace('"close":4.6', '"close":4.6000', 1) + "\n"
        self.assertNotEqual(alternate, canonical + "\n")
        self.assert_history_rejected_without_rewrite(alternate.encode("utf-8"))

    def test_process_lock_registry_releases_unused_entries(self) -> None:
        lock = _SiblingFileLock(self.root / "lifecycle.jsonl", shared=True)
        process_lock = weakref.ref(lock._process_lock)

        del lock
        gc.collect()

        self.assertIsNone(process_lock())

    def test_replace_and_fsync_failures_preserve_history_and_clean_temp(self) -> None:
        original = self.store.upsert((self.bar(source="ORIGINAL"),))
        before = self.path.read_bytes()
        replacement = self.bar(source="REPLACEMENT")
        for target, error in (
            ("etf_rotation.swing_data.os.replace", OSError("replace failed")),
            ("etf_rotation.swing_data.os.fsync", OSError("fsync failed")),
        ):
            with self.subTest(target=target), patch(target, side_effect=error):
                with self.assertRaisesRegex(OSError, str(error)):
                    self.store.upsert((replacement,))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.temp_artifacts(), [])
            self.assertEqual(self.store.load(), original)

    @unittest.skipUnless(os.name == "nt", "Windows replace sharing semantics")
    def test_transient_windows_replace_sharing_error_is_retried(self) -> None:
        self.store.upsert((self.bar(source="ORIGINAL"),))
        replacement = self.bar(
            observed_at="2026-08-28T15:11:00+08:00",
            source="REPLACEMENT",
        )
        real_replace = os.replace
        attempts = 0

        def transient_replace(source: object, destination: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                error = PermissionError(13, "transient sharing denial")
                error.winerror = 5  # type: ignore[attr-defined]
                raise error
            real_replace(source, destination)

        with patch("etf_rotation.swing_data.os.replace", side_effect=transient_replace):
            result = self.store.upsert((replacement,))

        self.assertEqual(result, (replacement,))
        self.assertEqual(self.store.load(), (replacement,))
        self.assertEqual(attempts, 2)

    def test_write_flush_and_close_failures_preserve_history_and_clean_temp(self) -> None:
        self.store.upsert((self.bar(source="ORIGINAL"),))
        before = self.path.read_bytes()
        real_named_temporary = tempfile.NamedTemporaryFile

        for failed_method in ("write", "flush", "close"):
            class FaultyTemporary:
                def __init__(inner_self, *args: object, **kwargs: object) -> None:
                    inner_self.handle = real_named_temporary(*args, **kwargs)

                def __enter__(inner_self) -> object:
                    handle = inner_self.handle
                    if failed_method == "close":
                        return handle

                    class Wrapper:
                        name = handle.name

                        def write(self, value: str) -> int:
                            if failed_method == "write":
                                raise OSError("write failed")
                            return handle.write(value)

                        def flush(self) -> None:
                            if failed_method == "flush":
                                raise OSError("flush failed")
                            handle.flush()

                        def fileno(self) -> int:
                            return handle.fileno()

                    return Wrapper()

                def __exit__(inner_self, exc_type: object, exc: object, traceback: object) -> None:
                    inner_self.handle.close()
                    if failed_method == "close" and exc is None:
                        raise OSError("close failed")

            with self.subTest(failed_method=failed_method), patch(
                "etf_rotation.swing_data.tempfile.NamedTemporaryFile",
                FaultyTemporary,
            ):
                with self.assertRaisesRegex(OSError, f"{failed_method} failed"):
                    self.store.upsert((self.bar(source="REPLACEMENT"),))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(self.temp_artifacts(), [])

    def test_cleanup_failure_does_not_mask_replace_error_or_target_canonical(self) -> None:
        self.store.upsert((self.bar(source="ORIGINAL"),))
        before = self.path.read_bytes()
        unlinked: list[Path] = []
        real_unlink = Path.unlink

        def fail_cleanup(path: Path, *args: object, **kwargs: object) -> None:
            unlinked.append(path)
            if path == self.path:
                raise AssertionError("canonical history must never be cleanup target")
            raise OSError("cleanup failed")

        with (
            patch("etf_rotation.swing_data.os.replace", side_effect=OSError("replace failed")),
            patch("etf_rotation.swing_data.Path.unlink", new=fail_cleanup),
        ):
            with self.assertRaisesRegex(OSError, "replace failed"):
                self.store.upsert((self.bar(source="REPLACEMENT"),))

        self.assertTrue(unlinked)
        self.assertNotIn(self.path, unlinked)
        self.assertEqual(self.path.read_bytes(), before)
        for artifact in self.temp_artifacts():
            real_unlink(artifact)

    def test_two_instances_do_not_lose_updates_and_newest_same_key_wins(self) -> None:
        second = DailyHistoryStore(self.path, self.metadata, ())
        barrier = threading.Barrier(3)
        failures: list[BaseException] = []

        def update(store: DailyHistoryStore, record: DailyBar) -> None:
            try:
                barrier.wait()
                store.upsert((record,))
            except BaseException as error:
                failures.append(error)

        threads = [
            threading.Thread(target=update, args=(self.store, self.bar("510300"))),
            threading.Thread(target=update, args=(second, self.bar("510500"))),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual({item.symbol for item in self.store.load()}, {"510300", "510500"})

        older = self.bar(observed_at="2026-08-28T15:11:00+08:00", source="OLDER")
        newer = self.bar(observed_at="2026-08-28T15:12:00+08:00", source="NEWER")
        barrier = threading.Barrier(3)
        threads = [
            threading.Thread(target=update, args=(self.store, older)),
            threading.Thread(target=update, args=(second, newer)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
        self.assertEqual(failures, [])
        self.assertEqual(self.store.query("510300")[0].source, "NEWER")

    def test_separate_processes_do_not_lose_updates_or_newest_same_key(self) -> None:
        child = """
import sys
import time
from pathlib import Path
from etf_rotation.etf_metadata import EtfMetadataStore
from etf_rotation.swing_data import DailyBar, DailyHistoryStore
from tests.swing_helpers import daily_bar_mapping

history, metadata_path, gate, symbol, observed_at, source = sys.argv[1:]
while not Path(gate).exists():
    time.sleep(0.005)
metadata = EtfMetadataStore(Path(metadata_path)).load()
record = DailyBar.from_mapping(daily_bar_mapping(
    symbol=symbol,
    observed_at=observed_at,
))
record = DailyBar.from_mapping({**record.to_dict(), "source": source})
DailyHistoryStore(Path(history), metadata, ()).upsert((record,))
"""

        def race(arguments: tuple[tuple[str, str, str], ...]) -> None:
            gate = self.root / f"gate-{time.time_ns()}"
            processes = [
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        child,
                        str(self.path),
                        str(self.metadata_path),
                        str(gate),
                        symbol,
                        observed_at,
                        source,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for symbol, observed_at, source in arguments
            ]
            gate.touch()
            results = [process.communicate(timeout=10) for process in processes]
            for process, (stdout, stderr) in zip(processes, results):
                self.assertEqual(process.returncode, 0, stdout + stderr)

        race((
            ("510300", "2026-08-28T15:10:00+08:00", "FIRST"),
            ("510500", "2026-08-28T15:10:00+08:00", "SECOND"),
        ))
        self.assertEqual({item.symbol for item in self.store.load()}, {"510300", "510500"})

        race((
            ("510300", "2026-08-28T15:11:00+08:00", "OLDER"),
            ("510300", "2026-08-28T15:12:00+08:00", "NEWER"),
        ))
        self.assertEqual(self.store.query("510300")[0].source, "NEWER")

    def test_reader_waits_for_writer_and_cannot_observe_partial_replacement(self) -> None:
        self.store.upsert((self.bar(source="ORIGINAL"),))
        replace_entered = threading.Event()
        allow_replace = threading.Event()
        reader_done = threading.Event()
        observed: list[tuple[DailyBar, ...]] = []
        real_replace = os.replace

        def blocked_replace(source: object, destination: object) -> None:
            replace_entered.set()
            self.assertTrue(allow_replace.wait(5))
            real_replace(source, destination)

        def read() -> None:
            observed.append(DailyHistoryStore(self.path, self.metadata, ()).load())
            reader_done.set()

        with patch("etf_rotation.swing_data.os.replace", side_effect=blocked_replace):
            writer = threading.Thread(target=self.store.upsert, args=((self.bar(source="NEW"),),))
            writer.start()
            self.assertTrue(replace_entered.wait(5))
            reader = threading.Thread(target=read)
            reader.start()
            time.sleep(0.05)
            self.assertFalse(reader_done.is_set())
            allow_replace.set()
            writer.join(5)
            reader.join(5)

        self.assertTrue(reader_done.is_set())
        self.assertEqual(observed[0][0].source, "NEW")

    def test_same_process_shared_readers_overlap(self) -> None:
        first_entered = threading.Event()
        second_entered = threading.Event()
        release = threading.Event()

        def read(entered: threading.Event) -> None:
            with _SiblingFileLock(self.path, shared=True):
                entered.set()
                release.wait(5)

        first = threading.Thread(target=read, args=(first_entered,))
        second = threading.Thread(target=read, args=(second_entered,))
        first.start()
        self.assertTrue(first_entered.wait(5))
        second.start()
        overlap = second_entered.wait(0.5)
        release.set()
        first.join(5)
        second.join(5)

        self.assertTrue(overlap)

    def test_same_process_waiting_writer_blocks_new_readers(self) -> None:
        first_reader = _SiblingFileLock(self.path, shared=True)
        first_reader.__enter__()
        writer_entered = threading.Event()
        release_writer = threading.Event()
        later_reader_entered = threading.Event()

        def write() -> None:
            with _SiblingFileLock(self.path, shared=False):
                writer_entered.set()
                release_writer.wait(5)

        def read() -> None:
            with _SiblingFileLock(self.path, shared=True):
                later_reader_entered.set()

        writer = threading.Thread(target=write)
        writer.start()
        deadline = time.monotonic() + 5
        while (
            first_reader._process_lock._waiting_writers == 0
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        later_reader = threading.Thread(target=read)
        later_reader.start()
        time.sleep(0.05)
        reader_bypassed_writer = later_reader_entered.is_set()
        first_reader.__exit__(None, None, None)
        writer_acquired = writer_entered.wait(5)
        reader_entered_during_writer = later_reader_entered.is_set()
        release_writer.set()
        writer.join(5)
        later_reader.join(5)

        self.assertFalse(reader_bypassed_writer)
        self.assertTrue(writer_acquired)
        self.assertFalse(reader_entered_during_writer)
        self.assertTrue(later_reader_entered.is_set())

    def test_subprocess_shared_readers_acquire_concurrently(self) -> None:
        ready_paths = (self.root / "reader-one", self.root / "reader-two")
        release = self.root / "readers-release"
        child = """
import sys
import time
from pathlib import Path
from etf_rotation.swing_data import _SiblingFileLock

history, ready, release = map(Path, sys.argv[1:])
with _SiblingFileLock(history, shared=True):
    ready.write_text("ready", encoding="ascii")
    while not release.exists():
        time.sleep(0.005)
"""
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", child, str(self.path), str(ready), str(release)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for ready in ready_paths
        ]
        deadline = time.monotonic() + 2
        while not all(path.exists() for path in ready_paths) and time.monotonic() < deadline:
            time.sleep(0.005)
        concurrent = all(path.exists() for path in ready_paths)
        release.touch()
        results = [process.communicate(timeout=5) for process in processes]

        for process, (stdout, stderr) in zip(processes, results):
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertTrue(concurrent)

    def test_subprocess_writer_lock_blocks_reader_until_release(self) -> None:
        expected = self.store.upsert((self.bar(source="ORIGINAL"),))
        ready = self.root / "writer-ready"
        release = self.root / "writer-release"
        child = """
import sys
import time
from pathlib import Path
from etf_rotation.swing_data import _SiblingFileLock

history, ready, release = map(Path, sys.argv[1:])
with _SiblingFileLock(history, shared=False):
    ready.write_text("ready", encoding="ascii")
    while not release.exists():
        time.sleep(0.005)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", child, str(self.path), str(ready), str(release)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(ready.exists())

        finished = threading.Event()
        observed: list[tuple[DailyBar, ...]] = []

        def read() -> None:
            observed.append(self.store.load())
            finished.set()

        reader = threading.Thread(target=read)
        reader.start()
        time.sleep(0.05)
        self.assertFalse(finished.is_set())
        release.touch()
        stdout, stderr = process.communicate(timeout=5)
        reader.join(5)

        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertTrue(finished.is_set())
        self.assertEqual(observed, [expected])

    def test_lock_is_released_after_exception(self) -> None:
        self.store.upsert((self.bar(source="ORIGINAL"),))
        with patch("etf_rotation.swing_data.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.upsert((self.bar(source="FAILED"),))

        finished = threading.Event()
        failure: list[BaseException] = []

        def update() -> None:
            try:
                DailyHistoryStore(self.path, self.metadata, ()).upsert((self.bar(source="RECOVERED"),))
            except BaseException as error:
                failure.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=update)
        thread.start()
        thread.join(5)
        self.assertTrue(finished.is_set())
        self.assertEqual(failure, [])
        self.assertEqual(self.store.load()[0].source, "RECOVERED")

    def test_resolves_final_symlink_alias_to_one_operational_history(self) -> None:
        expected = self.store.upsert((self.bar(source="ORIGINAL"),))
        alias = self.root / "daily-alias.jsonl"
        try:
            alias.symlink_to(self.path.name)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")
        if not alias.is_symlink() or not alias.exists():
            self.skipTest("symlink creation reported success but produced no usable link")

        alias_store = DailyHistoryStore(alias, self.metadata, ())
        self.assertEqual(alias_store.path, self.path.resolve(strict=False))
        self.assertEqual(alias_store.load(), expected)

        newer = self.bar(
            observed_at="2026-08-28T15:11:00+08:00",
            source="ALIAS",
        )
        alias_store.upsert((newer,))
        self.assertEqual(self.store.load(), (newer,))


if __name__ == "__main__":
    unittest.main()
