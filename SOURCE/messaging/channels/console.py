"""Console channel — stdin/stdout channel for development and testing.

Allows testing the full agent + approval flow without any external
messaging platform or voice hardware.
"""

from __future__ import annotations

import asyncio
import sys

from core.logging_config import get_logger

logger = get_logger(__name__)


class ConsoleChannel:
    """MessageChannel backed by stdin/stdout for local development."""

    channel_type = "console"

    async def send(self, text: str) -> None:
        """Print a message to stdout."""
        sys.stdout.write("[Viola]: %s\n" % text)
        sys.stdout.flush()

    async def send_image(self, path: str, caption: str = "") -> None:
        """Print image path to stdout (can't render in terminal)."""
        msg = "[Viola image]: %s" % path
        if caption:
            msg += " — %s" % caption
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    async def ask(self, prompt: str, timeout: float = 60.0) -> str | None:
        """Print *prompt* and read a line from stdin with timeout.

        Returns the user's text, or ``None`` on timeout.
        """
        sys.stdout.write("[Viola asks]: %s\n> " % prompt)
        sys.stdout.flush()
        try:
            loop = asyncio.get_running_loop()
            line = await asyncio.wait_for(
                loop.run_in_executor(None, sys.stdin.readline),
                timeout=timeout,
            )
            text = line.strip() if line else None
            return text or None
        except (TimeoutError, EOFError):
            sys.stdout.write("[timeout — no response]\n")
            sys.stdout.flush()
            return None

    async def send_typing(self) -> None:
        """Print a thinking indicator."""
        sys.stdout.write("[Viola is typing...]\n")
        sys.stdout.flush()
