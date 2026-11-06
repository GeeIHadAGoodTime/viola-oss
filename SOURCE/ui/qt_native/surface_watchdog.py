"""Notice a window that stopped showing the page, and bring it back.

On Jay's Surface the window has gone fully black or frozen while the process,
the page and the accessibility tree all stayed fine, and only a restart cured
it (1.0.5 build 9: three times on 11-01, once on 11-05). Nothing in the process
knows when this happens, because everything up to the last step still works.

So the check runs end to end, from the page to the glass. A few pixels in a
corner of the page change shade (the canary), and a moment later the same
spot is read back from the screen, after Chromium, Qt and the Windows
compositor have all had their turn. If the new shade never arrives, a link in
that chain is dead, whatever the cause.

Recovery climbs a ladder, cheapest first, and stops as soon as the canary shows
again: redraw the page view, resize the window, rebuild the native window (new
swap chain), and last reload the page. A reload stops music, so it only happens
while someone is using the computer.

The screen read can be blind on a given machine, so the watchdog does nothing at
all until it has seen the canary land at least once in this run.
"""

from __future__ import annotations

import ctypes
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
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
# The same ladder applies, because a reload is the only rung that helps.
SILENT_ROUNDS_BEFORE_RECOVERY = 3
# After the last rung, leave the window alone this long before trying again.
GIVE_UP_ROUNDS = 60
USER_ACTIVE_WINDOW_S = 120.0

CANARY_SIZE_CSS_PX = 4
# Close to the page's dark background, so nobody sees it change, but far enough
# apart that colour management cannot make one read as the other.
CANARY_SHADES = ((24, 24, 28), (66, 66, 76))

PROBE_SCRIPT = """
(function (shade) {
  var id = '__viola_surface_canary';
  var dot = document.getElementById(id);
  if (!dot) {
    dot = document.createElement('div');
    dot.id = id;
    dot.setAttribute('aria-hidden', 'true');
    dot.style.cssText = 'position:fixed;right:0;bottom:0;width:%(size)dpx;height:%(size)dpx;'
      + 'z-index:2147483647;pointer-events:none;';
    document.documentElement.appendChild(dot);
  }
  dot.style.backgroundColor = shade;
  // A string: PySide hands a plain object to the callback as an empty value.
  return JSON.stringify({
    visible: document.visibilityState === 'visible',
    covered: !!document.fullscreenElement,
  });
})
""" % {"size": CANARY_SIZE_CSS_PX}


class Rung(str, Enum):
    """One step of the recovery ladder, cheapest first."""

    REDRAW_VIEW = "redraw_view"
    RESIZE_WINDOW = "resize_window"
    REBUILD_WINDOW = "rebuild_window"
    RELOAD_PAGE = "reload_page"


LADDER = (Rung.REDRAW_VIEW, Rung.RESIZE_WINDOW, Rung.REBUILD_WINDOW, Rung.RELOAD_PAGE)


class Reading(str, Enum):
    """What one probe round found."""

    LANDED = "landed"  # the new shade reached the screen
    MISSED = "missed"  # the screen still shows something else
    SILENT = "silent"  # the page never answered the probe
    SKIPPED = "skipped"  # nothing to judge (covered, hidden, not foreground)


@dataclass
class SurfaceJudge:
    """Decide from probe results what to do next. No Qt, so tests drive it directly."""

    calibrated: bool = False
    misses: int = 0
    silent: int = 0
    next_rung: int = 0
    rest_rounds: int = 0
    steps_run: int = 0

    def observe(self, reading: Reading, *, user_active: bool = True) -> Rung | None:
        """Take one reading; return the recovery step to run now, if any."""
        if reading is Reading.SKIPPED:
            return None
        if reading is Reading.LANDED:
            if self.steps_run:
                logger.warning("Surface watchdog: the page is back on screen after %d step(s)", self.steps_run)
            if not self.calibrated:
                logger.info("Surface watchdog: the page was seen on screen; watching")
            self.calibrated = True
            self.misses = 0
            self.silent = 0
            self.next_rung = 0
            self.rest_rounds = 0
            self.steps_run = 0
            return None
        if not self.calibrated:
            return None
        if self.rest_rounds:
            self.rest_rounds -= 1
            return None
        if reading is Reading.MISSED:
            self.misses += 1
        else:
            self.silent += 1
        if self.misses < MISSES_BEFORE_RECOVERY and self.silent < SILENT_ROUNDS_BEFORE_RECOVERY:
            return None
        rung = LADDER[self.next_rung]
        if rung is Rung.RELOAD_PAGE and not user_active:
            # Nobody is looking and a reload would stop the music. The next
            # touch or key press lets this step run.
            return None
        self.next_rung += 1
        self.steps_run += 1
        self.misses = 0
        self.silent = 0
        if self.next_rung >= len(LADDER):
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


