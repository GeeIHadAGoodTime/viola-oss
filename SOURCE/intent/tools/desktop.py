"""Desktop automation tools for the agent (pywinauto + optional pyautogui).

Provides accessibility-driven window interaction, keyboard/mouse
automation, and screenshot capture for the agentic tool-use system.
All synchronous GUI calls are wrapped in ``asyncio.to_thread`` to
avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from importlib import import_module

from core.logging_config import get_logger
from core.platform import get_data_dir
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Dependency availability
# ---------------------------------------------------------------------------

_PYWINAUTO_AVAILABLE = False
_PYAUTOGUI_AVAILABLE = False
_PYAUTOGUI_UNAVAILABLE_REASON = _PYAUTOGUI_REQUIRED_MSG = (
    "Desktop automation requires pyautogui. Run: pip install pyautogui"
)

try:
    import_module("pywinauto")

    _PYWINAUTO_AVAILABLE = True
except ImportError:
    pass

try:
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.1
    _PYAUTOGUI_AVAILABLE = True
except ImportError:
    pass
except Exception as exc:  # noqa: BLE001, RUF100 - see comment below (#2585)
    # Originally `except (KeyError, OSError, RuntimeError)`. Widened after a real
    # Linux-desktop proof session (ticket #2585, a fresh WSLg Ubuntu desktop with
    # no ~/.Xauthority file -- a genuine, reachable X11 configuration, not a
    # container artifact) hit `Xlib.error.XauthError`, which subclasses plain
    # Exception, NOT OSError: pyautogui -> mouseinfo -> Xlib.display.Display()
    # opens `~/.Xauthority` eagerly at import time and raises XauthError when the
    # file is absent. The narrow tuple let that escape past this "degrade
    # gracefully" guard exactly like #1500's SystemExit did, crashing the whole
    # IntentPipeline (and therefore /v1/agents and the conversational command
    # path) before Qt could even show a window. mouseinfo/Xlib's import-time
    # failure surface is unenumerable in practice (it has already produced two
    # unrelated exception shapes); this guard's only job is "pyautogui is an
    # OPTIONAL capability that must never take down the app," so it now catches
    # every Exception subclass and relies on the separate `except SystemExit`
    # below for the one BaseException case.
    _PYAUTOGUI_UNAVAILABLE_REASON = "Desktop automation requires an available graphical display for pyautogui: %s" % exc
except SystemExit as exc:
    # pyautogui's `mouseinfo` dependency calls sys.exit() AT IMPORT TIME (not
    # ImportError/OSError/RuntimeError) when tkinter is unavailable -- "You
    # must install tkinter on Linux to use MouseInfo". viola.spec deliberately
    # excludes tkinter from the frozen bundle (Viola's own UI is 100% Qt, no
    # Tk usage anywhere), so this fires on every Linux boot: config.settings
    # -> logging_config -> telephony -> this module -> pyautogui -> mouseinfo.
    # Desktop-automation tools are optional (this module already treats a
    # missing pyautogui as a degraded-but-non-fatal capability everywhere
    # else via _PYAUTOGUI_AVAILABLE/_check_pyautogui()); a third-party
    # dependency's overzealous import-time sys.exit() must not be allowed to
    # crash the entire application before Qt even starts (#1500 -- traced via
    # the real crash traceback after viola_qt.py's own _fatal_boot() was
    # fixed to catch BaseException instead of Exception for this identical
    # class of bug).
    _PYAUTOGUI_UNAVAILABLE_REASON = "Desktop automation's pyautogui dependency could not initialize: %s" % exc

# ---------------------------------------------------------------------------
# Safety patterns
# ---------------------------------------------------------------------------

_CREDIT_CARD_RE = re.compile(r"\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}")
_SSN_RE = re.compile(r"\d{3}-\d{2}-\d{4}")

_MAX_ELEMENTS = 300
_MAX_CHARS = 12000
_MAX_NAME_LEN = 240

# Truncation priority for read_window. Lower number = kept first under cap
# pressure. Buttons / Hyperlinks / MenuItems are the actionable surface;
# Group / Pane / Custom containers are layout scaffolding and can be
# dropped first when the JSON budget is tight. This keeps "Add to Cart"-
# style buttons visible even when the page is dense.
_INTERACTIVITY_PRIORITY: dict[str, int] = {
    "Button": 0,
    "Hyperlink": 0,
    "Link": 0,
    "MenuItem": 1,
    "Edit": 1,
    "ComboBox": 1,
    "CheckBox": 1,
    "RadioButton": 1,
    "TabItem": 2,
    "ListItem": 2,
    "Tree": 2,
    "TreeItem": 2,
    "DataItem": 2,
    "Document": 3,
    "Text": 3,
    "Image": 4,
    "Group": 5,
    "Pane": 5,
    "Custom": 5,
}
_DEFAULT_INTERACTIVITY_PRIORITY = 4


def _element_priority(elem: dict) -> int:
    """Return truncation priority — lower means more important (kept first)."""
    return _INTERACTIVITY_PRIORITY.get(str(elem.get("type") or ""), _DEFAULT_INTERACTIVITY_PRIORITY)


_PYWINAUTO_REQUIRED_MSG = "Desktop automation requires pywinauto. Run: pip install pywinauto"
_KEYBOARD_REQUIRED_MSG = "Desktop keyboard automation requires pywinauto or pyautogui"
_MOUSE_REQUIRED_MSG = "Desktop mouse automation requires pywinauto or pyautogui"

# ---------------------------------------------------------------------------
# Honest-outcome vocabulary
# ---------------------------------------------------------------------------
#
# Input synthesis splits into two questions that have different answers.
#
#   1. Did the injection boundary accept the event? KNOWABLE. Win32
#      ``SendInput`` returns the accepted count and ``GetLastError`` says
#      why it refused; ``intent.tools.desktop_input_low`` now returns both.
#      A refusal is a real failure and must surface as one.
#
#   2. Did the event reach the application and do something? NOT KNOWABLE
#      from any injection-side call. ``SendInput`` has no channel back from
#      the target's message pump, ``keybd_event`` returns nothing at all,
#      and pywinauto's ``send_chars`` discards SendMessage's reply. An app
#      with no focused text field swallows keystrokes in silence.
#
# So a sync helper here marks a payload ``unverified`` when it submitted
# input it could not observe landing. The async wrappers below copy that
# onto ``ToolResult.unverified``, which is the type's third answer next to
# ok=True and ok=False: "this ran, and I cannot establish the outcome."
# Rounding it up to success is how a tool ends up telling a user something
# happened that nobody checked.

_UNVERIFIED_KEY = "unverified"
_UNVERIFIED_REASON_KEY = "unverified_reason"

# Why each blind path is blind, in words the model can pass to the user.
_REASON_ACCEPTED_NOT_ARRIVAL = (
    "Windows accepted the event, which is as far as input synthesis can see; nothing observed "
    "the target application receive or act on it"
)
_REASON_SEND_KEYS = (
    "keystrokes were handed to Windows, but nothing observed the target application receive them "
    "(pywinauto send_keys drives virtual keys through keybd_event, which returns no result at all)"
)
_REASON_SEND_CHARS = (
    "WM_CHAR messages were posted to the target window, but nothing observed the application act on "
    "them (pywinauto send_chars discards SendMessage's reply, and a window with no focused text field "
    "accepts and ignores them)"
)
_REASON_MOUSE = "the click was submitted to the OS, but nothing observed the target window react to it"
_REASON_CLIPBOARD = "the paste chord was submitted, but nothing observed the target application paste anything"


def _mark_unverified(payload: dict, reason: str) -> dict:
    """Flag a payload as submitted-but-unobserved, keeping any earlier reason."""
    payload[_UNVERIFIED_KEY] = True
    payload.setdefault(_UNVERIFIED_REASON_KEY, reason)
    return payload


def _dispatch_payload(dispatch: object) -> dict:
    """Turn an ``InputDispatch`` into the payload fields callers can read.

    A caller that gets no dispatch at all (an older stub, a monkeypatched
    fake) has no evidence either way, so it is reported as unverified --
    never as success.
    """
    requested = getattr(dispatch, "requested", None)
    accepted = getattr(dispatch, "accepted", None)
    if requested is None or accepted is None:
        return {
            _UNVERIFIED_KEY: True,
            _UNVERIFIED_REASON_KEY: "the input backend returned no delivery information",
        }
    payload: dict = {"events_requested": int(requested), "events_accepted": int(accepted)}
    if not dispatch.submitted:
        payload["error"] = dispatch.refusal_reason() or "Windows refused the synthesized input"
        return payload
    blind = getattr(dispatch, "blind_reason", None)
    if blind:
        _mark_unverified(payload, blind)
    else:
        # Accepted at the boundary, and that is the whole of what an
        # injection-side call can know: SendInput has no channel back from
        # the target's message pump, so "the app received it" is not on
        # offer. The MCP twin (_dispatch_envelope in
        # mcp_servers/computer_use/server.py) already said exactly this,
        # while this path returned a plain verified success -- so the same
        # keystroke through the same boundary came back unverified on one
        # surface and confirmed on the other. desktop_type_text is the tool
        # the "I typed your message into Notepad" claim is built from, so
        # this was the one place the branch's own thesis was not applied.
        _mark_unverified(payload, _REASON_ACCEPTED_NOT_ARRIVAL)
    return payload


# macOS types/clicks natively through Quartz CGEvent (services.computer_use
# .input); pywinauto/pyautogui are not required there. The pyautogui fallback
# is deliberately NOT used on macOS: without the Accessibility grant it posts
# events the OS silently drops (fake success), while the native path raises
# an honest error naming the missing permission.
_DARWIN_NATIVE_INPUT = sys.platform == "darwin"


def _check_pywinauto() -> str | None:
    """Return an error string if pywinauto is unavailable, else None."""
    if not _PYWINAUTO_AVAILABLE:
        return _PYWINAUTO_REQUIRED_MSG
    return None


def _check_pyautogui() -> str | None:
    """Return an error string if pyautogui is unavailable, else None."""
    if not _PYAUTOGUI_AVAILABLE:
        return _PYAUTOGUI_UNAVAILABLE_REASON
    return None


def _check_keyboard_dep() -> str | None:
    """Return an error string if no keyboard automation backend is available."""
    if _DARWIN_NATIVE_INPUT:
        return None
    if not _PYWINAUTO_AVAILABLE and not _PYAUTOGUI_AVAILABLE:
        return _KEYBOARD_REQUIRED_MSG
    return None


def _check_mouse_dep() -> str | None:
    """Return an error string if no mouse automation backend is available."""
    if _DARWIN_NATIVE_INPUT:
        return None
    if not _PYWINAUTO_AVAILABLE and not _PYAUTOGUI_AVAILABLE:
        return _MOUSE_REQUIRED_MSG
    return None


def _pywinauto_keyboard_module():
    return import_module("pywinauto.keyboard")


def _pywinauto_mouse_module():
    return import_module("pywinauto.mouse")


def _hwnd_int(hwnd: object) -> int:
    """Normalize ctypes HWND values and plain ints to an integer handle."""
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


def _get_foreground_input_hwnds() -> dict[str, int]:
    """Return the foreground window and focused child hwnd, when Windows exposes them."""
    import ctypes
    from ctypes import wintypes

    class _Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class _GuiThreadInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", _Rect),
        ]

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)]
    user32.GetGUIThreadInfo.restype = wintypes.BOOL

    foreground_hwnd = _hwnd_int(user32.GetForegroundWindow())
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        # GetGUIThreadInfo failing is "Windows would not tell us where focus
        # is", which is a different fact from "there is no focused control".
        # Pre-fix both came back as focus_hwnd=0, so the caller below could
        # not distinguish them and quietly aimed text at the foreground
        # window instead. Say which one it was.
        logger.debug("GetGUIThreadInfo failed; focus/active handles are unknown, not absent")
        return {
            "foreground_hwnd": foreground_hwnd,
            "focus_hwnd": 0,
            "active_hwnd": 0,
            "focus_known": False,
        }

    return {
        "foreground_hwnd": foreground_hwnd,
        "focus_hwnd": _hwnd_int(info.hwndFocus),
        "active_hwnd": _hwnd_int(info.hwndActive),
        "focus_known": True,
    }


def _get_root_hwnd(hwnd: int) -> int:
    import ctypes
    from ctypes import wintypes

    if not hwnd:
        return 0
    ga_root = 2
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    return _hwnd_int(user32.GetAncestor(hwnd, ga_root)) or hwnd


def _hwnd_belongs_to_window(child_hwnd: int, window_hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes

    if not child_hwnd or not window_hwnd:
        return False
    if child_hwnd == window_hwnd or _get_root_hwnd(child_hwnd) == window_hwnd:
        return True

    user32 = ctypes.windll.user32
    user32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
    user32.IsChild.restype = wintypes.BOOL
    return bool(user32.IsChild(window_hwnd, child_hwnd))


def _hwnd_wrapper(hwnd: int):
    from pywinauto.controls.hwndwrapper import HwndWrapper
    from pywinauto.win32_element_info import HwndElementInfo

    return HwndWrapper(HwndElementInfo(hwnd))


def _resolve_no_focus_type_target(window_title: str = "") -> tuple[object, int, str]:
    """Resolve the hwnd that should receive direct text without changing focus."""
    if window_title and window_title.strip():
        wrapper = _resolve_window(window_title)
        window_hwnd = _hwnd_int(wrapper.handle)
        foreground = _get_foreground_input_hwnds()
        focus_hwnd = foreground.get("focus_hwnd") or 0
        if _hwnd_belongs_to_window(focus_hwnd, window_hwnd):
            return (
                _hwnd_wrapper(focus_hwnd),
                focus_hwnd,
                "window_title_foreground_focus",
            )
        return _hwnd_wrapper(window_hwnd), window_hwnd, "window_title"

    foreground = _get_foreground_input_hwnds()
    focus_hwnd = foreground.get("focus_hwnd") or 0
    if focus_hwnd:
        return _hwnd_wrapper(focus_hwnd), focus_hwnd, "foreground_focus"

    foreground_hwnd = foreground.get("foreground_hwnd") or 0
    if foreground_hwnd:
        # Name WHY we fell back to the top-level window: Windows refused to
        # report focus, or it genuinely reported none. Both land the text
        # somewhere other than a known focused control, and the caller
        # records the distinction rather than smoothing it away.
        source = "foreground_window" if foreground.get("focus_known", True) else "foreground_window_focus_unknown"
        return _hwnd_wrapper(foreground_hwnd), foreground_hwnd, source

    raise RuntimeError("No foreground window is available for desktop typing")


def _type_text_with_pywinauto_foreground(text: str) -> dict:
    # Try SendInput+scancode path first. It generates WM_INPUT raw input AND
    # WM_KEYDOWN, so it reaches both raw-input apps (Minecraft Java in-game,
    # FPS games, anything using GLFW raw input) and message-queue apps.
    # pywinauto's send_keys uses User32 keybd_event which only generates
    # WM_KEYDOWN — fails silently against raw-input handlers (the bug behind
    # trace 0226750ea085 where every chat keystroke registered at the OS but
    # never reached Minecraft).
    try:
        from intent.tools import desktop_input_low

        dispatch = desktop_input_low.type_text(str(text))
        logger.info("desktop_type_text path=sendinput_scancode chars=%d", len(text))
        payload = _dispatch_payload(dispatch)
        if "error" in payload:
            # Windows refused the keystrokes -- the ordinary outcome when the
            # foreground window belongs to a higher-integrity process (UIPI)
            # or BlockInput is active. Pre-fix this returned
            # {"typed": len(text)} built from the REQUESTED string, so a type
            # that produced not one character on screen was reported as a
            # clean success. Falling through to pywinauto here would be
            # wrong too: send_keys is the same SendInput boundary and would
            # be refused identically, or worse, partially succeed and
            # duplicate what did land.
            payload["method"] = "sendinput_scancode"
            return payload
        payload["typed"] = len(text)
        payload["method"] = "sendinput_scancode"
        return payload
    except (ValueError, RuntimeError) as exc:
        # ValueError: unsupported char (Unicode beyond ASCII printable).
        # RuntimeError: non-Windows platform (shouldn't happen in this path).
        logger.info(
            "sendinput_scancode unavailable for text (%s); falling back to pywinauto",
            exc,
        )

    _pywinauto_keyboard_module().send_keys(
        str(text),
        with_spaces=True,
        with_tabs=True,
        with_newlines=True,
        pause=0.01,
    )
    logger.info("desktop_type_text path=pywinauto_send_keys chars=%d", len(text))
    # send_keys raises RuntimeError when its own SendInput call is short, so
    # reaching here means nothing was refused at the boundary. It still
    # proves nothing about arrival, and its virtual-key actions run through
    # keybd_event, which reports nothing whatsoever.
    return _mark_unverified(
        {"typed": len(text), "method": "pywinauto_send_keys"},
        _REASON_SEND_KEYS,
    )


def _type_text_without_stealing_focus(text: str, window_title: str = "") -> dict:
    wrapper, hwnd, target_source = _resolve_no_focus_type_target(window_title)
    try:
        wrapper.send_chars(str(text), with_spaces=True, with_tabs=True, with_newlines=True)
        method = "pywinauto_send_chars"
    except Exception as exc:
        logger.info(
            "desktop_type_text send_chars failed; path=pywinauto_type_keys_no_foreground hwnd=%s source=%s error=%s",
            hwnd,
            target_source,
            exc,
        )
        wrapper.type_keys(
            str(text),
            with_spaces=True,
            with_tabs=True,
            with_newlines=True,
            set_foreground=False,
            pause=0.01,
        )
        method = "pywinauto_type_keys_no_foreground"

    logger.info(
        "desktop_type_text path=%s hwnd=%s source=%s chars=%d",
        method,
        hwnd,
        target_source,
        len(text),
    )
    # Neither pywinauto path reports an outcome: send_chars throws away the
    # SendMessage reply, and type_keys(set_foreground=False) returns nothing.
    # "typed" is therefore the length of what we ASKED for, and saying so is
    # the difference between a count and a claim.
    return _mark_unverified(
        {
            "typed": len(text),
            "method": method,
            "respect_focus": True,
            "target_hwnd": hwnd,
            "target_source": target_source,
        },
        _REASON_SEND_CHARS,
    )


def desktop_click_element_unavailable_reason() -> str | None:
    """Return why desktop_click_element cannot run, if applicable."""
    # Element lookup is always driven by pywinauto; pyautogui is only a
    # coordinate fallback once a matching UIA element has been found.
    return _check_pywinauto()


def _resolve_window(title: str):
    """Find a single top-level window matching *title*, handling ambiguity.

    Fast path (microseconds): Win32 EnumWindows + GetWindowTextW substring
    match -> wrap the hwnd via HwndWrapper. This avoids the 10-20s UIA
    Desktop().windows() enumeration that used to dominate every screenshot,
    type, and click against a window-region target.

    Fallback (seconds): the original UIA enumeration. Triggered when the
    Win32 path returns nothing, which happens for child windows that aren't
    top-level or for substring matches that need pattern matching beyond
    GetWindowTextW.

    Returns a pywinauto wrapper object.
    """
    from pywinauto.findwindows import ElementNotFoundError

    # Fast path first.
    try:
        from services.computer_use.window_manager import _resolve_hwnd_fast

        hwnd = _resolve_hwnd_fast(title)
        if hwnd:
            try:
                return _hwnd_wrapper(int(hwnd))
            except Exception as exc:
                logger.debug(
                    "HwndWrapper for fast-resolved hwnd %s failed (%s); UIA fallback",
                    hwnd,
                    exc,
                )
    except ImportError:
        pass  # window_manager not available; UIA only

    # UIA fallback for cases the fast path can't handle (child windows,
    # complex pattern matches, executable-name matches).
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    pattern = re.compile(".*%s.*" % re.escape(title), re.IGNORECASE)

    matches = []
    for win in desktop.windows():
        try:
            win_title = win.window_text()
            if win_title and pattern.match(win_title):
                matches.append(win)
        except (OSError, RuntimeError, AttributeError):
            continue

    if not matches:
        raise ElementNotFoundError("No window found matching '%s'" % title)

    # Prefer the active window among matches
    for m in matches:
        try:
            if m.is_active():
                return m
        except (OSError, RuntimeError):
            continue

    return matches[0]


# ---------------------------------------------------------------------------
# Sync helper implementations
# ---------------------------------------------------------------------------


def _list_windows_sync() -> list[dict]:
    """List all visible windows (synchronous)."""
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    windows = desktop.windows()
    result = []
    for win in windows:
        try:
            title = win.window_text()
            if not title or not title.strip():
                continue
            result.append(
                {
                    "title": title,
                    "process": win.element_info.name,
                    "handle": win.handle,
                    "is_active": win.is_active(),
                }
            )
        except (OSError, RuntimeError, AttributeError) as exc:
            # Windows may close or become inaccessible during enumeration
            logger.debug("Skipping inaccessible window: %s", exc)
            continue
    return result


def _focus_window_sync(title: str) -> dict:
    """Bring a window to the foreground by partial title match (synchronous).

    ``set_focus()`` returns the wrapper, never whether Windows honoured the
    request -- and Windows refuses it routinely, since the caller must
    already own the foreground or have been last to receive input. Pre-fix
    this returned ``{"focused": <title>}`` regardless, which reads as "it is
    now in front" for a window that never moved. Reading back
    ``GetForegroundWindow`` costs microseconds and settles it.
    """
    wrapper = _resolve_window(title)
    target_hwnd = _hwnd_int(wrapper.handle)
    wrapper.set_focus()

    actual_hwnd = 0
    try:
        import ctypes

        actual_hwnd = _hwnd_int(ctypes.windll.user32.GetForegroundWindow())
    except (AttributeError, OSError, ValueError):
        logger.debug("Could not read the foreground window while verifying focus for %r", title)

    payload = {"focused": wrapper.window_text(), "handle": wrapper.handle}
    if actual_hwnd and target_hwnd and actual_hwnd == target_hwnd:
        payload["foreground_verified"] = True
        return payload
    if not actual_hwnd:
        return _mark_unverified(
            payload,
            "the focus request was submitted but Windows would not report which window is in the "
            "foreground, so anything typed or clicked next may go elsewhere",
        )
    return {
        "error": "Windows kept a different window (hwnd %d) in the foreground instead of '%s'; "
        "anything typed or clicked now would go to the wrong window." % (actual_hwnd, title)
    }


def _read_window_sync(title: str = "") -> dict:
    """Read the accessibility tree of a window (synchronous)."""
    if title:
        wrapper = _resolve_window(title)
    else:
        # Get the currently focused (foreground) window
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        from pywinauto import Application

        app = Application(backend="uia").connect(handle=hwnd)
        wrapper = app.window(handle=hwnd).wrapper_object()

    window_title = wrapper.window_text()

    elements: list[dict] = []
    interesting_types = {
        "Button",
        "Edit",
        "MenuItem",
        "TabItem",
        "ListItem",
        "ComboBox",
        "CheckBox",
        "RadioButton",
    }

    for child in wrapper.descendants():
        name = child.window_text()
        control_type = child.element_info.control_type
        if name or control_type in interesting_types:
            # Truncate long names (e.g. Document elements with full file text)
            if name and len(name) > _MAX_NAME_LEN:
                name = name[:_MAX_NAME_LEN] + "..."
            elem: dict = {
                "name": name,
                "type": control_type,
                "enabled": child.is_enabled(),
            }
            elements.append(elem)
            if len(elements) >= _MAX_ELEMENTS:
                break

    # Priority-aware truncation: when JSON exceeds the cap, drop low-
    # interactivity elements (Pane / Group / Custom) before clickable ones
    # (Button / Hyperlink / MenuItem). Pre-fix dropped from the END of the
    # tree, throwing away Steam Store buttons buried under page scaffolding.
    # Re-sort kept elements into original tree order so the model still
    # reads the page top-to-bottom.
    import json

    serialised = json.dumps(elements, default=str, ensure_ascii=False)
    truncated = False
    if len(serialised) > _MAX_CHARS:
        indexed = list(enumerate(elements))
        indexed.sort(key=lambda pair: (_element_priority(pair[1]), pair[0]))
        kept: list[tuple[int, dict]] = []
        for orig_idx, elem in indexed:
            kept.append((orig_idx, elem))
            if len(json.dumps([e for _, e in kept], default=str, ensure_ascii=False)) > _MAX_CHARS:
                kept.pop()
                truncated = True
                break
        kept.sort(key=lambda pair: pair[0])
        elements = [e for _, e in kept]

    return {
        "window_title": window_title,
        "elements": elements,
        "count": len(elements),
        "truncated": truncated,
    }


_VISION_LOCATE_PROMPT = (
    "You are a precise UI-element locator. Look at the screenshot and find "
    "the element described as: %s. Reply with ONLY the integer pixel "
    "coordinates of the element's center, formatted exactly as 'x,y' (no "
    "spaces, no labels, no extra text). If the element is not visible, "
    "reply with exactly 'NOT_FOUND' and nothing else."
)
_VISION_COORD_RE = re.compile(r"\b(\d{1,5})\s*,\s*(\d{1,5})\b")


def _foreground_window_origin() -> tuple[int, int] | None:
    """Return (left, top) of the current foreground window in screen coords.

    The vision fallback below captures with ``capture_mode="active_window"``,
    which crops the screenshot to this same window (see
    ``vision_tools._win_active_window_rect``). The vision LLM's returned
    pixel coordinates are therefore relative to this origin, not the
    screen's — callers MUST add it before clicking at absolute screen
    coordinates.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        # Use wintypes.RECT (not a locally-defined lookalike struct):
        # services.computer_use.window_manager sets
        # ``user32.GetWindowRect.argtypes = [c_void_p, POINTER(wintypes.RECT)]``
        # at import time, and that argtypes assignment is process-global (the
        # underlying ctypes function pointer is shared across every caller of
        # ``ctypes.windll.user32.GetWindowRect``). Passing a byref() to a
        # different-but-structurally-identical Structure subclass fails
        # ctypes' strict POINTER-type check with ``ArgumentError: expected
        # LP_RECT instance instead of pointer to _Rect`` once that other
        # module has run anywhere earlier in the process — which is normal
        # in a real session (any click/focus/read_window call imports it).
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return int(rect.left), int(rect.top)
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        logger.debug("Could not resolve foreground window origin for vision click offset")
        return None


