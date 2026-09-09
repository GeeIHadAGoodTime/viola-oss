"""Low-level keyboard and mouse synthesis for general computer/game control.

Implements the eight primitives where Anthropic's computer-use API
(2025-01-24) and BAAI's Cradle framework converge, plus one extension
Anthropic does not have:

  - mouse_move_relative(dx, dy, duration_ms)   ← Anthropic missing this
  - mouse_button_down(button)
  - mouse_button_up(button)
  - mouse_button_hold(button, duration_ms)
  - key_down(key)
  - key_up(key)
  - key_hold(key, duration_ms)
  - mouse_move_angle(degrees, axis, duration_ms)   ← Cradle-style sugar

Implementation: direct ctypes ``SendInput`` with scan-code keyboard events
(``KEYEVENTF_SCANCODE``) for compatibility with cursor-locked / DirectInput
games such as Minecraft Java (LWJGL3 → GLFW raw input via ``WM_INPUT``).

Anthropic's reference implementation is xdotool-based and exposes only
absolute ``mouse_move``. Anthropic ``hold_key`` is composed (down + sleep
+ up); we provide both the composed form and the separate ``key_down`` /
``key_up`` primitives so the agent can hold one key while doing something
else (e.g. walk forward while looking around).

Why scan codes: PyDirectInput (Half-Life 2-tested) and pynput's known FPS
issues converge on the same diagnosis — DirectInput-mode games discard
events that don't carry a scan code. ``KEYEVENTF_SCANCODE`` + the
``MOUSEEVENTF_MOVE`` (relative, no ``MOUSEEVENTF_ABSOLUTE``) flag is the
combination that surfaces as ``WM_INPUT`` raw input on Windows, which is
what GLFW / LWJGL3 / DirectInput all consume.
"""

from __future__ import annotations

import asyncio
import ctypes
import platform
import time
from ctypes import wintypes
from dataclasses import dataclass, replace
from typing import Literal

from core.logging_config import get_logger

logger = get_logger(__name__)

# ----------------------------------------------------------------------
# Win32 constants
# ----------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001  # Relative motion (no MOUSEEVENTF_ABSOLUTE).
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

# Win32 error codes worth naming. ERROR_ACCESS_DENIED is the UIPI signature:
# a lower-integrity process may not synthesize input into a higher-integrity
# foreground window, and SendInput reports that by accepting zero events.
ERROR_ACCESS_DENIED = 5

# OpenInputDesktop access mask. DESKTOP_READOBJECTS is the cheapest right that
# still proves we can reach the desktop currently receiving input.
_DESKTOP_READOBJECTS = 0x0001

