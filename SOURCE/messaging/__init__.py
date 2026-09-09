"""Messaging subsystem — channel-agnostic message ingestion and routing.

Provides the MessageChannel protocol, IncomingMessage model, and platform
adapters for Telegram and Slack.

Access control, deduplication, and format-aware rendering are integrated
at the adapter level — each listener enforces its own owner/allowlist check
and dedup before processing, and each channel applies format conversion on send.
"""

from __future__ import annotations

from messaging.channel import MessageChannel
from messaging.dedup import DedupCache, get_dedup_cache
from messaging.formatters import format_for_channel, get_formatter
from messaging.message import IncomingMessage

__all__ = [
    "DedupCache",
    "IncomingMessage",
    "MessageChannel",
    "format_for_channel",
    "get_dedup_cache",
    "get_formatter",
]
