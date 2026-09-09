"""Input dispatch for the desktop computer-use tool.

Windows drives pywinauto (mouse/keyboard/UIA) exactly as before. macOS
synthesizes events natively through Quartz ``CGEvent`` — mouse via
``CGEventCreateMouseEvent`` + ``CGEventPost`` (kCGHIDEventTap), keyboard via
``CGEventCreateKeyboardEvent`` (keycode map for named keys/chords,
``CGEventKeyboardSetUnicodeString`` for arbitrary text), scroll via
``CGEventCreateScrollWheelEvent``. macOS input synthesis requires the
Accessibility TCC grant; when ``AXIsProcessTrusted`` reports the grant is
missing, these functions raise an honest RuntimeError naming the permission
instead of posting events the OS will silently drop. Other platforms raise a
clear RuntimeError (no input backend) rather than an AttributeError out of
``ctypes.windll`` — the pre-fix macOS crash shape.
"""

from __future__ import annotations

import ctypes
import importlib
import sys
import time
from collections.abc import Sequence

from core.logging_config import get_logger
from services.computer_use.screen import MonitorRect, get_active_monitor, logical_to_physical

logger = get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_IS_DARWIN = sys.platform == "darwin"


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _mouse_module():
    try:
        return importlib.import_module("pywinauto.mouse")
    except ModuleNotFoundError as exc:
        msg = "pywinauto is required for computer-use mouse control"
        raise RuntimeError(msg) from exc


def _keyboard_module():
    try:
        return importlib.import_module("pywinauto.keyboard")
    except ModuleNotFoundError as exc:
        msg = "pywinauto is required for computer-use keyboard control"
        raise RuntimeError(msg) from exc


def _desktop_class():
    try:
        return importlib.import_module("pywinauto").Desktop
    except ModuleNotFoundError as exc:
        msg = "pywinauto is required for computer-use UI Automation control"
        raise RuntimeError(msg) from exc


def _require_windows() -> None:
    """Honest failure on platforms with no input backend (e.g. Linux).

    Reached only after the darwin branch has been taken, so hitting this off
    Windows means there is genuinely no synthesis backend. Raising here keeps
    the failure a documented RuntimeError instead of an AttributeError deep
    inside ``ctypes`` (the pre-fix macOS crash shape) or a misleading
    "pywinauto is required" message on platforms where pywinauto cannot work.
    """
    if not _IS_WINDOWS:
        msg = "computer-use input synthesis requires Windows or macOS (sys.platform=%r)" % sys.platform
        raise RuntimeError(msg)


def translate_coordinate(
    coordinate: Sequence[int],
    *,
    monitor: MonitorRect | None = None,
    monitor_id: int | None = None,
) -> tuple[int, int]:
    """Translate a logical coordinate to physical screen coordinates."""
    selected_monitor = monitor or get_active_monitor(monitor_id)
    return logical_to_physical(coordinate, selected_monitor)


# ---------------------------------------------------------------------------
# macOS (Quartz CGEvent) backend
# ---------------------------------------------------------------------------

_ACCESSIBILITY_HINT = (
    "macOS blocked synthetic input: Viola needs the Accessibility permission "
    "(System Settings > Privacy & Security > Accessibility). Grant it to the "
    "app running Viola and retry."
)

# Maximum UTF-16 code units per CGEventKeyboardSetUnicodeString event.
_DARWIN_TYPE_CHUNK_UTF16_UNITS = 20

# US-layout virtual keycodes (HIToolbox Events.h) for named keys and the
# printable characters reachable without shift. Matches the named-key surface
# the Windows path accepts through _KEY_ALIASES / pywinauto {NAME} syntax.
_DARWIN_KEYCODES: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "`": 50,
    "enter": 36,
    "return": 36,
    "tab": 48,
    "space": 49,
    "backspace": 51,
    "esc": 53,
    "escape": 53,
    "delete": 117,
    "home": 115,
    "end": 119,
    "pageup": 116,
    "pgup": 116,
    "pagedown": 121,
    "pgdn": 121,
    "up": 126,
    "down": 125,
    "left": 123,
    "right": 124,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
}