def _vision_locate_element_sync(name: str) -> tuple[int, int] | None:
    """Last-resort UIA fallback: ask the vision LLM for the element's pixel center.

    Used when all UIA strategies (exact-interactive, exact-leaf, partial,
    container) fail. Common case: CEF-embedded webviews like Steam where
    buttons exist on screen but aren't surfaced via UIA accessibility names.
    Returns ABSOLUTE screen-pixel coordinates (window origin already added),
    or None.
    """
    try:
        import asyncio

        from intent.tools.vision_tools import analyze_screen
    except Exception:
        logger.debug("Vision tools unavailable for click fallback")
        return None

    prompt = _VISION_LOCATE_PROMPT % name
    try:
        result = asyncio.run(analyze_screen(question=prompt, capture_mode="active_window"))
    except RuntimeError:
        logger.debug("Cannot run vision fallback inside an existing event loop")
        return None
    except Exception:
        logger.exception("Vision fallback errored while locating '%s'", name)
        return None

    if not isinstance(result, dict) or not result.get("success"):
        return None
    message = str(result.get("message") or "").strip()
    if not message or "NOT_FOUND" in message.upper():
        return None
    match = _VISION_COORD_RE.search(message)
    if not match:
        logger.debug("Vision fallback for '%s' returned non-coord text: %s", name, message[:100])
        return None
    try:
        rel_x, rel_y = int(match.group(1)), int(match.group(2))
    except ValueError:
        return None

    # capture_mode="active_window" cropped the screenshot to the foreground
    # window, so the vision LLM's coordinates are window-relative. Pre-fix,
    # the caller clicked these as if they were already absolute screen
    # coordinates — only correct for a maximized/borderless window sitting
    # at (0, 0) (#2773 item 2). Add the window's screen origin here so every
    # caller of this function gets absolute coordinates for free.
    origin = _foreground_window_origin()
    if origin is None:
        logger.debug("Vision fallback located '%s' but window origin is unknown; refusing to click blind", name)
        return None
    origin_x, origin_y = origin
    return rel_x + origin_x, rel_y + origin_y


