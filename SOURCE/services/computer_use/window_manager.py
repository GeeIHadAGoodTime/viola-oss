"""Window enumeration and focus helpers for desktop computer use.

Windows gets the full Win32/UIA surface (enumeration, focus, UIA fallback).
macOS gets a native foreground-window path (AppKit ``NSWorkspace`` +
Quartz ``CGWindowListCopyWindowInfo``) so screenshot-adjacent lookups such as
:func:`get_foreground_window_info` work instead of crashing on Win32 bindings.
Other platforms report the foreground window as unknown (``None``/``""``);
Win32-only helpers raise a clear ``RuntimeError`` if invoked there.
"""

from __future__ import annotations

import ctypes
import importlib
import sys
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Win32 fast-path bindings — avoid UIA Desktop().windows() (10-20s) for the
# common case of "focus a known window by title". Only fall back to UIA when
# we genuinely need the deeper accessibility tree (inspect_window etc.).
#
# This entire module wraps the Windows desktop window-management API. On
# Linux/macOS ``ctypes.windll`` and ``ctypes.wintypes`` do not exist, so even
# importing this module top-level would raise. The fast-path bindings are
# therefore set up only on Windows; on other platforms ``_user32``/``wintypes``
# stay ``None`` and the public helpers raise a clear, catchable error if they
# are ever invoked (desktop computer-use is a Windows-only capability — callers
# already import this module lazily from feature code, never at boot).
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"
_IS_DARWIN = sys.platform == "darwin"

if sys.platform == "win32":
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    _user32.GetWindowTextLengthW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsWindow.argtypes = [ctypes.c_void_p]
    _user32.IsWindow.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.SetForegroundWindow.restype = ctypes.c_bool
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = ctypes.c_bool

    _ENUMWINDOWSPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    _user32.EnumWindows.argtypes = [_ENUMWINDOWSPROC, ctypes.c_void_p]
    _user32.EnumWindows.restype = ctypes.c_bool
else:  # pragma: no cover - exercised on Linux/macOS only
    wintypes = None  # type: ignore[assignment]
    _user32 = None  # type: ignore[assignment]
    _ENUMWINDOWSPROC = None  # type: ignore[assignment]


def _get_window_title_fast(hwnd: int) -> str:
    """Read window title via Win32 GetWindowTextW (microseconds)."""
    if not hwnd:
        return ""
    length = _user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _get_window_class_fast(hwnd: int) -> str:
    """Read a top-level window class name via Win32."""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    if _user32.GetClassNameW(hwnd, buf, len(buf)) <= 0:
        return ""
    return buf.value


def _get_window_rect_fast(hwnd: int) -> wintypes.RECT:
    rect = wintypes.RECT()
    if not hwnd or not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        msg = "Could not read window rectangle for hwnd=%s" % hwnd
        raise RuntimeError(msg)
    return rect


def _normalize_window_match_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    visible_chars = [ch for ch in normalized if unicodedata.category(ch) != "Cf"]
    return " ".join("".join(visible_chars).split())


def _simplify_window_match_text(value: str) -> str:
    normalized = _normalize_window_match_text(value)
    chars = [ch if ch.isalnum() else " " for ch in normalized]
    return " ".join("".join(chars).split())


def _window_title_matches(target: str, candidate: str) -> bool:
    normalized_target = _normalize_window_match_text(target)
    if not normalized_target:
        return False
    normalized_candidate = _normalize_window_match_text(candidate)
    if normalized_target in normalized_candidate:
        return True
    simplified_target = _simplify_window_match_text(target)
    simplified_candidate = _simplify_window_match_text(candidate)
    return bool(simplified_target and simplified_target in simplified_candidate)


