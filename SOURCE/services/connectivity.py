"""Is this computer on the internet? Answered in well under three seconds.

With Wi-Fi off, a command used to walk the whole model path, retries included,
and ended 33 s later on a vague "I couldn't complete that", or on nothing at all
(1.0.5 build 9, High 2). A turn asks here first, so Viola can say so straight away.

Two checks, cheapest first:

* Is there a route out at all? Opening a UDP socket toward a public address
  sends nothing, it only asks the operating system for a route. With Wi-Fi off
  there is none and the answer is instant.
* With a route, can a TCP connection be made to the account service or, failing
  that, to a public address? A network with no way out (router up, line down)
  ends here. A good answer is remembered for a while, so ordinary turns pay
  nothing for this check.

"Offline" needs both targets to fail. Any doubt reads as online, so the normal
path and its own error handling take over.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from urllib.parse import urlparse

from core.logging_config import get_logger

logger = get_logger(__name__)

_PUBLIC_ADDRESSES = ("1.1.1.1", "2606:4700:4700::1111")
_TCP_TIMEOUT_S = 1.5
_ONLINE_REMEMBER_S = 20.0
_OFFLINE_REMEMBER_S = 3.0
_DEFAULT_CLOUD_HOST = "api.useviola.com"

_last_answer: tuple[float, bool] | None = None


def has_route_out() -> bool:
    """True when the operating system has a route to the public internet."""
    for address in _PUBLIC_ADDRESSES:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        try:
            with contextlib.closing(socket.socket(family, socket.SOCK_DGRAM)) as probe:
                probe.connect((address, 53))
                return True
        except OSError:
            continue
    return False


async def _tcp_reaches(host: str, port: int = 443) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=_TCP_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError, asyncio.TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
    return True


def _cloud_host() -> str:
    try:
        from config.settings import settings

        return urlparse(str(getattr(settings, "cloud_url", "") or "")).hostname or _DEFAULT_CLOUD_HOST
    except (ImportError, AttributeError, ValueError):
        return _DEFAULT_CLOUD_HOST


async def _reaches_internet() -> bool:
    checks = [asyncio.ensure_future(_tcp_reaches(host)) for host in (_cloud_host(), _PUBLIC_ADDRESSES[0])]
    try:
        for finished in asyncio.as_completed(checks):
            if await finished:
                return True
        return False
    finally:
        for check in checks:
            check.cancel()


async def is_offline() -> bool:
    """True when this computer cannot reach the internet right now."""
    global _last_answer
    now = time.monotonic()
    if _last_answer is not None:
        checked_at, online = _last_answer
        if now - checked_at < (_ONLINE_REMEMBER_S if online else _OFFLINE_REMEMBER_S):
            # The route check is free, and Wi-Fi going off must not wait for
            # the remembered "online" to run out.
            if online and not has_route_out():
                _last_answer = (now, False)
                return True
            return not online
    online = has_route_out() and await _reaches_internet()
    _last_answer = (now, online)
    if not online:
        logger.info("Connectivity: this computer is offline")
    return not online


def reset_for_tests() -> None:
    global _last_answer
    _last_answer = None