def _click_descendant(element, name: str, ctrl: str) -> dict | None:
    """Try every meaningful click path on a found UIA element.

    Pre-fix bug observed live in trace 001a99f81873: pywinauto's
    `click_input()` raises on Steam Store CEF-rendered Link elements
    even though UIA has the element rect. The OLD pipeline aborted
    with "Failed to click element 'X': 'Y'" — no rect-coord fallback.
    The agent then retries the same name expecting different results.

    Order:
      1. UIA `Invoke` pattern  — silent, no mouse movement, works for any
         control implementing InvokePattern (most native Buttons, Menus,
         Links). Doesn't steal focus.
      2. `click_input()` — pywinauto's UIA-driven click (moves cursor).
      3. Coordinate click on element rect center — bypasses UIA click
         logic when the element is found but click_input fails.

    Returns dict with method on success, None on total failure.
    """
    # 1. UIA Invoke (silent, no focus steal). This is the ONE path here that
    # carries real evidence: the invoke crosses into the target process and
    # its own accessibility provider runs the control's handler, raising if
    # the pattern is unsupported. Nothing below it observes anything.
    try:
        element.invoke()
        return {"clicked": name, "method": "uia_invoke_%s" % ctrl}
    except Exception:
        pass

    # 2. click_input (UIA-driven mouse click)
    try:
        element.click_input()
        return _mark_unverified({"clicked": name, "method": "accessibility_%s" % ctrl}, _REASON_MOUSE)
    except Exception as exc:
        logger.debug("click_input failed for '%s' (%s): %s", name, ctrl, exc)

    # 3. Coordinate click on element rect — last resort but reliable
    try:
        rect = element.rectangle()
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        if _PYWINAUTO_AVAILABLE:
            _pywinauto_mouse_module().click(button="left", coords=(cx, cy))
            return _mark_unverified(
                {
                    "clicked": name,
                    "method": "rect_coord_fallback_%s" % ctrl,
                    "x": cx,
                    "y": cy,
                },
                _REASON_MOUSE,
            )
        if _PYAUTOGUI_AVAILABLE:
            import pyautogui

            pyautogui.click(cx, cy)
            return _mark_unverified(
                {
                    "clicked": name,
                    "method": "rect_coord_fallback_%s" % ctrl,
                    "x": cx,
                    "y": cy,
                },
                _REASON_MOUSE,
            )
    except Exception as exc:
        logger.debug("rect-coord click failed for '%s' (%s): %s", name, ctrl, exc)

    return None


