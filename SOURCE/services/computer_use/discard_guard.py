"""Decide from the screen whether a desktop action would throw away unsaved work.

Build 11 guarded "Don't save" only when the model clicked it by its button
name. On the 1.0.5 build-11 laptop the model clicked it by element ref, and
one earlier "yes" for Notepad covered the click, so a different, older
Notepad window lost its text with no question asked. The rule is about the
effect, so this module looks at what the action would land on:

* a click by name, by snapshot ref, or at a screen point whose element is a
  "Don't save" / "Discard" button;
* any key press or typing while the window receiving it shows a save prompt
  (a dialog holding such a button), because Tab, Enter or a mnemonic letter
  can press it just as well.

The computer tool asks this module before acting; the answer names the
window whose work would be lost, so the one-time approval is bound to it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_user32: Any = ctypes.windll.user32 if _IS_WINDOWS else None  # type: ignore[attr-defined]

_GA_ROOT = 2
_GW_OWNER = 4
_UIA_BUTTON_CONTROL_TYPE = 50000
_UIA_TAB_ITEM_CONTROL_TYPE = 50019
_UIA_CONTROL_TYPE_PROPERTY = 30003
_TREE_SCOPE_DESCENDANTS = 4
_UIA_TIMEOUT_S = 2.0
_MAX_TABS_LISTED = 20

DISCARD_ERROR_CATEGORY = "COMPUTER_USE_DISCARD_APPROVAL_REQUIRED"
AMBIGUOUS_WINDOW_ERROR_CATEGORY = "COMPUTER_USE_WINDOW_AMBIGUOUS"

_DISCARD_NAMES: frozenset[str] = frozenset(
    {
        "don't save",
        "dont save",
        "do not save",
        "don't save all",
        "discard",
        "discard changes",
        "discard all",
        "close without saving",
        "quit without saving",
        "exit without saving",
    }
)
_DISCARD_PREFIXES: tuple[str, ...] = ("don't save", "dont save", "do not save", "discard ")
_CLOSE_NAMES: frozenset[str] = frozenset({"close", "close tab", "close window", "close all tabs", "exit", "quit"})
_CLOSE_KEY_CHORDS: frozenset[str] = frozenset({"ctrl+w", "ctrl+f4", "ctrl+shift+w", "alt+f4"})
# Actions that send keystrokes to the window in front (or to a named window).
KEY_ACTIONS: frozenset[str] = frozenset({"key", "type", "key_down", "key_hold", "background_type"})
# Clicks at a point: the element under that point decides.
POINT_CLICK_ACTIONS: frozenset[str] = frozenset({"click", "double_click"})
# Low-level presses at the cursor: the element under the cursor decides. Checked
# on the press, because a refused action releases held buttons on cleanup.
CURSOR_PRESS_ACTIONS: frozenset[str] = frozenset({"mouse_button_down", "mouse_button_hold"})


def normalize_control_name(name: object) -> str:
    """Lower case, straight apostrophes, no mnemonic "&": "Do&n’t Save" reads "don't save"."""
    text = str(name or "").replace("’", "'").replace("‘", "'").replace("&", "")
    return " ".join(text.lower().split()).strip(" .:")


def is_discard_name(name: object) -> bool:
    normalized = normalize_control_name(name)
    if not normalized:
        return False
    return normalized in _DISCARD_NAMES or normalized.startswith(_DISCARD_PREFIXES)


def is_close_name(name: object) -> bool:
    return normalize_control_name(name) in _CLOSE_NAMES


def is_close_chord(chord: str) -> bool:
    from services.computer_use.safety import normalize_key_chord

    try:
        normalized = normalize_key_chord(chord)
    except Exception:  # noqa: BLE001, RUF100 - an unparsable chord is not a close
        return False
    return normalized in _CLOSE_KEY_CHORDS


@dataclass(frozen=True)
class SavePrompt:
    """A save prompt on screen: the window whose work it guards, and its discard buttons."""

    window_handle: int
    window_title: str
    app_executable: str
    prompt_handle: int
    buttons: tuple[str, ...]


@dataclass(frozen=True)
class DiscardRisk:
    """An action that would throw away unsaved work in one window."""

    window_handle: int
    window_title: str
    app_executable: str
    other_tabs: tuple[str, ...]
    trigger: str
    control_name: str = ""
    unreadable: bool = False

    def description(self) -> str:
        """Plain words for the approval card: what would be lost, in which window."""
        app = _app_label(self.app_executable)
        title = str(self.window_title or "").strip()
        where = "the %s window “%s”" % (app, title) if title else "the %s window" % app
        if self.unreadable:
            return (
                "act in %s, which did not answer when Viola checked it for a save prompt; if one is open, "
                "this could throw away unsaved changes" % where
            )
        text = "throw away the unsaved changes in %s" % where
        if self.other_tabs:
            text += " (other tabs open in that window: %s)" % ", ".join(self.other_tabs)
        return text

    def details(self) -> dict[str, object]:
        return {
            "window_handle": self.window_handle,
            "window_title": self.window_title,
            "app_executable": self.app_executable,
            "other_tabs": list(self.other_tabs),
            "trigger": self.trigger,
            "control_name": self.control_name,
            "screen_unreadable": self.unreadable,
            "planned_action": self.description(),
        }


def _app_label(executable: str) -> str:
    name = str(executable or "").strip()
    if not name:
        return "app"
    return name[:1].upper() + name[1:]


# ---------------------------------------------------------------------------
# Win32 and UIA reads. Each is a module-level function so tests replace them.
# ---------------------------------------------------------------------------


def window_title(hwnd: int) -> str:
    if not hwnd or _user32 is None:
        return ""
    from services.computer_use.window_manager import _get_window_title_fast

    return _get_window_title_fast(int(hwnd))


def window_executable(hwnd: int) -> str:
    if not hwnd or _user32 is None:
        return ""
    from services.computer_use import safety, window_manager

    try:
        return safety.normalize_executable_name(
            window_manager._process_name(window_manager._process_id_for_handle(int(hwnd)))
        )
    except Exception:  # noqa: BLE001, RUF100 - unreadable process: unknown app
        return ""


def foreground_window() -> int:
    if _user32 is None:
        return 0
    return int(_user32.GetForegroundWindow() or 0)


def root_window(hwnd: int) -> int:
    if not hwnd or _user32 is None:
        return int(hwnd or 0)
    return int(_user32.GetAncestor(int(hwnd), _GA_ROOT) or hwnd)


def owner_window(hwnd: int) -> int:
    if not hwnd or _user32 is None:
        return 0
    return int(_user32.GetWindow(int(hwnd), _GW_OWNER) or 0)


def owned_windows(hwnd: int) -> list[int]:
    """Visible top-level windows owned by ``hwnd`` (a classic Save changes? dialog)."""
    if not hwnd or _user32 is None:
        return []
    from services.computer_use.window_manager import _enum_top_level_handles

    owned: list[int] = []
    for handle in _enum_top_level_handles():
        try:
            if owner_window(handle) == int(hwnd) and _user32.IsWindowVisible(handle):
                owned.append(int(handle))
        except Exception:  # noqa: BLE001, RUF100 - a window that vanished mid-walk
            continue
    return owned


def document_window(hwnd: int) -> int:
    """The window whose work a prompt guards: the prompt's owner, else the window itself."""
    current = root_window(hwnd)
    for _ in range(3):
        owner = owner_window(current)
        if not owner:
            break
        current = owner
    return current