def _resolve_hwnd_fast(title: str) -> int:
    """Find a visible top-level window by title substring via Win32 EnumWindows.

    Microseconds-fast vs UIA Desktop().windows() which takes 10-20s on a busy
    desktop. Returns 0 if no match.
    """
    target = _normalize_window_match_text(title)
    if not target:
        return 0
    found_hwnd = [0]

    @_ENUMWINDOWSPROC
    def _enum_proc(hwnd: int, _lparam: int) -> bool:
        if not _user32.IsWindowVisible(hwnd):
            return True
        win_title = _get_window_title_fast(int(hwnd))
        if win_title and _window_title_matches(target, win_title):
            found_hwnd[0] = int(hwnd)
            return False
        return True

    try:
        _user32.EnumWindows(_enum_proc, 0)
    except Exception:
        logger.debug("EnumWindows failed; fast hwnd resolve aborted")
    return found_hwnd[0]


# Cache: normalized-title -> (hwnd, last_used_ts). 30s TTL is short enough to
# invalidate stale state, long enough to amortize many key/click calls in a
# single agent sequence.
_HWND_CACHE: dict[str, tuple[int, float]] = {}
_HWND_CACHE_TTL_S = 30.0
_HWND_CACHE_LOCK = threading.Lock()


def _hwnd_cache_get(title: str) -> int:
    key = _normalize_window_match_text(title)
    if not key:
        return 0
    with _HWND_CACHE_LOCK:
        entry = _HWND_CACHE.get(key)
        if entry is None:
            return 0
        hwnd, ts = entry
        if time.time() - ts > _HWND_CACHE_TTL_S:
            _HWND_CACHE.pop(key, None)
            return 0
        if not _user32.IsWindow(hwnd) or not _user32.IsWindowVisible(hwnd):
            _HWND_CACHE.pop(key, None)
            return 0
        return hwnd


def _hwnd_cache_put(title: str, hwnd: int) -> None:
    key = _normalize_window_match_text(title)
    if not key or not hwnd:
        return
    with _HWND_CACHE_LOCK:
        _HWND_CACHE[key] = (hwnd, time.time())


def reset_hwnd_cache_for_tests() -> None:
    with _HWND_CACHE_LOCK:
        _HWND_CACHE.clear()


@dataclass(frozen=True)
class WindowInfo:
    """Top-level Windows desktop window metadata."""

    handle: int
    title: str
    executable: str
    process_id: int
    class_name: str
    left: int
    top: int
    right: int
    bottom: int
    minimized: bool
    visible: bool

    def to_dict(self, *, include_title: bool = True) -> dict[str, object]:
        payload = asdict(self)
        if not include_title:
            payload["title"] = ""
        payload["bounds"] = {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": max(0, self.right - self.left),
            "height": max(0, self.bottom - self.top),
        }
        return payload


def _import_desktop() -> Any:
    try:
        pywinauto = importlib.import_module("pywinauto")
    except ModuleNotFoundError as exc:
        msg = "pywinauto is required for computer-use window management"
        raise RuntimeError(msg) from exc
    return pywinauto.Desktop


def _require_win32() -> None:
    """Raise a clear, catchable error when Win32 bindings are unavailable.

    ``ctypes.windll`` does not exist off Windows; going through this guard
    instead yields an explicit RuntimeError rather than an AttributeError
    deep inside ctypes (the pre-fix macOS screenshot crash shape).
    """
    if _user32 is None:
        msg = "Win32 window management requires Windows (sys.platform=%r)" % sys.platform
        raise RuntimeError(msg)


def _process_id_for_handle(handle: int) -> int:
    _require_win32()
    process_id = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(int(handle), ctypes.byref(process_id))
    return int(process_id.value)


def _foreground_handle() -> int:
    _require_win32()
    return int(_user32.GetForegroundWindow() or 0)


def _process_name(process_id: int) -> str:
    if process_id <= 0:
        return ""
    try:
        psutil = importlib.import_module("psutil")
        return str(psutil.Process(process_id).name()).lower()
    except Exception:
        logger.debug("Could not resolve process name for pid %s", process_id)
        return ""


