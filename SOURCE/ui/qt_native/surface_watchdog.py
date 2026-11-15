"""Notice a window that stopped showing the page, and bring it back.

On Jay's Surface the window has gone fully black or frozen while the process,
the page and the accessibility tree all stayed fine, and only a restart cured
it (1.0.5 build 9: three times on 11-01, once on 11-05). Nothing in the process
knows when this happens, because everything up to the last step still works.

So the check runs end to end, from the page to the glass. A small dot near the
page's bottom-right corner changes shade (the canary), and a moment later the same
spot is read back from the screen, after Chromium, Qt and the Windows
compositor have all had their turn. If the new shade never arrives, a link in
that chain is dead, whatever the cause.

Where the dot is read comes from the page itself: the probe returns the dot's
box and the page's pixel ratio, and that box is placed on the screen through
the window's real client rectangle (Win32), not through Qt's idea of where the
view is. In 1.0.5 build 11 the screen was read at a point worked out from Qt's
view geometry, two CSS px in from the corner, on a maximized frameless window
whose client area Windows pulls in by the frame thickness. The read missed the
dot all day (83 errors, 19 page reloads). The dot also keeps 16 px away from
the window's edges, clear of the corner rounding and border Windows 11 draws on
a window that is not maximized.

Recovery climbs a ladder, cheapest first, and stops as soon as the canary shows
again: redraw the page view, resize the window, rebuild the native window (new
swap chain). A missed dot on a page that still answers never reloads the page:
after those three steps the watchdog stops for the session with one warning.
Only a page that stopped answering the probe gets the last step, a reload, and
never while someone could lose something by it (a live call, a running task,
text typed and not sent) or while nobody is at the computer.

The screen read can be blind on a given machine, and a new window layout can
put the dot somewhere the read does not reach, so the watchdog judges nothing
from the screen until it has seen the canary land in the current layout. Any
change of window size, position, state or screen starts that over.
"""

from __future__ import annotations

import ctypes
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from core.logging_config import get_logger

logger = get_logger(__name__)

PROBE_INTERVAL_MS = 5000
# Chromium, Qt and the compositor get this long to put the new shade on screen.
READ_DELAY_MS = 1200
# Two misses in a row (about 11 s) before anything happens: a window animating
# or being resized is not a dead window.
MISSES_BEFORE_RECOVERY = 2
# The page gave no answer to the probe for this many rounds: its renderer hangs.
SILENT_ROUNDS_BEFORE_RECOVERY = 3
# After a reload, leave the window alone this long before trying again.
GIVE_UP_ROUNDS = 60
USER_ACTIVE_WINDOW_S = 120.0
# Window moves and resizes made by a recovery step itself do not start the
# calibration over; this covers the bounce and the rebuild with margin.
OWN_CHANGE_GRACE_S = 3.0

# The dot: 6 x 6 CSS px, 32 px in from the right and 16 px up from the bottom,
# clear of the 8 px corner radius (16 physical px at 200 %) and the 1 px border
# Windows 11 draws on a window that is not maximized, and of any frame inset.
CANARY_WIDTH_CSS_PX = 6
CANARY_HEIGHT_CSS_PX = 6
CANARY_RIGHT_CSS_PX = 32
CANARY_BOTTOM_CSS_PX = 16
# Kept for older readers of this module: the dot's size.
CANARY_SIZE_CSS_PX = CANARY_WIDTH_CSS_PX
# Close to the page's dark background, so nobody sees it change, but far enough
# apart that colour management cannot make one read as the other.
CANARY_SHADES = ((24, 24, 28), (66, 66, 76))

# Windows 11 corner radius at 100 % scale, in physical px.
WINDOWS_CORNER_RADIUS_PX = 8
WINDOWS_BORDER_PX = 1

# Reasons a reload would lose something the user can see.
KEEP_LIVE_CALL = "live_call"
KEEP_RUNNING_TASK = "running_task"
KEEP_DRAFT = "draft"


def canary_read_offset_css() -> tuple[float, float]:
    """Where the screen is read: (CSS px in from the right, CSS px up from the bottom)."""
    return (
        CANARY_RIGHT_CSS_PX + CANARY_WIDTH_CSS_PX / 2.0,
        CANARY_BOTTOM_CSS_PX + CANARY_HEIGHT_CSS_PX / 2.0,
    )