class ScreenReadTimeout(Exception):
    """A screen read that decides whether an action discards work did not finish in time."""


def _run_bounded(work: Callable[[], Any], *, default: Any, label: str, strict: bool = False) -> Any:
    """Run a UIA read in a daemon thread for at most _UIA_TIMEOUT_S (UIA can hang on a busy app).

    ``strict``: a read that decides whether work is thrown away raises
    :class:`ScreenReadTimeout` instead of answering "nothing there", so the
    action is held rather than let through.
    """
    holder: dict[str, Any] = {"value": default}

    def _target() -> None:
        try:
            try:
                import comtypes

                comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            except Exception:  # noqa: BLE001, RUF100 - already initialised in this thread is fine
                pass
            holder["value"] = work()
        except Exception as exc:  # noqa: BLE001, RUF100 - UIA raises COM errors of many kinds
            logger.debug("%s failed: %s", label, type(exc).__name__)

    worker = threading.Thread(target=_target, daemon=True, name="discard-guard-uia")
    worker.start()
    worker.join(timeout=_UIA_TIMEOUT_S)
    if worker.is_alive():
        logger.info("%s took longer than %.1fs; treated as unknown", label, _UIA_TIMEOUT_S)
        if strict:
            raise ScreenReadTimeout(label)
        return default
    return holder["value"]


