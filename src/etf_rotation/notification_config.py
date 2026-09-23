"""Strict public notification configuration and current-user DPAPI secrets.

Authorization codes never belong in the JSON configuration. Secret persistence
encrypts first, then atomically replaces an encrypted file; there is no plaintext
fallback on an unsupported platform, encryption error, or write failure.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from collections.abc import Callable


_SYMBOL = re.compile(r"[0-9]{6}\Z")
_LOCAL_PART = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
_DOMAIN_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL = re.compile(rf"{_LOCAL_PART}(?:\.{_LOCAL_PART})*@{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+\Z")
_SECRET_MAGIC = b"ETF_NOTIFICATION_DPAPI_V1\x00"
_MAX_FILE_BYTES = 1024 * 1024


def default_config() -> dict:
    """Return detached v1 defaults. Threshold percentages use 1.0 for 1%."""
    return {
        "schema_version": 1,
        "enabled": False,
        "smtp_host": "smtp.163.com",
        "smtp_port": 465,
        "sender": "",
        "recipient": "",
        "username": "",
        "anomaly_enabled": True,
        "health_enabled": True,
        "excluded_symbols": [],
        "rules": {},
    }


def _text(value: object, *, allow_empty: bool = False, limit: int = 254) -> bool:
    return (
        type(value) is str
        and (allow_empty or bool(value))
        and len(value) <= limit
        and all(char.isprintable() and char not in "\r\n" for char in value)
    )


def _valid_email(value: object, allow_empty: bool) -> bool:
    if not _text(value, allow_empty=allow_empty):
        return False
    if value == "":
        return allow_empty
    return bool(_EMAIL.fullmatch(value)) and len(value.split("@", 1)[0]) <= 64


def validate_config(payload: object) -> dict:
    """Validate an exact v1 object without reflecting untrusted values in errors."""
    if type(payload) is not dict or payload.keys() != default_config().keys():
        raise ValueError("Notification configuration fields are invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("Notification configuration version is invalid")
    for field in ("enabled", "anomaly_enabled", "health_enabled"):
        if type(payload[field]) is not bool:
            raise ValueError("Notification switches must be booleans")
    if type(payload["smtp_host"]) is not str or payload["smtp_host"] != "smtp.163.com":
        raise ValueError("Only the v1 SMTP provider is supported")
    if type(payload["smtp_port"]) is not int or payload["smtp_port"] != 465:
        raise ValueError("Only implicit TLS on SMTP port 465 is supported")
    incomplete_allowed = not payload["enabled"]
    for field in ("sender", "recipient"):
        if not _valid_email(payload[field], incomplete_allowed):
            raise ValueError("Notification email address is invalid")
    if not _text(payload["username"], allow_empty=incomplete_allowed):
        raise ValueError("Notification username is invalid")
    if payload["username"] and (
        not payload["username"].isascii()
        or any(char.isspace() for char in payload["username"])
    ):
        raise ValueError("Notification username is invalid")
    excluded = payload["excluded_symbols"]
    if type(excluded) is not list:
        raise ValueError("Notification exclusions must be an array")
    if any(type(symbol) is not str or not _SYMBOL.fullmatch(symbol) for symbol in excluded):
        raise ValueError("Notification symbols must be six ASCII digits")
    if len(set(excluded)) != len(excluded):
        raise ValueError("Notification exclusions must be unique")
    raw_rules = payload["rules"]
    if type(raw_rules) is not dict:
        raise ValueError("Notification rules must be an object")
    rules = {}
    for symbol, rule in raw_rules.items():
        if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
            raise ValueError("Notification symbols must be six ASCII digits")
        if type(rule) is not dict or rule.keys() != {"threshold_pct", "cooldown_minutes"}:
            raise ValueError("Notification rule fields are invalid")
        threshold = rule["threshold_pct"]
        cooldown = rule["cooldown_minutes"]
        if type(threshold) not in (int, float) or not 0.1 <= threshold <= 10:
            raise ValueError("Notification threshold must be between 0.1 and 10 percent")
        if type(cooldown) is not int or not 30 <= cooldown <= 240:
            raise ValueError("Notification cooldown must be 30 to 240 minutes")
        rules[symbol] = {"threshold_pct": threshold, "cooldown_minutes": cooldown}
    return dict(payload, excluded_symbols=list(excluded), rules=rules)


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace only after a complete flushed write in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("Notification storage file is too large")
    return data


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate notification configuration field")
        result[key] = value
    return result


class ConfigStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            raw = _read_bounded(self.path)
        except FileNotFoundError:
            return default_config()
        except Exception:
            raise ValueError("Notification configuration could not be read") from None
        try:
            payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            return validate_config(payload)
        except Exception:
            raise ValueError("Notification configuration is invalid") from None

    def save(self, config: object) -> None:
        validated = validate_config(config)
        try:
            encoded = (json.dumps(validated, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) > _MAX_FILE_BYTES:
                raise ValueError("Notification configuration is too large")
            _atomic_write(self.path, encoded)
        except Exception:
            raise ValueError("Notification configuration could not be saved") from None


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    """Use current-user DPAPI, never CRYPTPROTECT_LOCAL_MACHINE or a UI prompt."""
    if sys.platform != "win32":
        raise ValueError("Encrypted notification secrets require Windows DPAPI")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    blob_pointer = ctypes.POINTER(_DataBlob)
    crypt32.CryptProtectData.argtypes = [blob_pointer, ctypes.c_wchar_p, blob_pointer,
                                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                         blob_pointer]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                                           blob_pointer]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    destination = _DataBlob()
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    try:
        # Flag 1 is CRYPTPROTECT_UI_FORBIDDEN; omitting flag 4 binds to this user.
        if not operation(ctypes.byref(source), None, None, None, None, 1,
                         ctypes.byref(destination)):
            raise ValueError("Notification secret encryption operation failed")
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        ctypes.memset(buffer, 0, ctypes.sizeof(buffer))
        if destination.pbData:
            ctypes.memset(destination.pbData, 0, destination.cbData)
            kernel32.LocalFree(ctypes.cast(destination.pbData, ctypes.c_void_p))


class SecretStore:
    """Persist ciphertext only; optional byte transforms isolate the crypto boundary."""

    def __init__(self, path: str | Path, *, encrypt: Callable | None = None,
                 decrypt: Callable | None = None):
        self.path = Path(path)
        self._encrypt = encrypt if encrypt is not None else lambda data: _dpapi(data, decrypt=False)
        self._decrypt = decrypt if decrypt is not None else lambda data: _dpapi(data, decrypt=True)

    def exists(self) -> bool:
        try:
            data = _read_bounded(self.path)
            return data.startswith(_SECRET_MAGIC) and len(data) > len(_SECRET_MAGIC)
        except FileNotFoundError:
            return False
        except Exception:
            raise ValueError("Notification secret storage could not be inspected") from None

    def save(self, secret: str) -> None:
        if not _text(secret, limit=4096):
            raise ValueError("Notification authorization code is invalid")
        try:
            plaintext = secret.encode("utf-8")
            ciphertext = self._encrypt(plaintext)
            if type(ciphertext) is not bytes or not ciphertext or plaintext in ciphertext:
                raise ValueError("Notification secret encryption failed")
            if len(ciphertext) + len(_SECRET_MAGIC) > _MAX_FILE_BYTES:
                raise ValueError("Notification secret ciphertext is too large")
            _atomic_write(self.path, _SECRET_MAGIC + ciphertext)
        except Exception:
            raise ValueError("Notification authorization code could not be secured") from None

    def load(self) -> str | None:
        try:
            data = _read_bounded(self.path)
        except FileNotFoundError:
            return None
        except Exception:
            raise ValueError("Notification authorization code could not be read") from None
        try:
            if not data.startswith(_SECRET_MAGIC) or len(data) <= len(_SECRET_MAGIC):
                raise ValueError("Invalid notification secret envelope")
            secret = self._decrypt(data[len(_SECRET_MAGIC):]).decode("utf-8")
            if not _text(secret, limit=4096):
                raise ValueError("Invalid notification authorization code")
            return secret
        except Exception:
            raise ValueError("Notification authorization code could not be decrypted") from None
