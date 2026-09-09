"""Hybrid Perception Layer for the Agentic Display.

Combines two complementary perception sources:

1. **Qt screenshots** — OS-level pixel fidelity via ``QWebEngineView.grab()``.
   Captures exactly what the user sees including overlays, popups, and CSS
   effects that CDP screenshots may miss.

2. **Browser accessibility tree** — semantic page structure from the attached
   browser page.  Gives the agent structured knowledge of interactive elements,
   their roles, names, and values without parsing raw HTML.

Together these let the agent both *see* the page visually and *understand*
its structure semantically, enabling accurate click-target identification
and form interaction.

Thread-safety
-------------
``QWebEngineView.grab()`` **must** run on the Qt main thread.  This module
uses ``QCoreApplication.postEvent()`` with a custom ``QEvent`` and receiver
(the proven cross-thread pattern from ``music/providers/browser_search.py``)
to dispatch the grab call, then awaits the result via
``asyncio.to_thread(threading.Event.wait)``.

Usage::

    from services.agent_perception import AgentPerception
    perception = AgentPerception(cdp_client=browser_client, webview=browser_webview)
    snap = await perception.perceive()
    # snap.screenshot_png  -> bytes | None
    # snap.accessibility_tree -> dict | None
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Qt imports (optional — desktop only)
# ---------------------------------------------------------------------------

try:
    from PySide6.QtCore import QBuffer, QCoreApplication, QEvent, QIODevice, QObject

    class _GrabEvent(QEvent):
        """Custom QEvent carrying a grab callable for cross-thread dispatch."""

        _EVENT_TYPE = QEvent.Type(QEvent.registerEventType())

        def __init__(self, fn: object) -> None:
            super().__init__(self._EVENT_TYPE)
            self.fn = fn

    class _GrabReceiver(QObject):
        """Receives ``_GrabEvent`` on the Qt main thread and executes it.

        Created lazily and moved to the application's main thread so that
        ``QCoreApplication.postEvent()`` delivers events there.
        """

        def event(self, event: QEvent) -> bool:
            if isinstance(event, _GrabEvent):
                try:
                    event.fn()  # type: ignore[operator]
                except Exception:
                    logger.exception("AgentPerception: Qt grab event failed")
                return True
            return super().event(event)

    _HAS_QT = True
except ImportError:
    _HAS_QT = False


class BrowserPerceptionClient(Protocol):
    @property
    def connected(self) -> bool: ...

    async def get_url(self) -> str: ...

    async def get_title(self) -> str: ...

    async def screenshot(self) -> bytes: ...

    async def get_accessibility_tree(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GRAB_TIMEOUT = 5.0  # seconds to wait for Qt main thread grab
_TREE_DEPTH_LIMIT = 15  # max depth when flattening a11y tree


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PerceptionSnapshot:
    """Complete perception state at a point in time."""

    screenshot_png: bytes | None = None
    accessibility_tree: dict[str, Any] | None = None
    page_url: str = ""
    page_title: str = ""
    timestamp: float = 0.0
    source: str = ""  # "qt", "cdp", "hybrid", or "none"
    error: str | None = None

    @property
    def has_screenshot(self) -> bool:
        return self.screenshot_png is not None and len(self.screenshot_png) > 0

    @property
    def has_tree(self) -> bool:
        return self.accessibility_tree is not None


@dataclass
class A11yNode:
    """Simplified accessibility tree node for agent consumption."""

    role: str = ""
    name: str = ""
    value: str = ""
    description: str = ""
    node_id: str = ""
    children: list[A11yNode] = field(default_factory=list)
    bounds: dict[str, float] | None = None  # x, y, width, height
    focusable: bool = False
    focused: bool = False


# ---------------------------------------------------------------------------
# AgentPerception
# ---------------------------------------------------------------------------


class AgentPerception:
    """Hybrid perception combining Qt screenshots with CDP accessibility trees.

    Parameters
    ----------
    cdp_client:
        An active browser client for accessibility tree and fallback screenshots.
    webview:
        Reference to the Qt ``QWebEngineView`` (browser overlay) for OS-level
        screenshots.  May be *None* if Qt is not available.
    """

    def __init__(
        self,
        cdp_client: BrowserPerceptionClient | None = None,
        webview: Any | None = None,
    ) -> None:
        self._cdp = cdp_client
        self._webview = webview
        self._receiver: Any | None = None  # _GrabReceiver, lazily created
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def perceive(self) -> PerceptionSnapshot:
        """Capture a full perception snapshot: screenshot + accessibility tree.

        Tries Qt screenshot first (OS-level fidelity), falls back to CDP.
        Always attempts the accessibility tree via CDP.
        """
        t0 = time.monotonic()
        snap = PerceptionSnapshot(timestamp=time.time())

        # Gather screenshot and a11y tree concurrently
        screenshot_coro = self.screenshot()
        tree_coro = self.get_accessibility_tree()

        screenshot_bytes, tree = await asyncio.gather(screenshot_coro, tree_coro, return_exceptions=True)

        # Process screenshot result
        if isinstance(screenshot_bytes, BaseException):
            from intent.log_redaction import redact_diagnostic_payload

            logger.warning("Perception screenshot failed: %s", redact_diagnostic_payload(str(screenshot_bytes)))
            snap.error = str(screenshot_bytes)
        elif screenshot_bytes:
            snap.screenshot_png = screenshot_bytes

        # Process tree result
        if isinstance(tree, BaseException):
            logger.warning("Perception a11y tree failed: %s", tree)
        elif tree:
            snap.accessibility_tree = tree

        # Determine source
        if snap.has_screenshot and snap.has_tree:
            snap.source = "hybrid"
        elif snap.has_screenshot:
            snap.source = "qt" if self._webview is not None else "cdp"
        elif snap.has_tree:
            snap.source = "cdp"
        else:
            snap.source = "none"

        # Get page metadata
        try:
            if self._cdp and self._cdp.connected:
                snap.page_url = await self._cdp.get_url()
                snap.page_title = await self._cdp.get_title()
        except Exception:
            logger.debug("Perception: could not get page metadata")

        elapsed = time.monotonic() - t0
        logger.info(
            "Perception snapshot: source=%s, screenshot=%s, tree=%s (%.1fs)",
            snap.source,
            "yes" if snap.has_screenshot else "no",
            "yes" if snap.has_tree else "no",
            elapsed,
        )
        return snap

    async def screenshot(self) -> bytes | None:
        """Capture a screenshot using the best available method.

        Prefers Qt (OS-level fidelity) over CDP.
        Returns PNG bytes, or *None* if no method is available.
        """
        # Try Qt first
        if self._webview is not None and _HAS_QT:
            result = await self._screenshot_qt()
            if result:
                return result

        # Fall back to CDP
        return await self._screenshot_cdp()

    async def get_accessibility_tree(self) -> dict[str, Any] | None:
        """Get the CDP accessibility tree for the current page.

        Returns the raw CDP accessibility tree dict, or *None* if CDP
        is not available.
        """
        if not self._cdp or not self._cdp.connected:
            return None

        try:
            return await self._cdp.get_accessibility_tree()
        except Exception:
            logger.exception("Perception: CDP a11y tree failed")
            return None

    def flatten_tree(self, raw_tree: dict[str, Any]) -> list[A11yNode]:
        """Convert a raw CDP accessibility tree into a flat list of nodes.

        Useful for presenting the tree to an LLM in a compact text format.
        Filters out ignored/invisible nodes and limits depth.
        """
        nodes_data = raw_tree.get("nodes", [])
        if not nodes_data:
            return []

        result: list[A11yNode] = []
        for node_data in nodes_data:
            role_data = node_data.get("role", {})
            role = role_data.get("value", "") if isinstance(role_data, dict) else str(role_data)

            # Skip ignored and non-interactive invisible nodes
            if role in ("none", "Ignored", "InlineTextBox"):
                continue
            ignored = node_data.get("ignored", False)
            if ignored:
                continue

            name_data = node_data.get("name", {})
            name = name_data.get("value", "") if isinstance(name_data, dict) else str(name_data)

            value_data = node_data.get("value", {})
            value = value_data.get("value", "") if isinstance(value_data, dict) else str(value_data)

            desc_data = node_data.get("description", {})
            desc = desc_data.get("value", "") if isinstance(desc_data, dict) else str(desc_data)

            node = A11yNode(
                role=role,
                name=name,
                value=value,
                description=desc,
                node_id=str(node_data.get("nodeId", "")),
            )

            # Extract properties
            for prop in node_data.get("properties", []):
                prop_name = prop.get("name", "")
                prop_val = prop.get("value", {}).get("value", False)
                if prop_name == "focusable":
                    node.focusable = bool(prop_val)
                elif prop_name == "focused":
                    node.focused = bool(prop_val)

            result.append(node)

        return result

    def tree_to_text(self, raw_tree: dict[str, Any], *, max_nodes: int = 200) -> str:
        """Convert a raw CDP accessibility tree to compact text for LLM consumption.

        Format::

            [role] "name" (value) {nodeId}

        Filters out noise nodes. Truncates at *max_nodes*.
        """
        nodes = self.flatten_tree(raw_tree)
        lines: list[str] = []
        for node in nodes[:max_nodes]:
            parts = [f"[{node.role}]"]
            if node.name:
                parts.append(f'"{node.name}"')
            if node.value:
                parts.append(f"({node.value})")
            if node.focusable:
                parts.append("*focusable*")
            if node.focused:
                parts.append("**focused**")
            parts.append(f"{{{node.node_id}}}")
            lines.append(" ".join(parts))

        header = f"Accessibility tree ({len(nodes)} nodes"
        if len(nodes) > max_nodes:
            header += f", showing first {max_nodes}"
        header += "):"

        return header + "\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def has_qt(self) -> bool:
        """Whether Qt screenshot capability is available."""
        return _HAS_QT and self._webview is not None

    @property
    def has_cdp(self) -> bool:
        """Whether CDP is connected."""
        return self._cdp is not None and self._cdp.connected

    def set_webview(self, webview: Any) -> None:
        """Update the webview reference (e.g. after lazy creation)."""
        self._webview = webview

    def set_cdp_client(self, cdp_client: BrowserPerceptionClient) -> None:
        """Update the browser client reference."""
        self._cdp = cdp_client

    # ------------------------------------------------------------------
    # Qt screenshot (main thread bridge)
    # ------------------------------------------------------------------

    async def _screenshot_qt(self) -> bytes | None:
        """Capture via QWebEngineView.grab() on the Qt main thread.

        Posts a callable to the Qt main thread via ``QCoreApplication.postEvent()``,
        waits for completion via ``threading.Event`` (non-blocking in async via
        ``asyncio.to_thread``).

        Returns PNG bytes or *None* on failure.
        """
        if not _HAS_QT or self._webview is None:
            return None

        result_holder: dict[str, Any] = {}
        done_event = threading.Event()
        webview = self._webview

        def _grab() -> None:
            """Runs on the Qt main thread."""
            try:
                pixmap = webview.grab()
                if pixmap.isNull():
                    result_holder["error"] = "QWebEngineView.grab() returned null pixmap"
                    return

                # Convert QPixmap to PNG bytes
                buf = QBuffer()
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                pixmap.save(buf, "PNG")
                result_holder["data"] = bytes(buf.data())
                buf.close()
            except Exception as exc:
                result_holder["error"] = str(exc)
            finally:
                done_event.set()

        # Post to Qt main thread
        if not self._post_to_main(_grab):
            logger.warning("Perception: could not post grab to Qt main thread")
            return None

        # Wait for result (non-blocking in async)
        try:
            completed = await asyncio.to_thread(done_event.wait, _GRAB_TIMEOUT)
        except Exception:
            logger.exception("Perception: Qt grab wait failed")
            return None

        if not completed:
            logger.warning("Perception: Qt grab timed out after %.1fs", _GRAB_TIMEOUT)
            return None

        if "error" in result_holder:
            logger.warning("Perception: Qt grab error: %s", result_holder["error"])
            return None

        return result_holder.get("data")

    def _post_to_main(self, fn: object) -> bool:
        """Post *fn* to execute on the Qt main thread via ``postEvent``.

        Uses ``QCoreApplication.postEvent()`` with a custom ``_GrabEvent``.
        Returns ``True`` if posted, ``False`` if Qt is not available.
        """
        if not _HAS_QT:
            return False

        with self._lock:
            if self._receiver is None:
                try:
                    app = QCoreApplication.instance()
                    if app is None:
                        logger.warning("Perception: no QCoreApplication instance")
                        return False
                    self._receiver = _GrabReceiver()
                    self._receiver.moveToThread(app.thread())
                    logger.debug("Perception: grab receiver created and moved to main thread")
                except Exception:
                    logger.exception("Perception: failed to create grab receiver")
                    return False

        try:
            QCoreApplication.postEvent(self._receiver, _GrabEvent(fn))
            return True
        except Exception:
            logger.exception("Perception: postEvent failed")
            return False

    # ------------------------------------------------------------------
    # CDP screenshot (fallback)
    # ------------------------------------------------------------------

    async def _screenshot_cdp(self) -> bytes | None:
        """Capture via CDP Page.captureScreenshot."""
        if not self._cdp or not self._cdp.connected:
            return None

        try:
            return await self._cdp.screenshot()
        except Exception:
            logger.exception("Perception: CDP screenshot failed")
            return None


__all__ = [
    "A11yNode",
    "AgentPerception",
    "PerceptionSnapshot",
]
