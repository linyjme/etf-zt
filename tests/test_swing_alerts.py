from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from etf_rotation.swing_alerts import (
    AlertEvent,
    AlertEventType,
    AlertInput,
    AlertProjection,
    AlertStoreError,
    SwingAlertStore,
    formal_alert_id,
)


SHANGHAI = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 1, 15, 30, tzinfo=SHANGHAI)


def formal_alert(**changes: object) -> AlertInput:
    values: dict[str, object] = {
        "trading_date": date(2026, 8, 31),
        "symbol": "510300",
        "state": "TRIAL_ENTRY_CANDIDATE",
        "strategy_version": "SWING_V1",
        "level": "YELLOW",
        "label": "试仓候选",
        "evidence": {"risk": 0.0075, "reasons": ["PULLBACK"]},
    }
    values.update(changes)
    return AlertInput(**values)


def overlay_alert(**changes: object) -> AlertInput:
    values: dict[str, object] = {
        "trading_date": date(2026, 9, 1),
        "symbol": "510300",
        "state": "PREDEFINED_STOP_TOUCHED",
        "strategy_version": "SWING_V1",
        "level": "RED",
        "label": "盘中触及预设止损",
        "evidence": {"price": 4.2, "stop": 4.25},
    }
    values.update(changes)
    return AlertInput(**values)


class SwingAlertStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "alerts.jsonl"
        self.store = SwingAlertStore(self.path, clock=lambda: NOW)

    def test_formal_id_is_exact_ascii_sha256_prefix(self) -> None:
        identity = "2026-08-31|510300|TRIAL_ENTRY_CANDIDATE|SWING_V1"
        expected = hashlib.sha256(identity.encode("ascii")).hexdigest()[:24]
        self.assertEqual(
            formal_alert_id(
                date(2026, 8, 31), "510300",
                "TRIAL_ENTRY_CANDIDATE", "SWING_V1",
            ),
            expected,
        )

    def test_formal_alert_is_deduplicated_and_acknowledged(self) -> None:
        alert = formal_alert()
        first = self.store.publish_formal(alert)
        second = self.store.publish_formal(alert)
        self.assertEqual(first.alert_id, second.alert_id)
        self.assertEqual(len(self.store.load_events()), 1)
        self.assertEqual(len(self.store.current()), 1)

        acknowledged = self.store.acknowledge(first.alert_id, "opaque-ack-1")
        repeated = self.store.acknowledge(first.alert_id, "opaque-ack-1")
        self.assertTrue(acknowledged.acknowledged)
        self.assertEqual(repeated, acknowledged)
        self.assertEqual(len(self.store.load_events()), 2)
        self.assertEqual(self.store.active_notifications(), ())

        rebuilt = SwingAlertStore(self.path).current()[0]
        self.assertTrue(rebuilt.acknowledged)
        self.assertEqual(rebuilt.evidence["reasons"], ("PULLBACK",))

    def test_same_formal_identity_with_changed_payload_is_rejected(self) -> None:
        self.store.publish_formal(formal_alert())
        with self.assertRaisesRegex(AlertStoreError, "different request"):
            self.store.publish_formal(formal_alert(label="被修改的标签"))

    def test_overlay_is_deduplicated_and_identity_does_not_collide_with_formal(self) -> None:
        shared = formal_alert(
            trading_date=date(2026, 9, 1),
            state="PREDEFINED_STOP_TOUCHED",
            level="RED",
            label="触及止损",
        )
        formal = self.store.publish_formal(shared)
        first = self.store.publish_overlay(shared)
        second = self.store.publish_overlay(shared)
        self.assertNotEqual(formal.alert_id, first.alert_id)
        self.assertEqual(first.alert_id, second.alert_id)
        self.assertEqual(len(self.store.load_events()), 2)

    def test_intraday_outage_retracts_only_intraday_overlay(self) -> None:
        formal = self.store.publish_formal(formal_alert())
        overlay = self.store.publish_overlay(overlay_alert())
        retracted = self.store.retract_overlays("INTRADAY_FEED_UNAVAILABLE")
        self.assertEqual(tuple(item.alert_id for item in retracted), (overlay.alert_id,))
        self.assertEqual(len(self.store.current()), 1)
        current = {
            item.alert_id: item
            for item in self.store.current(include_retracted=True)
        }
        self.assertFalse(current[formal.alert_id].retracted)
        self.assertTrue(current[overlay.alert_id].retracted)
        self.assertEqual(
            current[overlay.alert_id].retraction_reason,
            "INTRADAY_FEED_UNAVAILABLE",
        )
        self.assertEqual(self.store.retract_overlays("SECOND_OUTAGE"), ())
        self.assertEqual(len(self.store.load_events()), 3)

    def test_ignore_is_persisted_only_for_current_alert_and_excluded_from_active(self) -> None:
        first = self.store.publish_formal(formal_alert())
        ignored = self.store.ignore(first.alert_id, "opaque-ignore-1")
        self.assertTrue(ignored.ignored)
        self.assertEqual(self.store.active_notifications(), ())
        self.assertEqual(len(self.store.current()), 1)

        later = self.store.publish_formal(
            formal_alert(trading_date=date(2026, 9, 1)),
        )
        by_id = {item.alert_id: item for item in self.store.current()}
        self.assertTrue(by_id[first.alert_id].ignored)
        self.assertFalse(by_id[later.alert_id].ignored)
        self.assertEqual(self.store.active_notifications(), (later,))

    def test_idempotency_key_reuse_for_different_transition_is_rejected(self) -> None:
        first = self.store.publish_formal(formal_alert())
        second = self.store.publish_formal(
            formal_alert(trading_date=date(2026, 9, 1)),
        )
        self.store.acknowledge(first.alert_id, "opaque-transition")
        for action in (
            lambda: self.store.ignore(first.alert_id, "opaque-transition"),
            lambda: self.store.acknowledge(second.alert_id, "opaque-transition"),
        ):
            with self.subTest(action=action):
                with self.assertRaisesRegex(AlertStoreError, "different request"):
                    action()

    def test_idempotent_transition_replays_its_original_projection(self) -> None:
        published = self.store.publish_formal(formal_alert())
        acknowledged = self.store.acknowledge(published.alert_id, "stable-ack")
        self.store.ignore(published.alert_id, "later-ignore")
        repeated = self.store.acknowledge(published.alert_id, "stable-ack")
        self.assertEqual(repeated, acknowledged)
        self.assertFalse(repeated.ignored)
        self.assertTrue(self.store.current()[0].ignored)

    def test_transition_rejects_unknown_or_noncanonical_alert_id(self) -> None:
        for candidate in (
            "ABCDEF0123456789ABCDEF01",
            "abcdef0123456789abcdef0",
            "g" * 24,
            True,
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(AlertStoreError, "alert_id"):
                    self.store.acknowledge(candidate, "key")  # type: ignore[arg-type]
        with self.assertRaisesRegex(AlertStoreError, "does not exist"):
            self.store.acknowledge("a" * 24, "key")

    def test_models_are_deeply_immutable_and_serialize_to_copies(self) -> None:
        source = {"nested": {"values": [1, 2]}}
        alert = formal_alert(evidence=source)
        source["nested"]["values"].append(3)  # type: ignore[index,union-attr]
        projection = self.store.publish_formal(alert)
        self.assertEqual(projection.evidence["nested"]["values"], (1, 2))
        with self.assertRaises(TypeError):
            projection.evidence["new"] = 1  # type: ignore[index]
        payload = projection.to_dict()
        payload["evidence"]["nested"]["values"].append(9)  # type: ignore[index,union-attr]
        self.assertEqual(projection.evidence["nested"]["values"], (1, 2))

    def test_strict_input_validation_rejects_nonfinite_and_ambiguous_scalars(self) -> None:
        invalid = (
            {"trading_date": datetime(2026, 8, 31, tzinfo=SHANGHAI)},
            {"symbol": "５１０３００"},
            {"symbol": "51030"},
            {"state": "试仓"},
            {"strategy_version": "版本1"},
            {"level": "PURPLE"},
            {"label": " "},
            {"evidence": {"bad": float("nan")}},
            {"evidence": {1: "bad"}},
        )
        for changes in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(AlertStoreError):
                    formal_alert(**changes)
        with self.assertRaisesRegex(AlertStoreError, "timezone-aware"):
            SwingAlertStore(self.path, clock=lambda: datetime(2026, 9, 1)).publish_formal(
                formal_alert(),
            )

    def test_hostile_evidence_mapping_failure_is_wrapped(self) -> None:
        class HostileMapping(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                raise RuntimeError("hostile lookup")

            def __iter__(self):
                raise RuntimeError("hostile iteration")

            def __len__(self) -> int:
                return 1

            def items(self):
                def generate():
                    yield "safe", 1
                    raise RuntimeError("hostile iteration")

                return generate()

        with self.assertRaisesRegex(AlertStoreError, "JSON values"):
            formal_alert(evidence=HostileMapping())

    def test_corrupt_or_noncanonical_jsonl_fails_closed(self) -> None:
        valid = self.store.publish_formal(formal_alert())
        canonical = self.path.read_text(encoding="utf-8")
        samples = (
            canonical.rstrip("\n"),
            canonical + "\n",
            canonical.replace("\":", "\": ", 1),
            "{not json}\n",
            canonical.replace(valid.alert_id, valid.alert_id.upper()),
        )
        for index, content in enumerate(samples):
            with self.subTest(index=index):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(AlertStoreError):
                    self.store.current()
                self.path.write_text(canonical, encoding="utf-8")

    def test_threaded_publish_does_not_lose_alerts(self) -> None:
        alerts = [formal_alert(symbol=f"{510300 + index:06d}") for index in range(20)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            tuple(pool.map(self.store.publish_formal, alerts))
        self.assertEqual(len(self.store.current()), 20)

    def test_process_publish_does_not_lose_alerts(self) -> None:
        script = """
from datetime import date
from pathlib import Path
import sys
from etf_rotation.swing_alerts import AlertInput, SwingAlertStore

path, symbol = sys.argv[1:]
SwingAlertStore(Path(path)).publish_formal(AlertInput(
    trading_date=date(2026, 8, 31), symbol=symbol,
    state='TREND_OBSERVATION', strategy_version='SWING_V1',
    level='BLUE', label='trend', evidence={'value': 1.0},
))
"""
        environment = dict(os.environ)
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (source_root, environment.get("PYTHONPATH"))),
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.path), f"{510300 + i:06d}"],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for i in range(8)
        ]
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual(failures, [])
        self.assertEqual(len(self.store.current()), 8)

    def test_event_parser_rejects_extra_fields_and_invalid_sequences(self) -> None:
        projection = self.store.publish_formal(formal_alert())
        line = json.loads(self.path.read_text(encoding="utf-8"))
        line["extra"] = 1
        with self.assertRaises(AlertStoreError):
            AlertEvent.from_mapping(line)

        transition = {
            "schema_version": 1,
            "event_id": "00000000-0000-4000-8000-000000000001",
            "event_type": AlertEventType.ACKNOWLEDGED.value,
            "idempotency_key": "orphan",
            "recorded_at": NOW.isoformat(),
            "payload": {"alert_id": projection.alert_id},
        }
        self.path.write_bytes(
            (json.dumps(
                transition,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n").encode("utf-8"),
        )
        with self.assertRaisesRegex(AlertStoreError, "earlier published"):
            self.store.current()

    def test_projection_rejects_an_id_inconsistent_with_its_identity(self) -> None:
        alert = formal_alert()
        with self.assertRaisesRegex(AlertStoreError, "inconsistent"):
            AlertProjection(
                alert_id="a" * 24,
                scope="FORMAL",
                trading_date=alert.trading_date,
                symbol=alert.symbol,
                state=alert.state,
                strategy_version=alert.strategy_version,
                level=alert.level,
                label=alert.label,
                evidence=alert.evidence,
                published_at=NOW,
            )

    def test_published_event_rejects_a_user_idempotency_key(self) -> None:
        self.store.publish_formal(formal_alert())
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        payload["idempotency_key"] = "must-not-be-here"
        self.path.write_bytes(
            (json.dumps(
                payload, ensure_ascii=False, allow_nan=False,
                sort_keys=True, separators=(",", ":"),
            ) + "\n").encode("utf-8"),
        )
        with self.assertRaisesRegex(AlertStoreError, "publication.*idempotency"):
            self.store.current()


if __name__ == "__main__":
    unittest.main()
