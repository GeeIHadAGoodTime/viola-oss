"""Vision tools for the agent tool-use system.

Provides screen capture and analysis operations for the screen-awareness
feature.  The ``analyze_screen`` tool captures a screenshot, applies
privacy filtering, and delegates to a vision-capable LLM for analysis.

Security:
    Screenshots containing sensitive applications (password managers,
    banking, private browsing) are rejected before being sent to any
    LLM.  The privacy check is mandatory and cannot be disabled.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Mapping
from typing import Any

from core.logging_config import get_logger
from core.mode_manager import ModeManager, ViolaMode

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Privacy filter — blocks screenshots of sensitive applications
# ---------------------------------------------------------------------------

_PRIVACY_BLOCKED_TITLES: tuple[str, ...] = (
    "1password",
    "bitwarden",
    "dashlane",
    "keepass",
    "lastpass",
    "keychain",
    "credential",
    "password manager",
    "private browsing",
    "incognito",
    "inprivate",
)


def _is_privacy_blocked(window_title: str) -> bool:
    """Return True if the active window should not be captured.

    Checks the window title against a list of known sensitive
    application patterns (password managers, private browsing, etc.).
    """
    title_lower = window_title.lower()
    return any(blocked in title_lower for blocked in _PRIVACY_BLOCKED_TITLES)


# ---------------------------------------------------------------------------
# Screen capture helpers — cross-platform (Windows + macOS/darwin)
#
# The foreground-window primitives (title, bounds, cursor) are platform native.
# Each dispatches by ``sys.platform``.  The privacy-critical contract is:
#
#   ``_get_active_window_title`` returns ``str | None``.  ``None`` is the
#   FAIL-CLOSED signal — the platform could not reliably determine which
#   window is in front (or read its title), so the caller MUST refuse to
#   capture.  Only a concrete ``str`` (even an empty one, meaning "determined
#   to have no title") permits the privacy allowlist check to run and capture
#   to proceed.  A missing title must never silently default to "capture".
# ---------------------------------------------------------------------------

# CoreGraphics window-info dictionary keys (values are these exact strings, so
# the pure helpers below read them by literal string — no pyobjc import needed,
# which keeps the darwin branch unit-testable on any OS).
_CG_KEY_OWNER_PID = "kCGWindowOwnerPID"
_CG_KEY_LAYER = "kCGWindowLayer"
_CG_KEY_NAME = "kCGWindowName"
_CG_KEY_OWNER_NAME = "kCGWindowOwnerName"
_CG_KEY_BOUNDS = "kCGWindowBounds"


def _get_active_window_title() -> str | None:
    """Return the foreground window title, or ``None`` if indeterminate.

    ``None`` means the platform could not reliably determine the foreground
    window's title.  Callers MUST treat ``None`` as fail-closed and refuse to
    capture — never as "no sensitive app, go ahead".
    """
    if sys.platform == "win32":
        return _win_active_window_title()
    if sys.platform == "darwin":
        return _darwin_active_window_title()
    # Unknown platform: we have no reliable way to read the foreground title,
    # so we cannot run the privacy check. Fail closed.
    logger.debug("Active-window title unavailable on platform %s; failing closed", sys.platform)
    return None


def _get_active_window_rect() -> tuple[int, int, int, int] | None:
    """Return the foreground window bbox as (left, top, right, bottom)."""
    if sys.platform == "win32":
        return _win_active_window_rect()
    if sys.platform == "darwin":
        return _darwin_active_window_rect()
    logger.debug("Active-window bounds unavailable on platform %s", sys.platform)
    return None


def _get_cursor_pos() -> tuple[int, int] | None:
    """Return the current cursor position as (x, y) in top-left screen coords."""
    if sys.platform == "win32":
        return _win_cursor_pos()
    if sys.platform == "darwin":
        return _darwin_cursor_pos()
    logger.debug("Cursor position unavailable on platform %s", sys.platform)
    return None


def _get_all_visible_window_titles() -> list[str] | None:
    """Return titles of every visible top-level window, for full_screen privacy screening.

    ``full_screen`` capture grabs everything on screen, not just the
    foreground app, so the privacy check must screen every visible window's
    title (e.g. a password manager sitting in the background), not only the
    foreground one. Returns ``None`` (fail-closed) if the window list itself
    cannot be read.
    """
    if sys.platform == "win32":
        return _win_all_visible_window_titles()
    if sys.platform == "darwin":
        return _darwin_all_visible_window_titles()
    logger.debug("Visible-window enumeration unavailable on platform %s; failing closed", sys.platform)
    return None


def _get_window_under_cursor_title() -> str | None:
    """Return the title of the top-level window under the cursor, for cursor_region screening.

    ``cursor_region`` crops around the cursor regardless of what window is
    focused, so a non-foreground window (e.g. a password manager the user's
    mouse happens to be hovering) must be screened too. Returns ``None``
    (fail-closed) if it cannot be determined.
    """
    if sys.platform == "win32":
        return _win_window_under_cursor_title()
    if sys.platform == "darwin":
        return _darwin_window_under_cursor_title()
    logger.debug("Cursor-window title unavailable on platform %s; failing closed", sys.platform)
    return None


# --- Windows (win32) implementations ---------------------------------------


def _win_active_window_title() -> str | None:
    """Windows foreground window title, or ``None`` if it cannot be read."""
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            # No foreground window (e.g. secure desktop / lock screen).
            # Indeterminate -> fail closed.
            return None
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        return buf.value
    except Exception:
        logger.debug("Could not retrieve active window title (win32)")
        return None


def _win_active_window_rect() -> tuple[int, int, int, int] | None:
    """Windows foreground window bbox as (left, top, right, bottom)."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        # Use wintypes.RECT, not a locally-defined lookalike struct:
        # services.computer_use.window_manager sets
        # ``user32.GetWindowRect.argtypes = [c_void_p, POINTER(wintypes.RECT)]``
        # at import time. That argtypes assignment lives on the shared
        # ctypes function-pointer object for ``user32.GetWindowRect``, so it
        # applies to every caller in the process, not just that module. A
        # byref() to any OTHER Structure subclass -- even one with identical
        # fields -- then fails ctypes' strict POINTER-type check with
        # ``ArgumentError: expected LP_RECT instance instead of pointer to
        # <other class>``, once that module has run anywhere earlier in the
        # process (normal in a real session: any desktop click/focus/read
        # call imports it).
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        bbox = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return None
        return bbox
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        logger.debug("Could not retrieve active window bounds (win32)")
        return None