# Scan codes (Microsoft Set 1 / IBM PC AT). Game-friendly subset; extend
# the table when a new key is needed rather than falling back to virtual
# key codes — DirectInput-mode games read scan codes only.
_SCAN: dict[str, int] = {
    "escape": 0x01,
    "esc": 0x01,
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
    "9": 0x0A,
    "0": 0x0B,
    "minus": 0x0C,
    "-": 0x0C,
    "equals": 0x0D,
    "=": 0x0D,
    "backspace": 0x0E,
    "tab": 0x0F,
    "q": 0x10,
    "w": 0x11,
    "e": 0x12,
    "r": 0x13,
    "t": 0x14,
    "y": 0x15,
    "u": 0x16,
    "i": 0x17,
    "o": 0x18,
    "p": 0x19,
    "[": 0x1A,
    "lbracket": 0x1A,
    "]": 0x1B,
    "rbracket": 0x1B,
    "enter": 0x1C,
    "return": 0x1C,
    "lctrl": 0x1D,
    "ctrl": 0x1D,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "f": 0x21,
    "g": 0x22,
    "h": 0x23,
    "j": 0x24,
    "k": 0x25,
    "l": 0x26,
    ";": 0x27,
    "semicolon": 0x27,
    "'": 0x28,
    "apostrophe": 0x28,
    "`": 0x29,
    "grave": 0x29,
    "lshift": 0x2A,
    "shift": 0x2A,
    "\\": 0x2B,
    "backslash": 0x2B,
    "z": 0x2C,
    "x": 0x2D,
    "c": 0x2E,
    "v": 0x2F,
    "b": 0x30,
    "n": 0x31,
    "m": 0x32,
    ",": 0x33,
    "comma": 0x33,
    ".": 0x34,
    "period": 0x34,
    "/": 0x35,
    "slash": 0x35,
    "rshift": 0x36,
    "lalt": 0x38,
    "alt": 0x38,
    "space": 0x39,
    " ": 0x39,
    "capslock": 0x3A,
    "f1": 0x3B,
    "f2": 0x3C,
    "f3": 0x3D,
    "f4": 0x3E,
    "f5": 0x3F,
    "f6": 0x40,
    "f7": 0x41,
    "f8": 0x42,
    "f9": 0x43,
    "f10": 0x44,
    "f11": 0x57,
    "f12": 0x58,
    # Extended (E0-prefix) keys
    "rctrl": 0x1D,
    "ralt": 0x38,
    "up": 0x48,
    "down": 0x50,
    "left": 0x4B,
    "right": 0x4D,
    "home": 0x47,
    "end": 0x4F,
    "pgup": 0x49,
    "pageup": 0x49,
    "pgdn": 0x51,
    "pagedown": 0x51,
    "insert": 0x52,
    "delete": 0x53,
    "del": 0x53,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
}

_EXTENDED_KEYS: frozenset[str] = frozenset(
    {
        "rctrl",
        "ralt",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pgup",
        "pageup",
        "pgdn",
        "pagedown",
        "insert",
        "delete",
        "del",
        "win",
        "lwin",
        "rwin",
    }
)

Button = Literal["left", "right", "middle"]
Axis = Literal["horizontal", "vertical"]

_BUTTON_DOWN_FLAGS = {
    "left": MOUSEEVENTF_LEFTDOWN,
    "right": MOUSEEVENTF_RIGHTDOWN,
    "middle": MOUSEEVENTF_MIDDLEDOWN,
}
_BUTTON_UP_FLAGS = {
    "left": MOUSEEVENTF_LEFTUP,
    "right": MOUSEEVENTF_RIGHTUP,
    "middle": MOUSEEVENTF_MIDDLEUP,
}

# Heuristic for mouse_move_angle: most 3D games default to ~10-15 raw
# mouse units per degree of camera rotation. The agent should calibrate
# this empirically when sub-degree precision matters; the default works
# for "rotate roughly 90 degrees right" intent.
_DEFAULT_PIXELS_PER_DEGREE = 12

# Cap step count on duration-based motion to avoid runaway loops if a
# caller passes a huge duration_ms with tiny steps.
_MAX_MOTION_STEPS = 200
_MOTION_STEP_MS = 10


# ----------------------------------------------------------------------
# ctypes structures
# ----------------------------------------------------------------------


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


def _is_windows() -> bool:
    return platform.system() == "Windows"


_USER32: object | None = None


def _user32():
    """Return user32 bound so ``GetLastError`` is readable after a call.

    ``ctypes.windll.user32`` is a process-wide cached handle built WITHOUT
    ``use_last_error``, so ctypes makes no guarantee that the error code
    surviving a call belongs to that call -- any other ctypes call on any
    thread can overwrite it first. ``WinDLL(..., use_last_error=True)``
    makes ctypes swap in and save the thread error around each call, which
    is the only way to learn WHY Windows refused an event rather than just
    that it did. Same idiom as ``core/win32_job.py``.
    """
    global _USER32
    if _USER32 is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # HDESK is a pointer; the default c_int restype truncates it on
        # 64-bit, which can turn a live handle into a bogus value.
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        _USER32 = user32
    return _USER32


def _last_error() -> int:
    """Win32 error saved by the most recent ``use_last_error`` ctypes call."""
    getter = getattr(ctypes, "get_last_error", None)
    if getter is None:  # non-Windows: nothing to read
        return 0
    try:
        return int(getter())
    except (OSError, ValueError):
        return 0