def point_is_inside_rounded_window(
    from_right_px: float,
    from_bottom_px: float,
    *,
    scale: float,
    radius_px: float = WINDOWS_CORNER_RADIUS_PX,
    border_px: float = WINDOWS_BORDER_PX,
) -> bool:
    """True when a physical pixel this far in from the bottom-right corner shows the window.

    ``from_right_px`` and ``from_bottom_px`` count to the pixel's centre from the
    window's outer edges. The window's own pixels are those inside the rounded
    corner and clear of the border Windows draws on the edge.
    """
    radius = radius_px * scale
    border = border_px * scale
    if from_right_px <= border or from_bottom_px <= border:
        return False
    if from_right_px >= radius or from_bottom_px >= radius:
        return True
    # Inside the corner square: the pixel must lie within the arc, less the border.
    dx = radius - from_right_px
    dy = radius - from_bottom_px
    return (dx * dx + dy * dy) ** 0.5 <= radius - border


PROBE_SCRIPT = """
(function (shade) {
  var id = '__viola_surface_canary';
  var dot = document.getElementById(id);
  if (!dot) {
    dot = document.createElement('div');
    dot.id = id;
    dot.setAttribute('aria-hidden', 'true');
    dot.style.cssText = 'position:fixed;right:%(right)dpx;bottom:%(bottom)dpx;width:%(width)dpx;height:%(height)dpx;'
      + 'z-index:2147483647;pointer-events:none;';
    document.documentElement.appendChild(dot);
  }
  dot.style.backgroundColor = shade;
  // Anything drawn over the dot (a dialog in the top layer, a dimming
  // backdrop, a full-screen element) changes the pixel without the window
  // being dead. elementFromPoint skips pointer-events:none, so let the dot
  // take hits for this one synchronous check.
  var covered = !!document.fullscreenElement;
  try {
    if (!covered && document.querySelector(':modal')) covered = true;
  } catch (e) {}
  if (!covered) {
    var modal = document.querySelector('[aria-modal="true"], dialog[open]');
    if (modal && modal.getClientRects().length) covered = true;
  }
  if (!covered) {
    var box = dot.getBoundingClientRect();
    dot.style.pointerEvents = 'auto';
    var hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
    dot.style.pointerEvents = 'none';
    covered = hit !== dot;
  }
  // What a reload would throw away.
  var keep = [];
  var root = document.documentElement;
  if (root.getAttribute('data-viola-live-call') === '1') keep.push('%(call)s');
  if (root.getAttribute('data-viola-task-running') === '1'
      || document.querySelector('button[aria-label="Stop response"]')) keep.push('%(task)s');
  var fields = document.querySelectorAll('textarea, input:not([type]), input[type="text"], input[type="search"], '
    + 'input[type="email"], input[type="url"], input[type="tel"], [contenteditable="true"], [contenteditable=""]');
  for (var i = 0; i < fields.length; i++) {
    var f = fields[i];
    var text = f.isContentEditable ? f.textContent : f.value;
    if (text && String(text).trim()) { keep.push('%(draft)s'); break; }
  }
  // A string: PySide hands a plain object to the callback as an empty value.
  var b = dot.getBoundingClientRect();
  return JSON.stringify({
    visible: document.visibilityState === 'visible',
    covered: covered,
    keep: keep,
    dot: [b.left, b.top, b.width, b.height],
    viewport: [window.innerWidth, window.innerHeight],
    dpr: window.devicePixelRatio || 1,
  });
})
""" % {
    "right": CANARY_RIGHT_CSS_PX,
    "bottom": CANARY_BOTTOM_CSS_PX,
    "width": CANARY_WIDTH_CSS_PX,
    "height": CANARY_HEIGHT_CSS_PX,
    "call": KEEP_LIVE_CALL,
    "task": KEEP_RUNNING_TASK,
    "draft": KEEP_DRAFT,
}


class Rung(str, Enum):
    """One step of the recovery ladder, cheapest first."""

    REDRAW_VIEW = "redraw_view"
    RESIZE_WINDOW = "resize_window"
    REBUILD_WINDOW = "rebuild_window"
    RELOAD_PAGE = "reload_page"