def _click_element_sync(name: str, window_title: str = "") -> dict:
    """Click a UI element by accessibility name (synchronous).

    Robust strategy chain (each path tries Invoke → click_input → rect-coord):
      1. Single tree walk + exact-title match, sorted by interactive priority.
      2. Single tree walk + partial-title regex match (case-insensitive).
      3. Vision-LLM fallback — ask vision model for the element's pixel
         center and click there. Triggered only when no UIA element matches.

    Pre-fix bugs this addresses:
      - Strategy 2 (old) iterated 6 separate `child_window(title=name,
        control_type=X)` calls — each rewalked the UIA tree from scratch
        (~12s on failure). Now one walk + in-memory filter.
      - Old code aborted with "Failed to click 'X': 'Y'" when click_input
        raised on a found element. Now falls back to UIA Invoke and
        rect-coord click before giving up.
    """
    from pywinauto import Application

    if window_title:
        wrapper_resolved = _resolve_window(window_title)
        hwnd = wrapper_resolved.handle
    else:
        import ctypes

        hwnd = ctypes.windll.user32.GetForegroundWindow()

    app = Application(backend="uia").connect(handle=hwnd)
    window_spec = app.window(handle=hwnd)

    # Single tree walk — collect every descendant once, classify by control type.
    # CRITICAL: name-match must EXCLUDE container types whose window_text()
    # aggregates descendant text. Pre-fix bug observed live in trace
    # d487101b6647 step 8: click(name='Add to Cart') matched a CEF Document
    # element whose text was the concatenation of every child on the Steam
    # Store page (including 'Add to Cart' inside a child Button). The
    # Document's center rect is the middle of the page — clicking there
    # registered on whitespace, NOT the actual button. click_input doesn't
    # fail (Documents accept clicks), so the bug looked like success but
    # Hades II never landed in the cart.
    interactive_priority = (
        "Button",
        "Hyperlink",
        "Link",
        "MenuItem",
        "TabItem",
        "ListItem",
        "CheckBox",
        "RadioButton",
        "Edit",
    )
    priority_idx = {t: i for i, t in enumerate(interactive_priority)}
    container_types = frozenset({"Document", "Pane", "Group", "Custom", "Window"})

    try:
        wrapper = window_spec.wrapper_object()
        all_descendants = wrapper.descendants()
    except Exception:
        logger.exception("UIA tree walk failed for window")
        all_descendants = []

    name_lower = name.casefold()

    def _ctrl(child) -> str:
        return str(getattr(child.element_info, "control_type", "") or "")

    def _is_interactive(child) -> bool:
        return _ctrl(child) in priority_idx

    def _is_container(child) -> bool:
        return _ctrl(child) in container_types

    def _score(child) -> int:
        return priority_idx.get(_ctrl(child), len(interactive_priority))

    # Strategy 1: exact title against an INTERACTIVE control (Button etc.).
    exact_interactive = [c for c in all_descendants if _is_interactive(c) and c.window_text() == name]
    if not exact_interactive:
        exact_interactive = [
            c for c in all_descendants if _is_interactive(c) and c.window_text().casefold() == name_lower
        ]
    exact_interactive.sort(key=_score)
    for cand in exact_interactive:
        result = _click_descendant(cand, name, _ctrl(cand) or "Unknown")
        if result is not None:
            return result

    # Strategy 2: exact title against a non-container leaf (Text/Image/etc).
    exact_leaf = [
        c for c in all_descendants if not _is_container(c) and not _is_interactive(c) and c.window_text() == name
    ]
    for cand in exact_leaf:
        result = _click_descendant(cand, name, _ctrl(cand) or "Unknown")
        if result is not None:
            return result

    # Strategy 3: partial title match against interactive controls.
    partial_interactive = [
        c for c in all_descendants if _is_interactive(c) and name_lower in c.window_text().casefold()
    ]
    partial_interactive.sort(key=_score)
    for cand in partial_interactive:
        result = _click_descendant(cand, name, _ctrl(cand) or "Unknown")
        if result is not None:
            return result

    # Strategy 4: container last-resort — flagged in trace as suspect because
    # the click center is unlikely to hit the real target.
    container_matches = [c for c in all_descendants if _is_container(c) and c.window_text() == name]
    if container_matches:
        logger.warning(
            "click(name=%r) falling back to %s container — likely a phantom click on whitespace",
            name,
            _ctrl(container_matches[0]),
        )
        for cand in container_matches:
            result = _click_descendant(cand, name, _ctrl(cand) or "Unknown")
            if result is not None:
                result["method"] = result.get("method", "container_fallback") + "_suspect_phantom"
                result["warning"] = (
                    "Clicked %s container — center may not be the intended target. "
                    "Try click(coordinate=[x,y]) using the coords from analyze_screen instead." % _ctrl(cand)
                )
                return result

    # Strategy 5: vision-LLM coordinate fallback. Triggered only when no UIA
    # element matches at all (CEF-embedded surfaces with no accessibility
    # name exposed). Asks the vision model for the element's pixel center.
    # _vision_locate_element_sync returns ABSOLUTE screen coordinates (the
    # window-relative vision result plus the window's screen origin), so
    # these are safe to click directly.
    coords = _vision_locate_element_sync(name)
    if coords is not None:
        cx, cy = coords
        # Doubly unobserved: a vision model GUESSED the pixel and nothing
        # checked the click landed on the thing it named.
        vision_reason = (
            "the coordinates came from a vision model's reading of a screenshot and nothing observed "
            "the click land on the named element"
        )
        if _PYWINAUTO_AVAILABLE:
            _pywinauto_mouse_module().click(button="left", coords=(cx, cy))
            return _mark_unverified(
                {
                    "clicked": name,
                    "method": "vision_coordinate_fallback",
                    "x": cx,
                    "y": cy,
                },
                vision_reason,
            )
        if _PYAUTOGUI_AVAILABLE:
            import pyautogui

            pyautogui.click(cx, cy)
            return _mark_unverified(
                {
                    "clicked": name,
                    "method": "vision_coordinate_fallback",
                    "x": cx,
                    "y": cy,
                },
                vision_reason,
            )

    return {"error": "Could not find element '%s'" % name}


