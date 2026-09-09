"""CDP Stream Bridge — screencast relay between Playwright page and WebSocket.

Takes a Playwright Page, starts a CDP screencast, and relays JPEG frames
to a callable.  Also translates client input events (mouse, touch, keyboard,
scroll) back to CDP Input domain calls.

This is a dumb pipe: **no** frame data or keystroke values are logged.
"""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from playwright.async_api import CDPSession, Page

logger = get_logger(__name__)

# Screencast defaults
_FORMAT = "jpeg"
_QUALITY = 80
_MAX_WIDTH = 1280
_MAX_HEIGHT = 720


class BrowserStreamBridge:
    """Bridges CDP screencast frames to a WebSocket client.

    Parameters
    ----------
    page : Page
        The Playwright page to screencast.
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._cdp: CDPSession | None = None
        self._running = False
        self.on_frame: Callable[[bytes], Coroutine[Any, Any, None]] | None = None

    async def start(self, session_id: str) -> None:
        """Start CDP screencast on the page.

        Parameters
        ----------
        session_id : str
            Identifier used for lifecycle logging only.
        """
        if self._running:
            return

        self._cdp = await self._page.context.new_cdp_session(self._page)
        self._cdp.on("Page.screencastFrame", self._on_screencast_frame)

        await self._cdp.send(
            "Page.startScreencast",
            {
                "format": _FORMAT,
                "quality": _QUALITY,
                "maxWidth": _MAX_WIDTH,
                "maxHeight": _MAX_HEIGHT,
            },
        )
        self._running = True
        logger.info("Stream bridge started for session %s", session_id)

    # ------------------------------------------------------------------
    # Frame handling (no data logged)
    # ------------------------------------------------------------------

    def _on_screencast_frame(self, params: dict[str, Any]) -> None:
        """Handle incoming screencast frame from CDP."""
        if not self._running:
            return

        # Ack immediately so CDP keeps sending frames
        frame_session_id = params.get("sessionId", 0)
        if self._cdp is not None:
            asyncio.ensure_future(self._ack_frame(frame_session_id))

        # Decode and relay to the WebSocket callback
        data_b64 = params.get("data", "")
        if data_b64 and self.on_frame is not None:
            jpeg_bytes = base64.b64decode(data_b64)
            asyncio.ensure_future(self._relay_frame(jpeg_bytes))

    async def _ack_frame(self, session_id: int) -> None:
        """Acknowledge a screencast frame."""
        if self._cdp is not None:
            try:
                await self._cdp.send(
                    "Page.screencastFrameAck",
                    {
                        "sessionId": session_id,
                    },
                )
            except Exception:
                pass  # Non-fatal: next frame will still arrive

    async def _relay_frame(self, jpeg_bytes: bytes) -> None:
        """Send frame bytes to the WebSocket client callback."""
        if self.on_frame is not None:
            try:
                await self.on_frame(jpeg_bytes)
            except Exception:
                pass  # Client disconnect handled by the WS handler

    # ------------------------------------------------------------------
    # Input relay (no values logged)
    # ------------------------------------------------------------------

    async def relay_input(self, event_data: dict[str, Any]) -> None:
        """Translate a client input JSON message to CDP Input domain calls.

        Supported event types: mouse, touch, key, scroll, text.
        """
        if self._cdp is None or not self._running:
            return

        event_type = event_data.get("type")
        try:
            if event_type == "mouse":
                await self._dispatch_mouse(event_data)
            elif event_type == "touch":
                await self._dispatch_touch(event_data)
            elif event_type == "key":
                await self._dispatch_key(event_data)
            elif event_type == "scroll":
                await self._dispatch_scroll(event_data)
            elif event_type == "text":
                await self._dispatch_text(event_data)
        except Exception:
            pass  # Input relay failures are non-fatal

    async def _dispatch_mouse(self, data: dict[str, Any]) -> None:
        assert self._cdp is not None
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": data.get("action", "mousePressed"),
                "x": data.get("x", 0),
                "y": data.get("y", 0),
                "button": data.get("button", "left"),
                "clickCount": data.get("clickCount", 1),
            },
        )

    async def _dispatch_touch(self, data: dict[str, Any]) -> None:
        assert self._cdp is not None
        touch_points = [{"x": p.get("x", 0), "y": p.get("y", 0)} for p in data.get("touchPoints", [])]
        await self._cdp.send(
            "Input.dispatchTouchEvent",
            {
                "type": data.get("action", "touchStart"),
                "touchPoints": touch_points,
            },
        )

    async def _dispatch_key(self, data: dict[str, Any]) -> None:
        assert self._cdp is not None
        params: dict[str, Any] = {
            "type": data.get("action", "keyDown"),
        }
        if "key" in data:
            params["key"] = data["key"]
        if "code" in data:
            params["code"] = data["code"]
        if "text" in data:
            params["text"] = data["text"]
        # CDP modifier bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8. Required for
        # keyboard shortcuts (Ctrl+A, Cmd+C, ...) to register inside the page.
        modifiers = data.get("modifiers")
        if isinstance(modifiers, int) and modifiers:
            params["modifiers"] = modifiers
        if "windowsVirtualKeyCode" in data:
            params["windowsVirtualKeyCode"] = data["windowsVirtualKeyCode"]
        await self._cdp.send("Input.dispatchKeyEvent", params)

    async def _dispatch_text(self, data: dict[str, Any]) -> None:
        """Type a string into the focused element via CDP ``Input.insertText``.

        ``insertText`` commits the text as if pasted/IME-composed — the correct
        primitive for "type this whole string" rather than per-character key
        events (which miss IME, dead keys, and emoji).
        """
        assert self._cdp is not None
        text = data.get("text")
        if not isinstance(text, str) or not text:
            return
        await self._cdp.send("Input.insertText", {"text": text})

    async def _dispatch_scroll(self, data: dict[str, Any]) -> None:
        assert self._cdp is not None
        await self._cdp.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseWheel",
                "x": data.get("x", 0),
                "y": data.get("y", 0),
                "deltaX": data.get("deltaX", 0),
                "deltaY": data.get("deltaY", 0),
            },
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """Stop screencast and clean up the CDP session."""
        if not self._running:
            return
        self._running = False

        if self._cdp is not None:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None

        logger.info("Stream bridge stopped")

    @property
    def is_running(self) -> bool:
        """Whether the screencast is actively streaming."""
        return self._running