def _window_to_info(window: Any) -> WindowInfo:
    handle = int(getattr(window, "handle", 0) or 0)
    rectangle = window.rectangle()
    element_info = getattr(window, "element_info", None)
    class_name = str(getattr(element_info, "class_name", "") or "")
    process_id = _process_id_for_handle(handle) if handle else int(getattr(element_info, "process_id", 0) or 0)
    return WindowInfo(
        handle=handle,
        title=str(window.window_text() or ""),
        executable=_process_name(process_id),
        process_id=process_id,
        class_name=class_name,
        left=int(rectangle.left),
        top=int(rectangle.top),
        right=int(rectangle.right),
        bottom=int(rectangle.bottom),
        minimized=bool(window.is_minimized()),
        visible=bool(window.is_visible()),
    )


def list_windows(*, include_minimized: bool = False, include_titles: bool = True) -> dict[str, object]:
    """List visible top-level windows.

    Windows uses pywinauto's UIA backend. macOS uses native Quartz
    ``CGWindowListCopyWindowInfo`` — the UIA backend is Windows-only and
    ``Desktop(backend="uia")`` HANGS for minutes on macOS (pywinauto trying to
    reach a Windows UIA COM server that does not exist), which stalled the whole
    agent on "list all open windows". Other platforms honestly report an empty
    list instead of raising out of the Win32/UIA bindings.
    """
    if _IS_DARWIN:
        return _list_windows_darwin(include_minimized=include_minimized, include_titles=include_titles)
    if not _IS_WINDOWS:
        logger.debug("No native window-listing path on %s; reporting empty", sys.platform)
        return {"ok": True, "action": "list_windows", "windows": [], "count": 0}

    Desktop = _import_desktop()
    desktop = Desktop(backend="uia")
    windows: list[dict[str, object]] = []
    for window in desktop.windows():
        try:
            info = _window_to_info(window)
        except Exception:
            logger.debug("Skipping window with unreadable metadata")
            continue
        if not info.visible:
            continue
        if info.minimized and not include_minimized:
            continue
        if not info.title and not info.executable:
            continue
        windows.append(info.to_dict(include_title=include_titles))

    return {
        "ok": True,
        "action": "list_windows",
        "windows": windows,
        "count": len(windows),
    }


def _list_windows_darwin(*, include_minimized: bool, include_titles: bool) -> dict[str, object]:
    """List on-screen top-level windows via Quartz CGWindowList (macOS).

    Enumerates standard-layer (``kCGWindowLayer == 0``) on-screen windows,
    skipping the menu bar, Dock, and system overlays. Window titles
    (``kCGWindowName``) require the Screen Recording grant; without it the
    bounds/owner still list correctly and titles come back empty. Never raises —
    an enumeration failure reports an empty list.
    """
    windows: list[dict[str, object]] = []
    raw: list[Any] = []
    try:
        quartz = importlib.import_module("Quartz")
        options = quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements
        raw = list(quartz.CGWindowListCopyWindowInfo(options, quartz.kCGNullWindowID) or [])
    except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.debug("Could not enumerate windows via Quartz CGWindowList")
        return {"ok": True, "action": "list_windows", "windows": [], "count": 0}

    for window in raw:
        try:
            if int(window.get("kCGWindowLayer", -1) or 0) != 0:
                continue  # menu bar, Dock, system overlays
            pid = int(window.get("kCGWindowOwnerPID", 0) or 0)
            bounds = window.get("kCGWindowBounds", {}) or {}
            left = int(bounds.get("X", 0))
            top = int(bounds.get("Y", 0))
            width = max(0, int(bounds.get("Width", 0)))
            height = max(0, int(bounds.get("Height", 0)))
            on_screen = bool(window.get("kCGWindowIsOnscreen", True))
            owner_name = str(window.get("kCGWindowOwnerName", "") or "")
            info = WindowInfo(
                handle=int(window.get("kCGWindowNumber", 0) or 0),
                title=str(window.get("kCGWindowName", "") or ""),
                executable=_process_name(pid) or owner_name.lower(),
                process_id=pid,
                class_name="",
                left=left,
                top=top,
                right=left + width,
                bottom=top + height,
                minimized=not on_screen,
                visible=on_screen,
            )
        except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
            logger.debug("Skipping window with unreadable Quartz metadata")
            continue
        if info.minimized and not include_minimized:
            continue
        if not info.title and not info.executable:
            continue
        windows.append(info.to_dict(include_title=include_titles))

    return {
        "ok": True,
        "action": "list_windows",
        "windows": windows,
        "count": len(windows),
    }


