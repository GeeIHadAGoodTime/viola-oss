from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import Any, cast

from contracts.fastapi_helpers import SafeJSONResponse
from core.logging_config import get_logger
from fastapi import FastAPI

from .api_response import describe_violation, failure_response

logger = get_logger(__name__)

# Health/monitoring probes by convention return ``{"status": "ok"}``-style
# bodies, NOT the canonical ``{"ok": true, "data": ...}`` envelope. They
# must be permanently exempt from envelope validation so probe-driven
# wrappers (Qt startup monitor at ``ui/qt_native/monitor_client.py``, Fly
# health checks, k8s readiness gates, etc.) don't get 500s back and tear
# down the hub.
#
# Pre-2026-05-01 the BaseHTTPMiddleware version of this class silently
# passed health probes through via a ``return response`` body-read-error
# fallback, which masked the fact that ``/health/live`` and
# ``/health/ready`` were never added to the per-app skip_paths lists in
# ``backend/fastapi_app.py`` or ``ui/server.py``. The pure-ASGI rewrite
# (commit ff966465) correctly removed that fallback — it was hiding real
# envelope violations everywhere — which exposed the missing skip entries
# and started crashing the desktop hub on every Qt monitor probe (~30-60s
# loop). Prefix-based skip below makes the exemption permanent across all
# current AND future health endpoints, so adding new probes never
# regresses the hub.
_HEALTH_PROBE_PREFIXES = (
    "/health/",
    "/v1/health/",
    "/api/v1/health/",
    "/monitoring/",
)
_HEALTH_PROBE_EXACT = (
    "/health",
    "/v1/health",
    "/api/v1/health",
)
# Hosted-payment-confirmation routes return a deliberately non-canonical
# envelope (``{"confirmed": true}``, ``{"sessions": [...]}``,
# ``{"cards": [...]}``, the HTML page itself, etc.) by design — they
# predate the canonical ``ok``-envelope contract and have their own
# session-cookie-bound protocol documented in
# ``docs/PAYMENT_CONFIRMATION_FLOW.md``. Forcing them through the
# wrapper turns every successful approve/reject/status/cards call into
# a client-visible 500, which breaks both the hold-to-confirm UI XHR
# and any orchestrator that POSTs to ``/confirm/{token}/approve``
# directly. Exempt the whole ``/confirm/`` prefix permanently.
_PAYMENT_CONFIRM_PREFIXES = ("/confirm/",)
_PAYMENT_CONFIRM_EXACT = ("/confirm", "/confirm-preview", "/confirm-test-session")
_STATIC_ASSET_PREFIXES = ("/static/", "/icons/")
# GoTrue speaks its own Supabase-compatible JSON protocol (token bodies,
# refresh responses, OTP payloads, and error shapes). Desktop and cloud auth
# proxy routes must return that raw shape, not the canonical Viola envelope.
_GOTRUE_AUTH_PREFIXES = ("/auth/v1/",)
_GOTRUE_AUTH_EXACT = ("/auth/v1",)
_COMPRESSED_CONTENT_ENCODINGS = {b"gzip", b"br", b"deflate"}