def _uia_names_of_type(hwnd: int, control_type: int) -> list[str]:
    from pywinauto.uia_defines import IUIA

    iuia = IUIA().iuia
    element = iuia.ElementFromHandle(int(hwnd))
    condition = iuia.CreatePropertyCondition(_UIA_CONTROL_TYPE_PROPERTY, control_type)
    found = element.FindAll(_TREE_SCOPE_DESCENDANTS, condition)
    names: list[str] = []
    for index in range(int(found.Length or 0)):
        try:
            names.append(str(found.GetElement(index).CurrentName or ""))
        except Exception:  # noqa: BLE001, RUF100 - an element that vanished mid-read
            continue
    return names


def discard_buttons_in(hwnd: int) -> list[str]:
    """Names of the "Don't save" / "Discard" buttons inside one top-level window."""
    if not hwnd or not _IS_WINDOWS:
        return []
    names = _run_bounded(
        lambda: _uia_names_of_type(hwnd, _UIA_BUTTON_CONTROL_TYPE),
        default=[],
        label="Save prompt check",
        strict=True,
    )
    return [name for name in names if is_discard_name(name)]


def tab_names_in(hwnd: int) -> list[str]:
    """Tab titles of one window (Notepad, editors and browsers keep documents as tabs)."""
    if not hwnd or not _IS_WINDOWS:
        return []
    names = _run_bounded(lambda: _uia_names_of_type(hwnd, _UIA_TAB_ITEM_CONTROL_TYPE), default=[], label="Tab read")
    return [str(name).strip() for name in names if str(name).strip()]


def element_at_point(x: int, y: int) -> tuple[str, int]:
    """(accessible name, top-level window) of the element at a physical screen point."""
    if not _IS_WINDOWS:
        return "", 0

    def _read() -> tuple[str, int]:
        from pywinauto import Desktop

        element = Desktop(backend="uia").from_point(int(x), int(y))
        name = str(element.window_text() or "")
        handle = 0
        try:
            handle = int(getattr(element.element_info, "handle", 0) or 0)
        except Exception:  # noqa: BLE001, RUF100 - windowless XAML elements have no handle
            handle = 0
        if not handle and _user32 is not None:
            point = ctypes.wintypes.POINT(int(x), int(y))
            handle = int(_user32.WindowFromPoint(point) or 0)
        return name, root_window(handle)

    return _run_bounded(_read, default=("", 0), label="Element-at-point read", strict=True)