def _darwin_frontmost_pid() -> int:
    """Return the frontmost application's pid via AppKit NSWorkspace (0 if unknown)."""
    try:
        appkit = importlib.import_module("AppKit")
        app = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return 0
        return int(app.processIdentifier())
    except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.debug("Could not resolve frontmost app via NSWorkspace")
        return 0


def _get_foreground_window_info_darwin() -> WindowInfo | None:
    """Frontmost-window metadata via native macOS APIs (Quartz + AppKit).

    Uses NSWorkspace for the authoritative frontmost pid and Quartz
    ``CGWindowListCopyWindowInfo`` for that app's on-screen window bounds.
    Returns *None* when neither API can answer — callers already treat
    ``None`` as "foreground unknown" and proceed.
    """
    frontmost_pid = _darwin_frontmost_pid()
    windows: list[Any] = []
    try:
        quartz = importlib.import_module("Quartz")
        options = quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements
        windows = list(quartz.CGWindowListCopyWindowInfo(options, quartz.kCGNullWindowID) or [])
    except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.debug("Could not enumerate windows via Quartz CGWindowList")

    chosen: Any | None = None
    for window in windows:
        try:
            if int(window.get("kCGWindowLayer", -1) or 0) != 0:
                continue  # menu bar, dock, system overlays
            pid = int(window.get("kCGWindowOwnerPID", 0) or 0)
        except Exception:  # noqa: BLE001, S112, RUF100 - best-effort platform guard; must not raise
            continue
        if frontmost_pid and pid != frontmost_pid:
            continue
        chosen = window
        break

    if chosen is None:
        if frontmost_pid <= 0:
            return None
        # Frontmost app has no on-screen standard window (e.g. all minimized).
        return WindowInfo(
            handle=0,
            title="",
            executable=_process_name(frontmost_pid),
            process_id=frontmost_pid,
            class_name="",
            left=0,
            top=0,
            right=0,
            bottom=0,
            minimized=True,
            visible=False,
        )

    bounds = chosen.get("kCGWindowBounds", {}) or {}
    pid = int(chosen.get("kCGWindowOwnerPID", 0) or 0)
    left = int(bounds.get("X", 0))
    top = int(bounds.get("Y", 0))
    width = max(0, int(bounds.get("Width", 0)))
    height = max(0, int(bounds.get("Height", 0)))
    owner_name = str(chosen.get("kCGWindowOwnerName", "") or "")
    return WindowInfo(
        handle=int(chosen.get("kCGWindowNumber", 0) or 0),
        # kCGWindowName requires the Screen Recording grant; empty is fine.
        title=str(chosen.get("kCGWindowName", "") or ""),
        executable=_process_name(pid) or owner_name.lower(),
        process_id=pid,
        class_name="",
        left=left,
        top=top,
        right=left + width,
        bottom=top + height,
        minimized=False,
        visible=True,
    )