class _ResponseEnvelopeMiddleware:
    """Validate outgoing JSON responses adhere to the canonical envelope.

    Pure-ASGI (not BaseHTTPMiddleware) to avoid the asyncpg cross-loop bug.
    Buffers the response body via the send wrapper, validates the parsed
    JSON, then either replays the buffered messages or substitutes a 500
    error envelope.

    Health and monitoring probes are skipped unconditionally via
    ``_HEALTH_PROBE_PREFIXES`` / ``_HEALTH_PROBE_EXACT`` so probe-driven
    process supervisors never see envelope-violation 500s. Per-app
    ``skip_paths`` extends that list for any other surfaces (test routes,
    ``/metrics``, ``/openapi.json``, etc.).
    """

    def __init__(
        self,
        app,
        *,
        skip_paths: Sequence[str] | None = None,
    ) -> None:
        self.app = app
        self._skip_paths = tuple(skip_paths or ("/metrics", "/openapi.json", "/docs"))

    def _should_skip(self, path: str) -> bool:
        if not path:
            return False
        if path in self._skip_paths:
            return True
        if path in _HEALTH_PROBE_EXACT or path in _PAYMENT_CONFIRM_EXACT or path in _GOTRUE_AUTH_EXACT:
            return True
        if any(path.startswith(prefix) for prefix in _HEALTH_PROBE_PREFIXES):
            return True
        if any(path.startswith(prefix) for prefix in _STATIC_ASSET_PREFIXES):
            return True
        if any(path.startswith(prefix) for prefix in _GOTRUE_AUTH_PREFIXES):
            return True
        return any(path.startswith(prefix) for prefix in _PAYMENT_CONFIRM_PREFIXES)

    async def __call__(self, scope, receive, send) -> None:
        """Buffer the downstream response, validate, replay or substitute 500."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        if self._should_skip(path):
            await self.app(scope, receive, send)
            return

        # Buffer outgoing messages so we can inspect the full body before
        # forwarding any of them to the real send.
        start_message: dict | None = None
        body_chunks: list[bytes] = []
        finished = False

        async def buffer_send(message: dict) -> None:
            nonlocal start_message, finished
            mtype = message.get("type")
            if mtype == "http.response.start":
                start_message = message
            elif mtype == "http.response.body":
                body_chunks.append(message.get("body", b"") or b"")
                if not message.get("more_body", False):
                    finished = True

        await self.app(scope, receive, buffer_send)

        if start_message is None:
            return

        headers = list(start_message.get("headers", []))
        ctype = b""
        content_encoding = b""
        for name, value in headers:
            lower_name = name.lower()
            if lower_name == b"content-type":
                ctype = value or b""
            elif lower_name == b"content-encoding":
                content_encoding = value or b""

        if b"json" not in ctype.lower():
            await send(start_message)
            await _replay_chunks(send, body_chunks)
            return

        content_encoding_tokens = {token.strip().lower() for token in content_encoding.split(b",") if token.strip()}
        if content_encoding_tokens.intersection(_COMPRESSED_CONTENT_ENCODINGS):
            await send(start_message)
            await _replay_chunks(send, body_chunks)
            return

        full_body = b"".join(body_chunks)
        if start_message.get("status") == 204 or not full_body:
            await send(start_message)
            await _replay_chunks(send, body_chunks)
            return

        try:
            payload = json.loads(full_body)
        except ValueError:
            logger.exception("Response contract violation: invalid JSON payload for %s", path)
            envelope = failure_response(
                "invalid_json",
                "Handler returned malformed JSON.",
            )
            replacement = SafeJSONResponse(status_code=500, content=envelope)
            await replacement(scope, receive, send)
            return

        is_valid, violation = describe_violation(payload)
        if not is_valid:
            logger.error("Response contract violation on %s: %s", path, violation)
            envelope = failure_response(
                "response_contract_violation",
                violation or "Response did not match the canonical envelope.",
            )
            replacement = SafeJSONResponse(status_code=500, content=envelope)
            await replacement(scope, receive, send)
            return

        await send(start_message)
        await _replay_chunks(send, body_chunks)


async def _replay_chunks(send, chunks: list[bytes]) -> None:
    """Forward buffered http.response.body chunks with correct ``more_body`` framing.

    The previous implementation set ``more_body=False`` on every chunk, which
    closed the response after the first chunk and silently dropped the rest.
    Static files >64 KB and any multi-chunk StreamingResponse hit that bug —
    /static/react/assets/*.js truncated at 65536 bytes, /v1/knowledge/*/blob
    returned a 0-byte body to gzip-aware clients, etc. Now ``more_body=True``
    for all chunks except the last, and a single empty terminator runs after
    so an empty buffer still closes the response cleanly.
    """
    if not chunks:
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        return
    last = len(chunks) - 1
    for index, chunk in enumerate(chunks):
        await send(
            {
                "type": "http.response.body",
                "body": chunk,
                "more_body": index < last,
            }
        )


def attach_response_contract(app: FastAPI, *, skip_paths: Iterable[str] | None = None) -> None:
    """Attach middleware enforcing the canonical response envelope."""

    skip = tuple(skip_paths or ())
    existing = getattr(app.state, "_response_contract_attached", False)
    if existing:
        return
    # Note: BaseHTTPMiddleware typing is imperfect in Starlette; use cast to satisfy type checker
    app.add_middleware(cast(Any, _ResponseEnvelopeMiddleware), skip_paths=skip)
    app.state._response_contract_attached = True


__all__ = ["attach_response_contract"]
