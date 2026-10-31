"""Warm the LLM client stack in the background right after startup.

The first command after launch paid one-time costs on the asyncio event loop:
the OpenAI SDK imports its ``responses`` resource (and the response models) the
first time ``client.responses`` is read, and every new HTTP client loads the
CA bundle into a fresh SSL context. On the round-7 integration smoke the very
first "play some jazz" spent 3.8 s of loop stalls inside that lazy import
(``_responses_create_with_storage_consent``) before the request was even sent;
later commands did not pay it.

Other one-time costs of the first turn, from the same spans: the core-tools
MCP server module import (it builds a pydantic model per tool at import), the
tokenizer's BPE table load, and the first hardware-vault (NCrypt) unwrap of the
signed-in user's trace key.

``warm_llm_runtime`` does that work once on a background thread (it is a
post-bind initializer, like the "llm" initializer that already builds the
intent stack in the background), so it never delays startup and never runs on
the event loop. Every step is best-effort: a failure only means the first
command pays that cost as before.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlparse

from core.logging_config import get_logger

logger = get_logger(__name__)

_SSL_CONTEXT: ssl.SSLContext | None = None
_SSL_LOCK = threading.Lock()


def shared_ssl_context() -> ssl.SSLContext | None:
    """One verified client SSL context (system trust via httpx defaults), built once.

    Building it loads the CA bundle (0.2 s and more on a busy PC); HTTP clients
    created later reuse it instead of rebuilding it on the event loop.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    with _SSL_LOCK:
        if _SSL_CONTEXT is None:
            try:
                import httpx

                _SSL_CONTEXT = httpx.create_ssl_context()
            except Exception:  # noqa: BLE001, RUF100 - callers fall back to httpx's own default
                logger.debug("Shared SSL context could not be built", exc_info=True)
                return None
        return _SSL_CONTEXT


def _cloud_host() -> str:
    try:
        from config.settings import settings as app_settings

        url = str(getattr(app_settings, "cloud_url", "") or getattr(app_settings, "api_base_url", "") or "")
    except Exception:  # noqa: BLE001, RUF100 - no cloud URL means nothing to resolve
        return ""
    return urlparse(url).hostname or ""


def warm_llm_runtime() -> dict[str, Any]:
    """Import the SDK pieces the first turn needs and pre-build shared TLS state."""
    timings: dict[str, Any] = {}

    def _step(name: str, func: Any) -> None:
        started = time.perf_counter()
        try:
            func()
            timings[name] = round((time.perf_counter() - started) * 1000.0)
        except Exception as exc:  # noqa: BLE001, RUF100 - warm-up is best-effort
            timings[name] = "failed: %s" % type(exc).__name__

    def _openai_sdk() -> None:
        import openai  # noqa: F401
        import openai.resources.chat  # noqa: F401
        import openai.resources.responses  # noqa: F401
        from openai.types.responses import Response  # noqa: F401

    def _ssl() -> None:
        shared_ssl_context()

    def _dns() -> None:
        host = _cloud_host()
        if host:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)

    def _core_tools() -> None:
        import mcp_servers.core_tools.server  # noqa: F401

    def _tokenizer() -> None:
        from intent.token_budget import count_text_tokens

        count_text_tokens("warm up")

    def _trace_key() -> None:
        from core.user_context import get_desktop_authenticated_user_id

        user_id = get_desktop_authenticated_user_id()
        if user_id:
            from services.persistence.trace_keys import KeyProvider

            KeyProvider(user_id).unwrap_trace_key()

    _step("openai_sdk", _openai_sdk)
    _step("ssl_context", _ssl)
    _step("cloud_dns", _dns)
    _step("core_tools", _core_tools)
    _step("bpe_tables", _tokenizer)
    _step("trace_key", _trace_key)
    logger.info("LLM client warm-up done: %s", timings)
    return timings


__all__ = ["shared_ssl_context", "warm_llm_runtime"]