# A page that answers but whose dot never reaches the screen: a reload cannot
# help a dead swap chain and throws away what is on the page.
MISSED_LADDER = (Rung.REDRAW_VIEW, Rung.RESIZE_WINDOW, Rung.REBUILD_WINDOW)
# A page that stopped answering: its renderer hangs, and only a reload helps.
SILENT_LADDER = (Rung.REDRAW_VIEW, Rung.RESIZE_WINDOW, Rung.REBUILD_WINDOW, Rung.RELOAD_PAGE)
LADDER = SILENT_LADDER


class Reading(str, Enum):
    """What one probe round found."""

    LANDED = "landed"  # the new shade reached the screen
    MISSED = "missed"  # the screen still shows something else
    SILENT = "silent"  # the page never answered the probe
    COVERED = "covered"  # a dialog or backdrop on the page is over the dot
    SKIPPED = "skipped"  # nothing to judge (hidden, not foreground, layout changed)


@dataclass
class SurfaceJudge:
    """Decide from probe results what to do next. No Qt, so tests drive it directly."""

    # The dot has landed in the current window layout: screen reads count.
    calibrated: bool = False
    # The dot has landed at least once this run: the page and the read work here.
    seen_once: bool = False
    misses: int = 0
    silent: int = 0
    next_rung: int = 0
    rest_rounds: int = 0
    steps_run: int = 0
    # The missed-dot ladder ran out: no more screen-based recovery this session.
    gave_up: bool = False
    held_reload_for: tuple[str, ...] = field(default=())

    def disarm(self, why: str) -> None:
        """The window layout changed: nothing counts until the dot lands again."""
        if self.calibrated or self.misses or self.silent or self.steps_run:
            logger.debug("Surface watchdog: %s; waiting to see the page in the new layout", why)
        self.calibrated = False
        self.misses = 0
        self.silent = 0
        self.next_rung = 0
        self.steps_run = 0
        self.rest_rounds = 0

    def observe(
        self,
        reading: Reading,
        *,
        user_active: bool = True,
        keep_page: tuple[str, ...] | list[str] = (),
    ) -> Rung | None:
        """Take one reading; return the recovery step to run now, if any."""
        # Only readings in a row count. Build 11 kept counting across hours of
        # skipped rounds (Viola not in front), so two stray misses or two late
        # answers far apart were enough to start the ladder.
        if reading is not Reading.MISSED:
            self.misses = 0
        if reading is not Reading.SILENT:
            self.silent = 0
        if reading in (Reading.SKIPPED, Reading.COVERED):
            return None
        if reading is Reading.LANDED:
            if self.steps_run:
                logger.warning("Surface watchdog: the page is back on screen after %d step(s)", self.steps_run)
            if not self.seen_once:
                logger.info("Surface watchdog: the page was seen on screen; watching")
            self.calibrated = True
            self.seen_once = True
            self.misses = 0
            self.silent = 0
            self.next_rung = 0
            self.rest_rounds = 0
            self.steps_run = 0
            self.held_reload_for = ()
            return None
        if reading is Reading.MISSED:
            if not self.calibrated or self.gave_up:
                return None
        elif not self.seen_once:
            # A page that never answered once in this run: nothing to judge.
            return None
        if self.rest_rounds:
            self.rest_rounds -= 1
            return None
        if reading is Reading.MISSED:
            self.misses += 1
        else:
            self.silent += 1
        hung = self.silent >= SILENT_ROUNDS_BEFORE_RECOVERY
        if not hung and self.misses < MISSES_BEFORE_RECOVERY:
            return None
        ladder = SILENT_LADDER if hung else MISSED_LADDER
        if self.next_rung >= len(ladder):
            # Only the missed-dot ladder ends here: three steps did not bring
            # the dot back while the page kept answering. Stop for the session.
            self.gave_up = True
            self.misses = 0
            logger.warning(
                "Surface watchdog: the test dot still does not reach the screen after %d step(s) while the "
                "page keeps answering; not touching the window again this session",
                self.steps_run,
            )
            return None
        rung = ladder[self.next_rung]
        if rung is Rung.RELOAD_PAGE:
            if not user_active:
                # Nobody is looking and a reload would stop the music. The next
                # touch or key press lets this step run.
                return None
            keep = tuple(keep_page)
            if keep:
                if keep != self.held_reload_for:
                    logger.warning(
                        "Surface watchdog: the page is not answering, but a reload would lose %s; holding it",
                        ", ".join(keep),
                    )
                self.held_reload_for = keep
                return None
        self.held_reload_for = ()
        self.next_rung += 1
        self.steps_run += 1
        self.misses = 0
        self.silent = 0
        if rung is Rung.RELOAD_PAGE:
            self.next_rung = 0
            self.rest_rounds = GIVE_UP_ROUNDS
        return rung


