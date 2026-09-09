"""Web chat API routes.

POST /chat/message  — process a chat message (requires auth)
GET  /chat/history  — retrieve conversation history (requires auth)
GET  /chat          — serve the chat page HTML
"""

from __future__ import annotations

from datetime import datetime

from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from auth.dependencies import CurrentUser
from chat.service import (
    RateLimiter,
    get_unified_history,
    get_unified_history_as_messages,
    get_web_history,
    process_message,
    save_web_exchange,
)
from contracts.fastapi_helpers import envelope_error, envelope_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Query, status

# Per-user chat content (the message body and the cross-channel history
# readout) must never be cached by an intermediate CDN/proxy. Cloudflare
# Tunnel is the launch deployment topology and Cloudflare's default cache
# behavior keys on URL plus the Vary header set — without an explicit
# no-store directive on these per-user JSON responses, a misconfigured
# cache rule could serve user A's conversation in response to user B's
# request. Set the directive on every per-user response below.
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, private, max-age=0",
    "Pragma": "no-cache",
}


def _apply_no_store(response: JSONResponse) -> JSONResponse:
    """Attach the per-user no-store cache directive to a JSON response."""
    for header, value in _NO_STORE_HEADERS.items():
        response.headers[header] = value
    return response


logger = get_logger(__name__)

router = APIRouter(tags=["chat"])

_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatMessageRequest(BaseModel):
    # CROSS-R4 follow-up: forbid unmodeled fields so a spoofed
    # ``conversation_id`` (or any other cross-tenant pivot attempt) is
    # rejected by Pydantic with 422 rather than silently dropped.
    # Chat messages intentionally expose only ``text`` — conversation
    # scoping comes from the authenticated user context, never the body.
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_pool():
    """Get the asyncpg pool from the auth database singleton."""
    from auth.database import get_auth_db

    db = get_auth_db()
    if db is None or not hasattr(db, "_pool") or db._pool is None:
        return None
    return db._pool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/message")
async def send_message(
    request: ChatMessageRequest,
    user: CurrentUser,
):
    """Process a chat message and return the assistant response."""
    # Rate limit: burst only. Customer-facing usage is bounded by managed
    # spend caps, not daily message counts.
    ok, reason = await _rate_limiter.check_burst_async(user.id)
    if not ok:
        raise envelope_error(429, "rate_limited", reason)

    pool = _get_pool()

    # Get cross-channel history for LLM context (Telegram + Web merged)
    # so a conversation started on Telegram continues seamlessly here
    history = None
    if pool is not None:
        try:
            history = await get_unified_history_as_messages(pool, user.id, limit=5)
        except Exception:
            logger.warning("Failed to load chat history for LLM context")

    response_text, source = await process_message(
        text=request.text,
        user_id=user.id,
        history=history,
        channel="web",
    )

    # Persist exchange
    row_id = None
    if pool is not None:
        try:
            row_id = await save_web_exchange(
                pool,
                user_id=user.id,
                user_text=request.text,
                assistant_text=response_text,
                source=source,
            )
        except Exception:
            logger.exception("Failed to save web chat exchange")

    return _apply_no_store(
        envelope_response(
            {
                "id": row_id,
                "response": response_text,
                "source": source,
            }
        )
    )


@router.get("/history")
async def get_history(
    user: CurrentUser,
    limit: int = 20,
    all_channels: bool = True,
    after: str | None = Query(
        None,
        description="ISO-8601 timestamp; return only messages created after this. "
        "Used for incremental polling from the web UI.",
    ),
):
    """Return conversation history for the authenticated user.

    By default returns messages from all channels (web + Telegram) so the
    user sees their full conversation history regardless of where they
    started.  Pass ``all_channels=false`` to show web-only.

    Pass ``after=<ISO timestamp>`` to get only messages newer than that
    timestamp — the web UI polls this every few seconds to pick up
    messages from other tabs, devices, or channels (e.g. Telegram).
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    after_dt = None
    if after:
        try:
            after_dt = datetime.fromisoformat(after)
        except (ValueError, TypeError):
            pass

    pool = _get_pool()
    if pool is None:
        return _apply_no_store(envelope_response({"messages": []}))

    try:
        if all_channels:
            rows = await get_unified_history(
                pool,
                user.id,
                limit=limit,
                after=after_dt,
            )
        else:
            rows = await get_web_history(pool, user.id, limit=limit)
    except Exception:
        logger.exception("Failed to load chat history")
        return _apply_no_store(envelope_response({"messages": []}))

    messages = []
    for row in reversed(rows):  # chronological order
        channel = row.get("channel", "web")
        messages.append(
            {
                "id": row["id"],
                "role": "user",
                "content": row["user_text"],
                "channel": channel,
                "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
            }
        )
        if row["assistant_text"]:
            messages.append(
                {
                    "id": row["id"] + "-a",
                    "role": "assistant",
                    "content": row["assistant_text"],
                    "source": row.get("source"),
                    "channel": channel,
                    "timestamp": row["created_at"].isoformat() if row["created_at"] else None,
                }
            )

    return _apply_no_store(envelope_response({"messages": messages}))


@router.get("", include_in_schema=False)
async def chat_page():
    """Serve the web chat HTML page."""
    from pathlib import Path

    html_path = Path(__file__).resolve().parent.parent / "ui" / "static" / "chat.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Chat page not found")
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
