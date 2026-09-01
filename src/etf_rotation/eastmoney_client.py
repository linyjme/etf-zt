"""Shared Eastmoney HTTP transport and market routing primitives."""

from __future__ import annotations

from collections.abc import Callable
import base64
import json
import shutil
import subprocess
import time
from typing import TypeVar
from urllib.request import Request, urlopen


Transport = Callable[[Request, float], bytes]
_Error = TypeVar("_Error", bound=Exception)


class EastmoneyMarketError(ValueError):
    """Raised when an Eastmoney secid market cannot be derived safely."""


def market_for_symbol(
    symbol: str,
    *,
    error_type: type[_Error] = EastmoneyMarketError,
) -> int:
    """Map one exact six-digit ASCII security code to Eastmoney market id."""
    if (
        type(symbol) is not str
        or len(symbol) != 6
        or not symbol.isascii()
        or not symbol.isdigit()
    ):
        raise error_type(f"证券代码必须是6位数字: {symbol}")
    if symbol[0] in "0123":
        return 0
    if symbol[0] in "5679":
        return 1
    raise error_type(f"无法映射证券市场: {symbol}")


def _powershell_transport(request: Request, timeout: float) -> bytes:
    encoded_url = base64.b64encode(request.full_url.encode("utf-8")).decode("ascii")
    script = (
        "$u=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
        + encoded_url
        + "'));$r=Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec "
        + str(max(1, int(timeout)))
        + ";[Convert]::ToBase64String($r.Content)"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        timeout=timeout + 3,
    )
    return base64.b64decode(result.stdout.strip(), validate=True)


def _default_transport(request: Request, timeout: float) -> bytes:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    command = [
        curl,
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--connect-timeout",
        str(timeout),
        "--max-time",
        str(timeout),
    ]
    for name, value in request.header_items():
        command.extend(("--header", f"{name}: {value}"))
    command.append(request.full_url)
    last_error = "行情请求失败"
    for attempt in range(3):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                timeout=timeout + 2,
            )
        except subprocess.TimeoutExpired as error:
            last_error = str(error)
        else:
            if result.stdout:
                try:
                    json.loads(result.stdout.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                else:
                    return result.stdout
            last_error = result.stderr.decode("utf-8", errors="replace").strip() or f"curl 退出码 {result.returncode}"
        if attempt < 2:
            time.sleep(0.2 * (attempt + 1))
    try:
        return _powershell_transport(request, timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        raise OSError(last_error)