def shade_css(shade: tuple[int, int, int]) -> str:
    return "rgb(%d,%d,%d)" % shade


def nearest_shade(pixel: tuple[int, int, int]) -> int:
    """Index of the canary shade this pixel is closest to."""

    def distance(shade: tuple[int, int, int]) -> int:
        return sum((a - b) ** 2 for a, b in zip(pixel, shade, strict=True))

    return 0 if distance(CANARY_SHADES[0]) <= distance(CANARY_SHADES[1]) else 1


def pixel_shows_shade(pixel: tuple[int, int, int], index: int) -> bool:
    """True when ``pixel`` is the expected shade, not just dark or black."""
    target = CANARY_SHADES[index]
    if nearest_shade(pixel) != index:
        return False
    return max(abs(a - b) for a, b in zip(pixel, target, strict=True)) <= 14


def parse_probe_answer(result: object) -> dict:
    """The page's answer as a dict with ``visible``, ``covered`` and ``keep``."""
    try:
        state = json.loads(result) if isinstance(result, str) and result else {}
    except ValueError:
        state = {}
    if not isinstance(state, dict):
        state = {}
    keep = state.get("keep")
    return {
        "visible": bool(state.get("visible")),
        "covered": bool(state.get("covered")),
        "keep": tuple(str(k) for k in keep) if isinstance(keep, list) else (),
        "dot": _numbers(state.get("dot"), 4),
        "viewport": _numbers(state.get("viewport"), 2),
        "dpr": _positive(state.get("dpr")),
    }


def _numbers(value: object, count: int) -> tuple[float, ...] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def dot_screen_point(
    *,
    client_rect_px: tuple[int, int, int, int],
    view_offset_px: tuple[float, float],
    dot_css: tuple[float, float, float, float],
    page_dpr: float,
) -> tuple[int, int] | None:
    """The physical screen pixel at the middle of the dot, or None when the page clips it.

    ``client_rect_px`` is the window's real client area on screen (left, top,
    width, height) as Windows reports it; ``view_offset_px`` is where the page
    view starts inside that area; ``dot_css`` is the dot's box as the page
    measured it and ``page_dpr`` the page's own pixel ratio (scale times zoom).
    """
    left, top, width, height = client_rect_px
    x = left + view_offset_px[0] + (dot_css[0] + dot_css[2] / 2.0) * page_dpr
    y = top + view_offset_px[1] + (dot_css[1] + dot_css[3] / 2.0) * page_dpr
    px, py = int(x), int(y)
    if not (left <= px < left + width and top <= py < top + height):
        return None
    return px, py


def _client_rect_on_screen(hwnd: int) -> tuple[int, int, int, int] | None:
    """The window's client area on screen in physical px, from Windows itself."""
    if sys.platform != "win32" or not hwnd:
        return None
    from ctypes import wintypes

    try:
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        origin = wintypes.POINT(0, 0)
        if not user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return None
        if not user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(origin)):
            return None
    except (AttributeError, OSError, ctypes.ArgumentError):
        return None
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


def _seconds_since_last_input() -> float | None:
    """How long since the last key or touch, or None when unknown."""
    if sys.platform != "win32":
        return None

    class _LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = _LastInput()
    info.cbSize = ctypes.sizeof(_LastInput)
    try:
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    except (AttributeError, OSError):
        return None
    return ((tick - info.dwTime) & 0xFFFFFFFF) / 1000.0


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _window_owns_point(hwnd: int, x: int, y: int) -> bool:
    """True when ``hwnd`` is in front and nothing covers physical point (x, y)."""
    if sys.platform != "win32":
        return True

    ga_root = 2
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.WindowFromPoint.argtypes = (_Point,)
        foreground = user32.GetForegroundWindow()
        # A locked screen has no foreground window of ours, and reading the
        # screen there would look like a dead surface.
        if not foreground or user32.GetAncestor(foreground, ga_root) != hwnd:
            return False
        under = user32.WindowFromPoint(_Point(x, y))
        return bool(under) and user32.GetAncestor(under, ga_root) == hwnd
    except (AttributeError, OSError, ctypes.ArgumentError):
        return False


