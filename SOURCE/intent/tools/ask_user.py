"""Agent tool: ask the user a question mid-task.

Uses the active agent executor's channel (voice or messaging) to ask
a question and wait for the user's response.  Falls back to any
connected messaging channel if voice is unavailable or times out.
"""

from __future__ import annotations

from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# Voice ask timeout (seconds) — shorter because silence is awkward
_VOICE_TIMEOUT = 20.0
# Messaging ask timeout (seconds) — longer for text replies
_MESSAGING_TIMEOUT = 300.0


def _pending_question_result(question: str, channel_type: str = "none") -> ToolResult:
    """Return structured state for a question that must be answered next turn."""
    return ToolResult(
        ok=True,
        data={
            "answer": None,
            "channel": channel_type,
            "needs_user_reply": True,
            "question": question,
            "continue_listening": True,
        },
    )


def _get_agent_channel():
    """Get the channel from the active agent executor, if any.

    Multi-tenant: the active executor lookup is scoped by the ambient
    user context.  Without a current-user ContextVar we cannot prove which
    tenant owns the executor, so we refuse to hand back a stranger's
    channel rather than fall back to "last active".
    """
    try:
        from messaging.channel import get_request_channel

        channel = get_request_channel()
        if channel is not None:
            return channel
    except Exception:
        logger.debug("Failed to get request channel, checking active executor")

    try:
        from core.user_context import get_current_user_id
    except Exception:
        logger.debug("user_context unavailable; ask_user cannot resolve executor channel")
        return None

    try:
        user_id = get_current_user_id()
    except LookupError:
        logger.debug("ask_user: no ambient user_id; refusing cross-tenant channel fallback")
        return None
    except Exception:
        logger.debug("ask_user: user_id resolution failed")
        return None

    try:
        from intent.agent_executor import get_active_executor

        executor = get_active_executor(user_id=user_id)
        if executor is not None:
            return getattr(executor, "_channel", None)
    except Exception:
        logger.debug("Failed to get active executor channel, returning None")
    return None


def _get_messaging_fallback_channel():
    """Find the first active messaging channel (Telegram, Discord, etc.).

    Walks the MessageRouter's listener list to find one with an owner
    channel already created.
    """
    try:
        from ui.server import get_app

        app = get_app()
        if app is None:
            return None
        router = getattr(getattr(app, "state", None), "message_router", None)
        if router is None:
            return None

        # Check each listener for an existing owner channel
        for listener in getattr(router, "_listeners", []):
            # TelegramListener stores channels by chat_id
            channels = getattr(listener, "_channels", {})
            if channels:
                # Return the first channel (owner channel)
                return next(iter(channels.values()), None)
    except Exception as exc:
        logger.debug("Messaging fallback lookup failed: %s", exc)
    return None


async def ask_user_handler(question: str, context: str = "") -> ToolResult:
    """Ask the user a question and wait for their response.

    Use this when you need information to continue a task — a form field
    value, a preference, a decision, or confirmation of an action.

    The question is asked by voice first.  If no voice response is
    received within 20 seconds, the question falls back to the user's
    messaging channel (Telegram, Discord, etc.) with a 5-minute timeout.

    Args:
        question: The question to ask the user.
        context: Brief context for why you're asking (optional, not
                 shown to the user — for your own reference).

    Returns:
        ToolResult with the user's answer, or an error if no response.
    """
    if not question or not question.strip():
        return ToolResult(ok=False, data=None, error="Question cannot be empty.")

    question = question.strip()
    # 1. Try the agent's own channel (voice or messaging)
    channel = _get_agent_channel()
    channel_type = getattr(channel, "channel_type", "unknown") if channel is not None else "unknown"
    if channel is not None:
        if getattr(channel, "active_delivery", True) is False:
            logger.info(
                "ask_user: channel %s cannot wait for an in-request reply; returning pending question",
                channel_type,
            )
            return _pending_question_result(question, channel_type)
        try:
            timeout = _VOICE_TIMEOUT if channel_type == "voice" else _MESSAGING_TIMEOUT
            answer = await channel.ask(question, timeout=timeout)
            if answer and answer.strip():
                logger.info(
                    "ask_user got response via %s (%d chars)",
                    channel_type,
                    len(answer),
                )
                return ToolResult(
                    ok=True,
                    data={"answer": answer.strip(), "channel": channel_type},
                )
            # Voice returned None — fall through to messaging
            if channel_type == "voice":
                logger.info("ask_user: no voice response, trying messaging fallback")
        except Exception as exc:
            logger.debug("ask_user channel.ask failed (%s): %s", channel_type, exc)

    # 2. Fall back to messaging channel — but ONLY if we had a channel
    # (voice) that didn't respond.  When the agent has no channel at all
    # (foreground API task via /v1/command), return pending-question state.
    # Otherwise _get_messaging_fallback_channel() finds Telegram and blocks
    # for 300s — the user is waiting on the HTTP response, not Telegram.
    if channel_type == "voice":
        msg_channel = _get_messaging_fallback_channel()
    else:
        msg_channel = None
    if msg_channel is not None:
        msg_type = getattr(msg_channel, "channel_type", "messaging")
        try:
            fallback_question = (
                "%s\n\n(I tried asking by voice but didn't get a response. "
                "Reply here when you get a chance.)" % question
            )
            answer = await msg_channel.ask(fallback_question, timeout=_MESSAGING_TIMEOUT)
            if answer and answer.strip():
                logger.info(
                    "ask_user got response via %s fallback (%d chars)",
                    msg_type,
                    len(answer),
                )
                return ToolResult(
                    ok=True,
                    data={"answer": answer.strip(), "channel": msg_type},
                )
        except Exception as exc:
            logger.debug("ask_user messaging fallback failed (%s): %s", msg_type, exc)

    if channel is not None:
        return ToolResult(
            ok=False,
            data={"pending_question": question},
            error="no_response",
        )

    # 3. No interactive channel exists. Return structured state for the
    # controller to present as the final user-facing question.
    logger.info("ask_user: no channel available for question: %s", question[:80])
    return _pending_question_result(question)
