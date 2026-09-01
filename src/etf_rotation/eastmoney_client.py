"""Shared Eastmoney HTTP transport and market routing primitives."""

from __future__ import annotations

from collections.abc import Callable
import base64
import binascii
import json
import shutil
import subprocess
import time
from typing import TypeVar
from urllib.request import Request, urlopen


Transport = Callable[[Request, float], bytes]
_Error = TypeVar("_Error", bound=Exception)
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class EastmoneyMarketError(ValueError):
    """Raised when an Eastmoney secid market cannot be derived safely."""


def market_for_symbol(
    symbol: str,
    *,
    error_type: type[_Error] = EastmoneyMarketError,
) -> int:
    """Map one exact six-digit ASCII security code to Eastmoney market id."""
    if type(symbol) is not str:
        raise error_type("证券代码必须是6位数字")
    if (
        len(symbol) != 6
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
    encoded_headers = base64.b64encode(json.dumps(
        request.header_items(),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")).decode("ascii")
    timeout_ms = max(1, int(timeout * 1000))
    script = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Net.Http;"
        "$u=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"
        + encoded_url
        + "'));$h=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString("
        "[Convert]::FromBase64String('"
        + encoded_headers
        + "')));$max="
        + str(MAX_RESPONSE_BYTES)
        + ";$handler=[Net.Http.HttpClientHandler]::new();"
        "$handler.AllowAutoRedirect=$true;"
        "$client=[Net.Http.HttpClient]::new($handler);"
        "$cts=[Threading.CancellationTokenSource]::new();"
        "$response=$null;$stream=$null;$memory=$null;try{"
        "$cts.CancelAfter("
        + str(timeout_ms)
        + ");foreach($pair in $h){[void]$client.DefaultRequestHeaders."
        "TryAddWithoutValidation([string]$pair[0],[string]$pair[1])};"
        "$response=$client.GetAsync($u,[Net.Http.HttpCompletionOption]::"
        "ResponseHeadersRead,$cts.Token).GetAwaiter().GetResult();"
        "$response.EnsureSuccessStatusCode()|Out-Null;"
        "$length=$response.Content.Headers.ContentLength;"
        "if($null -ne $length -and $length -gt $max){throw 'response too large'};"
        "$stream=$response.Content.ReadAsStreamAsync().GetAwaiter().GetResult();"
        "$memory=[IO.MemoryStream]::new();$buffer=New-Object byte[] 81920;"
        "while(($read=$stream.ReadAsync($buffer,0,$buffer.Length,$cts.Token)."
        "GetAwaiter().GetResult()) -gt 0){"
        "if($memory.Length+$read -gt $max){throw 'response too large'};"
        "$memory.Write($buffer,0,$read)};"
        "[Convert]::ToBase64String($memory.ToArray())"
        "}finally{if($null -ne $memory){$memory.Dispose()};"
        "if($null -ne $stream){$stream.Dispose()};"
        "if($null -ne $response){$response.Dispose()};$cts.Dispose();"
        "$client.Dispose();$handler.Dispose()}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True,
            capture_output=True,
            timeout=timeout + 3,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise OSError("PowerShell行情请求失败") from error
    if type(result.stdout) is not bytes:
        raise OSError("PowerShell行情响应格式无效")
    max_encoded_bytes = 4 * ((MAX_RESPONSE_BYTES + 2) // 3)
    if len(result.stdout) > max_encoded_bytes + 2:
        raise OSError("PowerShell行情响应超过大小限制")
    encoded = result.stdout.strip()
    if len(encoded) > max_encoded_bytes:
        raise OSError("PowerShell行情响应超过大小限制")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise OSError("PowerShell行情响应格式无效") from error
    if len(decoded) > MAX_RESPONSE_BYTES:
        raise OSError("PowerShell行情响应超过大小限制")
    return decoded


def _default_transport(request: Request, timeout: float) -> bytes:
    """Fetch one response with ``timeout`` applied independently per attempt."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        try:
            response_context = urlopen(request, timeout=timeout)
        except OSError as error:
            raise OSError("行情请求失败") from error
        with response_context as response:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except (TypeError, ValueError):
                    declared_length = None
                if (
                    declared_length is not None
                    and declared_length > MAX_RESPONSE_BYTES
                ):
                    raise OSError("行情响应超过大小限制")
            try:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            except OSError as error:
                raise OSError("行情响应读取失败") from error
            if type(body) is not bytes:
                raise OSError("行情响应格式无效")
            if len(body) > MAX_RESPONSE_BYTES:
                raise OSError("行情响应超过大小限制")
            return body
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
        "--max-filesize",
        str(MAX_RESPONSE_BYTES),
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
        except subprocess.TimeoutExpired:
            last_error = "行情请求超时"
        except OSError:
            last_error = "行情请求失败"
        else:
            if len(result.stdout) > MAX_RESPONSE_BYTES:
                last_error = "行情响应超过大小限制"
            elif result.stdout:
                try:
                    json.loads(result.stdout.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                else:
                    return result.stdout
                last_error = "行情响应格式无效"
            else:
                last_error = "行情请求失败"
        if attempt < 2:
            time.sleep(0.2 * (attempt + 1))
    try:
        return _powershell_transport(request, timeout)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        raise OSError(last_error)
