"""Messaging Hub — singleton access to outbound messaging channels.

Provides ``get_messaging_hub()`` which returns a facade over the
``MessageRouter``.  Used by:
  - ``commerce/link_delivery.py`` to deliver payment links
  - Timer/reminder completions to push notifications
  - Proactive messaging (compiled research, morning briefings)

The hub is initialised lazily — callers get ``None`` if no router
has been registered yet (app still booting).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    from messaging.channel import MessageChannel

logger = get_logger(__name__)

_hub_instance: MessagingHub | None = None


class MessagingHub:
    """Facade for outbound messaging across all connected channels."""

    def __init__(self, router: Any) -> None:
        self._router = router

    def get_channel(self, name: str) -> MessageChannel | None:
        """Return a specific channel by platform name (e.g. 'telegram').

        Searches the router's active listeners for one whose
        ``channel_type`` matches *name*.
        """
        for listener in getattr(self._router, "_listeners", []):
            ct = getattr(listener, "channel_type", None)
            if ct and ct == name:
                return listener
        return None

    def active_channels(self) -> list[MessageChannel]:
        """Return all currently active messaging channels."""
        channels: list[MessageChannel] = []
        for listener in getattr(self._router, "_listeners", []):
            if hasattr(listener, "send") and hasattr(listener, "channel_type"):
                channels.append(listener)
        return channels

    @property
    def active_platform_names(self) -> list[str]:
        """Names of platforms that have a running listener."""
        return getattr(self._router, "active_platforms", [])

    async def send(self, channel_name: str, message: str) -> bool:
        """Send a message to a specific channel by name.

        The message is formatted for the target channel's native format
        before sending (Markdown -> HTML for Telegram, mrkdwn for Slack, etc.).

        Returns True if the message was sent, False otherwise.
        """
        ch = self.get_channel(channel_name)
        if ch is None:
            logger.debug("Channel '%s' not available for outbound message", channel_name)
            return False
        try:
            await ch.send(message)
            logger.info("Outbound message sent via %s (%d chars)", channel_name, len(message))
            return True
        except Exception:
            logger.exception("Failed to send outbound message via %s", channel_name)
            return False

    async def broadcast(self, message: str) -> int:
        """Send a message to ALL active channels. Returns count of successful sends.

        Multi-tenant note: MessagingHub channels are device-scoped — each channel
        (Telegram bot, Slack bot, etc.) is configured by the device owner and sends
        to the owner's chat.  This is NOT the EventHub (which requires user_id for
        WebSocket scoping).  MessagingHub.broadcast is safe for device-local use.
        """
        sent = 0
        for ch in self.active_channels():
            try:
                await ch.send(message)
                sent += 1
            except Exception:
                logger.debug("Broadcast failed on %s", getattr(ch, "channel_type", "?"))
        return sent


def register_messaging_hub(router: Any) -> MessagingHub:
    """Register the MessageRouter and return the hub singleton.

    Called once during app startup after the router is created.
    """
    global _hub_instance
    _hub_instance = MessagingHub(router)
    logger.info("Messaging hub registered with router")
    return _hub_instance


def get_messaging_hub() -> MessagingHub | None:
    """Return the messaging hub singleton, or None if not yet registered."""
    return _hub_instance
