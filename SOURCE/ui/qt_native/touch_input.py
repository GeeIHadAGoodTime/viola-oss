"""Touch-screen support for the desktop shell (Windows tablets such as a Surface).

QtWebEngine already receives real touch input on Windows (Qt 6 delivers
WM_POINTER touch as QTouchEvent and Chromium sees ``pointerType: "touch"``),
but three things a tablet user needs are not done by QtWebEngine itself:

* **Telling the page it is on a tablet.** QtWebEngine answers the CSS pointer
  media queries from Qt's input-device list, which on Windows always contains
  a mouse, so ``(pointer: coarse)`` never matches. :func:`is_touch_first`
  reads Windows' own answer (an integrated touch digitizer and the device in
  slate/tablet posture) and the shell passes it to the page.
* **Showing the touch keyboard.** :class:`TouchKeyboard` wraps the WinRT
  ``InputPane`` for one window: ``try_show()`` asks Windows to show the touch
  keyboard and ``occluded_rect()`` reports where a docked keyboard covers the
  screen.
* **Not typing behind the keyboard.** :func:`bottom_inset_for` turns that
  rectangle into how much of the window's bottom edge is covered, so the shell
  can lift the page above a docked keyboard.

Everything here is best effort: on any other platform, on a PC without a touch
screen, or if a Windows call fails, the functions answer "no touch" / "no
keyboard" and never raise.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# GetSystemMetrics indices and SM_DIGITIZER bits (winuser.h).
SM_DIGITIZER = 94
SM_CONVERTIBLESLATEMODE = 0x2003
NID_INTEGRATED_TOUCH = 0x01
NID_EXTERNAL_TOUCH = 0x02
NID_READY = 0x80

# Touch targets for the native window chrome when a touch screen is present.
TOUCH_TITLE_BAR_HEIGHT = 44
TOUCH_TITLE_BUTTON_WIDTH = 56
MOUSE_TITLE_BAR_HEIGHT = 32
MOUSE_TITLE_BUTTON_WIDTH = 46


def _get_system_metric(index: int) -> int:
    if sys.platform != "win32":
        return 0
    try:
        return int(ctypes.windll.user32.GetSystemMetrics(index))
    except (AttributeError, OSError):
        return 0


def has_touch_screen(digitizer: int | None = None) -> bool:
    """True when Windows reports a ready touch digitizer (built-in or external)."""
    value = _get_system_metric(SM_DIGITIZER) if digitizer is None else digitizer
    return bool(value & NID_READY) and bool(value & (NID_INTEGRATED_TOUCH | NID_EXTERNAL_TOUCH))


def is_touch_first(digitizer: int | None = None, slate_mode: int | None = None) -> bool:
    """True when the device is being used as a tablet right now.

    Needs a touch screen AND Windows' convertible posture reporting slate mode
    (``SM_CONVERTIBLESLATEMODE`` is 0 with the keyboard detached or folded
    back). A touch laptop with its keyboard attached is not touch-first: the
    page still switches to touch sizing on the first finger press.
    """
    if not has_touch_screen(digitizer):
        return False
    value = _get_system_metric(SM_CONVERTIBLESLATEMODE) if slate_mode is None else slate_mode
    return value == 0


def title_bar_metrics(touch: bool) -> tuple[int, int]:
    """(height, caption button width) for the custom title bar."""
    if touch:
        return TOUCH_TITLE_BAR_HEIGHT, TOUCH_TITLE_BUTTON_WIDTH
    return MOUSE_TITLE_BAR_HEIGHT, MOUSE_TITLE_BUTTON_WIDTH


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float


def bottom_inset_for(window: Rect, occluded: Rect | None) -> int:
    """How many px of the window's bottom edge a docked keyboard covers.

    Both rectangles are in the same screen coordinate space (logical /
    effective pixels). An empty or missing occluded rect (keyboard hidden, or
    floating, which Windows reports as empty by design) means 0. The result
    never exceeds half the window, so the page can never be squeezed away.
    """
    if occluded is None or occluded.width <= 0 or occluded.height <= 0:
        return 0
    overlaps_horizontally = occluded.x < window.x + window.width and window.x < occluded.x + occluded.width
    if not overlaps_horizontally:
        return 0
    window_bottom = window.y + window.height
    covered = window_bottom - max(occluded.y, window.y)
    if covered <= 0:
        return 0
    return int(min(covered, window.height / 2))


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> _GUID:
        value = uuid.UUID(text)
        guid = cls()
        guid.Data1, guid.Data2, guid.Data3 = value.fields[0], value.fields[1], value.fields[2]
        guid.Data4[:] = value.bytes[8:]
        return guid


class _RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


_IID_INPUT_PANE_INTEROP = "75CF2C57-9195-4931-8332-F0B409E916AF"
_IID_INPUT_PANE = "640ADA70-06F3-4C87-A678-9829C9127C28"
_IID_INPUT_PANE2 = "8A6B3F26-7090-4793-944C-C3F2CDE26276"

# vtable slots: IUnknown 0-2, IInspectable 3-5, then the interface's own.
_SLOT_QUERY_INTERFACE = 0
_SLOT_RELEASE = 2
_SLOT_INTEROP_GET_FOR_WINDOW = 6
_SLOT_PANE_GET_OCCLUDED_RECT = 10
_SLOT_PANE2_TRY_SHOW = 6
_SLOT_PANE2_TRY_HIDE = 7


def _vtable_call(obj: ctypes.c_void_p, slot: int, restype, *argtypes):
    vtable = ctypes.cast(ctypes.cast(obj, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[slot])


class TouchKeyboard:
    """The Windows touch keyboard (WinRT InputPane) for one top-level window.

    Created lazily on first use; if WinRT is unavailable every method is a
    no-op returning False / None.
    """

    def __init__(self, hwnd: int) -> None:
        self._hwnd = int(hwnd)
        self._pane: ctypes.c_void_p | None = None
        self._pane2: ctypes.c_void_p | None = None
        self._failed = sys.platform != "win32"

    def _ensure(self) -> bool:
        if self._pane is not None:
            return True
        if self._failed:
            return False
        try:
            from ctypes import wintypes

            combase = ctypes.WinDLL("combase")
            combase.WindowsCreateString.argtypes = [wintypes.LPCWSTR, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
            combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
            combase.RoGetActivationFactory.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            ]
            class_name = "Windows.UI.ViewManagement.InputPane"
            hstring = ctypes.c_void_p()
            if combase.WindowsCreateString(class_name, len(class_name), ctypes.byref(hstring)) != 0:
                raise OSError("WindowsCreateString failed")
            factory = ctypes.c_void_p()
            try:
                hr = combase.RoGetActivationFactory(
                    hstring, ctypes.byref(_GUID.parse(_IID_INPUT_PANE_INTEROP)), ctypes.byref(factory)
                )
            finally:
                combase.WindowsDeleteString(hstring)
            if hr != 0 or not factory:
                raise OSError(f"RoGetActivationFactory hr={hr & 0xFFFFFFFF:#x}")
            pane = ctypes.c_void_p()
            try:
                hr = _vtable_call(
                    factory,
                    _SLOT_INTEROP_GET_FOR_WINDOW,
                    ctypes.HRESULT,
                    wintypes.HWND,
                    ctypes.POINTER(_GUID),
                    ctypes.POINTER(ctypes.c_void_p),
                )(factory, self._hwnd, ctypes.byref(_GUID.parse(_IID_INPUT_PANE)), ctypes.byref(pane))
            finally:
                _vtable_call(factory, _SLOT_RELEASE, ctypes.c_ulong)(factory)
            if hr != 0 or not pane:
                raise OSError(f"IInputPaneInterop.GetForWindow hr={hr & 0xFFFFFFFF:#x}")
            pane2 = ctypes.c_void_p()
            hr = _vtable_call(
                pane,
                _SLOT_QUERY_INTERFACE,
                ctypes.HRESULT,
                ctypes.POINTER(_GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(pane, ctypes.byref(_GUID.parse(_IID_INPUT_PANE2)), ctypes.byref(pane2))
            self._pane = pane
            self._pane2 = pane2 if hr == 0 and pane2 else None
            return True
        except (AttributeError, OSError, ValueError) as exc:
            self._failed = True
            logger.info("Touch keyboard (InputPane) unavailable: %s", exc)
            return False

    def try_show(self) -> bool:
        """Ask Windows to show the touch keyboard. True when Windows agreed."""
        if not self._ensure() or self._pane2 is None:
            return False
        try:
            shown = ctypes.c_bool(False)
            hr = _vtable_call(self._pane2, _SLOT_PANE2_TRY_SHOW, ctypes.HRESULT, ctypes.POINTER(ctypes.c_bool))(
                self._pane2, ctypes.byref(shown)
            )
            return hr == 0 and bool(shown.value)
        except OSError as exc:
            logger.debug("InputPane.TryShow failed: %s", exc)
            return False

    def try_hide(self) -> bool:
        if not self._ensure() or self._pane2 is None:
            return False
        try:
            hidden = ctypes.c_bool(False)
            hr = _vtable_call(self._pane2, _SLOT_PANE2_TRY_HIDE, ctypes.HRESULT, ctypes.POINTER(ctypes.c_bool))(
                self._pane2, ctypes.byref(hidden)
            )
            return hr == 0 and bool(hidden.value)
        except OSError as exc:
            logger.debug("InputPane.TryHide failed: %s", exc)
            return False

    def occluded_rect(self) -> Rect | None:
        """Where a docked touch keyboard covers the screen (effective px), or None."""
        if not self._ensure() or self._pane is None:
            return None
        try:
            rect = _RectF()
            hr = _vtable_call(self._pane, _SLOT_PANE_GET_OCCLUDED_RECT, ctypes.HRESULT, ctypes.POINTER(_RectF))(
                self._pane, ctypes.byref(rect)
            )
        except OSError as exc:
            logger.debug("InputPane.OccludedRect failed: %s", exc)
            return None
        if hr != 0 or rect.Width <= 0 or rect.Height <= 0:
            return None
        return Rect(float(rect.X), float(rect.Y), float(rect.Width), float(rect.Height))
