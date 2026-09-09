"""mss-based desktop screenshot capture and coordinate normalization."""

from __future__ import annotations

import base64
import hashlib
import importlib
import io
from dataclasses import dataclass
from typing import Any, Sequence

from core.logging_config import get_logger
from services.computer_use import LOGICAL_HEIGHT, LOGICAL_WIDTH

logger = get_logger(__name__)


@dataclass(frozen=True)
class MonitorRect:
    """Physical monitor rectangle in desktop coordinates."""

    monitor_id: int
    left: int
    top: int
    width: int
    height: int

    @classmethod
    def from_mss_monitor(cls, monitor_id: int, raw: dict[str, int]) -> MonitorRect:
        return cls(
            monitor_id=monitor_id,
            left=int(raw.get("left", 0)),
            top=int(raw.get("top", 0)),
            width=max(1, int(raw.get("width", 1))),
            height=max(1, int(raw.get("height", 1))),
        )


def logical_to_physical(
    coordinate: Sequence[int],
    monitor: MonitorRect,
    *,
    logical_width: int = LOGICAL_WIDTH,
    logical_height: int = LOGICAL_HEIGHT,
) -> tuple[int, int]:
    """Translate a logical 1024x768 coordinate to a physical monitor coordinate."""
    if len(coordinate) != 2:
        msg = "coordinate must have exactly two values"
        raise ValueError(msg)
    logical_x = max(0, min(logical_width - 1, int(coordinate[0])))
    logical_y = max(0, min(logical_height - 1, int(coordinate[1])))
    physical_x = monitor.left + round((logical_x / max(logical_width - 1, 1)) * (monitor.width - 1))
    physical_y = monitor.top + round((logical_y / max(logical_height - 1, 1)) * (monitor.height - 1))
    return (
        max(monitor.left, min(monitor.left + monitor.width - 1, physical_x)),
        max(monitor.top, min(monitor.top + monitor.height - 1, physical_y)),
    )


def physical_to_logical(
    coordinate: Sequence[int],
    monitor: MonitorRect,
    *,
    logical_width: int = LOGICAL_WIDTH,
    logical_height: int = LOGICAL_HEIGHT,
) -> tuple[int, int]:
    """Translate a physical monitor coordinate to the logical 1024x768 plane."""
    if len(coordinate) != 2:
        msg = "coordinate must have exactly two values"
        raise ValueError(msg)
    physical_x = max(monitor.left, min(monitor.left + monitor.width - 1, int(coordinate[0])))
    physical_y = max(monitor.top, min(monitor.top + monitor.height - 1, int(coordinate[1])))
    logical_x = round(((physical_x - monitor.left) / max(monitor.width - 1, 1)) * (logical_width - 1))
    logical_y = round(((physical_y - monitor.top) / max(monitor.height - 1, 1)) * (logical_height - 1))
    return (
        max(0, min(logical_width - 1, logical_x)),
        max(0, min(logical_height - 1, logical_y)),
    )


def _import_pillow_image() -> Any:
    pil_image = importlib.import_module("PIL.Image")
    return pil_image


def _import_mss() -> Any:
    try:
        return importlib.import_module("mss")
    except ModuleNotFoundError as exc:
        msg = "mss is required for computer-use screenshots"
        raise RuntimeError(msg) from exc


def _all_monitor_rects(raw_monitors: Sequence[dict[str, int]]) -> list[MonitorRect]:
    return [MonitorRect.from_mss_monitor(index, raw) for index, raw in enumerate(raw_monitors[1:], start=1)]


def _monitor_containing_point(monitors: Sequence[MonitorRect], point: tuple[int, int] | None) -> MonitorRect | None:
    if point is None:
        return None
    x, y = point
    for monitor in monitors:
        if monitor.left <= x < monitor.left + monitor.width and monitor.top <= y < monitor.top + monitor.height:
            return monitor
    return None


def get_active_monitor(monitor_id: int | None = None) -> MonitorRect:
    """Return the requested monitor or the monitor containing the foreground window center."""
    mss_module = _import_mss()
    with mss_module.mss() as screenshotter:
        monitors = _all_monitor_rects(screenshotter.monitors)

    if not monitors:
        msg = "No monitors available for computer-use screenshot capture"
        raise RuntimeError(msg)

    if monitor_id is not None:
        for monitor in monitors:
            if monitor.monitor_id == monitor_id:
                return monitor
        msg = "Monitor %s is not available" % monitor_id
        raise ValueError(msg)

    try:
        from services.computer_use.window_manager import get_foreground_window_center

        foreground_center = get_foreground_window_center()
    except Exception:
        logger.debug("Could not resolve foreground monitor; using primary monitor")
        foreground_center = None

    return _monitor_containing_point(monitors, foreground_center) or monitors[0]