# Chord modifier names -> canonical macOS modifier. "win"/"meta" map to
# command so cross-platform chords like "win+left" keep their intent.
_DARWIN_MODIFIERS: dict[str, str] = {
    "ctrl": "control",
    "control": "control",
    "alt": "option",
    "option": "option",
    "shift": "shift",
    "win": "command",
    "windows": "command",
    "meta": "command",
    "cmd": "command",
    "command": "command",
}


def _quartz():
    """Import Quartz lazily so non-darwin platforms never require pyobjc."""
    try:
        return importlib.import_module("Quartz")
    except ModuleNotFoundError as exc:
        msg = "pyobjc (Quartz) is required for computer-use input on macOS"
        raise RuntimeError(msg) from exc


def _darwin_accessibility_trusted() -> bool | None:
    """Return the Accessibility TCC grant state, or None when undeterminable."""
    for module_name in ("ApplicationServices", "HIServices"):
        try:
            module = importlib.import_module(module_name)
            trusted = module.AXIsProcessTrusted()
        except Exception:  # noqa: BLE001, S112, RUF100 - best-effort platform guard; must not raise
            continue
        return bool(trusted)
    logger.debug("Could not determine Accessibility trust via AXIsProcessTrusted")
    return None


def _require_darwin_trust() -> None:
    """Refuse to post events the OS would silently drop without Accessibility.

    ``CGEventPost`` has no error return: without the grant the events are
    swallowed and a fake ``ok`` would be reported. Failing honestly here is
    the only way the agent (and the user) learn the actual blocker.
    """
    if _darwin_accessibility_trusted() is False:
        raise RuntimeError(_ACCESSIBILITY_HINT)


def _darwin_post(quartz, event) -> None:
    if event is None:
        msg = "Quartz failed to create the synthetic input event (no graphical session?)"
        raise RuntimeError(msg)
    quartz.CGEventPost(quartz.kCGHIDEventTap, event)


def _darwin_cursor_position() -> tuple[int, int]:
    """Cursor position via CGEventGetLocation (works without the AX grant)."""
    quartz = _quartz()
    location = quartz.CGEventGetLocation(quartz.CGEventCreate(None))
    return round(location.x), round(location.y)


def _darwin_button_events(quartz, button: str) -> tuple[int, int, int, int]:
    """Return (down, up, dragged, cg_button) event constants for a button name."""
    mapping = {
        "left": (
            quartz.kCGEventLeftMouseDown,
            quartz.kCGEventLeftMouseUp,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGMouseButtonLeft,
        ),
        "right": (
            quartz.kCGEventRightMouseDown,
            quartz.kCGEventRightMouseUp,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGMouseButtonRight,
        ),
        "middle": (
            quartz.kCGEventOtherMouseDown,
            quartz.kCGEventOtherMouseUp,
            quartz.kCGEventOtherMouseDragged,
            quartz.kCGMouseButtonCenter,
        ),
    }
    if button not in mapping:
        msg = "unsupported mouse button %r; expected left/right/middle" % button
        raise ValueError(msg)
    return mapping[button]


def _darwin_move_pointer(quartz, point) -> None:
    _darwin_post(
        quartz,
        quartz.CGEventCreateMouseEvent(None, quartz.kCGEventMouseMoved, point, quartz.kCGMouseButtonLeft),
    )


def _darwin_click(coords: tuple[int, int], *, button: str, double: bool) -> None:
    quartz = _quartz()
    down, up, _dragged, cg_button = _darwin_button_events(quartz, button)
    _require_darwin_trust()
    point = quartz.CGPointMake(float(coords[0]), float(coords[1]))
    _darwin_move_pointer(quartz, point)
    clicks = 2 if double else 1
    for click_state in range(1, clicks + 1):
        for event_type in (down, up):
            event = quartz.CGEventCreateMouseEvent(None, event_type, point, cg_button)
            quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventClickState, click_state)
            _darwin_post(quartz, event)
        if click_state < clicks:
            time.sleep(0.05)


def _darwin_move(coords: tuple[int, int]) -> None:
    quartz = _quartz()
    _require_darwin_trust()
    _darwin_move_pointer(quartz, quartz.CGPointMake(float(coords[0]), float(coords[1])))