def cursor_position() -> tuple[int, int] | None:
    if _user32 is None:
        return None
    point = ctypes.wintypes.POINT()
    if not _user32.GetCursorPos(ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def find_save_prompt(hwnd: int) -> SavePrompt | None:
    """The save prompt shown by this window or a dialog it owns, if any."""
    if not hwnd:
        return None
    root = root_window(hwnd)
    document = document_window(root)
    candidates: list[int] = []
    for handle in (root, *owned_windows(document), document):
        if handle and handle not in candidates:
            candidates.append(handle)
    for handle in candidates:
        buttons = discard_buttons_in(handle)
        if buttons:
            return SavePrompt(
                window_handle=document,
                window_title=window_title(document),
                app_executable=window_executable(document),
                prompt_handle=handle,
                buttons=tuple(buttons),
            )
    return None


def _other_tabs(document: int, title: str) -> tuple[str, ...]:
    tabs = tab_names_in(document)
    if len(tabs) <= 1:
        return ()
    current = normalize_control_name(title)
    others = []
    for tab in tabs:
        # Notepad names a tab "notes. Modified." when it holds unsaved changes.
        cleaned = normalize_control_name(tab).lstrip("*").strip()
        if cleaned.endswith(". modified"):
            cleaned = cleaned[: -len(". modified")].strip()
        if cleaned and cleaned in current:
            # The tab being closed is named in the window title ("notes - Notepad").
            continue
        others.append(tab)
    if len(others) == len(tabs):
        # Could not tell which tab is the one being closed: list them all.
        return tuple(tabs[:_MAX_TABS_LISTED])
    return tuple(others[:_MAX_TABS_LISTED])


def risk_for_window(hwnd: int, *, trigger: str, control_name: str = "") -> DiscardRisk:
    document = document_window(hwnd) if hwnd else 0
    title = window_title(document)
    return DiscardRisk(
        window_handle=document,
        window_title=title,
        app_executable=window_executable(document),
        other_tabs=_other_tabs(document, title) if document else (),
        trigger=trigger,
        control_name=control_name,
    )


def assess_named_click(name: str, hwnd: int) -> DiscardRisk | None:
    if not is_discard_name(name):
        return None
    return risk_for_window(hwnd or foreground_window(), trigger="click_name", control_name=name)


def assess_ref_click(element_name: str, hwnd: int) -> DiscardRisk | None:
    if not is_discard_name(element_name):
        return None
    return risk_for_window(hwnd or foreground_window(), trigger="click_ref", control_name=element_name)


def assess_point(x: int, y: int, *, trigger: str = "click_point") -> tuple[DiscardRisk | None, str, int]:
    """(risk, element name, top-level window) for a press at a physical point."""
    name, hwnd = element_at_point(int(x), int(y))
    if not is_discard_name(name):
        return None, name, hwnd
    return risk_for_window(hwnd or foreground_window(), trigger=trigger, control_name=name), name, hwnd


def assess_key(hwnd: int) -> DiscardRisk | None:
    """A key press into a window that shows a save prompt could press its "Don't save"."""
    target = hwnd or foreground_window()
    prompt = find_save_prompt(target)
    if prompt is None:
        return None
    return risk_for_window(prompt.window_handle, trigger="key", control_name=prompt.buttons[0])


def unreadable_risk(hwnd: int, *, trigger: str) -> DiscardRisk:
    """The screen could not be read in time: hold the action as if it could discard work."""
    document = document_window(hwnd) if hwnd else 0
    return DiscardRisk(
        window_handle=document,
        window_title=window_title(document),
        app_executable=window_executable(document),
        other_tabs=(),
        trigger=trigger,
        control_name="",
        unreadable=True,
    )


def save_prompt_note(prompt: SavePrompt) -> dict[str, object]:
    """What the tool result says after a close brought up a save prompt."""
    app = _app_label(prompt.app_executable)
    title = prompt.window_title or "the window"
    return {
        "save_prompt": {
            "window_title": prompt.window_title,
            "window_handle": prompt.window_handle,
            "app_executable": prompt.app_executable,
            "buttons": list(prompt.buttons),
        },
        "note": (
            "%s is asking whether to save changes to “%s”: it is NOT closed yet. Pressing %s throws "
            "the unsaved changes away and cannot be undone; Viola asks the user before that happens. If the user "
            "did not ask to discard this work, tell them what is unsaved and let them choose."
        )
        % (app, title, " / ".join(prompt.buttons)),
    }


def app_windows(app_executable: str) -> list[dict[str, object]]:
    """Every listed (titled, shown or minimized) top-level window of one app."""
    from services.computer_use import safety, window_manager

    target = safety.normalize_executable_name(app_executable)
    if not target:
        return []
    listed = window_manager.list_windows(include_minimized=True, include_titles=True)
    windows = listed.get("windows") if isinstance(listed, dict) else None
    matches: list[dict[str, object]] = []
    for window in windows or []:
        if not isinstance(window, dict):
            continue
        if safety.normalize_executable_name(str(window.get("executable") or "")) == target:
            matches.append(window)
    return matches


def describe_windows(windows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "title": str(window.get("title") or ""),
            "window_handle": int(window.get("handle") or 0),
            "minimized": bool(window.get("minimized")),
        }
        for window in windows
    ]