@dataclass(frozen=True)
class InputDispatch:
    """What Windows actually did with a batch of synthesized input events.

    ``requested`` events were handed to ``SendInput``; ``accepted`` is how
    many Windows took. They differ for exactly the reasons that make a
    keystroke silently vanish: UIPI (the foreground window belongs to a
    higher-integrity process, so a normal-integrity Viola may not inject
    into it) and ``BlockInput``. Windows reports that refusal in the return
    value, and this type is how it stops being thrown away.

    Acceptance is NOT arrival. ``SendInput`` hands the event to the
    session's input queue and returns; there is no channel back from the
    target application's message pump, so an accepted event can still be
    consumed by nothing. ``blind_reason`` names the case where we can tell
    that acceptance is especially unlikely to mean anything the user can
    see (locked workstation / secure desktop). An accepted event with no
    blind_reason is "submitted, outcome unobserved" -- never "it worked".
    """

    requested: int
    accepted: int
    last_error: int = 0
    blind_reason: str | None = None

    @property
    def refused(self) -> int:
        return max(0, self.requested - self.accepted)

    @property
    def submitted(self) -> bool:
        """True when Windows accepted every event we handed it."""
        return self.requested > 0 and self.accepted == self.requested

    @property
    def observed(self) -> bool:
        """Always False: no injection-side call can observe the target.

        Kept explicit so a future caller reaching for "did it land?" finds a
        documented no instead of reading ``submitted`` as a yes.
        """
        return False

    def refusal_reason(self) -> str | None:
        """Why Windows refused, in words, or None when nothing was refused."""
        if self.submitted:
            return None
        if self.requested == 0:
            return "no input events were generated"
        base = "Windows accepted %d of %d synthesized input events" % (self.accepted, self.requested)
        if self.last_error == ERROR_ACCESS_DENIED:
            return (
                base + "; access was denied (ERROR_ACCESS_DENIED), which is what Windows reports when the "
                "foreground window runs at a higher integrity level than Viola (UIPI) or input is blocked"
            )
        if self.last_error:
            return base + " (Win32 error %d)" % self.last_error
        return base

    def merge(self, other: InputDispatch) -> InputDispatch:
        """Combine two dispatches into one honest total for a multi-event call."""
        return InputDispatch(
            requested=self.requested + other.requested,
            accepted=self.accepted + other.accepted,
            # Keep the FIRST real error: it explains the first refusal, and a
            # later success would otherwise reset it to 0 and hide the cause.
            last_error=self.last_error or other.last_error,
            blind_reason=self.blind_reason or other.blind_reason,
        )


_EMPTY_DISPATCH = InputDispatch(requested=0, accepted=0)


def _input_desktop_blind_reason() -> str | None:
    """Name the reason an accepted event still reaches nobody, or None.

    ``SendInput`` returns success for an event synthesized while the
    workstation is locked or a secure desktop (UAC prompt, Ctrl+Alt+Del,
    the sign-in screen) owns input: the event enters this process's own
    desktop queue and nothing tells us the user is not looking at that
    desktop. ``OpenInputDesktop`` is the one cheap read that distinguishes
    it -- it fails with ERROR_ACCESS_DENIED when the input desktop belongs
    to Winlogon rather than to us.

    Returns None when the input desktop is ours, and also when the check
    itself cannot run: this reports observations, it never invents a
    blocker it did not see.
    """
    if not _is_windows():
        return None
    try:
        user32 = _user32()
        handle = user32.OpenInputDesktop(0, False, _DESKTOP_READOBJECTS)
    except (AttributeError, OSError, ValueError):
        logger.debug("OpenInputDesktop unavailable; not claiming the input desktop is unreachable")
        return None
    if handle:
        try:
            user32.CloseDesktop(handle)
        except (AttributeError, OSError, ValueError):
            logger.debug("CloseDesktop failed for the input-desktop probe handle")
        return None
    error = _last_error()
    if error == ERROR_ACCESS_DENIED:
        return (
            "the interactive desktop is locked or a secure desktop (sign-in / UAC prompt) currently owns "
            "input, so Windows accepting the event does not mean anything reached the user's screen"
        )
    return "could not confirm the interactive desktop is reachable (OpenInputDesktop failed, Win32 error %d)" % error