def _encode_image(image: Any, image_format: str, quality: int) -> tuple[bytes, str]:
    normalized_format = (image_format or "png").strip().lower()
    buffer = io.BytesIO()
    if normalized_format in {"jpg", "jpeg"}:
        rgb_image = image.convert("RGB")
        rgb_image.save(buffer, format="JPEG", quality=max(1, min(int(quality), 100)), optimize=True)
        return buffer.getvalue(), "image/jpeg"
    image.save(buffer, format="PNG")
    return buffer.getvalue(), "image/png"


def _build_summary(monitor: MonitorRect) -> str:
    try:
        from services.computer_use.window_manager import describe_desktop

        return describe_desktop(monitor)
    except Exception:
        logger.debug("Could not build desktop summary for screenshot")
        return "Foreground: unknown | Active monitor: %d (%dx%d) | Background apps: unknown" % (
            monitor.monitor_id,
            monitor.width,
            monitor.height,
        )


def capture_screenshot(
    *,
    monitor_id: int | None = None,
    image_format: str = "png",
    quality: int = 80,
) -> dict[str, object]:
    """Capture and normalize a desktop screenshot for LLM vision input."""
    monitor = get_active_monitor(monitor_id)
    mss_module = _import_mss()
    image_module = _import_pillow_image()

    raw_monitor = {
        "left": monitor.left,
        "top": monitor.top,
        "width": monitor.width,
        "height": monitor.height,
    }
    with mss_module.mss() as screenshotter:
        shot = screenshotter.grab(raw_monitor)

    image = image_module.frombytes("RGB", shot.size, shot.rgb)
    resampling = getattr(image_module, "Resampling", image_module).LANCZOS
    normalized = image.resize((LOGICAL_WIDTH, LOGICAL_HEIGHT), resampling)
    image_bytes, mime_type = _encode_image(normalized, image_format, quality)
    hash_prefix = hashlib.sha256(image_bytes).hexdigest()[:12]
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    summary = _build_summary(monitor)

    return {
        "ok": True,
        "action": "screenshot",
        "image_base64": image_base64,
        "mime_type": mime_type,
        "logical_width": LOGICAL_WIDTH,
        "logical_height": LOGICAL_HEIGHT,
        "physical_width": monitor.width,
        "physical_height": monitor.height,
        "physical_left": monitor.left,
        "physical_top": monitor.top,
        "monitor_id": monitor.monitor_id,
        "screenshot_hash_prefix": hash_prefix,
        "summary": summary,
        "snapshot": summary,
        "force_image_for_llm": True,
        "title": summary,
        "url": "",
    }


def capture_region(
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    monitor_id: int | None = None,
    image_format: str = "png",
    quality: int = 80,
) -> dict[str, object]:
    """Capture a logical 1024x768 desktop region."""
    monitor = get_active_monitor(monitor_id)
    start_x, start_y = logical_to_physical((x, y), monitor)
    end_x, end_y = logical_to_physical((x + max(1, width), y + max(1, height)), monitor)
    left = min(start_x, end_x)
    top = min(start_y, end_y)
    capture_width = max(1, abs(end_x - start_x))
    capture_height = max(1, abs(end_y - start_y))

    mss_module = _import_mss()
    image_module = _import_pillow_image()
    raw_region = {
        "left": left,
        "top": top,
        "width": capture_width,
        "height": capture_height,
    }
    with mss_module.mss() as screenshotter:
        shot = screenshotter.grab(raw_region)

    image = image_module.frombytes("RGB", shot.size, shot.rgb)
    image_bytes, mime_type = _encode_image(image, image_format, quality)
    hash_prefix = hashlib.sha256(image_bytes).hexdigest()[:12]
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    summary = _build_summary(monitor)
    return {
        "ok": True,
        "action": "observe_region",
        "image_base64": image_base64,
        "mime_type": mime_type,
        "logical_region": {
            "x": int(x),
            "y": int(y),
            "width": int(width),
            "height": int(height),
        },
        "physical_left": left,
        "physical_top": top,
        "physical_width": capture_width,
        "physical_height": capture_height,
        "monitor_id": monitor.monitor_id,
        "screenshot_hash_prefix": hash_prefix,
        "summary": summary,
        "snapshot": summary,
        "force_image_for_llm": True,
        "title": summary,
        "url": "",
    }