def _type_text_sync(text: str, respect_focus: bool = True, window_title: str = "") -> dict:
    """Type text at the current cursor position (synchronous)."""
    if _DARWIN_NATIVE_INPUT and not _PYWINAUTO_AVAILABLE:
        from services.computer_use import input as computer_input

        # The Quartz path refuses honestly when the Accessibility grant is
        # missing (services/computer_use/input.py::_require_darwin_trust), so
        # getting here means the events were posted. CGEventPost has no error
        # return and no channel back from the target app, so posted is all we
        # know.
        result = computer_input.type_text(text)
        return _mark_unverified(
            {"typed": int(result.get("chars_typed", len(text))), "method": "quartz_unicode"},
            "the events were posted via CGEventPost, which has no error return and no channel back "
            "from the target application",
        )

    if _PYWINAUTO_AVAILABLE:
        if respect_focus:
            try:
                return _type_text_without_stealing_focus(text, window_title)
            except Exception as exc:
                if window_title:
                    # A specific target window was requested (commonly because
                    # the caller wants text NOT to land in whatever else is
                    # focused). Falling back to foreground input here would
                    # silently type into a different window and report
                    # success (#2773 item 4) — fail closed instead.
                    logger.warning(
                        "desktop_type_text could not resolve/type into window_title=%r; "
                        "refusing to fall back to the foreground window: %s",
                        window_title,
                        exc,
                    )
                    return {
                        "error": "Could not type into window '%s': %s" % (window_title, exc),
                    }
                logger.info(
                    "desktop_type_text no-focus path unavailable; falling back to foreground input: %s",
                    exc,
                )
        return _type_text_with_pywinauto_foreground(text)

    import pyautogui

    # Attempt ASCII-only fast path
    try:
        pyautogui.write(text, interval=0.02)
        return _mark_unverified(
            {"typed": len(text), "method": "write"},
            "pyautogui.write returns nothing, so the keystrokes were submitted with no report of "
            "whether the OS accepted them or the application received them",
        )
    except (ValueError, UnicodeEncodeError) as exc:
        logger.debug("ASCII write failed, falling back to clipboard paste: %s", exc)

    # Unicode fallback: clipboard paste
    try:
        import pyperclip

        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        return _mark_unverified(
            {"typed": len(text), "method": "clipboard_paste"},
            _REASON_CLIPBOARD,
        )
    except ImportError:
        return {"error": "Cannot type unicode text without pyperclip. Run: pip install pyperclip"}


