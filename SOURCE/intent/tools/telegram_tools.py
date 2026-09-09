"""Telegram outbound messaging tools for the agent executor."""

from __future__ import annotations

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)


async def _external_channels_denial() -> ToolResult | None:
    user_id: str | None = None
    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
        if user_id == "default":
            user_id = None
    except LookupError:
        user_id = None
    except Exception:
        logger.exception("telegram user context lookup failed; checking owner control without user scope")
        user_id = None

    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(
            "external_channels",
            user_id=user_id,
            action="telegram_send",
        )
    except Exception:
        logger.exception("telegram owner safety control check failed closed")
        return ToolResult(
            ok=False,
            data=None,
            error="This capability is temporarily paused while safety controls recover.",
        )
    if decision.allowed:
        return None
    return ToolResult(
        ok=False,
        data=None,
        error=decision.public_message or "This capability is temporarily paused for safety.",
    )


async def telegram_send_handler(message: str) -> ToolResult:
    """Send a message to the user via Telegram.

    Uses the configured bot token and owner chat ID to deliver
    a message to the user's Telegram chat.

    Args:
        message: The text message to send.
    """
    if not message or not message.strip():
        return ToolResult(ok=False, data=None, error="Message cannot be empty.")

    owner_denial = await _external_channels_denial()
    if owner_denial is not None:
        return owner_denial

    try:
        from messaging.router import _get_messaging_config

        bot_token = str(_get_messaging_config("telegram_bot_token", "") or "").strip()
        owner_chat_id = str(_get_messaging_config("telegram_owner_chat_id", "") or "").strip()

        if not bot_token:
            return ToolResult(
                ok=False,
                data=None,
                error="Telegram bot token is not configured. Telegram setup is required before sending.",
            )
        if not owner_chat_id:
            return ToolResult(
                ok=False,
                data=None,
                error=(
                    "No Telegram chat registered yet. "
                    "Message the bot on Telegram first to register, "
                    "then I can send messages to you."
                ),
            )

        import httpx

        url = "https://api.telegram.org/bot%s/sendMessage" % bot_token
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": int(owner_chat_id),
                    "text": message.strip(),
                    "parse_mode": "HTML",
                },
            )
            data = resp.json()

        if data.get("ok"):
            return ToolResult(
                ok=True,
                data="Message sent to Telegram successfully.",
            )
        else:
            desc = data.get("description", "Unknown error")
            return ToolResult(ok=False, data=None, error="Telegram API error: %s" % desc)

    except Exception as exc:
        logger.exception("telegram_send_handler failed")
        return ToolResult(ok=False, data=None, error="Failed to send Telegram message: %s" % exc)
