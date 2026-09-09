"""routing/startup_gate.py - honest answers while the route table is still filling.

The desktop hub binds its port and marks itself ready as soon as the FastAPI
lifespan finishes, but a large part of the API surface is mounted afterwards by
post-bind background initializers (``diagnostics/startup_telemetry.py``). Until
those finish, a request for a real endpoint - ``POST /v1/transcribe``, the
push-to-talk path, is the one a user can reach in the first seconds - falls off
the end of the router and comes back **404 Not Found**.

404 is a false statement here. It means "there is no such resource", so a
client is right to stop retrying and surface a hard error. The truth is "this
resource is not mounted yet, try again in a moment", which HTTP already has a
code for: **503 Service Unavailable** with ``Retry-After``.

This middleware sits innermost (closest to the router) so it sees the router's
own 404, and rewrites it to 503 **only** while a route-mounting post-bind
initializer is still outstanding. Once the route surface is complete it latches
off and every later request is an untouched pass-through, so there is no steady
-state cost and a genuinely unknown path still gets its honest 404.

A route initializer that *failed* counts as finished: it is never going to
mount its routes, so answering "still starting" forever would just be a
different lie. The failure stays visible in ``/health/startup`` and
``/health/details``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

logger = get_logger(__name__)

DEFAULT_RETRY_AFTER_S = 2

STARTING_ERROR_CODE = "service_starting"
STARTING_MESSAGE = "Viola is still starting up. This feature is not available yet - please retry in a moment."


def _starting_body(pending: list[str], retry_after_s: int) -> bytes:
    """Return the envelope body the response contract enforcer expects."""
    payload: dict[str, Any] = {
        "ok": False,
        "data": None,
        "error": {
            "code": STARTING_ERROR_CODE,
            "message": STARTING_MESSAGE,
            "retry_after_s": retry_after_s,
            "pending_initializers": pending,
        },
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class StartingRouteGateMiddleware:
    """Rewrite router 404s to 503 while post-bind route mounting is in flight."""

    def __init__(
        self,
        app: Any,
        *,
        retry_after_s: int = DEFAULT_RETRY_AFTER_S,
        owner_app: Any = None,
    ) -> None:
        self.app = app
        self.retry_after_s = int(retry_after_s)
        # The FastAPI instance this gate belongs to. The subsystem bookkeeping in
        # startup_telemetry is process-wide, but "are my routes still mounting?"
        # is a question about THIS app: a process that builds several apps (any
        # test module, an embedded host) would otherwise let one app's launch arm
        # every other app's gate and answer 503 for their honest 404s.
        self._owner_app = owner_app
        # Latch: once this app's route surface is complete it never becomes
        # incomplete again, so stop consulting telemetry entirely.
        self._settled = False

    def _launched(self) -> bool:
        """Return True once this app's post-bind initializers were launched.

        Registration is not launch. ``run_post_bind_initializers`` is what puts
        them in flight, and it may never run at all - a host that skips the
        server_factory sequence, or a real server whose readiness wait times out
        before ``core/server_factory.py`` reaches the launch call. Until then
        nothing is in flight, so an unmatched path keeps its honest 404.
        """
        owner = self._owner_app
        if owner is None:
            return False
        try:
            return bool(getattr(owner.state, "_post_bind_initializers_started", False))
        except (AttributeError, RuntimeError):
            return False

    def _pending(self) -> list[str] | None:
        """Return outstanding route initializers, or None when none remain."""
        if self._settled:
            return None
        if not self._launched():
            # Deliberately NOT latched: the launch may still be ahead of us, and
            # settling here would disarm the gate before its window even opens.
            return None
        try:
            from diagnostics.startup_telemetry import pending_route_initializers

            pending = pending_route_initializers()
        except (ImportError, AttributeError, RuntimeError):
            # Telemetry unavailable: latch off rather than rewrite every 404.
            logger.debug("Startup route gate disabled: telemetry unavailable", exc_info=True)
            self._settled = True
            return None
        if not pending:
            self._settled = True
            return None
        return pending

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or self._settled:
            await self.app(scope, receive, send)
            return

        state = {"rewriting": False}

        async def _send(message: Any) -> None:
            message_type = message.get("type")
            if message_type == "http.response.start":
                if int(message.get("status", 0)) != 404:
                    await send(message)
                    return
                pending = self._pending()
                if pending is None:
                    await send(message)
                    return
                state["rewriting"] = True
                body = _starting_body(pending, self.retry_after_s)
                logger.info(
                    "Route not mounted yet, answering 503 instead of 404: path=%s pending=%s",
                    scope.get("path", ""),
                    ",".join(pending),
                )
                await send(
                    {
                        "type": "http.response.start",
                        "status": 503,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"retry-after", str(self.retry_after_s).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return
            if message_type == "http.response.body" and state["rewriting"]:
                # Swallow the original 404 body; ours was already sent.
                return
            await send(message)

        await self.app(scope, receive, _send)


def attach_starting_route_gate(app: FastAPI, *, retry_after_s: int = DEFAULT_RETRY_AFTER_S) -> None:
    """Install the gate as the innermost middleware on ``app``.

    Must be called before any other ``add_middleware`` call on this app:
    Starlette inserts each new middleware at the front of the user list and
    builds the stack from the back, so the first one added ends up closest to
    the router - which is exactly where the 404 is produced.
    """
    app.add_middleware(StartingRouteGateMiddleware, retry_after_s=retry_after_s, owner_app=app)


__all__ = [
    "DEFAULT_RETRY_AFTER_S",
    "STARTING_ERROR_CODE",
    "STARTING_MESSAGE",
    "StartingRouteGateMiddleware",
    "attach_starting_route_gate",
]