def _hotkey_sync(keys: str) -> dict:
    """Press a keyboard shortcut (synchronous)."""
    parts = [k.strip().lower() for k in keys.split("+")]

    if _DARWIN_NATIVE_INPUT and not _PYWINAUTO_AVAILABLE:
        from services.computer_use import input as computer_input

        computer_input.press_key(keys)
        return _mark_unverified(
            {"pressed": keys, "keys": parts, "method": "quartz_cgevent"},
            "the chord was posted via CGEventPost, which has no error return and no channel back from "
            "the target application",
        )

    # Try SendInput+scancode path first. Generates WM_INPUT raw input + WM_KEYDOWN
    # so it reaches raw-input apps (Minecraft Java in-game, GLFW/DirectInput
    # games) AND message-queue apps. pywinauto's send_keys uses User32
    # keybd_event which only generates WM_KEYDOWN — silent failure on
    # raw-input handlers (trace 0226750ea085: in-game chat keystrokes never
    # reached Minecraft).
    try:
        from intent.tools import desktop_input_low

        dispatch = desktop_input_low.key_chord(keys)
        payload = _dispatch_payload(dispatch)
        payload["keys"] = parts
        payload["method"] = "sendinput_scancode"
        if "error" in payload:
            # A refused chord is a real failure, and a PARTIALLY refused one
            # is worse than nothing: "ctrl" refused while "s" landed types a
            # stray character into the user's document. Either way, pre-fix
            # this returned {"pressed": keys} echoing the request.
            return payload
        payload["pressed"] = keys
        return payload
    except (ValueError, RuntimeError) as exc:
        # ValueError: unsupported key name (not in our scancode table).
        # RuntimeError: non-Windows platform.
        logger.info("sendinput_scancode unavailable for chord %r (%s); falling back", keys, exc)

    if _PYWINAUTO_AVAILABLE:
        from services.computer_use.input import key_to_pywinauto

        _pywinauto_keyboard_module().send_keys(key_to_pywinauto(keys))
        # A chord is virtual keys, and pywinauto drives those through
        # win32api.keybd_event, which returns nothing -- not even a count.
        # This path cannot tell a delivered ctrl+s from a discarded one.
        return _mark_unverified(
            {"pressed": keys, "keys": parts, "method": "pywinauto_send_keys"},
            _REASON_SEND_KEYS,
        )

    import pyautogui

    pyautogui.hotkey(*parts)
    return _mark_unverified(
        {"pressed": keys, "keys": parts},
        "pyautogui.hotkey returns nothing, so the chord was submitted with no report of whether the "
        "OS accepted it or the application received it",
    )


