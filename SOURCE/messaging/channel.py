"""Channel protocol — the interface every messaging adapter implements.

All messaging platforms (voice, Telegram, Slack, console)
implement this protocol.  The approval system and agent loop use only
this interface, making them completely channel-agnostic.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Generator
from typing import Protocol, runtime_checkable


@runtime_checkable
class MessageChannel(Protocol):
    """Any channel that can exchange messages with a user."""

    @property
    def channel_type(self) -> str:
        """Identifier: 'voice', 'telegram', 'slack', 'console'."""
        ...

    async def send(self, text: str) -> None:
        """Send a text message to the user."""
        ...

    async def send_image(self, path: str, caption: str = "") -> None:
        """Send an image file (screenshots, etc.) to the user."""
        ...

    async def ask(self, prompt: str, timeout: float = 60.0) -> str | None:
        """Ask the user a question and wait for a response.

        Returns the user's text reply, or ``None`` on timeout / failure.
        This is the foundation for approval gates.
        """
        ...

    async def send_typing(self) -> None:
        """Indicate that Viola is processing (typing indicator)."""
        ...


# ---------------------------------------------------------------------------
# Per-request channel selection (multi-tenant isolation — CHAN-R7)
# ---------------------------------------------------------------------------
# ``AIController`` and ``TTSSpeaker`` are shared singletons on a pipeline,
# so concurrent requests from different users race on their instance-level
# ``_channel`` attribute. If user A's agent loop awaits while user B's
# request writes a new channel, user A's subsequent ``self._channel.send``
# delivers to user B's channel — a cross-tenant data leak, not just UX.
# The TTS suppression gate reads ``self._channel.channel_type`` and can
# be similarly flipped for an unrelated user mid-flight.
#
# Resolution: IntentPipeline._process_inner publishes the caller's
# channel via this contextvar; AIController._channel and
# TTSSpeaker._channel are properties that resolve the per-request
# channel first. Contextvars are task-local, so arbitrary asyncio
# interleaving between users cannot expose one user's channel to
# another's agent loop.

_request_channel_cv: contextvars.ContextVar[MessageChannel | None] = contextvars.ContextVar(
    "viola_request_channel",
    default=None,
)


def get_request_channel() -> MessageChannel | None:
    """Return the channel bound for the current request/task, if any."""
    return _request_channel_cv.get()


@contextlib.contextmanager
def use_request_channel(
    channel: MessageChannel | None,
) -> Generator[None, None, None]:
    """Scoped context: bind *channel* as the per-request channel.

    Nested scopes stack correctly — the inner channel wins inside the
    block and the outer is restored on exit.
    """
    token = _request_channel_cv.set(channel)
    try:
        yield
    finally:
        _request_channel_cv.reset(token)


# ---------------------------------------------------------------------------
# Optional button extension
# ---------------------------------------------------------------------------
# Channels that support interactive UI elements (Slack
# Block Kit actions) can add the following opt-in capabilities.  They are
# NOT part of the ``MessageChannel`` protocol so existing adapters remain
# valid without changes.  Callers detect them via ``hasattr()``.
#
#     @property
#     def supports_buttons(self) -> bool:
#         """Return True if the channel supports interactive buttons."""
#         ...
#
#     async def ask_with_buttons(
#         self,
#         prompt: str,
#         buttons: list[str],
#         timeout: float = 60.0,
#     ) -> str | None:
#         """Ask with interactive buttons, returning the chosen label."""
#         ...