class SurfaceWatchdog:
    """Runs the probe on a timer. All Qt work stays on the UI thread."""

    def __init__(
        self,
        window: object,
        *,
        recover: Callable[[Rung], None],
        probe_allowed: Callable[[], bool],
    ) -> None:
        from PySide6.QtCore import QEvent, QObject, QTimer

        self._window = window
        self._recover = recover
        self._probe_allowed = probe_allowed
        self.judge = SurfaceJudge()
        self._shade = 0
        self._awaiting_answer = False
        self._keep_page: tuple[str, ...] = ()
        # Bumped on every layout change; a read started in an older layout is skipped.
        self._layout_epoch = 0
        self._probe_epoch = 0
        self._own_change_until = 0.0
        self._last_miss: dict | None = None
        self._page_state: dict | None = None
        self._clipped_logged = False
        self._timer = QTimer(window)
        self._timer.setInterval(PROBE_INTERVAL_MS)
        self._timer.timeout.connect(self._round)
        self._read_timer = QTimer(window)
        self._read_timer.setSingleShot(True)
        self._read_timer.setInterval(READ_DELAY_MS)
        self._read_timer.timeout.connect(self._read_screen)

        watched = {
            QEvent.Type.Move: "the window moved",
            QEvent.Type.Resize: "the window was resized",
            QEvent.Type.WindowStateChange: "the window state changed",
        }
        for name, why in (
            ("ScreenChangeInternal", "the window moved to another screen"),
            ("DevicePixelRatioChange", "the screen scale changed"),
        ):
            event_type = getattr(QEvent.Type, name, None)
            if event_type is not None:
                watched[event_type] = why
        watchdog = self

        class _LayoutWatcher(QObject):
            def eventFilter(self, obj, event):  # noqa: N802 - Qt API
                why = watched.get(event.type())
                if why is not None:
                    watchdog.layout_changed(why)
                return False

        self._layout_watcher = _LayoutWatcher(window)
        try:
            window.installEventFilter(self._layout_watcher)
            view = getattr(window, "webview", None)
            if view is not None:
                view.installEventFilter(self._layout_watcher)
        except (AttributeError, RuntimeError):
            logger.debug("Surface watchdog: layout changes not watched", exc_info=True)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
            logger.info("Surface watchdog started")

    def stop(self) -> None:
        self._timer.stop()
        self._read_timer.stop()

    def layout_changed(self, why: str) -> None:
        """Size, position, state or screen changed: wait to see the dot land again."""
        if time.monotonic() < self._own_change_until:
            return
        self._layout_epoch += 1
        self.judge.disarm(why)

    def _settle(self, reading: Reading) -> None:
        idle_s = _seconds_since_last_input()
        user_active = idle_s is None or idle_s <= USER_ACTIVE_WINDOW_S
        rung = self.judge.observe(reading, user_active=user_active, keep_page=self._keep_page)
        if rung is None:
            return
        if reading is Reading.SILENT:
            logger.error("Surface watchdog: the page is not answering (%s); running %s", reading.value, rung.value)
        else:
            logger.info(
                "Surface watchdog: the test dot did not reach the screen; running %s (%s)",
                rung.value,
                self._last_miss,
            )
        self._own_change_until = time.monotonic() + OWN_CHANGE_GRACE_S
        try:
            self._recover(rung)
        except RuntimeError:
            logger.exception("Surface watchdog: %s failed", rung.value)

    def _round(self) -> None:
        if self._awaiting_answer:
            # The last probe never came back: the renderer is not running JS.
            self._awaiting_answer = False
            self._settle(Reading.SILENT)
            return
        if not self._probe_allowed():
            self._settle(Reading.SKIPPED)
            return
        page = self._window.webview.page()
        self._shade = 1 - self._shade
        self._awaiting_answer = True
        self._probe_epoch = self._layout_epoch
        script = "%s(%r)" % (PROBE_SCRIPT, shade_css(CANARY_SHADES[self._shade]))
        page.runJavaScript(script, 0, self._on_probe_answer)

    def _on_probe_answer(self, result: object) -> None:
        self._awaiting_answer = False
        state = parse_probe_answer(result)
        self._keep_page = state["keep"]
        self._page_state = state
        if not state["visible"]:
            self._settle(Reading.SKIPPED)
            return
        if state["covered"]:
            self._settle(Reading.COVERED)
            return
        self._read_timer.start()

    def _read_screen(self) -> None:
        if self._probe_epoch != self._layout_epoch:
            self._settle(Reading.SKIPPED)
            return
        pixel = self._screen_pixel()
        if pixel is None:
            self._settle(Reading.SKIPPED)
        elif pixel_shows_shade(pixel, self._shade):
            self._settle(Reading.LANDED)
        else:
            logger.debug(
                "Surface watchdog: screen shows %s, expected shade %d (%s)", pixel, self._shade, self._last_miss
            )
            self._settle(Reading.MISSED)

    def _screen_pixel(self) -> tuple[int, int, int] | None:
        """Read the middle of the canary from the screen, or None when it can't be judged."""
        from PySide6.QtCore import QPoint

        window = self._window
        view = window.webview
        if not self._probe_allowed():
            return None
        screen = window.screen()
        if screen is None:
            return None
        ratio = screen.devicePixelRatio()
        origin = screen.geometry().topLeft()
        hwnd = int(window.winId())
        state = getattr(self, "_page_state", None) or {}
        offset = view.mapTo(window, QPoint(0, 0))
        view_offset_px = (offset.x() * ratio, offset.y() * ratio)
        client = _client_rect_on_screen(hwnd)
        if client is None:
            # Not Windows: Qt's own geometry is all there is.
            top_left = window.mapToGlobal(QPoint(0, 0))
            client = (
                origin.x() + int((top_left.x() - origin.x()) * ratio),
                origin.y() + int((top_left.y() - origin.y()) * ratio),
                int(window.width() * ratio),
                int(window.height() * ratio),
            )
        dot = state.get("dot")
        page_dpr = state.get("dpr") or ratio
        if dot is None or dot[2] <= 0 or dot[3] <= 0:
            return None
        point = dot_screen_point(client_rect_px=client, view_offset_px=view_offset_px, dot_css=dot, page_dpr=page_dpr)
        self._last_miss = self._geometry_facts(client, view_offset_px, dot, page_dpr, point)
        if point is None:
            if not self._clipped_logged:
                self._clipped_logged = True
                logger.warning(
                    "Surface watchdog: the page's corner is outside the window's client area (%s)", self._last_miss
                )
            return None
        physical_x, physical_y = point
        screen_box = screen.geometry()
        if not (
            origin.x() <= physical_x < origin.x() + screen_box.width() * ratio
            and origin.y() <= physical_y < origin.y() + screen_box.height() * ratio
        ):
            return None
        if not _window_owns_point(hwnd, physical_x, physical_y):
            return None
        return _grab_physical_pixel(screen, physical_x, physical_y)

    def _geometry_facts(self, client, view_offset_px, dot, page_dpr, point) -> dict | None:
        """What the next laptop log needs to judge a miss without guessing."""
        window = self._window
        try:
            geometry = window.geometry()
            view = window.webview
            return {
                "state": "fullscreen" if window.isFullScreen() else ("maximized" if window.isMaximized() else "normal"),
                "qt_window": (geometry.x(), geometry.y(), geometry.width(), geometry.height()),
                "qt_view": (view.width(), view.height()),
                "client_px": tuple(client),
                "view_offset_px": tuple(round(v, 1) for v in view_offset_px),
                "dot_css": tuple(round(v, 1) for v in dot),
                "page_dpr": page_dpr,
                "read_at": point,
            }
        except (AttributeError, RuntimeError):
            return None


def _grab_physical_pixel(screen: object, physical_x: int, physical_y: int) -> tuple[int, int, int] | None:
    """One physical screen pixel, read through Qt's logical grab at any scale."""
    ratio = screen.devicePixelRatio()
    origin = screen.geometry().topLeft()
    # Qt keeps a screen's corner at its physical position and scales inside it.
    logical_x = (physical_x - origin.x()) / ratio
    logical_y = (physical_y - origin.y()) / ratio
    box_x, box_y = int(logical_x) - 1, int(logical_y) - 1
    image = screen.grabWindow(0, box_x, box_y, 3, 3).toImage()
    if image.isNull() or image.width() < 1 or image.height() < 1:
        return None
    scale_x = image.width() / 3.0
    scale_y = image.height() / 3.0
    ix = min(image.width() - 1, max(0, int((logical_x - box_x) * scale_x)))
    iy = min(image.height() - 1, max(0, int((logical_y - box_y) * scale_y)))
    color = image.pixelColor(ix, iy)
    return color.red(), color.green(), color.blue()