def _screenshot_sync(region: str = "full") -> dict:
    """Take a screenshot (synchronous)."""
    import pyautogui

    screenshots_dir = get_data_dir() / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    filename = "viola_%s.png" % datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = screenshots_dir / filename

    if region == "full":
        img = pyautogui.screenshot()
        captured_region = "full"
    else:
        # Attempt to capture a specific window region
        dep_err = _check_pywinauto()
        if dep_err:
            # Pre-fix this silently fell back to a full-screen screenshot but
            # still returned region=<requested window title>, so a caller
            # that asked for one window's contents (privacy-relevant: they
            # may deliberately want to exclude everything else visible) got
            # the whole screen mislabeled as that window (#2773 item 5). Fail
            # instead of misreporting what was actually captured.
            raise RuntimeError(dep_err)
        wrapper = _resolve_window(region)
        rect = wrapper.rectangle()
        img = pyautogui.screenshot(region=(rect.left, rect.top, rect.width(), rect.height()))
        captured_region = region

    img.save(str(filepath))
    return {"path": str(filepath), "region": captured_region}


def _mouse_click_sync(x: int, y: int, button: str = "left") -> dict:
    """Click at specific screen coordinates (synchronous).

    Every backend here returns None, so the payload records the requested
    coordinates and says outright that nothing observed the click land. A
    coordinate click is the path most likely to hit nothing at all (the
    window moved, the element scrolled, another window is on top), which is
    exactly why it must not read as a confirmed click.
    """
    if _DARWIN_NATIVE_INPUT and not _PYWINAUTO_AVAILABLE:
        from services.computer_use import input as computer_input

        computer_input.click_physical((x, y), button=button)
        return _mark_unverified({"x": x, "y": y, "button": button, "method": "quartz_cgevent"}, _REASON_MOUSE)

    if _PYWINAUTO_AVAILABLE:
        _pywinauto_mouse_module().click(button=button, coords=(x, y))
        return _mark_unverified({"x": x, "y": y, "button": button, "method": "pywinauto_mouse"}, _REASON_MOUSE)

    import pyautogui

    pyautogui.click(x, y, button=button)
    return _mark_unverified({"x": x, "y": y, "button": button}, _REASON_MOUSE)