def _annotate_blind(dispatch: InputDispatch) -> InputDispatch:
    """Attach the input-desktop observation to a fully-accepted dispatch.

    Only meaningful when everything was accepted: a refusal already has a
    concrete reason and does not need a softer one. Called once per public
    primitive, never per event, so a 100-character type costs one probe.
    """
    if not dispatch.submitted or dispatch.blind_reason:
        return dispatch
    reason = _input_desktop_blind_reason()
    if reason is None:
        return dispatch
    return replace(dispatch, blind_reason=reason)


def _send_inputs(*inputs: _INPUT) -> InputDispatch:
    """Submit one or more synthesized INPUT events via SendInput.

    Returns what Windows did with them. Raises on non-Windows platforms;
    primitives below should refuse with a structured envelope earlier
    rather than letting this surface ctypes errors.

    Pre-fix this returned a bare count that every caller discarded, and
    every public primitive was typed ``-> None``, so a refused event -- the
    ordinary outcome against an elevated window -- existed only as a log
    line nobody read while the tool layer above reported success.
    """
    if not _is_windows():
        raise RuntimeError("desktop_input_low primitives are Windows-only")
    n = len(inputs)
    arr = (_INPUT * n)(*inputs)
    user32 = _user32()
    accepted = int(user32.SendInput(n, arr, ctypes.sizeof(_INPUT)) or 0)
    error = _last_error() if accepted != n else 0
    if accepted != n:
        logger.warning("SendInput accepted %d of %d events (Win32 error %d)", accepted, n, error)
    return InputDispatch(requested=n, accepted=accepted, last_error=error)


# ----------------------------------------------------------------------
# Mouse primitives
# ----------------------------------------------------------------------


def _mouse_button_event(button: Button, flags: dict[str, int], verb: str) -> InputDispatch:
    flag = flags.get(button)
    if flag is None:
        raise ValueError("unknown button %r; expected left/right/middle" % button)
    inp = _INPUT(
        type=INPUT_MOUSE,
        u=_INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, flag, 0, None)),
    )
    dispatch = _send_inputs(inp)
    if not dispatch.submitted:
        logger.warning("mouse_button_%s(%s) refused: %s", verb, button, dispatch.refusal_reason())
    return dispatch


def mouse_button_down(button: Button = "left") -> InputDispatch:
    """Press and hold a mouse button until ``mouse_button_up`` is called."""
    return _annotate_blind(_mouse_button_event(button, _BUTTON_DOWN_FLAGS, "down"))


def mouse_button_up(button: Button = "left") -> InputDispatch:
    """Release a held mouse button."""
    return _annotate_blind(_mouse_button_event(button, _BUTTON_UP_FLAGS, "up"))


async def mouse_button_hold(button: Button, duration_ms: int) -> InputDispatch:
    """Down + sleep + up. Use ``mouse_button_down``/``up`` directly when
    you need to hold across other actions.

    Reports both halves: a hold whose press was refused is not a hold, and
    a press that landed with a refused release leaves a stuck button the
    caller has to know about.
    """
    duration_ms = max(0, int(duration_ms))
    down = _mouse_button_event(button, _BUTTON_DOWN_FLAGS, "down")
    up = _EMPTY_DISPATCH
    try:
        await asyncio.sleep(duration_ms / 1000.0)
    finally:
        up = _mouse_button_event(button, _BUTTON_UP_FLAGS, "up")
    return _annotate_blind(down.merge(up))