def _window_owns_point(hwnd: int, x: int, y: int) -> bool:
    """True when ``hwnd`` is in front and nothing covers physical point (x, y)."""
    if sys.platform != "win32":
        return True

    class _Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

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
        from PySide6.QtCore import QTimer

        self._window = window
        self._recover = recover
        self._probe_allowed = probe_allowed
        self.judge = SurfaceJudge()
        self._shade = 0
        self._awaiting_answer = False
        self._timer = QTimer(window)
        self._timer.setInterval(PROBE_INTERVAL_MS)
        self._timer.timeout.connect(self._round)
        self._read_timer = QTimer(window)
        self._read_timer.setSingleShot(True)
        self._read_timer.setInterval(READ_DELAY_MS)
        self._read_timer.timeout.connect(self._read_screen)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
            logger.info("Surface watchdog started")

    def stop(self) -> None:
        self._timer.stop()
        self._read_timer.stop()

    def _settle(self, reading: Reading) -> None:
        idle_s = _seconds_since_last_input()
        user_active = idle_s is None or idle_s <= USER_ACTIVE_WINDOW_S
        rung = self.judge.observe(reading, user_active=user_active)
        if rung is None:
            return
        logger.error("Surface watchdog: the page is not reaching the screen (%s); running %s", reading.value, rung.value)
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
        script = "%s(%r)" % (PROBE_SCRIPT, shade_css(CANARY_SHADES[self._shade]))
        page.runJavaScript(script, 0, self._on_probe_answer)

    def _on_probe_answer(self, result: object) -> None:
        self._awaiting_answer = False
        try:
            state = json.loads(result) if isinstance(result, str) and result else {}
        except ValueError:
            state = {}
        if not state.get("visible") or state.get("covered"):
            self._settle(Reading.SKIPPED)
            return
        self._read_timer.start()

    def _read_screen(self) -> None:
        pixel = self._screen_pixel()
        if pixel is None:
            self._settle(Reading.SKIPPED)
        elif pixel_shows_shade(pixel, self._shade):
            self._settle(Reading.LANDED)
        else:
            logger.debug("Surface watchdog: screen shows %s, expected shade %d", pixel, self._shade)
            self._settle(Reading.MISSED)

    def _screen_pixel(self) -> tuple[int, int, int] | None:
        """Read the middle of the canary from the screen, or None when it can't be judged."""
        from PySide6.QtCore import QPoint

        window = self._window
        view = window.webview
        if not self._probe_allowed():
            return None
        inset = CANARY_SIZE_CSS_PX // 2
        point = view.mapToGlobal(QPoint(view.width() - inset, view.height() - inset))
        screen = window.screen()
        if screen is None or not screen.geometry().contains(point):
            return None
        origin = screen.geometry().topLeft()
        ratio = screen.devicePixelRatio()
        # Qt keeps a screen's corner at its physical position and scales inside it.
        physical_x = origin.x() + int((point.x() - origin.x()) * ratio)
        physical_y = origin.y() + int((point.y() - origin.y()) * ratio)
        if not _window_owns_point(int(window.winId()), physical_x, physical_y):
            return None
        grabbed = screen.grabWindow(0, point.x() - origin.x(), point.y() - origin.y(), 1, 1).toImage()
        if grabbed.isNull() or grabbed.width() < 1:
            return None
        color = grabbed.pixelColor(0, 0)
        return color.red(), color.green(), color.blue()