def get_foreground_window_info() -> WindowInfo | None:
    """Return metadata for the current foreground window, or *None* if unknown.

    Windows uses the Win32 fast path intentionally: the computer tool calls
    this before most actions for target-app and UAC checks; using
    pywinauto/UIA here makes the first key/action against UIA-hostile apps
    such as Minecraft stall for many seconds before the actual SendInput
    dispatch. macOS uses the native Quartz/AppKit path. Other platforms
    honestly report *None* (foreground unknown) instead of raising an
    AttributeError out of the Win32 bindings — the pre-fix shape that made
    `computer(action=screenshot)` fail on macOS before capture even ran.
    """
    if _IS_DARWIN:
        return _get_foreground_window_info_darwin()
    if not _IS_WINDOWS:
        logger.debug("No native foreground-window path on %s; reporting unknown", sys.platform)
        return None
    handle = _foreground_handle()
    if not handle:
        return None
    try:
        rect = _get_window_rect_fast(handle)
        process_id = _process_id_for_handle(handle)
        return WindowInfo(
            handle=handle,
            title=_get_window_title_fast(handle),
            executable=_process_name(process_id),
            process_id=process_id,
            class_name=_get_window_class_fast(handle),
            left=int(rect.left),
            top=int(rect.top),
            right=int(rect.right),
            bottom=int(rect.bottom),
            minimized=bool(_user32.IsIconic(handle)),
            visible=bool(_user32.IsWindowVisible(handle)),
        )
    except Exception:
        logger.debug("Could not read foreground window metadata")
        return None


def get_foreground_app_executable_name() -> str:
    """Return the foreground executable name, lower-case when available."""
    info = get_foreground_window_info()
    return info.executable if info is not None else ""


def get_foreground_window_center() -> tuple[int, int] | None:
    """Return the foreground window center in physical screen coordinates."""
    info = get_foreground_window_info()
    if info is None:
        return None
    return (
        info.left + max(0, info.right - info.left) // 2,
        info.top + max(0, info.bottom - info.top) // 2,
    )


def _matches_window(
    info: WindowInfo,
    *,
    handle: int | None,
    title: str | None,
    app_executable: str | None,
) -> bool:
    if handle is not None and info.handle == handle:
        return True
    if title and _window_title_matches(title, info.title):
        return True
    normalized_executable = Path(app_executable or "").name.lower()
    return bool(normalized_executable and info.executable == normalized_executable)


def _focus_window_unsupported() -> dict[str, object]:
    """Honest "not supported here" envelope for platforms with no focus backend.

    Used off Windows/darwin (e.g. Linux) so callers get a catchable error
    envelope instead of the pre-fix AttributeError from
    ``_user32.GetForegroundWindow()`` on a ``None`` binding (#2594).
    """
    logger.debug("No native window-focus path on %s; reporting unsupported", sys.platform)
    return {
        "ok": False,
        "error_category": "COMPUTER_USE_UNSUPPORTED_PLATFORM",
        "reason": "Window focus is not supported on %s" % sys.platform,
    }