def mouse_move_relative_step(dx: int, dy: int) -> InputDispatch:
    """Single immediate relative-motion event (no smoothing).

    Returns the RAW dispatch, without the input-desktop probe: this is the
    inner step ``mouse_move_relative`` loops over, and probing per step
    would run the check hundreds of times for one gesture. Callers using it
    directly get accepted-vs-requested, which is the part that matters per
    event; the blind check belongs on the whole gesture.
    """
    inp = _INPUT(
        type=INPUT_MOUSE,
        u=_INPUT_UNION(mi=_MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, None)),
    )
    return _send_inputs(inp)


async def mouse_move_relative(dx: int, dy: int, duration_ms: int = 0) -> InputDispatch:
    """Move mouse by ``(dx, dy)`` raw units. Required for cursor-locked games.

    If ``duration_ms <= 0`` the motion is a single SendInput. Otherwise
    the delta is split into ~10ms steps for smoothness, capped at 200
    steps to avoid runaway loops. A smoothed move reports the total across
    every step, so a partially-refused sweep cannot read as a whole one.
    """
    dx = int(dx)
    dy = int(dy)
    duration_ms = max(0, int(duration_ms))

    if duration_ms <= 0:
        return _annotate_blind(mouse_move_relative_step(dx, dy))

    num_steps = max(1, min(duration_ms // _MOTION_STEP_MS, _MAX_MOTION_STEPS))
    step_dx = dx // num_steps
    step_dy = dy // num_steps
    rem_x = dx - (step_dx * num_steps)
    rem_y = dy - (step_dy * num_steps)
    sleep_per_step = duration_ms / 1000.0 / num_steps

    total = _EMPTY_DISPATCH
    for i in range(num_steps):
        ddx = step_dx + (rem_x if i == num_steps - 1 else 0)
        ddy = step_dy + (rem_y if i == num_steps - 1 else 0)
        total = total.merge(mouse_move_relative_step(ddx, ddy))
        if i < num_steps - 1:
            await asyncio.sleep(sleep_per_step)
    return _annotate_blind(total)


async def mouse_move_angle(
    degrees: float,
    axis: Axis = "horizontal",
    duration_ms: int = 0,
) -> InputDispatch:
    """Sugar over ``mouse_move_relative`` for "rotate camera N degrees".

    Uses a heuristic of ~12 raw mouse units per degree at typical 3D-game
    sensitivity. Calibrate empirically when sub-degree precision matters.
    """
    delta = int(float(degrees) * _DEFAULT_PIXELS_PER_DEGREE)
    if axis == "horizontal":
        return await mouse_move_relative(delta, 0, duration_ms)
    if axis == "vertical":
        return await mouse_move_relative(0, delta, duration_ms)
    raise ValueError("axis must be 'horizontal' or 'vertical', got %r" % axis)


# ----------------------------------------------------------------------
# Keyboard primitives
# ----------------------------------------------------------------------


def _resolve_key(key: str) -> tuple[int, bool]:
    """Return (scan_code, is_extended). Raises ``ValueError`` on unknown key."""
    if not isinstance(key, str):
        raise ValueError("key must be a string, got %r" % type(key).__name__)
    norm = key.strip().lower()
    if norm not in _SCAN:
        raise ValueError("unknown key %r; not in scan-code table" % key)
    return _SCAN[norm], norm in _EXTENDED_KEYS


def _key_event(key: str, *, key_up_flag: bool) -> InputDispatch:
    scan, extended = _resolve_key(key)
    flags = KEYEVENTF_SCANCODE
    if key_up_flag:
        flags |= KEYEVENTF_KEYUP
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    inp = _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=_KEYBDINPUT(0, scan, flags, 0, None)),
    )
    dispatch = _send_inputs(inp)
    if not dispatch.submitted:
        logger.warning(
            "key_%s(%s) refused: %s",
            "up" if key_up_flag else "down",
            key,
            dispatch.refusal_reason(),
        )
    return dispatch


def key_down(key: str) -> InputDispatch:
    """Press and hold a key until ``key_up`` is called."""
    return _annotate_blind(_key_event(key, key_up_flag=False))


def key_up(key: str) -> InputDispatch:
    """Release a held key."""
    return _annotate_blind(_key_event(key, key_up_flag=True))


