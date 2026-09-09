"""Messaging diagnostics API routes.

Exposes Telegram conversation history and log tails for programmatic
access — the same data the /debug and /log bot commands return.

Classification: GREEN

.. note::
    Both endpoints require authentication via ``require_auth``.
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import Depends, Query
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox

log = get_logger(__name__)


def _find_telegram_listener(app_state: object) -> object | None:
    """Walk MessageRouter → listeners to find TelegramListener."""
    router = getattr(app_state, "message_router", None)
    if router is None:
        return None
    for listener in getattr(router, "_listeners", []):
        # Avoid hard import — check by class name
        if type(listener).__name__ == "TelegramListener":
            return listener
    return None


def register_messaging_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    router = context.router
    app = context.app

    @router.get("/v1/messaging/telegram/history", dependencies=[Depends(require_auth)])
    async def telegram_history(limit: int = Query(default=20, ge=1, le=200)):
        """Return Telegram conversation history from persistent storage.

        Query params:
            limit: Number of most-recent exchanges to return (default 20, max 200).
        """
        from messaging.channels.telegram import (
            _get_owner_chat_id,
            load_persistent_history,
        )

        owner_id = _get_owner_chat_id()

        exchanges = load_persistent_history(limit=limit)

        # Mask owner chat_id: show first 3 and last 2 digits only
        masked_owner = ""
        if owner_id and len(owner_id) > 5:
            masked_owner = owner_id[:3] + "***" + owner_id[-2:]
        elif owner_id:
            masked_owner = "***"

        return success_response(
            {
                "exchanges": exchanges,
                "exchange_count": len(exchanges),
                "owner_chat_id": masked_owner,
            }
        )

    @router.get("/v1/messaging/telegram/log", dependencies=[Depends(require_auth)])
    async def telegram_log(count: int = Query(default=20, ge=1, le=500)):
        """Return the last N Telegram-related log lines, sanitized.

        Query params:
            count: Number of log lines to return (default 20, max 500).
        """
        from messaging.channels.telegram import get_telegram_log_lines

        lines = get_telegram_log_lines(count)
        if not lines:
            return success_response({"lines": [], "count": 0})

        return success_response({"lines": lines, "count": len(lines)})