def _darwin_drag(start: tuple[int, int], end: tuple[int, int], *, duration_ms: int) -> None:
    quartz = _quartz()
    down, up, dragged, cg_button = _darwin_button_events(quartz, "left")
    _require_darwin_trust()
    start_point = quartz.CGPointMake(float(start[0]), float(start[1]))
    _darwin_move_pointer(quartz, start_point)
    _darwin_post(quartz, quartz.CGEventCreateMouseEvent(None, down, start_point, cg_button))
    bounded_ms = max(0, min(int(duration_ms), 30000))
    steps = max(2, min(60, bounded_ms // 16 or 2))
    for step in range(1, steps + 1):
        fraction = step / steps
        point = quartz.CGPointMake(
            float(start[0] + (end[0] - start[0]) * fraction),
            float(start[1] + (end[1] - start[1]) * fraction),
        )
        _darwin_post(quartz, quartz.CGEventCreateMouseEvent(None, dragged, point, cg_button))
        if bounded_ms:
            time.sleep(bounded_ms / steps / 1000)
    end_point = quartz.CGPointMake(float(end[0]), float(end[1]))
    _darwin_post(quartz, quartz.CGEventCreateMouseEvent(None, up, end_point, cg_button))


def _darwin_scroll(coords: tuple[int, int], *, scroll_x: int, scroll_y: int) -> None:
    quartz = _quartz()
    _require_darwin_trust()
    _darwin_move_pointer(quartz, quartz.CGPointMake(float(coords[0]), float(coords[1])))
    if scroll_y or scroll_x:
        # Quartz wheel deltas: positive = up (wheel 1) / left (wheel 2).
        # The tool contract is positive scroll_y = up, positive scroll_x =
        # right, so the horizontal delta flips sign.
        event = quartz.CGEventCreateScrollWheelEvent(
            None,
            quartz.kCGScrollEventUnitLine,
            2,
            int(scroll_y),
            -int(scroll_x),
        )
        _darwin_post(quartz, event)


def _chord_parts(key: str) -> list[str]:
    normalized = str(key or "").strip().lower()
    if not normalized:
        msg = "key must not be empty"
        raise ValueError(msg)
    return [part.strip() for part in normalized.split("+") if part.strip()]


def _darwin_set_unicode_string(quartz, event, text: str) -> None:
    quartz.CGEventKeyboardSetUnicodeString(event, len(text.encode("utf-16-le")) // 2, text)


def _darwin_press_key(key: str) -> None:
    quartz = _quartz()
    parts = _chord_parts(key)
    flags = 0
    for modifier in parts[:-1]:
        canonical = _DARWIN_MODIFIERS.get(modifier)
        if canonical is None:
            msg = "unsupported modifier %r in key %r for macOS input synthesis" % (modifier, key)
            raise ValueError(msg)
        flags |= {
            "control": quartz.kCGEventFlagMaskControl,
            "option": quartz.kCGEventFlagMaskAlternate,
            "shift": quartz.kCGEventFlagMaskShift,
            "command": quartz.kCGEventFlagMaskCommand,
        }[canonical]
    key_part = parts[-1]
    keycode = _DARWIN_KEYCODES.get(key_part)
    if keycode is None:
        if len(key_part) == 1 and not flags:
            _require_darwin_trust()
            _darwin_type_text(key_part)
            return
        msg = "unsupported key %r for macOS input synthesis" % key
        raise ValueError(msg)
    _require_darwin_trust()
    down = quartz.CGEventCreateKeyboardEvent(None, keycode, True)
    up = quartz.CGEventCreateKeyboardEvent(None, keycode, False)
    if flags:
        quartz.CGEventSetFlags(down, flags)
        quartz.CGEventSetFlags(up, flags)
    _darwin_post(quartz, down)
    _darwin_post(quartz, up)


def _darwin_type_text(text: str) -> None:
    quartz = _quartz()
    _require_darwin_trust()
    chunk = ""
    chunk_units = 0
    chunks: list[str] = []
    for char in str(text):
        units = len(char.encode("utf-16-le")) // 2
        if chunk and chunk_units + units > _DARWIN_TYPE_CHUNK_UTF16_UNITS:
            chunks.append(chunk)
            chunk = ""
            chunk_units = 0
        chunk += char
        chunk_units += units
    if chunk:
        chunks.append(chunk)
    for piece in chunks:
        down = quartz.CGEventCreateKeyboardEvent(None, 0, True)
        _darwin_set_unicode_string(quartz, down, piece)
        _darwin_post(quartz, down)
        up = quartz.CGEventCreateKeyboardEvent(None, 0, False)
        _darwin_set_unicode_string(quartz, up, piece)
        _darwin_post(quartz, up)
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Public input surface (platform-dispatched)
# ---------------------------------------------------------------------------


def _current_cursor_position() -> tuple[int, int]:
    if _IS_DARWIN:
        return _darwin_cursor_position()
    _require_windows()
    point = _POINT()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        msg = "Could not read current cursor position"
        raise RuntimeError(msg)
    return int(point.x), int(point.y)


def _semantic_click(coords: tuple[int, int], *, button: str, double: bool) -> bool:
    """Try UIA-backed click at a point before falling back to pixel mouse input."""
    if button not in {"left", "right"}:
        return False
    try:
        element = _desktop_class()(backend="uia").from_point(*coords)
        element.click_input(button=button, double=double)
        return True
    except Exception:
        logger.debug("UIA click at coordinate unavailable; falling back to mouse input")
        return False


def click(
    coordinate: Sequence[int],
    *,
    button: str = "left",
    double: bool = False,
    monitor_id: int | None = None,
) -> dict[str, object]:
    """Click a logical coordinate (pywinauto on Windows, Quartz on macOS)."""
    coords = translate_coordinate(coordinate, monitor_id=monitor_id)
    if _IS_DARWIN:
        _darwin_click(coords, button=button, double=double)
        used_uia = False
    else:
        _require_windows()
        used_uia = _semantic_click(coords, button=button, double=double)
        if not used_uia:
            mouse = _mouse_module()
            if double:
                mouse.double_click(button=button, coords=coords)
            else:
                mouse.click(button=button, coords=coords)
    return {
        "ok": True,
        "physical_coordinate": list(coords),
        "logical_coordinate": [int(coordinate[0]), int(coordinate[1])],
        "button": button,
        "double": double,
        "uia_semantic": used_uia,
    }


def click_physical(
    coordinate: Sequence[int],
    *,
    button: str = "left",
    double: bool = False,
) -> dict[str, object]:
    """Click raw physical/global screen coordinates (no logical translation)."""
    coords = (int(coordinate[0]), int(coordinate[1]))
    if _IS_DARWIN:
        _darwin_click(coords, button=button, double=double)
    else:
        _require_windows()
        mouse = _mouse_module()
        if double:
            mouse.double_click(button=button, coords=coords)
        else:
            mouse.click(button=button, coords=coords)
    return {
        "ok": True,
        "physical_coordinate": [coords[0], coords[1]],
        "button": button,
        "double": double,
    }


def move_mouse(coordinate: Sequence[int], *, monitor_id: int | None = None) -> dict[str, object]:
    """Move the mouse to a logical coordinate."""
    coords = translate_coordinate(coordinate, monitor_id=monitor_id)
    if _IS_DARWIN:
        _darwin_move(coords)
    else:
        _require_windows()
        _mouse_module().move(coords=coords)
    return {
        "ok": True,
        "physical_coordinate": list(coords),
        "logical_coordinate": [int(coordinate[0]), int(coordinate[1])],
    }


def drag(
    coordinate: Sequence[int],
    end_coordinate: Sequence[int],
    *,
    duration_ms: int = 500,
    monitor_id: int | None = None,
) -> dict[str, object]:
    """Drag from one logical coordinate to another."""
    start = translate_coordinate(coordinate, monitor_id=monitor_id)
    end = translate_coordinate(end_coordinate, monitor_id=monitor_id)
    if _IS_DARWIN:
        _darwin_drag(start, end, duration_ms=duration_ms)
    else:
        _require_windows()
        mouse = _mouse_module()
        mouse.move(coords=start)
        mouse.press(button="left", coords=start)
        if duration_ms > 0:
            time.sleep(min(duration_ms, 30000) / 1000)
        mouse.release(button="left", coords=end)
    return {
        "ok": True,
        "physical_start": list(start),
        "physical_end": list(end),
        "logical_start": [int(coordinate[0]), int(coordinate[1])],
        "logical_end": [int(end_coordinate[0]), int(end_coordinate[1])],
    }


def scroll(
    *,
    coordinate: Sequence[int] | None = None,
    scroll_y: int = 0,
    scroll_x: int = 0,
    monitor_id: int | None = None,
) -> dict[str, object]:
    """Scroll at the current pointer or a logical coordinate."""
    used_current_cursor = coordinate is None
    coords = (
        _current_cursor_position() if used_current_cursor else translate_coordinate(coordinate, monitor_id=monitor_id)
    )
    wheel_dist = int(scroll_y)
    horizontal_dist = int(scroll_x)
    if _IS_DARWIN:
        _darwin_scroll(coords, scroll_x=horizontal_dist, scroll_y=wheel_dist)
    else:
        _require_windows()
        mouse = _mouse_module()
        if wheel_dist:
            mouse.scroll(coords=coords, wheel_dist=wheel_dist)
        if horizontal_dist:
            mouse.scroll(coords=coords, wheel_dist=horizontal_dist)
    return {
        "ok": True,
        "physical_coordinate": list(coords),
        "logical_coordinate": [int(coordinate[0]), int(coordinate[1])] if coordinate is not None else None,
        "used_current_cursor": used_current_cursor,
        "scroll_x": horizontal_dist,
        "scroll_y": wheel_dist,
    }


_KEY_ALIASES: dict[str, str] = {
    "enter": "{ENTER}",
    "return": "{ENTER}",
    "tab": "{TAB}",
    "esc": "{ESC}",
    "escape": "{ESC}",
    "backspace": "{BACKSPACE}",
    "delete": "{DELETE}",
    "space": "{SPACE}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
}


def key_to_pywinauto(key: str) -> str:
    """Translate a model key/chord string to pywinauto send_keys syntax."""
    normalized = str(key or "").strip().lower()
    if not normalized:
        msg = "key must not be empty"
        raise ValueError(msg)
    parts = [part.strip().lower() for part in normalized.split("+") if part.strip()]
    if len(parts) == 1:
        return _KEY_ALIASES.get(parts[0], "{%s}" % parts[0].upper() if len(parts[0]) > 1 else parts[0])

    modifiers = ""
    key_part = parts[-1]
    for modifier in parts[:-1]:
        if modifier in {"ctrl", "control"}:
            modifiers += "^"
        elif modifier == "alt":
            modifiers += "%"
        elif modifier == "shift":
            modifiers += "+"
        elif modifier in {"win", "windows", "meta", "cmd"}:
            modifiers += "{VK_LWIN down}"
    translated_key = _KEY_ALIASES.get(key_part, "{%s}" % key_part.upper() if len(key_part) > 1 else key_part)
    if modifiers.endswith("{VK_LWIN down}"):
        return "%s%s{VK_LWIN up}" % (modifiers, translated_key)
    return "%s%s" % (modifiers, translated_key)


def press_key(key: str) -> dict[str, object]:
    """Press a key or chord."""
    if _IS_DARWIN:
        _darwin_press_key(key)
        return {"ok": True, "key": key}
    _require_windows()
    translated = key_to_pywinauto(key)
    _keyboard_module().send_keys(translated)
    return {"ok": True, "key": key}


def type_text(text: str) -> dict[str, object]:
    """Type text into the focused control."""
    if _IS_DARWIN:
        _darwin_type_text(str(text))
        return {"ok": True, "chars_typed": len(text)}
    _require_windows()
    _keyboard_module().send_keys(str(text), with_spaces=True, pause=0.01)
    return {"ok": True, "chars_typed": len(text)}


def wait(duration_ms: int) -> dict[str, object]:
    """Pause between actions."""
    bounded = max(0, min(int(duration_ms), 30000))
    time.sleep(bounded / 1000)
    return {"ok": True, "duration_ms": bounded}