async def key_hold(key: str, duration_ms: int) -> InputDispatch:
    """Down + sleep + up. Matches Anthropic's ``hold_key`` shape.

    Reports press and release together: a refused press means no hold
    happened at all, and a refused release means a key is still down.
    """
    duration_ms = max(0, int(duration_ms))
    down = _key_event(key, key_up_flag=False)
    up = _EMPTY_DISPATCH
    try:
        await asyncio.sleep(duration_ms / 1000.0)
    finally:
        up = _key_event(key, key_up_flag=True)
    return _annotate_blind(down.merge(up))


# ----------------------------------------------------------------------
# Chord and text typing — SendInput+scancode path so they reach raw-input
# apps (games using GLFW raw input, DirectInput-mode apps).
# ----------------------------------------------------------------------

# Map of typeable characters to (scan_code, requires_shift). Covers ASCII
# printable + space/tab/newline. Anything outside this map needs to fall
# back to a Unicode-aware path (clipboard paste).
_CHAR_TO_SCAN: dict[str, tuple[int, bool]] = {}


def _build_char_map() -> None:
    base = {
        "a": 0x1E,
        "b": 0x30,
        "c": 0x2E,
        "d": 0x20,
        "e": 0x12,
        "f": 0x21,
        "g": 0x22,
        "h": 0x23,
        "i": 0x17,
        "j": 0x24,
        "k": 0x25,
        "l": 0x26,
        "m": 0x32,
        "n": 0x31,
        "o": 0x18,
        "p": 0x19,
        "q": 0x10,
        "r": 0x13,
        "s": 0x1F,
        "t": 0x14,
        "u": 0x16,
        "v": 0x2F,
        "w": 0x11,
        "x": 0x2D,
        "y": 0x15,
        "z": 0x2C,
        "1": 0x02,
        "2": 0x03,
        "3": 0x04,
        "4": 0x05,
        "5": 0x06,
        "6": 0x07,
        "7": 0x08,
        "8": 0x09,
        "9": 0x0A,
        "0": 0x0B,
        " ": 0x39,
        "\n": 0x1C,
        "\t": 0x0F,
        "-": 0x0C,
        "=": 0x0D,
        "[": 0x1A,
        "]": 0x1B,
        "\\": 0x2B,
        ";": 0x27,
        "'": 0x28,
        ",": 0x33,
        ".": 0x34,
        "/": 0x35,
        "`": 0x29,
    }
    for c, sc in base.items():
        _CHAR_TO_SCAN[c] = (sc, False)
    # Uppercase = shift + lowercase
    for c in "abcdefghijklmnopqrstuvwxyz":
        _CHAR_TO_SCAN[c.upper()] = (base[c], True)
    # Shifted symbols
    shifted = {
        "!": "1",
        "@": "2",
        "#": "3",
        "$": "4",
        "%": "5",
        "^": "6",
        "&": "7",
        "*": "8",
        "(": "9",
        ")": "0",
        "_": "-",
        "+": "=",
        "{": "[",
        "}": "]",
        "|": "\\",
        ":": ";",
        '"': "'",
        "<": ",",
        ">": ".",
        "?": "/",
        "~": "`",
    }
    for shifted_c, base_c in shifted.items():
        _CHAR_TO_SCAN[shifted_c] = (base[base_c], True)


_build_char_map()


def _send_scancode_press(scan: int, *, extended: bool = False) -> InputDispatch:
    """Single keydown event via SendInput with scan code."""
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    inp = _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=_KEYBDINPUT(0, scan, flags, 0, None)),
    )
    return _send_inputs(inp)


def _send_scancode_release(scan: int, *, extended: bool = False) -> InputDispatch:
    """Single keyup event via SendInput with scan code."""
    flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    inp = _INPUT(
        type=INPUT_KEYBOARD,
        u=_INPUT_UNION(ki=_KEYBDINPUT(0, scan, flags, 0, None)),
    )
    return _send_inputs(inp)


