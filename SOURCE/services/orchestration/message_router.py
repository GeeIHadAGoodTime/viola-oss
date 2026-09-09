"""Inter-agent message routing backed by the orchestration blackboard."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Final

from core.logging_config import get_logger

from . import blackboard

logger = get_logger(__name__)

MESSAGE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    # `(?P<zone>Z)?`: a post-2026-08 board entry is `[author HH:MMZ]` (UTC); a bare
    # `HH:MM` predates that fix. See blackboard.ENTRY_TS_RE for the full rationale.
    r"^- \[(?P<sender>[^\s\]]+) (?P<timestamp>\d{2}:\d{2})(?P<zone>Z)?\] (?P<body>.+)$"
)
DELIVERED_MARKER: Final[str] = "[DELIVERED-TO:"


def send_agent_message(sender: str, target: str, message: str) -> None:
    """Send a blackboard-routed message to a target agent."""

    payload = message if message.lstrip().startswith(f"@{target}") else f"@{target} {message}"
    blackboard.cmd_msg(sender, payload)
    logger.info("Sent orchestration message from %s to %s", sender, target)


def check_agent_messages(agent_codename: str) -> list[dict[str, Any]]:
    """Return newly delivered messages addressed to ``agent_codename``."""

    text = blackboard._cleanup(blackboard._read())
    sections = blackboard._parse_sections(text)
    if "MESSAGES" not in sections:
        return []

    start, end = sections["MESSAGES"]
    section = text[start:end]
    target_pattern = re.compile(r"@" + re.escape(agent_codename) + r"(?:\s|$|[.,;:!?])", re.IGNORECASE)
    messages: list[dict[str, Any]] = []
    updated_text = text

    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        if "[DELIVERED]" in stripped or DELIVERED_MARKER in stripped:
            continue
        if not target_pattern.search(stripped):
            continue

        match = MESSAGE_LINE_RE.match(stripped)
        if match is None:
            continue

        body = match.group("body")
        messages.append(
            {
                "sender": match.group("sender"),
                "target": agent_codename,
                "timestamp": match.group("timestamp"),
                "message": body,
                "raw": stripped,
            }
        )
        updated_text = updated_text.replace(stripped, f"{stripped} [DELIVERED-TO:{agent_codename}]", 1)

    if messages:
        blackboard._write(updated_text)
        logger.info("Delivered %d orchestration messages to %s", len(messages), agent_codename)

    return messages


async def send_agent_message_async(sender: str, target: str, message: str) -> None:
    """Async wrapper for :func:`send_agent_message`."""

    await asyncio.to_thread(send_agent_message, sender, target, message)


async def check_agent_messages_async(agent_codename: str) -> list[dict[str, Any]]:
    """Async wrapper for :func:`check_agent_messages`."""

    return await asyncio.to_thread(check_agent_messages, agent_codename)


__all__ = [
    "check_agent_messages",
    "check_agent_messages_async",
    "send_agent_message",
    "send_agent_message_async",
]
