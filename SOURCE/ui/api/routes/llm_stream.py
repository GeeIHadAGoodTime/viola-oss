"""SSE streaming endpoint for LLM responses (B6).

Provides ``GET /api/stream/{task_id}`` which yields Server-Sent Events
as tokens arrive from the LLM provider.  The client connects, tokens
are streamed as they arrive, and the connection closes when the response
is complete.

For non-streaming contexts (voice/TTS), the canonical provider router is used
instead of legacy direct OpenAI handlers.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from http import HTTPStatus
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi.responses import StreamingResponse

from contracts.api_response import failure_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from services.llm.stream_bus import (
    STREAM_TTL_SECONDS as _STREAM_TTL_SECONDS,
    _active_streams,
    _stream_created_at,
    _stream_owners,
    attach_stream_viewer,
    detach_stream_viewer,
    expire_stale_streams,
    get_stream_event_history,
    get_stream_owner,
    get_stream_queue,
    normalize_stream_id,
    register_stream,
    remove_unbound_orphan_stream,
    stream_event_is_terminal,
)

logger = get_logger(__name__)

_SSE_KEEPALIVE_SECONDS = 15.0
_SSE_MAX_STREAM_SECONDS = float(_STREAM_TTL_SECONDS)
_SSE_HEADERS = {
    "Cache-Control": "no-store, no-cache, no-transform",
    "Connection": "keep-alive",
    "Referrer-Policy": "no-referrer",
    "X-Accel-Buffering": "no",
}

__all__ = [
    "_STREAM_TTL_SECONDS",
    "_active_streams",
    "_stream_created_at",
    "_stream_owners",
    "llm_stream_router",
    "router",
]


def _sse_payload(payload: dict[str, object]) -> str:
    return "data: %s\n\n" % json.dumps(payload, ensure_ascii=False)


def _sse_response(payload: dict[str, object], *, status_code: int) -> StreamingResponse:
    async def _single_event():
        yield _sse_payload(payload)

    return StreamingResponse(
        _single_event(),
        media_type="text/event-stream",
        status_code=status_code,
        headers=_SSE_HEADERS,
    )


def _stream_id_from_request(request: Request) -> str:
    stream_id = str(request.path_params.get("stream_id") or "").strip()
    if stream_id:
        return stream_id
    path = str(getattr(request.url, "path", "") or request.scope.get("path", ""))
    marker = "/api/stream/"
    if marker not in path:
        return ""
    return unquote(path.split(marker, 1)[1].split("/", 1)[0]).strip()


async def _resolve_request_user_id(request: Request) -> str | None:
    """Extract authenticated user_id from request or ContextVar.

    Returns None when no authenticated principal is available.
    """
    # 1. FastAPI auth middleware (request.state.user)
    user = getattr(request.state, "user", None)
    if user is not None and getattr(user, "id", None):
        return user.id

    user_context = getattr(request.state, "user_context", None)
    if user_context is not None and getattr(user_context, "user_id", None):
        return user_context.user_id

    # 2. ContextVar (set by auth middleware for async contexts)
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except LookupError:
        pass

    # Browser EventSource cannot attach Authorization headers. Cloud clients
    # exchange their GoTrue bearer token for a short-lived identity-bound token
    # and pass it as a query param on the initial SSE connection.
    stream_claims = _consume_stream_auth_token_from_query(request)
    if stream_claims is not None and getattr(stream_claims, "user_id", None):
        return stream_claims.user_id

    # EventSource cannot send the X-API-Key header. For loopback desktop SSE
    # streams only, accept the already-authenticated query-key path and map it
    # to the same active principal used by header API-key requests.
    query_api_key = request.query_params.get("api_key")
    if query_api_key:
        try:
            # #2646 / M-BILL-1 (#337): prefer the signed-in desktop account over
            # the anonymous device id, mirroring the header API-key path.
            # Signed-out installs still resolve the device id.
            from core.user_context import get_current_or_desktop_active_user_id
            from ui.core.security import is_desktop_surface, is_loopback_request
            from ui.security.config import get_security_config

            config = get_security_config()
            if (
                is_desktop_surface()
                and is_loopback_request(request)
                and config.auth_api_key
                and hmac.compare_digest(query_api_key, config.auth_api_key)
            ):
                return get_current_or_desktop_active_user_id()
        except Exception:
            logger.debug("Stream query API-key principal fallback failed", exc_info=True)
    return None


def _consume_stream_auth_token_from_query(request: Request, stream_id: str | None = None):
    """Verify a short-lived stream token and cache its identity claims."""
    checked = getattr(request.state, "_stream_token_checked", False)
    cached = getattr(request.state, "stream_token_claims", None)
    if checked:
        return cached
    request.state._stream_token_checked = True

    token = (request.query_params.get("stream_token") or "").strip()
    if not token:
        return None
    try:
        auth_plugin = getattr(request.app.state, "auth_plugin", None)
        if auth_plugin is None:
            from ui.security.auth import AuthenticationPlugin
            from ui.security.config import get_security_config

            auth_plugin = AuthenticationPlugin(get_security_config())
        stream_id = stream_id or _stream_id_from_request(request)
        claims = None
        verify_stream_auth_token = getattr(auth_plugin, "verify_stream_auth_token", None)
        if callable(verify_stream_auth_token) and stream_id:
            claims = verify_stream_auth_token(token, stream_id=stream_id)
        if claims is None:
            claims = auth_plugin.consume_ws_auth_token(token)
    except (RuntimeError, TypeError, ValueError, AttributeError, UnicodeDecodeError):
        logger.warning("Stream query token verification failed", exc_info=True)
        return None

    if claims is None:
        return None
    if not getattr(claims, "user_id", None):
        try:
            from services.persistence.state_store import LOCAL_USER_ID
            from ui.core.security import is_desktop_surface, is_loopback_request

            if is_desktop_surface() and is_loopback_request(request):
                claims = SimpleNamespace(
                    user_id=LOCAL_USER_ID,
                    session_id=getattr(claims, "session_id", None),
                )
            else:
                return None
        except (ImportError, RuntimeError, ValueError, AttributeError) as exc:
            logger.debug("Legacy stream token local principal fallback failed: %s", exc)
            return None
    request.state.stream_token_claims = claims
    if not getattr(request.state, "user_context", None):
        request.state.user_context = SimpleNamespace(
            user_id=claims.user_id,
            session_id=getattr(claims, "session_id", None),
        )
    return claims


async def _require_request_user_id(request: Request) -> str:
    """Resolve the authenticated request principal or fail loudly."""
    user_id = await _resolve_request_user_id(request)
    if user_id:
        return user_id

    client_host = request.client.host if request.client is not None else "unknown"
    logger.error(
        "Authenticated stream request missing principal path=%s client=%s",
        request.url.path,
        client_host,
    )
    raise HTTPException(status_code=500, detail="Authenticated request missing principal")


async def _require_stream_auth(request: Request) -> None:
    """Require authentication for LLM streaming endpoints (H5 fix)."""
    try:
        auth_plugin = getattr(request.app.state, "auth_plugin", None)
        if auth_plugin is None:
            from ui.security.auth import AuthenticationPlugin
            from ui.security.config import get_security_config

            auth_plugin = AuthenticationPlugin(get_security_config())
        authenticated = await auth_plugin.verify_request(request)
    except Exception:
        logger.exception("Auth check failed for stream endpoint")
        authenticated = False

    if not authenticated:
        claims = _consume_stream_auth_token_from_query(request)
        if claims is not None and getattr(claims, "user_id", None):
            return
        raise HTTPException(status_code=401, detail="Authentication required")


router = APIRouter(
    tags=["llm_stream"],
    dependencies=[Depends(_require_stream_auth)],
)
llm_stream_router = router


def _stream_error_response(message: str, status_code: int) -> StreamingResponse:
    async def _error():
        yield "data: %s\n\n" % json.dumps({"error": True, "message": message})

    return StreamingResponse(
        _error(),
        media_type="text/event-stream",
        status_code=status_code,
        headers={
            "Cache-Control": "no-store, no-cache, no-transform",
            "Connection": "keep-alive",
            "Referrer-Policy": "no-referrer",
            "X-Accel-Buffering": "no",
        },
    )


def _expire_stale_streams() -> None:
    """Remove streams older than _STREAM_TTL_SECONDS to prevent unbounded growth."""
    expire_stale_streams()


@router.post("/api/stream/start")
async def start_stream(request: Request) -> dict[str, str]:
    """Reject the retired direct-LLM stream starter.

    Streaming responses must originate from ``/v1/command`` or ChatMode so the
    request stays inside the command contract: authenticated user scoping,
    account/spend gates, idempotency, and non-query prompt transport.
    """
    await _require_request_user_id(request)
    raise HTTPException(
        status_code=HTTPStatus.GONE,
        detail=failure_response(
            "legacy_stream_start_disabled",
            "Legacy direct LLM streaming is disabled. Use /v1/command with stream_id or ChatMode.",
        ),
    )


@router.get("/api/stream/{task_id}")
async def stream_events(task_id: str, request: Request) -> StreamingResponse:
    """Stream LLM tokens as Server-Sent Events.

    Each event is formatted as::

        data: {"token": "Hello"}

        data: {"done": true, "content": "Hello world", "tokens_used": 2}

    The stream closes after the ``done`` or ``error`` event.

    Args:
        task_id: A command or ChatMode stream id.
    """
    try:
        stream_id = normalize_stream_id(task_id)
    except ValueError:
        return _stream_error_response("Invalid task_id", HTTPStatus.BAD_REQUEST)
    if stream_id is None:
        return _stream_error_response("Invalid task_id", HTTPStatus.BAD_REQUEST)

    queue = get_stream_queue(stream_id)
    if queue is None:
        if request.query_params.get("create") == "1":
            reader_id = await _require_request_user_id(request)
            try:
                queue = register_stream(stream_id, owner_id=reader_id)
            except PermissionError:
                logger.warning("Rejected pending stream create %s for non-owner user %s", stream_id, reader_id)
                return _stream_error_response("Stream access denied", HTTPStatus.FORBIDDEN)
            except ValueError:
                logger.warning("Failed to create pending stream %s for user %s", stream_id, reader_id)
                return _stream_error_response("Invalid task_id", HTTPStatus.BAD_REQUEST)
        if queue is not None:
            logger.debug("Created pending command stream %s", stream_id)
        else:
            return _stream_error_response("Unknown task_id", HTTPStatus.NOT_FOUND)

    # Verify the authenticated user owns this stream. FAIL CLOSED: a stream with
    # no confirmed owner must never be readable — it could carry another tenant's
    # tokens / reasoning / tool-call events. register_stream() always sets an owner
    # (mandatory owner_id), so an empty owner here is an anomaly that must DENY,
    # not skip the ownership gate (the prior `if owner:` shape was fail-open).
    owner = get_stream_owner(stream_id) or ""
    reader_id = await _require_request_user_id(request)
    if not owner or reader_id != owner:
        logger.warning(
            "Stream access denied: task %s owner=%s requested by %s",
            stream_id,
            owner or "<none>",
            reader_id,
        )
        return _stream_error_response("Stream access denied", HTTPStatus.FORBIDDEN)

    async def _event_generator():
        cursor = 0
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        attach_stream_viewer(stream_id)
        try:
            yield ": connected\n\n"
            while True:
                elapsed = loop.time() - started_at
                if elapsed >= _SSE_MAX_STREAM_SECONDS:
                    yield _sse_payload({"error": True, "message": "Stream timeout"})
                    return

                history = get_stream_event_history(stream_id)
                while cursor < len(history):
                    event = history[cursor]
                    cursor += 1
                    if event is None:
                        return
                    yield _sse_payload(event)
                    if stream_event_is_terminal(event):
                        return

                remaining = max(0.0, _SSE_MAX_STREAM_SECONDS - (loop.time() - started_at))
                wait_seconds = min(_SSE_KEEPALIVE_SECONDS, remaining)
                if wait_seconds <= 0:
                    yield _sse_payload({"error": True, "message": "Stream timeout"})
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    return
        except TimeoutError:
            yield _sse_payload({"error": True, "message": "Stream timeout"})
        finally:
            detach_stream_viewer(stream_id)
            remove_unbound_orphan_stream(stream_id)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