# ---------------------------------------------------------------------------
# Async tool handlers
# ---------------------------------------------------------------------------


def _result_from_payload(payload: dict) -> ToolResult:
    """Build a ToolResult that says exactly what the sync helper established.

    Three outcomes, not two: an ``error`` key means the action provably did
    not happen; an ``unverified`` flag means it was submitted and nobody
    watched; anything else means the code has real evidence it worked.
    """
    if "error" in payload:
        return ToolResult(ok=False, error=str(payload["error"]), data=payload or None)
    return ToolResult(ok=True, data=payload, unverified=bool(payload.get(_UNVERIFIED_KEY)))


async def desktop_list_windows() -> ToolResult:
    """List all visible windows with title, process name, handle, and active state."""
    dep_err = _check_pywinauto()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    try:
        result = await asyncio.to_thread(_list_windows_sync)
        return ToolResult(ok=True, data={"windows": result, "count": len(result)})
    except Exception as exc:
        logger.exception("Failed to list windows")
        return ToolResult(ok=False, error="Failed to list windows: %s" % exc)


async def desktop_focus_window(title: str) -> ToolResult:
    """Bring a window to the foreground by partial title match."""
    dep_err = _check_pywinauto()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    if not title or not title.strip():
        return ToolResult(ok=False, error="Window title must not be empty")

    try:
        result = await asyncio.to_thread(_focus_window_sync, title)
        return _result_from_payload(result)
    except Exception as exc:
        logger.exception("Failed to focus window '%s'", title)
        return ToolResult(ok=False, error="Failed to focus window '%s': %s" % (title, exc))


async def desktop_read_window(title: str = "") -> ToolResult:
    """Read the accessibility tree of a window.

    If no title is given, reads the currently focused window.
    Returns up to 100 elements, truncated to 3000 characters.
    """
    dep_err = _check_pywinauto()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    try:
        result = await asyncio.to_thread(_read_window_sync, title)
        if "error" in result:
            return ToolResult(ok=False, error=result["error"])
        return ToolResult(ok=True, data=result, truncated=result.get("truncated", False))
    except Exception as exc:
        logger.exception("Failed to read window '%s'", title)
        return ToolResult(ok=False, error="Failed to read window: %s" % exc)


async def desktop_click_element(name: str, window_title: str = "") -> ToolResult:
    """Click a UI element by its accessibility name.

    Tries pywinauto accessibility match first, then falls back to
    coordinate-based pyautogui click.
    """
    dep_err = desktop_click_element_unavailable_reason()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    if not name or not name.strip():
        return ToolResult(ok=False, error="Element name must not be empty")

    try:
        result = await asyncio.to_thread(_click_element_sync, name, window_title)
        return _result_from_payload(result)
    except Exception as exc:
        logger.exception("Failed to click element '%s'", name)
        return ToolResult(ok=False, error="Failed to click element '%s': %s" % (name, exc))


async def desktop_type_text(text: str, *, respect_focus: bool = True, window_title: str = "") -> ToolResult:
    """Type text into the active or requested desktop target.

    By default this resolves the focused hwnd at the start of typing and sends
    text directly to that handle so a user click in another window does not
    redirect the keystrokes. pyautogui is retained as the legacy fallback.
    Blocks credit card and SSN patterns for safety.
    """
    dep_err = _check_keyboard_dep()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    if not text:
        return ToolResult(ok=False, error="Text must not be empty")

    # Safety: block sensitive data patterns
    if _CREDIT_CARD_RE.search(text):
        logger.warning("Blocked attempt to type credit card pattern")
        return ToolResult(ok=False, error="Refused: text contains a credit card number pattern")

    if _SSN_RE.search(text):
        logger.warning("Blocked attempt to type SSN pattern")
        return ToolResult(ok=False, error="Refused: text contains a Social Security Number pattern")

    try:
        result = await asyncio.to_thread(_type_text_sync, text, respect_focus, window_title)
        return _result_from_payload(result)
    except Exception as exc:
        logger.exception("Failed to type text")
        return ToolResult(ok=False, error="Failed to type text: %s" % exc)


async def desktop_hotkey(keys: str) -> ToolResult:
    """Press a keyboard shortcut (e.g. 'ctrl+c', 'alt+tab', 'ctrl+shift+s').

    The key string is translated through pywinauto when available, with
    pyautogui retained as the legacy fallback.
    """
    dep_err = _check_keyboard_dep()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    if not keys or not keys.strip():
        return ToolResult(ok=False, error="Keys must not be empty")

    logger.info("Executing hotkey: %s", keys)

    try:
        result = await asyncio.to_thread(_hotkey_sync, keys)
        return _result_from_payload(result)
    except Exception as exc:
        logger.exception("Failed to press hotkey '%s'", keys)
        return ToolResult(ok=False, error="Failed to press hotkey '%s': %s" % (keys, exc))


async def desktop_screenshot(region: str = "full") -> ToolResult:
    """Take a screenshot of the full screen or a specific window.

    Saves to ~/Pictures/Viola Screenshots/ with an auto-generated
    timestamp filename.
    """
    dep_err = _check_pyautogui()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    try:
        result = await asyncio.to_thread(_screenshot_sync, region)
        return ToolResult(ok=True, data=result)
    except Exception as exc:
        logger.exception("Failed to take screenshot (region=%s)", region)
        return ToolResult(ok=False, error="Failed to take screenshot: %s" % exc)


async def desktop_mouse_click(x: int, y: int, button: str = "left") -> ToolResult:
    """Click at specific screen coordinates (last resort).

    Args:
        x: Horizontal pixel coordinate
        y: Vertical pixel coordinate
        button: Mouse button - 'left', 'right', or 'middle'
    """
    dep_err = _check_mouse_dep()
    if dep_err:
        return ToolResult(ok=False, error=dep_err)

    if button not in ("left", "right", "middle"):
        return ToolResult(
            ok=False,
            error="Invalid button '%s'. Use 'left', 'right', or 'middle'" % button,
        )

    try:
        result = await asyncio.to_thread(_mouse_click_sync, int(x), int(y), button)
        return _result_from_payload(result)
    except Exception as exc:
        logger.exception("Failed to click at (%s, %s)", x, y)
        return ToolResult(ok=False, error="Failed to click at (%s, %s): %s" % (x, y, exc))