def _win_cursor_pos() -> tuple[int, int] | None:
    """Windows cursor position as (x, y)."""
    try:
        import ctypes
        from ctypes import wintypes

        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):  # type: ignore[attr-defined]
            return None
        return (int(point.x), int(point.y))
    except (AttributeError, OSError, TypeError, ValueError):
        logger.debug("Could not retrieve cursor position (win32)")
        return None


def _win_all_visible_window_titles() -> list[str] | None:
    """Windows: titles of every visible top-level window (EnumWindows).

    Returns ``None`` (fail-closed) if the enumeration itself fails; an empty
    list is a valid "screened, nothing sensitive titled" result.
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        titles: list[str] = []

        # IMPORTANT: use the exact same primitive ctypes types (c_bool,
        # c_void_p, c_void_p) that services.computer_use.window_manager uses
        # for its own EnumWindows callback prototype, not wintypes.BOOL /
        # wintypes.HWND / wintypes.LPARAM. ctypes.WINFUNCTYPE caches/memoizes
        # by the exact tuple of type objects, so a signature built from
        # different-but-equivalent types (e.g. wintypes.BOOL is c_long, not
        # c_bool) produces a genuinely different callback TYPE. That module
        # sets ``user32.EnumWindows.argtypes`` at import time on the shared,
        # process-wide EnumWindows function pointer, so once it has run
        # anywhere in the process (any desktop click/focus/read call imports
        # it), passing a callback of a different WINFUNCTYPE fails with
        # ``ArgumentError: expected WinFunctionType instance instead of
        # WinFunctionType`` -- matching these types exactly means both
        # callers share the identical cached type, so whichever set argtypes
        # last still accepts the other's callback.
        wndenumproc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _enum_proc(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value:
                titles.append(buf.value)
            return True

        callback = wndenumproc(_enum_proc)
        if not user32.EnumWindows(callback, 0):
            logger.debug("EnumWindows reported failure; failing closed for full-screen privacy scan")
            return None
        return titles
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        logger.debug("Could not enumerate visible windows (win32)")
        return None


def _win_window_under_cursor_title() -> str | None:
    """Windows: title of the top-level window under the current cursor position."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        point = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            return None
        hwnd = user32.WindowFromPoint(point)
        if not hwnd:
            return None

        # GA_ROOT = 2: walk up to the top-level owner window so a click deep
        # inside a child control still screens the whole app window, not just
        # that control's (often title-less) hwnd.
        ga_root = 2
        root_hwnd = user32.GetAncestor(hwnd, ga_root) or hwnd

        length = user32.GetWindowTextLengthW(root_hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(root_hwnd, buf, length + 1)
        return buf.value
    except (AttributeError, OSError, TypeError, ValueError, ctypes.ArgumentError):
        logger.debug("Could not determine window under cursor (win32)")
        return None


# --- macOS (darwin) implementations ----------------------------------------
#
# Quartz / CoreGraphics is reached through pyobjc, which macOS ships transitively
# via ``pyautogui`` (a bundled desktop dependency) — no new package is added.
# The pure ``_darwin_*_from_window`` helpers take plain dicts so the platform
# logic (including the fail-closed title rule) is unit-testable off a Mac.


def _darwin_frontmost_window(
    window_infos: list[Mapping[str, Any]] | None,
    frontmost_pid: int | None,
) -> Mapping[str, Any] | None:
    """Pure: pick the frontmost on-screen normal window owned by the app.

    CoreGraphics returns windows front-to-back, so the first layer-0 window
    owned by the frontmost application's pid is the one the user is looking at.
    """
    if not window_infos or frontmost_pid is None:
        return None
    for win in window_infos:
        try:
            if int(win.get(_CG_KEY_OWNER_PID, -1)) != int(frontmost_pid):
                continue
            # Layer 0 is the normal window layer; menus/overlays sit above it.
            if int(win.get(_CG_KEY_LAYER, 0)) != 0:
                continue
        except (TypeError, ValueError):
            continue
        return win
    return None


def _darwin_title_from_window(win: Mapping[str, Any] | None) -> str | None:
    """Pure: composite "OwnerApp WindowTitle", or ``None`` if indeterminate.

    FAIL-CLOSED: if the window-name key is absent we cannot read the window
    title (on macOS this is exactly what happens without Screen Recording
    permission — the owner app name is still visible but the per-window title
    is withheld). Since the privacy blocklist includes window-title-only
    signals ("private browsing", "incognito"), an unreadable title means we
    cannot rule out a sensitive window, so we return ``None`` and refuse to
    capture rather than falling back to the app name alone.
    """
    if win is None:
        return None
    if _CG_KEY_NAME not in win:
        return None
    owner = str(win.get(_CG_KEY_OWNER_NAME, "") or "")
    name = str(win.get(_CG_KEY_NAME, "") or "")
    return ("%s %s" % (owner, name)).strip()


def _darwin_bounds_from_window(
    win: Mapping[str, Any] | None,
) -> tuple[int, int, int, int] | None:
    """Pure: (left, top, right, bottom) from a window's kCGWindowBounds."""
    if not win:
        return None
    bounds = win.get(_CG_KEY_BOUNDS)
    if not isinstance(bounds, Mapping):
        return None
    try:
        left = int(bounds["X"])
        top = int(bounds["Y"])
        width = int(bounds["Width"])
        height = int(bounds["Height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return (left, top, left + width, top + height)


def _darwin_active_window_info() -> Mapping[str, Any] | None:
    """Fetch the frontmost window's CoreGraphics info dict (or ``None``)."""
    try:
        import Quartz  # type: ignore[import-not-found]
        from AppKit import NSWorkspace  # type: ignore[import-not-found]

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = int(app.processIdentifier())
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        window_infos = list(raw) if raw else []
        return _darwin_frontmost_window(window_infos, pid)
    except Exception:  # noqa: BLE001, RUF100 - Quartz can raise anything; vision must degrade, not crash
        logger.debug("Could not query macOS foreground window via Quartz")
        return None


def _darwin_active_window_title() -> str | None:
    """macOS foreground window title, or ``None`` (fail-closed) if unreadable."""
    win = _darwin_active_window_info()
    if win is None:
        logger.debug("macOS foreground window indeterminate; failing closed")
        return None
    title = _darwin_title_from_window(win)
    if title is None:
        logger.debug(
            "macOS window title unreadable (Screen Recording permission?); failing closed",
        )
    return title


def _darwin_onscreen_normal_windows() -> list[Mapping[str, Any]] | None:
    """Fetch every on-screen, normal-layer (layer 0) CoreGraphics window info dict."""
    try:
        import Quartz  # type: ignore[import-not-found]

        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        window_infos = list(raw) if raw else []
    except Exception:  # noqa: BLE001, RUF100 - Quartz can raise anything; vision must degrade, not crash
        logger.debug("Could not enumerate macOS on-screen windows via Quartz")
        return None
    normal: list[Mapping[str, Any]] = []
    for win in window_infos:
        try:
            if int(win.get(_CG_KEY_LAYER, 0)) != 0:
                continue
        except (TypeError, ValueError):
            continue
        normal.append(win)
    return normal


def _darwin_all_visible_window_titles() -> list[str] | None:
    """macOS: titles of every on-screen normal-layer window (full_screen privacy screen).

    FAIL-CLOSED like ``_darwin_active_window_title``: if any window's title
    is unreadable (typically because Screen Recording permission isn't
    granted, which withholds the per-window name for every window
    uniformly), we cannot rule that window out, so the whole scan fails
    closed rather than silently omitting it.
    """
    windows = _darwin_onscreen_normal_windows()
    if windows is None:
        return None
    titles: list[str] = []
    for win in windows:
        title = _darwin_title_from_window(win)
        if title is None:
            logger.debug("macOS window title unreadable during full-screen privacy scan; failing closed")
            return None
        if title:
            titles.append(title)
    return titles


def _darwin_window_under_cursor_title() -> str | None:
    """macOS: title of the topmost on-screen window whose bounds contain the cursor."""
    pos = _darwin_cursor_pos()
    if pos is None:
        return None
    x, y = pos
    windows = _darwin_onscreen_normal_windows()
    if windows is None:
        return None
    # CGWindowListCopyWindowInfo returns windows front-to-back, so the first
    # bounds match is the topmost window under the cursor.
    for win in windows:
        bounds = _darwin_bounds_from_window(win)
        if bounds is None:
            continue
        left, top, right, bottom = bounds
        if left <= x < right and top <= y < bottom:
            return _darwin_title_from_window(win)
    return None


def _darwin_active_window_rect() -> tuple[int, int, int, int] | None:
    """macOS foreground window bbox as (left, top, right, bottom)."""
    return _darwin_bounds_from_window(_darwin_active_window_info())


def _darwin_cursor_pos() -> tuple[int, int] | None:
    """macOS cursor position as (x, y) in top-left screen coords."""
    try:
        import Quartz  # type: ignore[import-not-found]

        # CGEventGetLocation returns top-left-origin coords, matching the
        # coordinate space ImageGrab expects for a bbox.
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        return (int(loc.x), int(loc.y))
    except Exception:  # noqa: BLE001, RUF100 - Quartz can raise anything; vision must degrade, not crash
        logger.debug("Could not retrieve cursor position (darwin)")
        return None


def _capture_screenshot(capture_mode: str = "active_window") -> bytes | None:
    """Capture a screenshot and return it as PNG bytes.

    Args:
        capture_mode: One of ``"active_window"``, ``"full_screen"``,
                      or ``"cursor_region"``.

    Returns:
        PNG image bytes, or ``None`` if capture failed.
    """
    try:
        import io

        from PIL import ImageGrab  # type: ignore[import-untyped]

        if capture_mode == "full_screen":
            img = ImageGrab.grab(all_screens=True)
        elif capture_mode == "cursor_region":
            # Capture an 800x600 region centred on the cursor
            pos = _get_cursor_pos()
            if pos is None:
                logger.warning("Cursor-region capture unavailable; refusing full-screen fallback")
                return None
            x, y = pos
            bbox = (x - 400, y - 300, x + 400, y + 300)
            img = ImageGrab.grab(bbox=bbox)
        else:
            bbox = _get_active_window_rect()
            if bbox is None:
                logger.warning("Active-window capture unavailable; refusing full-screen fallback")
                return None
            img = ImageGrab.grab(bbox=bbox)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except ImportError:
        logger.warning("Pillow not installed — screen capture unavailable")
        return None
    except Exception:
        logger.exception("Screen capture failed")
        return None


def _current_user_id_for_llm_accounting() -> str:
    """Resolve the account for vision-LLM spend accounting, or fail closed.

    SAFETY CORE (spend + multi-tenant): vision spend MUST be attributed to a real
    account and checked against that account's cap. Per the Multi-Tenant Rule
    ("if your code doesn't have a user_id, it fails loudly — never silently falls
    back to global"), a vision call whose user context cannot be resolved must NOT
    reserve/settle budget under an empty ("global") user_id — that leaves the
    spend unattributed and unchecked against the real user's cap (#2768). So this
    raises ``VisionAnalysisUnavailable`` (which the caller maps to a clean
    success=False) instead of returning "".
    """
    from core.user_context import is_placeholder_user_id

    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
    except LookupError as exc:
        raise VisionAnalysisUnavailable(
            "I couldn't confirm which account this is for, so I can't analyse the screen right now.",
        ) from exc
    except Exception as exc:
        logger.exception("Could not resolve current user for vision LLM accounting")
        raise VisionAnalysisUnavailable(
            "I couldn't confirm which account this is for, so I can't analyse the screen right now.",
        ) from exc

    if is_placeholder_user_id(user_id):
        raise VisionAnalysisUnavailable(
            "I couldn't confirm which account this is for, so I can't analyse the screen right now.",
        )
    return user_id


# ---------------------------------------------------------------------------
# Follow-up session tracking
# ---------------------------------------------------------------------------


class ScreenInsightSession:
    """Tracks context for follow-up questions about the same screenshot.

    After the initial ``analyze_screen`` call, the user may ask follow-up
    questions ("what about the second paragraph?").  The session keeps the
    last analysis result so the agent can reference it without re-capturing.
    """

    _instance: ScreenInsightSession | None = None

    @classmethod
    def get_instance(cls) -> ScreenInsightSession:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self.last_analysis: str = ""
        self.last_question: str = ""
        self.last_capture_mode: str = ""
        self.last_timestamp: float = 0.0

    def update(
        self,
        analysis: str,
        question: str,
        capture_mode: str,
    ) -> None:
        """Record the latest analysis for follow-up context."""
        self.last_analysis = analysis
        self.last_question = question
        self.last_capture_mode = capture_mode
        self.last_timestamp = time.monotonic()

    def has_recent_context(self, max_age_s: float = 120.0) -> bool:
        """Return True if a recent analysis is available for follow-up."""
        if not self.last_analysis:
            return False
        return (time.monotonic() - self.last_timestamp) < max_age_s


# ---------------------------------------------------------------------------
# Tool function (exposed to agent / MCP)
# ---------------------------------------------------------------------------


async def analyze_screen(
    question: str = "",
    capture_mode: str = "active_window",
) -> dict[str, Any]:
    """Capture the screen and analyse it with a vision LLM.

    Args:
        question:     Optional question to guide the analysis
                      (e.g., "what's the error on screen?").
        capture_mode: One of ``"active_window"``, ``"full_screen"``,
                      ``"cursor_region"``.

    Returns:
        ``{"success": bool, "message": str, "data": {...}}``

    Security:
        If the active window matches a privacy-blocked pattern
        (password managers, private browsing, etc.), the capture
        is refused and a safe error is returned.
    """
    valid_modes = ("active_window", "full_screen", "cursor_region")
    if capture_mode not in valid_modes:
        return {
            "success": False,
            "message": "Invalid capture_mode. Use one of: %s" % ", ".join(valid_modes),
        }

    # --- Privacy check (FAIL CLOSED) ---
    # A ``None`` title means the platform could not reliably determine which
    # window is in front (or read its title). We must NOT capture in that case
    # — the privacy allowlist can only run against a known title, so an unknown
    # title is treated as potentially-sensitive and refused.
    window_title = _get_active_window_title()
    if window_title is None:
        logger.warning(
            "Screen capture blocked: foreground window/title indeterminate (privacy fail-closed)",
        )
        return {
            "success": False,
            "message": "I couldn't confirm which window is in front, so I skipped the screen to protect your privacy.",
        }
    if _is_privacy_blocked(window_title):
        logger.warning(
            "Screen capture blocked by privacy filter: %s",
            window_title[:60],
        )
        return {
            "success": False,
            "message": "I skip password managers to protect your security.",
        }

    # The check above only screens the FOREGROUND window's title. That's
    # sufficient for "active_window" capture (it captures exactly that
    # window), but "full_screen" grabs every screen — including a
    # non-foreground password manager sitting in the background — and
    # "cursor_region" grabs a box around the cursor, which may be hovering a
    # sensitive window that never had focus. Pre-fix, both bypassed the
    # privacy contract entirely (#2773 item 6). Screen the actual capture
    # surface for each mode, still fail-closed on an indeterminate read.
    if capture_mode == "full_screen":
        visible_titles = _get_all_visible_window_titles()
        if visible_titles is None:
            logger.warning(
                "Screen capture blocked: could not enumerate visible windows (privacy fail-closed)",
            )
            return {
                "success": False,
                "message": "I couldn't confirm what's visible on screen, so I skipped the screen to protect your privacy.",
            }
        blocked_title = next((t for t in visible_titles if _is_privacy_blocked(t)), None)
        if blocked_title is not None:
            logger.warning(
                "Screen capture blocked by privacy filter (full_screen, background window): %s",
                blocked_title[:60],
            )
            return {
                "success": False,
                "message": "I skip password managers to protect your security.",
            }
    elif capture_mode == "cursor_region":
        cursor_window_title = _get_window_under_cursor_title()
        if cursor_window_title is None:
            logger.warning(
                "Screen capture blocked: could not determine window under cursor (privacy fail-closed)",
            )
            return {
                "success": False,
                "message": "I couldn't confirm what's under the cursor, so I skipped the screen to protect your privacy.",
            }
        if _is_privacy_blocked(cursor_window_title):
            logger.warning(
                "Screen capture blocked by privacy filter (cursor_region): %s",
                cursor_window_title[:60],
            )
            return {
                "success": False,
                "message": "I skip password managers to protect your security.",
            }

    # --- Enter screen-look mode ---
    mgr = ModeManager.get_instance()
    previous_mode = mgr.current_mode
    mgr.enter_mode(ViolaMode.SCREEN_LOOK)

    try:
        # --- Capture ---
        # macOS screen capture (mss -> CoreGraphics) is a synchronous call that
        # blocks in mach_msg on the window server. Without the Screen Recording
        # TCC grant it can block indefinitely; run inline on the asyncio event
        # loop it froze the ENTIRE app (health + every request hung for minutes,
        # confirmed via `sample`: main thread parked in mach_msg). Offload it to
        # a worker thread AND bound it with a timeout so a missing-permission
        # hang fails gracefully with an actionable message instead of freezing
        # the server. Windows GDI capture never blocks like this — macOS-specific.
        try:
            image_bytes = await asyncio.wait_for(
                asyncio.to_thread(_capture_screenshot, capture_mode),
                timeout=12.0,
            )
        except TimeoutError:
            logger.warning("Screen capture timed out (Screen Recording permission likely not granted)")
            return {
                "success": False,
                "message": (
                    "Screen capture timed out. On macOS, grant Screen Recording to Viola in "
                    "System Settings > Privacy & Security > Screen Recording, then try again."
                ),
            }
        if image_bytes is None:
            return {
                "success": False,
                "message": "Screen capture failed. Ensure Pillow is installed.",
            }

        # --- Build LLM prompt ---
        # Trace e07d914d762e showed the agent burning ~45s on 11 analyze_screen
        # calls because each one only answered a single narrow question and
        # the agent had to follow up. Enrich the default response so a single
        # call surfaces the broad state the agent typically needs (foreground
        # app, dominant content, any open dialog/modal/menu, any error or
        # notification text, and command-result indicators when chat is
        # involved). The narrow user question still gets a direct answer.
        prompt_parts = [
            "Describe the screen and answer the user's question if any. Cover, in this order:",
            "(1) Foreground application/window and what kind of context it is in (menu, dialog, in-game, document, web page, etc.).",
            "(2) Any dominant content, modals, dialogs, or notifications.",
            "(3) Any chat/console/command-line text visible (verbatim if short).",
            "(4) Any error, warning, or status text visible (verbatim if short).",
        ]
        if question:
            prompt_parts.append("(5) Direct answer to the user's question: %s" % question)
        else:
            prompt_parts.append("(5) Anything notable that an automation agent would want to know.")

        # Check for follow-up context
        session = ScreenInsightSession.get_instance()
        if session.has_recent_context() and not question:
            prompt_parts.append(
                "Previous analysis for context: %s" % session.last_analysis[:500],
            )

        prompt = " ".join(prompt_parts)

        # --- Analyse with vision LLM ---
        analysis_text = await _call_vision_llm(prompt, image_bytes)

        # --- Record for follow-up ---
        session.update(
            analysis=analysis_text,
            question=question,
            capture_mode=capture_mode,
        )

        logger.info(
            "Screen analysis complete: %d chars, mode=%s",
            len(analysis_text),
            capture_mode,
        )

        return {
            "success": True,
            "message": analysis_text,
            "data": {
                "capture_mode": capture_mode,
                "window_title": window_title,
                "question": question,
                "analysis_length": len(analysis_text),
            },
        }

    except VisionAnalysisUnavailable as exc:
        # The screen was captured but the vision LLM call failed. This must NOT
        # be reported as success=True with the error sentence as the "analysis"
        # (the computer_use server maps ok from success, so the model would be
        # told the screen was read).
        logger.info("analyze_screen vision analysis unavailable: %s", exc)
        return {
            "success": False,
            "message": str(exc),
        }
    except Exception:
        logger.exception("analyze_screen failed")
        return {
            "success": False,
            "message": "Screen analysis encountered an error. Please try again.",
        }
    finally:
        # Return to previous mode (unless something else changed it)
        if mgr.current_mode == ViolaMode.SCREEN_LOOK:
            if previous_mode == ViolaMode.SCREEN_LOOK:
                mgr.exit_to_normal()
            else:
                mgr.enter_mode(previous_mode)


class VisionAnalysisUnavailable(Exception):
    """The vision LLM call could not be completed.

    Raised (not returned as a string) so the caller can report success=False
    instead of handing the model an error sentence dressed up as a successful
    screen analysis. Carries a user-facing message.
    """


async def _call_vision_llm(prompt: str, image_bytes: bytes) -> str:
    """Send the screenshot to a vision-capable LLM and return the analysis.

    Uses OpenAI Responses directly because the provider router does not support
    image inputs yet. Raises ``VisionAnalysisUnavailable`` when the call cannot
    be completed (no API key, missing dependency, provider error) so the caller
    never reports a failed analysis as a success.
    """
    try:
        import base64

        import config.defaults as defaults
        from config.settings import settings
        from services.llm.openai_utils import (
            OPENAI_DEFAULT_BASE_URL,
            extract_message_text_from_response,
        )
        from services.llm.providers.openai_compatible import _is_reasoning_model

        api_key = settings.openai_api_key
        if not api_key:
            raise VisionAnalysisUnavailable("Vision analysis requires an API key. Check your settings.")

        import openai

        image_b64 = base64.b64encode(image_bytes).decode("ascii")

        # SEC-08: vision payload contains the user's screen — explicitly
        # opt out of server-side storage unless the user has consented.
        from services.llm.openai_consent import enforce_storage_consent

        model = defaults.DEFAULT_AGENT_MODEL
        _api_kwargs: dict = {
            "model": model,
            "instructions": (
                "You are Viola's screen-awareness module. The user has shared "
                "what is currently visible on their display. Analyse the visual "
                "content and give a concise, helpful answer. Be direct. Never "
                "mention you are looking at a 'screenshot' - speak as though "
                "you can see the user's screen naturally."
            ),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,%s" % image_b64,
                        },
                    ],
                },
            ],
        }
        # Vision is a perception task, not a reasoning task. With agent-default
        # effort=high and max_output_tokens=500, gpt-5.x models consume the full
        # token budget on internal reasoning and return empty visible output —
        # observed live as a 52-char fallback message ("I could see the screen
        # but couldn't form a response.") on 22 of 28 analyze_screen calls in
        # the canonical Steam Hades II drive (trace 1abcd3e00be0). Force minimal
        # reasoning effort and raise the cap so ample tokens remain for visible
        # output.
        _api_kwargs["max_output_tokens"] = 2000
        if _is_reasoning_model(model):
            # gpt-5.x rejects 'minimal' (only 'none|low|medium|high|xhigh').
            # Use 'none' — vision is perception, not reasoning.
            _api_kwargs["reasoning"] = {
                "effort": "none",
                "summary": "auto",
            }
        else:
            _api_kwargs["temperature"] = 0.3
        enforce_storage_consent(_api_kwargs)

        from services.llm.spend_accounting import (
            LlmSpendReservation,
            estimate_openai_payload_usage,
            usage_from_openai_response,
        )

        estimated_usage = estimate_openai_payload_usage(_api_kwargs, default_output_tokens=2000)
        # user_id is resolved fail-closed BEFORE any reservation: an unresolved
        # account raises VisionAnalysisUnavailable here and nothing is reserved.
        reservation = LlmSpendReservation(
            user_id=_current_user_id_for_llm_accounting(),
            model=model,
            estimated_usage=estimated_usage,
            operation="vision_screen_analysis",
        )

        # SAFETY CORE (spend): everything from reserve() onward runs inside ONE
        # try/finally so the reservation is ALWAYS settled — including when
        # openai.AsyncOpenAI(...) (or anything else before responses.create)
        # raises. Previously only responses.create was settle-guarded, so a
        # client-construction error between reserve() and the inner try leaked
        # the reserved budget forever (#2768). settle() is idempotent: the
        # success path settles with actual usage and the finally's
        # settle(failed=True) is then a no-op; on any error path (partial
        # reserve, client construction, the network call) the finally releases
        # the hold.
        try:
            await reservation.reserve()
            client = openai.AsyncOpenAI(api_key=api_key, base_url=OPENAI_DEFAULT_BASE_URL)
            response = await client.responses.create(**_api_kwargs)
            await reservation.settle(usage_from_openai_response(response, estimated_usage))
            return (
                extract_message_text_from_response(response) or "I could see the screen but couldn't form a response."
            )
        finally:
            await reservation.settle(failed=True)

    except VisionAnalysisUnavailable:
        raise
    except ImportError as exc:
        logger.warning("openai package not available for vision analysis")
        raise VisionAnalysisUnavailable(
            "I captured the screen but cannot analyse it right now — the openai package is not installed."
        ) from exc
    except Exception as exc:
        logger.exception("Vision LLM call failed")
        raise VisionAnalysisUnavailable(
            "I captured the screen but the vision analysis failed. Please check your LLM provider configuration."
        ) from exc
