"""Screen-capture context types for the screen awareness subsystem.

Holds :class:`ScreenContext`, the typed container for a captured screenshot
plus window metadata, used by :mod:`vision.privacy_filter` and
:mod:`vision.context_enrichment`.

The Win32 (``ctypes``/``user32``) capture orchestration that used to live here
(``ScreenCaptureService``) has been removed: it had zero call sites (grep
confirms only its own definition and the ``vision/__init__.py`` re-export
referenced it) and its five ``_user32.*`` calls were unconditional, crashing
with ``AttributeError`` on any non-Windows platform the moment
``ScreenCaptureService`` was constructed -- see issue #2664 (same class as
#2594's ``focus_window``, fixed in PR #2656). The live screen-awareness path
is ``intent/tools/vision_tools.py``, which already has correct win32/darwin
branches; the cloud path is ``services/cloud_vision/screen_share.py``, which
builds its context from browser-uploaded frames via
``vision.context_enrichment.EnrichedContext`` directly. Neither ever routed
through ``ScreenCaptureService``.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

from core.logging_config import get_logger

logger = get_logger(__name__)

try:
    from PIL import Image

    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
    logger.debug("Pillow not available -- ScreenContext.to_base64_jpeg will fail")

# ---------------------------------------------------------------------------
# ScreenContext dataclass
# ---------------------------------------------------------------------------


@dataclass
class ScreenContext:
    """A captured screenshot together with contextual metadata.

    Attributes:
        image: The captured screenshot as a PIL ``Image``.
        window_title: Title of the active window at capture time.
        app_name: Executable name of the owning process.
        url: Browser URL if applicable, otherwise ``None``.
        mouse_x: Cursor X coordinate at capture time.
        mouse_y: Cursor Y coordinate at capture time.
        monitor_index: Index of the monitor used for capture.
    """

    image: Image.Image  # type: ignore[name-defined]
    window_title: str = ""
    app_name: str = ""
    url: str | None = None
    mouse_x: int = 0
    mouse_y: int = 0
    monitor_index: int = 0

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def to_base64_jpeg(self, quality: int = 85, max_width: int = 1920) -> str:
        """Encode the screenshot as a base64 JPEG string.

        The image is down-scaled proportionally if its width exceeds
        *max_width* to control payload size sent to vision LLMs.

        Args:
            quality: JPEG quality (1--100).
            max_width: Maximum width in pixels before down-scaling.

        Returns:
            Base64-encoded JPEG string (no data-URI prefix).

        Raises:
            RuntimeError: If Pillow is not installed.
        """
        if not _HAS_PIL:
            raise RuntimeError(
                "ScreenContext.to_base64_jpeg requires the 'Pillow' package.  Install it with: pip install Pillow"
            )

        img = self.image
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.LANCZOS)  # type: ignore[attr-defined]

        # Convert RGBA -> RGB if needed (JPEG does not support alpha)
        if img.mode in ("RGBA", "LA", "PA"):
            img = img.convert("RGB")

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @property
    def description(self) -> str:
        """Human-readable summary of the capture context."""
        parts: list[str] = []
        if self.app_name:
            parts.append(self.app_name)
        if self.window_title:
            parts.append(self.window_title)
        if self.url:
            parts.append(self.url)
        return " | ".join(parts) if parts else "Unknown context"