def _focus_window_darwin(
    *,
    handle: int | None,
    title: str | None,
    app_executable: str | None,
) -> dict[str, object]:
    """Focus a window/app on macOS via AppKit ``NSRunningApplication``.

    macOS has no per-window HWND to "set foreground" — Quartz's on-screen
    window list (reused from :func:`_list_windows_darwin`) resolves a target
    window/handle/title to its owning process id, then
    ``NSRunningApplication.activateWithOptions_`` brings that whole
    application to the front (the native macOS analog of
    ``SetForegroundWindow``; there is no native single-window raise API
    without the Accessibility permission AppleScript itself requires).
    Never raises — an unmatched target returns the same
    ``COMPUTER_USE_WINDOW_NOT_FOUND`` envelope the Windows UIA fallback uses.
    """
    target_title = (title or "").strip()
    target_pid = 0
    matched_title = ""
    matched_executable = ""

    if handle or target_title:
        try:
            quartz = importlib.import_module("Quartz")
            options = quartz.kCGWindowListOptionOnScreenOnly | quartz.kCGWindowListExcludeDesktopElements
            windows = list(quartz.CGWindowListCopyWindowInfo(options, quartz.kCGNullWindowID) or [])
        except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
            logger.debug("Could not enumerate windows via Quartz CGWindowList for focus_window")
            windows = []
        for window in windows:
            try:
                if int(window.get("kCGWindowLayer", -1) or 0) != 0:
                    continue  # menu bar, dock, system overlays
                win_number = int(window.get("kCGWindowNumber", 0) or 0)
                win_title = str(window.get("kCGWindowName", "") or "")
            except Exception:  # noqa: BLE001, S112, RUF100 - best-effort platform guard; must not raise
                continue
            if handle and win_number == handle:
                target_pid = int(window.get("kCGWindowOwnerPID", 0) or 0)
                matched_title = win_title
                break
            if target_title and win_title and _window_title_matches(target_title, win_title):
                target_pid = int(window.get("kCGWindowOwnerPID", 0) or 0)
                matched_title = win_title
                break

    if not target_pid and app_executable:
        normalized_executable = Path(app_executable).name.lower()
        try:
            appkit = importlib.import_module("AppKit")
            for app in appkit.NSWorkspace.sharedWorkspace().runningApplications():
                exe_name = ""
                try:
                    url = app.executableURL()
                    exe_name = Path(str(url.path())).name.lower() if url is not None else ""
                except Exception:  # noqa: BLE001, S112, RUF100 - best-effort platform guard; must not raise
                    exe_name = ""
                localized_name = str(app.localizedName() or "").lower()
                if normalized_executable and (
                    exe_name == normalized_executable or normalized_executable in localized_name
                ):
                    target_pid = int(app.processIdentifier())
                    matched_executable = exe_name or localized_name
                    break
        except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
            logger.debug("Could not resolve running application via NSWorkspace for focus_window")

    if not target_pid:
        return {
            "ok": False,
            "error_category": "COMPUTER_USE_WINDOW_NOT_FOUND",
            "reason": "No matching window was found",
        }

    try:
        appkit = importlib.import_module("AppKit")
        running_app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(target_pid)
        if running_app is None:
            return {
                "ok": False,
                "error_category": "COMPUTER_USE_WINDOW_NOT_FOUND",
                "reason": "Matched process is no longer running",
            }
        activate_options = getattr(appkit, "NSApplicationActivateIgnoringOtherApps", 1 << 1)
        running_app.activateWithOptions_(activate_options)
    except Exception:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.debug("Could not activate darwin app pid=%s via NSRunningApplication", target_pid)
        return {
            "ok": False,
            "error_category": "COMPUTER_USE_WINDOW_NOT_FOUND",
            "reason": "Could not activate the matched application",
        }

    # ``activateWithOptions_`` returns a BOOL that reflects whether the
    # request was ACCEPTED, not whether the app came forward, and macOS
    # defers activation asynchronously. The Windows path reads the
    # foreground back to settle this; there is no equally cheap synchronous
    # equivalent here, so this reports honestly that it asked rather than
    # inventing a verification it did not perform.
    return {
        "ok": True,
        "action": "focus_window",
        "window_handle": 0,
        "window_title": matched_title,
        "target_app_executable": matched_executable or _process_name(target_pid),
        "foreground_verified": False,
        "unverified": True,
        "unverified_reason": (
            "activation was requested via NSRunningApplication, which returns before macOS has actually "
            "brought the app forward, so nothing here observed it reach the front"
        ),
    }