def _type_char_events(c: str) -> InputDispatch:
    """Emit one character's events and total what Windows accepted."""
    if c not in _CHAR_TO_SCAN:
        raise ValueError("unsupported character %r for scancode typing" % c)
    scan, requires_shift = _CHAR_TO_SCAN[c]
    total = _EMPTY_DISPATCH
    if requires_shift:
        total = total.merge(_send_scancode_press(_SCAN["shift"]))
    total = total.merge(_send_scancode_press(scan))
    total = total.merge(_send_scancode_release(scan))
    if requires_shift:
        total = total.merge(_send_scancode_release(_SCAN["shift"]))
    return total


def type_char(c: str) -> InputDispatch:
    """Type a single character via SendInput+scancode. Raises on unsupported chars."""
    return _annotate_blind(_type_char_events(c))


def type_text(text: str, *, char_delay_ms: int = 5) -> InputDispatch:
    """Type a string char by char via SendInput+scancode.

    Each character generates a press+release SendInput pair. Raises
    ``ValueError`` if ANY character in *text* is unsupported so callers can
    fall back to clipboard paste or another Unicode-aware path.

    Validation happens atomically, BEFORE any input is sent. Pre-fix, this
    raised on the first unsupported character only after already sending
    SendInput for every character before it; the caller's fallback then
    re-typed the whole string via pywinauto, so e.g. "café" (with a
    trailing accent the scancode table can't express) landed as
    "cafcafé" — a silent duplication reported as clean success
    (#2773 item 3). Validating up front means this path either types the
    whole string exactly once or types nothing at all.

    Returns the total across every character. A string where Windows took
    the first few keystrokes and then refused the rest is a partial type,
    and the returned dispatch is the only place that is visible: ``len`` of
    the requested string never was.
    """
    unsupported = [c for c in text if c not in _CHAR_TO_SCAN]
    if unsupported:
        raise ValueError("unsupported character %r for scancode typing" % unsupported[0])
    total = _EMPTY_DISPATCH
    for c in text:
        total = total.merge(_type_char_events(c))
        if char_delay_ms > 0:
            time.sleep(char_delay_ms / 1000.0)
    if not total.submitted:
        logger.warning("type_text refused mid-string: %s", total.refusal_reason())
    return _annotate_blind(total)


def key_chord(chord: str) -> InputDispatch:
    """Press a chord like 'ctrl+s' or a single key like 'enter' / 'down' / 't'.

    Splits on '+', presses every modifier down in order, presses+releases the
    main key, then releases modifiers in reverse order. All via
    SendInput+scancode so it reaches raw-input apps the same way our
    individual key_down/key_up primitives do.

    Returns the total across modifiers and main key. A chord whose ctrl was
    refused but whose 's' landed is not "ctrl+s"; it is a stray 's' typed
    into the document, which is exactly the shape a caller must be able to
    see.
    """
    if not chord or not chord.strip():
        raise ValueError("chord must not be empty")
    parts = [p.strip().lower() for p in chord.split("+") if p.strip()]
    if not parts:
        raise ValueError("chord %r parsed to empty" % chord)
    modifiers = parts[:-1]
    main_key = parts[-1]
    total = _EMPTY_DISPATCH
    for mod in modifiers:
        total = total.merge(_key_event(mod, key_up_flag=False))
    try:
        total = total.merge(_key_event(main_key, key_up_flag=False))
        total = total.merge(_key_event(main_key, key_up_flag=True))
    finally:
        for mod in reversed(modifiers):
            total = total.merge(_key_event(mod, key_up_flag=True))
    if not total.submitted:
        logger.warning("key_chord(%s) refused: %s", chord, total.refusal_reason())
    return _annotate_blind(total)


__all__ = [
    "ERROR_ACCESS_DENIED",
    "InputDispatch",
    "key_chord",
    "key_down",
    "key_hold",
    "key_up",
    "mouse_button_down",
    "mouse_button_hold",
    "mouse_button_up",
    "mouse_move_angle",
    "mouse_move_relative",
    "mouse_move_relative_step",
    "type_char",
    "type_text",
]
