"""Execute an ``http`` hook from settings/session config.

Parity reference: ``src/utils/hooks/execHttpHook.ts``. POSTs the hook
envelope JSON to a configured URL, with explicit header allowlisting and an
SSRF guard.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from core.logging_config import get_logger
from intent.hooks.settings_runner import HookCommand, HookExecutionContext

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 256 * 1024
_ENV_VAR_PATTERN = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_BLOCKED_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",  # nosec B104 - this is a blocked target, not a bind address.
        "::1",
        "169.254.169.254",  # AWS/GCP metadata
        "metadata.google.internal",
    }
)
_BLOCKED_NETWORK_PREFIXES = (
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
)

ExecHttpHook = Callable[[HookCommand, Mapping[str, Any], HookExecutionContext], Awaitable[Any]]


async def default_exec_http(
    hook: HookCommand,
    envelope: Mapping[str, Any],
    context: HookExecutionContext,
) -> Any:
    """POST the hook envelope to ``hook.url`` and parse the response.

    Blocks loopback, link-local, and RFC1918 destinations unless the URL is
    explicitly cloud-tunnelled (no IP check is performed for hostnames that
    aren't IP literals — DNS resolution happens at request time).
    """

    if not hook.url:
        return None

    block_reason = _block_reason(hook.url)
    if block_reason:
        logger.warning("Blocked http hook URL: %s (%s)", hook.url, block_reason)
        return None

    headers = _resolve_headers(hook)
    payload = json.dumps(envelope, sort_keys=True, default=str).encode("utf-8")
    timeout = hook.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS

    try:
        status, body = await asyncio.wait_for(_post_json(hook.url, payload, headers), timeout=timeout)
    except TimeoutError:
        logger.warning("HTTP hook %s timed out after %.0fs", hook.url, timeout)
        return None
    except Exception as exc:
        logger.exception("HTTP hook %s failed", hook.url)
        return None

    if status >= 400:
        logger.warning("HTTP hook %s returned %d", hook.url, status)
        return None

    return _parse_hook_response(body)


def _parse_hook_response(body: str) -> dict[str, Any]:
    stripped = (body or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError("HTTP hook response must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("HTTP hook response must be a JSON object")
    return parsed


def _resolve_headers(hook: HookCommand) -> dict[str, str]:
    allowed = set(hook.allowed_env_vars or ())
    resolved: dict[str, str] = {"content-type": "application/json"}
    for key, raw_value in hook.headers.items():
        resolved[key] = _interpolate_env(raw_value, allowed)
    return resolved


def _interpolate_env(value: str, allowed: set[str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if not name or name not in allowed:
            return ""
        return os.environ.get(name, "")

    return _ENV_VAR_PATTERN.sub(_replace, value)


def _block_reason(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "unsupported scheme: %s" % parsed.scheme
    host = (parsed.hostname or "").lower()
    if not host:
        return "missing host"
    if host in _BLOCKED_HOSTS:
        return "blocked host"
    if any(host.startswith(prefix) for prefix in _BLOCKED_NETWORK_PREFIXES):
        return "private network"
    if host.endswith(".local"):
        return "mDNS .local domain"
    return None


async def _post_json(url: str, payload: bytes, headers: dict[str, str]) -> tuple[int, str]:
    """POST JSON to ``url``. Uses :mod:`httpx` if available, ``urllib`` otherwise."""

    try:
        import httpx
    except ImportError:
        return await _post_with_urllib(url, payload, headers)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, content=payload, headers=headers)
        text = response.text[:_MAX_RESPONSE_BYTES]
        return response.status_code, text


async def _post_with_urllib(url: str, payload: bytes, headers: dict[str, str]) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    loop = asyncio.get_running_loop()

    def _do_post() -> tuple[int, str]:
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(
                request, timeout=10.0
            ) as response:  # nosec B310 - scheme/host validated by _block_reason.
                body = response.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
                return response.status, body
        except urllib.error.HTTPError as exc:
            body = exc.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace") if exc.fp else ""
            return exc.code, body

    return await loop.run_in_executor(None, _do_post)


__all__ = ["ExecHttpHook", "default_exec_http"]
