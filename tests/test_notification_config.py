from __future__ import annotations

import importlib
import importlib.util
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


class NotificationConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            importlib.util.find_spec("etf_rotation.notification_config"),
            "notification configuration module must exist",
        )
        self.api = importlib.import_module("etf_rotation.notification_config")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "notifications.json"

    def complete(self):
        config = self.api.default_config()
        config.update(sender="sender@163.com", recipient="recipient@example.com",
                      username="sender@163.com", enabled=True)
        return config

    def test_defaults_are_exact_and_independent(self):
        expected = {"schema_version": 1, "enabled": False,
                    "smtp_host": "smtp.163.com", "smtp_port": 465,
                    "sender": "", "recipient": "", "username": "",
                    "anomaly_enabled": True, "health_enabled": True,
                    "excluded_symbols": [], "rules": {}}
        self.assertEqual(expected, self.api.default_config())
        first = self.api.default_config()
        first["excluded_symbols"].append("510300")
        self.assertEqual(expected, self.api.default_config())

    def test_validation_returns_deeply_detached_config_and_percent_units(self):
        config = self.complete()
        config["rules"] = {"510300": {"threshold_pct": 1.0, "cooldown_minutes": 30}}
        config["excluded_symbols"] = ["510500"]
        validated = self.api.validate_config(config)
        self.assertEqual(config, validated)
        validated["rules"]["510300"]["threshold_pct"] = 2.0
        validated["excluded_symbols"].append("159915")
        self.assertEqual(1.0, config["rules"]["510300"]["threshold_pct"])
        self.assertEqual(["510500"], config["excluded_symbols"])

    def test_disabled_incomplete_config_is_allowed_but_enabled_is_rejected(self):
        config = self.api.default_config()
        self.assertEqual(config, self.api.validate_config(config))
        config["enabled"] = True
        with self.assertRaises(ValueError):
            self.api.validate_config(config)

    def test_exact_fields_and_container_types_are_required(self):
        for payload in (None, [], {}, "secret-do-not-repeat"):
            with self.subTest(payload_type=type(payload)):
                with self.assertRaises(ValueError):
                    self.api.validate_config(payload)
        config = self.complete()
        config["password-secret-do-not-repeat"] = "private-value"
        with self.assertRaises(ValueError) as raised:
            self.api.validate_config(config)
        self.assertNotIn("secret-do-not-repeat", str(raised.exception))
        self.assertNotIn("private-value", str(raised.exception))
        del config["password-secret-do-not-repeat"]
        del config["health_enabled"]
        with self.assertRaises(ValueError):
            self.api.validate_config(config)

    def test_scalar_types_host_restriction_and_header_injection(self):
        invalid = {
            "schema_version": [True, 1.0, 2, "1"],
            "enabled": [0, "false", None],
            "anomaly_enabled": [1, None],
            "health_enabled": ["true", 0],
            "smtp_host": ["attacker.example", "smtp.163.com\r\nX: y", "SMTP.163.COM", None],
            "smtp_port": [True, 465.0, 25, "465"],
            "sender": [None, "a@163.com\r\nBcc: x@x.com", "A <a@163.com>", "a..b@163.com", "a@-bad.com"],
            "recipient": ["one@example.com,two@example.com", "no-address", "x@x.com\nX: y", " x@example.com", "x@example.com\x00"],
            "username": [None, "user\r\nX: y", "user\x00", "user\tname"],
            "excluded_symbols": ["510300", {}, ["51030"], [510300], ["５１０３００"], ["510300", "510300"]],
            "rules": [[], None, {"51030": {"threshold_pct": 1, "cooldown_minutes": 30}}],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    config = self.complete()
                    config[field] = value
                    with self.assertRaises(ValueError):
                        self.api.validate_config(config)

    def test_rule_boundaries_and_types(self):
        for threshold in (0.1, 1, 10):
            for cooldown in (30, 31, 240):
                config = self.complete()
                config["rules"] = {"510300": {"threshold_pct": threshold, "cooldown_minutes": cooldown}}
                self.assertEqual(config, self.api.validate_config(config))
        for field, values in {
            "threshold_pct": [True, None, "1", 0.09, 10.01, math.nan, math.inf, -math.inf],
            "cooldown_minutes": [True, None, "30", 30.0, 0, 1, 29, 241],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    config = self.complete()
                    config["rules"] = {"510300": {"threshold_pct": 1, "cooldown_minutes": 30}}
                    config["rules"]["510300"][field] = value
                    with self.assertRaises(ValueError):
                        self.api.validate_config(config)
        for rule in ({}, {"threshold_pct": 1}, {"threshold_pct": 1, "cooldown_minutes": 30, "extra": 1}, None):
            config = self.complete()
            config["rules"] = {"510300": rule}
            with self.assertRaises(ValueError):
                self.api.validate_config(config)

    def test_missing_store_defaults_and_round_trip(self):
        store = self.api.ConfigStore(self.path)
        self.assertEqual(self.api.default_config(), store.load())
        self.assertFalse(self.path.exists())
        config = self.complete()
        store.save(config)
        self.assertEqual(config, store.load())
        self.assertEqual(config, json.loads(self.path.read_text(encoding="utf-8")))
        self.assertEqual([self.path], list(self.path.parent.iterdir()))

    def test_corrupt_config_fails_closed_without_exposing_contents(self):
        self.path.parent.mkdir()
        for data in (b"private-password {", b"\xff", b'{"enabled": true}',
                     b'{"schema_version":1,"schema_version":1}'):
            self.path.write_bytes(data)
            with self.assertRaises(ValueError) as raised:
                self.api.ConfigStore(self.path).load()
            self.assertNotIn("private-password", str(raised.exception))
            self.assertIsNone(raised.exception.__cause__)

    def test_duplicate_json_keys_are_rejected_even_when_otherwise_valid(self):
        self.path.parent.mkdir()
        encoded = json.dumps(self.complete())
        self.path.write_text(encoded[:-1] + ', "enabled": true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.api.ConfigStore(self.path).load()

    def test_invalid_save_preserves_existing_valid_file(self):
        store = self.api.ConfigStore(self.path)
        config = self.complete()
        store.save(config)
        invalid = dict(config, secret="never-persist-this")
        with self.assertRaises(ValueError):
            store.save(invalid)
        self.assertEqual(config, store.load())

    def test_atomic_replace_failure_preserves_old_config_and_cleans_temp(self):
        store = self.api.ConfigStore(self.path)
        original = self.complete()
        store.save(original)
        with patch.object(self.api.os, "replace", side_effect=OSError("private-path-secret")):
            with self.assertRaises(ValueError) as raised:
                store.save(dict(original, enabled=False))
        self.assertNotIn("private-path-secret", str(raised.exception))
        self.assertEqual(original, store.load())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("etf_rotation.notification_config"),
                             "secret storage module must exist")
        self.api = importlib.import_module("etf_rotation.notification_config")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "notification-secret.bin"
        self.secret = "synthetic-test-authorization-code"

    def store(self, **kwargs):
        return self.api.SecretStore(
            self.path, encrypt=kwargs.get("encrypt", lambda value: b"cipher:" + value[::-1]),
            decrypt=kwargs.get("decrypt", lambda value: value[7:][::-1]),
        )

    def test_missing_secret_is_not_present_and_loads_none(self):
        self.assertFalse(self.store().exists())
        self.assertIsNone(self.store().load())
        self.assertFalse(self.path.exists())

    def test_encrypted_round_trip_never_stores_plaintext(self):
        store = self.store()
        store.save(self.secret)
        self.assertTrue(store.exists())
        self.assertEqual(self.secret, store.load())
        self.assertNotIn(self.secret.encode(), self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))

    def test_encryption_failure_preserves_existing_ciphertext_without_leaking(self):
        store = self.store()
        store.save(self.secret)
        original = self.path.read_bytes()
        def fail(_):
            raise RuntimeError(self.secret)
        with self.assertRaises(ValueError) as raised:
            self.store(encrypt=fail).save("another-synthetic-secret")
        self.assertNotIn(self.secret, str(raised.exception))
        self.assertEqual(original, self.path.read_bytes())

    def test_identity_encryption_is_rejected_without_writing(self):
        with self.assertRaises(ValueError):
            self.store(encrypt=lambda value: value).save(self.secret)
        self.assertFalse(self.path.exists())

    def test_raw_plaintext_and_corrupt_ciphertext_fail_closed(self):
        self.path.parent.mkdir()
        self.path.write_text(self.secret, encoding="utf-8")
        self.assertFalse(self.store().exists())
        with self.assertRaises(ValueError) as raised:
            self.store().load()
        self.assertNotIn(self.secret, str(raised.exception))
        self.store().save(self.secret)
        def fail(_):
            raise OSError(self.secret)
        with self.assertRaises(ValueError) as raised:
            self.store(decrypt=fail).load()
        self.assertNotIn(self.secret, str(raised.exception))

    def test_invalid_secret_rejected_before_encrypting(self):
        for secret in (None, "", b"bytes", "x\r\ny", "x\x00y"):
            with self.subTest(value=secret):
                with self.assertRaises(ValueError):
                    self.store().save(secret)
        self.assertFalse(self.path.exists())

    def test_unsupported_platform_does_not_fallback_to_plaintext(self):
        with patch.object(self.api.sys, "platform", "linux"):
            with self.assertRaises(ValueError) as raised:
                self.api.SecretStore(self.path).save(self.secret)
        self.assertNotIn(self.secret, str(raised.exception))
        self.assertFalse(self.path.exists())

    def test_atomic_secret_write_failure_preserves_existing_ciphertext(self):
        store = self.store()
        store.save(self.secret)
        original = self.path.read_bytes()
        with patch.object(self.api.os, "replace", side_effect=OSError(self.secret)):
            with self.assertRaises(ValueError) as raised:
                store.save("synthetic-replacement")
        self.assertNotIn(self.secret, str(raised.exception))
        self.assertEqual(original, self.path.read_bytes())
        self.assertEqual([self.path], list(self.path.parent.iterdir()))

    def test_native_dpapi_uses_current_user_no_ui_and_cleans_native_output_buffers(self):
        ctypes = self.api.ctypes
        protected = b"opaque-native-test-ciphertext"
        buffers = []
        operations = []
        freed = []

        def operation(decrypt):
            def call(source, description, entropy, reserved, prompt, flags, destination):
                source_blob = ctypes.cast(source, ctypes.POINTER(self.api._DataBlob)).contents
                source_bytes = ctypes.string_at(source_blob.pbData, source_blob.cbData)
                operations.append((decrypt, description, entropy, reserved, prompt, flags, source_bytes))
                data = self.secret.encode() if decrypt else protected
                buffer = ctypes.create_string_buffer(data)
                buffers.append(buffer)
                target = ctypes.cast(destination, ctypes.POINTER(self.api._DataBlob)).contents
                target.cbData = len(data)
                target.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
                return 1
            return Mock(side_effect=call)

        def free(pointer):
            buffer = buffers[-1]
            freed.append(ctypes.string_at(pointer, len(buffer) - 1))
            return None

        crypt32 = SimpleNamespace(CryptProtectData=operation(False), CryptUnprotectData=operation(True))
        kernel32 = SimpleNamespace(LocalFree=Mock(side_effect=free))
        with patch.object(self.api.sys, "platform", "win32"), patch.object(
            ctypes, "WinDLL", side_effect=lambda name, **_: crypt32 if name == "crypt32" else kernel32,
            create=True,
        ):
            store = self.api.SecretStore(self.path)
            store.save(self.secret)
            self.assertEqual(self.secret, store.load())
        self.assertEqual(2, len(operations))
        self.assertEqual([False, True], [entry[0] for entry in operations])
        self.assertEqual([self.secret.encode(), protected], [entry[-1] for entry in operations])
        for entry in operations:
            self.assertEqual((None, None, None, None, 1), entry[1:6])
        self.assertEqual([b"\x00" * len(protected), b"\x00" * len(self.secret.encode())], freed)
        self.assertNotIn(self.secret.encode(), self.path.read_bytes())

    def test_native_encryption_failure_never_writes_a_secret_file(self):
        crypt32 = SimpleNamespace(CryptProtectData=Mock(return_value=0), CryptUnprotectData=Mock())
        kernel32 = SimpleNamespace(LocalFree=Mock())
        with patch.object(self.api.sys, "platform", "win32"), patch.object(
            self.api.ctypes, "WinDLL", side_effect=lambda name, **_: crypt32 if name == "crypt32" else kernel32,
            create=True,
        ):
            with self.assertRaises(ValueError) as raised:
                self.api.SecretStore(self.path).save(self.secret)
        self.assertNotIn(self.secret, str(raised.exception))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