def _apply_foreground(target_hwnd: int) -> dict[str, object]:
    """Raise a window and REPORT whether it actually came to the foreground.

    ``SetForegroundWindow`` returns a BOOL and fails routinely: Windows
    refuses the call outright unless the calling process owns the current
    foreground window, was last to receive input, or is otherwise on the
    documented allow-list. Pre-fix that BOOL was discarded inside a
    try/except and the envelope said ``ok: True`` regardless, so a focus
    that never happened was reported as done -- and the caller's next click
    or keystroke went to whatever window was really in front.

    The outcome is verifiable rather than merely submitted, and cheaply:
    ``GetForegroundWindow`` reads back who actually holds focus. That read
    is the observer, so this returns a proven yes or a proven no, not a
    guess. A refused focus is reported as a failure the caller can act on.

    The read-back is used INSTEAD of ``SetForegroundWindow``'s return value
    rather than alongside it, because the BOOL is the weaker signal: under
    the foreground lock Windows can return TRUE and merely flash the
    window's taskbar button, leaving a different window in front. Who is
    actually in front is the fact that decides where the next keystroke
    goes, so that is the fact we report.
    """
    # Both are plain ctypes calls into user32, so the reachable failure
    # surface is enumerable: a missing/None handle off-Windows, a bad
    # argument type, or an OS-level error.
    _ctypes_errors = (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError)
    try:
        _user32.SetForegroundWindow(target_hwnd)
    except _ctypes_errors:
        logger.debug("SetForegroundWindow raised for hwnd=%s", target_hwnd)

    # Ground truth: ask Windows who is in front NOW.
    try:
        actual_hwnd = int(_user32.GetForegroundWindow() or 0)
    except _ctypes_errors:
        logger.debug("GetForegroundWindow unavailable while verifying focus for hwnd=%s", target_hwnd)
        actual_hwnd = 0

    if actual_hwnd == target_hwnd:
        return {
            "ok": True,
            "action": "focus_window",
            "window_handle": target_hwnd,
            "window_title": _get_window_title_fast(target_hwnd),
            "foreground_verified": True,
        }

    if not actual_hwnd:
        # We could not read the foreground window, so we know neither that
        # focus moved nor that it did not.
        logger.warning("Could not verify foreground window after focusing hwnd=%s", target_hwnd)
        return {
            "ok": True,
            "action": "focus_window",
            "window_handle": target_hwnd,
            "window_title": _get_window_title_fast(target_hwnd),
            "foreground_verified": False,
            "unverified": True,
            "unverified_reason": (
                "the focus request was submitted but Windows would not report which window is in the "
                "foreground, so anything typed or clicked next may go elsewhere"
            ),
        }

    logger.warning(
        "focus_window did not take: asked for hwnd=%s, foreground is hwnd=%s",
        target_hwnd,
        actual_hwnd,
    )
    return {
        "ok": False,
        "action": "focus_window",
        "error_category": "COMPUTER_USE_FOCUS_REFUSED",
        "reason": (
            "Windows kept '%s' in the foreground instead of the requested window; SetForegroundWindow is "
            "refused unless the calling process already owns the foreground or was the last to receive "
            "input. Anything typed or clicked now would go to the wrong window."
            % (_get_window_title_fast(actual_hwnd) or "another window")
        ),
        "window_handle": target_hwnd,
        "window_title": _get_window_title_fast(target_hwnd),
        "foreground_window_handle": actual_hwnd,
        "foreground_window_title": _get_window_title_fast(actual_hwnd),
        "foreground_verified": False,
    }


