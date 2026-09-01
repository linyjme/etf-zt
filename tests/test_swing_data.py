from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
import tempfile
import unittest

from etf_rotation.etf_metadata import EtfMetadata, EtfMetadataStore
from etf_rotation.swing_data import DailyBar, DailyBarValidator, SwingDataError
from tests.swing_helpers import daily_bar_mapping, metadata_fixture


SHANGHAI = timezone(timedelta(hours=8))


class BrokenMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("broken mapping")

    def __iter__(self) -> Iterator[str]:
        yield "schema_version"

    def __len__(self) -> int:
        return 1


class OSErrorMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise OSError("mapping unavailable")

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
        raise OSError("metadata unavailable")


class HostileKey:
    def __repr__(self) -> str:
        raise OSError("repr unavailable")


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
            raise OSError("calendar unavailable")
            yield date(2026, 8, 28)

        with self.assertRaises(SwingDataError) as raised:
            DailyBarValidator(broken_dates())
        self.assertIsInstance(raised.exception.__cause__, OSError)


if __name__ == "__main__":
    unittest.main()
