"""Normalized incoming message model.

Every messaging platform converts its native event into an
``IncomingMessage`` before handing it to the intent pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from messaging.channel import MessageChannel


@dataclass
class IncomingMessage:
    """A message received from any channel."""

    text: str
    channel: MessageChannel
    user_id: str = ""
    chat_id: str = ""
    sender_name: str = ""
    reply_to_id: str = ""
    media_url: str = ""
    media_type: str = ""
    chat_type: str = "direct"
    timestamp: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