def focus_window(
    *,
    handle: int | None = None,
    title: str | None = None,
    app_executable: str | None = None,
) -> dict[str, object]:
    """Focus a window by HWND, title substring, or executable name.

    Windows fast path (microseconds):
      1. If the requested window is already foreground, return as a no-op.
      2. If we have a cached hwnd for this title, SetForegroundWindow on it.
      3. EnumWindows + GetWindowText resolve by title.
    UIA fallback (seconds): only when the fast path can't match (e.g.
    matching by ``app_executable`` requires process info).

    macOS resolves the target via Quartz/AppKit (:func:`_focus_window_darwin`).
    Other platforms honestly report unsupported (:func:`_focus_window_unsupported`)
    instead of the pre-fix ``_user32.GetForegroundWindow()`` AttributeError that
    fired on every non-Windows platform, including macOS (#2594) -- this was the
    only unguarded function in an otherwise cross-platform-ported file.
    """
    if _IS_DARWIN:
        return _focus_window_darwin(handle=handle, title=title, app_executable=app_executable)
    if not _IS_WINDOWS:
        return _focus_window_unsupported()

    target_title = (title or "").strip()
    target_handle = handle if handle else 0

    # 1. Foreground match — cheapest possible no-op.
    fg_hwnd = int(_user32.GetForegroundWindow() or 0)
    if fg_hwnd:
        # These two branches are the only pre-existing VERIFIED outcomes in
        # this function: the target is in front because GetForegroundWindow
        # just said so. Marked as such so the flag means the same thing on
        # every path.
        if target_handle and fg_hwnd == target_handle:
            return {
                "ok": True,
                "action": "focus_window",
                "window_handle": fg_hwnd,
                "window_title": _get_window_title_fast(fg_hwnd),
                "no_op": True,
                "foreground_verified": True,
            }
        if target_title:
            fg_title = _get_window_title_fast(fg_hwnd)
            if fg_title and _window_title_matches(target_title, fg_title):
                _hwnd_cache_put(target_title, fg_hwnd)
                return {
                    "ok": True,
                    "action": "focus_window",
                    "window_handle": fg_hwnd,
                    "window_title": fg_title,
                    "no_op": True,
                    "foreground_verified": True,
                }

    # 2 + 3. Resolve target hwnd. Prefer explicit handle, then cache, then
    # Win32 EnumWindows. Skip UIA unless we need executable matching.
    resolved_hwnd = target_handle
    if not resolved_hwnd and target_title:
        resolved_hwnd = _hwnd_cache_get(target_title)
    if not resolved_hwnd and target_title:
        resolved_hwnd = _resolve_hwnd_fast(target_title)
        if resolved_hwnd:
            _hwnd_cache_put(target_title, resolved_hwnd)

    if resolved_hwnd:
        return _apply_foreground(int(resolved_hwnd))

    # 4. UIA fallback — needed when the caller is matching by executable
    # name (we need process_id resolution from the UIA window) or when the
    # fast path returned nothing for a handle that's set.
    Desktop = _import_desktop()
    desktop = Desktop(backend="uia")
    for window in desktop.windows():
        try:
            info = _window_to_info(window)
        except Exception:
            logger.debug("Skipping window during focus lookup")
            continue
        if _matches_window(info, handle=handle, title=title, app_executable=app_executable):
            try:
                window.set_focus()
            except Exception:  # noqa: BLE001, RUF100 - pywinauto/COM raise an unenumerable set; the
                # read-back below is what decides the outcome, so a raise here is not the verdict.
                logger.debug("set_focus raised for hwnd=%s during UIA focus fallback", info.handle)
            if title:
                _hwnd_cache_put(title, info.handle)
            # Same read-back as the fast path: pywinauto's set_focus returns
            # the wrapper, never whether Windows honoured the request.
            outcome = _apply_foreground(int(info.handle or 0))
            outcome.setdefault("target_app_executable", info.executable)
            return outcome
    return {
        "ok": False,
        "error_category": "COMPUTER_USE_WINDOW_NOT_FOUND",
        "reason": "No matching window was found",
    }


def describe_desktop(monitor: Any) -> str:
    """Build the short text summary included with screenshots."""
    foreground = get_foreground_window_info()
    if foreground is None:
        foreground_text = "unknown"
    elif foreground.title:
        foreground_text = "%s (%s)" % (foreground.title, foreground.executable or "unknown")
    else:
        foreground_text = foreground.executable or "unknown"

    try:
        visible = list_windows(include_minimized=False, include_titles=False)
        apps = []
        for item in visible.get("windows", []):
            if isinstance(item, dict):
                executable = str(item.get("executable", "") or "")
                if executable and executable not in apps:
                    apps.append(executable)
        if foreground is not None and foreground.executable in apps:
            apps.remove(foreground.executable)
        background = ", ".join(apps[:5]) if apps else "none"
    except Exception:
        logger.debug("Could not summarize background apps")
        background = "unknown"

    return "Foreground: %s | Active monitor: %s (%sx%s) | Background apps: %s" % (
        foreground_text,
        getattr(monitor, "monitor_id", "?"),
        getattr(monitor, "width", "?"),
        getattr(monitor, "height", "?"),
        background,
    )
