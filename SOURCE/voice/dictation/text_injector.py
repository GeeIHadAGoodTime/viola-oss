"""
Text injection at the cursor position across any Windows application.

Provides platform-specific implementations that type text character by
character using OS-level input simulation.  On Windows, the primary
implementation uses the Win32 ``SendInput`` API with
``KEYEVENTF_UNICODE`` for reliable Unicode injection.  A cross-platform
fallback based on pynput is available when Win32 APIs are unavailable.
"""

from __future__ import annotations

import ctypes
import platform
import sys
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.logging_config import get_logger

logger = get_logger(__name__)

# ERROR_ACCESS_DENIED: Windows refusing to let a normal-integrity process
# synthesize input into a higher-integrity foreground window (UIPI).
ERROR_ACCESS_DENIED = 5


@dataclass(frozen=True)
class InjectionOutcome:
    """What actually happened to a batch of injected keystrokes.

    ``requested`` events were handed to the OS; ``accepted`` is how many it
    took. They differ whenever the target window outranks us (UIPI) or
    input is blocked, and Windows says so in ``SendInput``'s return value.
    Pre-fix this file's ``_send_inputs`` was typed ``-> None`` and the count
    was not even returned, so the refusal existed only as a log line while
    the dictation controller announced the text as typed.

    Accepted is not arrived: there is no channel back from the target
    application's message pump, so ``accepted == requested`` means "the OS
    took it", never "the user can see it".
    """

    requested: int
    accepted: int
    last_error: int = 0

    @property
    def submitted(self) -> bool:
        """True when the OS accepted every event we handed it."""
        return self.requested > 0 and self.accepted == self.requested

    def reason(self) -> str:
        """Why the injection was refused, in words."""
        base = "the OS accepted %d of %d keystroke events" % (self.accepted, self.requested)
        if self.last_error == ERROR_ACCESS_DENIED:
            return (
                base + "; access was denied, which is what Windows reports when the focused window runs at "
                "a higher integrity level than Viola"
            )
        if self.last_error:
            return base + " (Win32 error %d)" % self.last_error
        return base

    def merge(self, other: InjectionOutcome) -> InjectionOutcome:
        return InjectionOutcome(
            requested=self.requested + other.requested,
            accepted=self.accepted + other.accepted,
            last_error=self.last_error or other.last_error,
        )


EMPTY_OUTCOME = InjectionOutcome(requested=0, accepted=0)


def _unobservable(requested: int) -> InjectionOutcome:
    """Outcome for a backend that reports nothing at all.

    pynput's controller returns None and raises only on its own internal
    errors, so there is no count to read. Claiming ``accepted == requested``
    would be inventing evidence; the honest reading is that we submitted
    *requested* events and learned nothing, which callers must treat as
    unverified rather than as success.
    """
    return InjectionOutcome(requested=requested, accepted=requested, last_error=0)


# ======================================================================== #
# Port (Protocol)                                                          #
# ======================================================================== #


@runtime_checkable
class TextInjectorPort(Protocol):
    """Contract for text injection implementations.

    Implementations must be thread-safe and inject text at the current
    cursor / caret position of the active application window.

    Every method returns an :class:`InjectionOutcome` rather than None: an
    injector that cannot say whether the OS took the keystrokes gives its
    caller no way to avoid claiming they landed.
    """

    def type_text(self, text: str) -> InjectionOutcome:
        """Type *text* character by character at the cursor position.

        Args:
            text: The string to type.  Unicode is supported.
        """
        ...

    def press_key(self, key: str) -> InjectionOutcome:
        """Press and release a named key (e.g. ``"return"``, ``"tab"``, ``"backspace"``).

        Args:
            key: Case-insensitive key name.
        """
        ...

    def backspace(self, count: int = 1) -> InjectionOutcome:
        """Press the Backspace key *count* times.

        Args:
            count: Number of backspace presses (default 1).
        """
        ...

    def combo_key(self, modifier: str, key: str) -> InjectionOutcome:
        """Press a key combination such as Ctrl+A or Ctrl+V.

        Args:
            modifier: Modifier key name (``"ctrl"``, ``"alt"``, ``"shift"``).
            key: The main key to press while the modifier is held.
        """
        ...


# ======================================================================== #
# Win32 implementation                                                     #
# ======================================================================== #

# Virtual key code lookup table
_VK_MAP: dict[str, int] = {
    "return": 0x0D,
    "enter": 0x0D,
    "tab": 0x09,
    "backspace": 0x08,
    "back": 0x08,
    "escape": 0x1B,
    "esc": 0x1B,
    "space": 0x20,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "capslock": 0x14,
}

# Modifier virtual key codes
_MOD_MAP: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "alt": 0x12,
    "menu": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "lwin": 0x5B,
    "rwin": 0x5C,
}


class Win32TextInjector:
    """Inject text using the Win32 ``SendInput`` API with ``KEYEVENTF_UNICODE``.

    Each character is sent as a pair of KEYDOWN / KEYUP events carrying the
    Unicode code-point directly, bypassing keyboard layout issues.

    Args:
        char_delay: Seconds to sleep between characters (default 0.005).
    """

    # SendInput constants
    _INPUT_KEYBOARD = 1
    _KEYEVENTF_UNICODE = 0x0004
    _KEYEVENTF_KEYUP = 0x0002
    _KEYEVENTF_EXTENDEDKEY = 0x0001

    def __init__(self, char_delay: float = 0.005) -> None:
        self._char_delay = char_delay

        if sys.platform != "win32":
            raise RuntimeError("Win32TextInjector is only available on Windows")

        import ctypes.wintypes

        self._ctypes = ctypes
        # use_last_error=True so GetLastError after SendInput belongs to that
        # call. ``ctypes.windll.user32`` is a shared, process-wide handle
        # built without it, where any other ctypes call can overwrite the
        # error code first -- so it can tell us an event was refused but
        # never reliably why.
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

        # Build C struct types once
        self._build_input_types()
        logger.info("Win32TextInjector initialised (char_delay=%s)", char_delay)

    # -- ctypes structure setup -------------------------------------------- #

    def _build_input_types(self) -> None:
        """Define the KEYBDINPUT / INPUT structs for SendInput."""
        import ctypes.wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.wintypes.WORD),
                ("wScan", ctypes.wintypes.WORD),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("time", ctypes.wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT(ctypes.Structure):
            class _INPUT_UNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT)]

            _anonymous_ = ("_union",)
            _fields_ = [
                ("type", ctypes.wintypes.DWORD),
                ("_union", _INPUT_UNION),
            ]

        self._KEYBDINPUT = KEYBDINPUT
        self._INPUT = INPUT

    # -- low-level helpers ------------------------------------------------- #

    def _make_unicode_input(self, char: str, *, key_up: bool = False) -> object:
        """Create an INPUT struct for a Unicode character event."""
        flags = self._KEYEVENTF_UNICODE
        if key_up:
            flags |= self._KEYEVENTF_KEYUP
        ki = self._KEYBDINPUT(
            wVk=0,
            wScan=ord(char),
            dwFlags=flags,
            time=0,
            dwExtraInfo=self._ctypes.pointer(self._ctypes.c_ulong(0)),
        )
        inp = self._INPUT(type=self._INPUT_KEYBOARD)
        inp.ki = ki
        return inp

    def _make_vk_input(self, vk: int, *, key_up: bool = False) -> object:
        """Create an INPUT struct for a virtual key event."""
        flags = 0
        if key_up:
            flags |= self._KEYEVENTF_KEYUP
        # Extended keys (navigation, etc.)
        if vk in (0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E):
            flags |= self._KEYEVENTF_EXTENDEDKEY
        ki = self._KEYBDINPUT(
            wVk=vk,
            wScan=0,
            dwFlags=flags,
            time=0,
            dwExtraInfo=self._ctypes.pointer(self._ctypes.c_ulong(0)),
        )
        inp = self._INPUT(type=self._INPUT_KEYBOARD)
        inp.ki = ki
        return inp

    def _send_inputs(self, *inputs: object) -> InjectionOutcome:
        """Send one or more INPUT structs via SendInput and report the result.

        Pre-fix this was typed ``-> None``: the count Windows returned was
        compared to ``n``, logged on mismatch, and then dropped, so the one
        piece of ground truth about whether the keystrokes were accepted
        never left this method.
        """
        n = len(inputs)
        arr = (self._INPUT * n)(*inputs)
        sent = int(
            self._user32.SendInput(
                n,
                self._ctypes.pointer(arr),
                self._ctypes.sizeof(self._INPUT),
            )
            or 0
        )
        error = 0
        if sent != n:
            try:
                error = int(self._ctypes.get_last_error())
            except (AttributeError, OSError, ValueError):
                error = 0
            logger.warning("SendInput sent %d/%d inputs (Win32 error %d)", sent, n, error)
        return InjectionOutcome(requested=n, accepted=sent, last_error=error)

    # -- public interface -------------------------------------------------- #

    def type_text(self, text: str) -> InjectionOutcome:
        """Type text character by character using KEYEVENTF_UNICODE SendInput.

        Args:
            text: Unicode string to inject.

        Returns:
            The total across every character. A dictation phrase where the
            OS took the first few keystrokes and refused the rest is a
            partial injection, and this is where that is visible.
        """
        total = EMPTY_OUTCOME
        for char in text:
            down = self._make_unicode_input(char, key_up=False)
            up = self._make_unicode_input(char, key_up=True)
            total = total.merge(self._send_inputs(down, up))
            if self._char_delay > 0:
                time.sleep(self._char_delay)
        return total

    def press_key(self, key: str) -> InjectionOutcome:
        """Press and release a named key.

        Args:
            key: Case-insensitive key name (e.g. ``"return"``, ``"tab"``).

        Raises:
            ValueError: If the key name is not recognised.
        """
        vk = _VK_MAP.get(key.lower())
        if vk is None:
            # Try single character
            if len(key) == 1:
                return self.type_text(key)
            raise ValueError("Unknown key name: %s" % key)
        down = self._make_vk_input(vk, key_up=False)
        up = self._make_vk_input(vk, key_up=True)
        return self._send_inputs(down, up)

    def backspace(self, count: int = 1) -> InjectionOutcome:
        """Press Backspace *count* times.

        Args:
            count: Number of backspace presses.
        """
        vk_back = _VK_MAP["backspace"]
        total = EMPTY_OUTCOME
        for _ in range(count):
            down = self._make_vk_input(vk_back, key_up=False)
            up = self._make_vk_input(vk_back, key_up=True)
            total = total.merge(self._send_inputs(down, up))
            if self._char_delay > 0:
                time.sleep(self._char_delay)
        return total

    def combo_key(self, modifier: str, key: str) -> InjectionOutcome:
        """Press a modifier+key combination (e.g. Ctrl+A, Ctrl+V).

        Args:
            modifier: Modifier name (``"ctrl"``, ``"alt"``, ``"shift"``).
            key: The key to press with the modifier.

        Raises:
            ValueError: If the modifier name is not recognised.
        """
        mod_vk = _MOD_MAP.get(modifier.lower())
        if mod_vk is None:
            raise ValueError("Unknown modifier: %s" % modifier)

        # Determine the main key VK
        key_vk = _VK_MAP.get(key.lower())
        if key_vk is None:
            # Single letter — use the uppercase ASCII code as VK
            if len(key) == 1 and key.isascii():
                key_vk = ord(key.upper())
            else:
                raise ValueError("Unknown key for combo: %s" % key)

        mod_down = self._make_vk_input(mod_vk, key_up=False)
        key_down = self._make_vk_input(key_vk, key_up=False)
        key_up = self._make_vk_input(key_vk, key_up=True)
        mod_up = self._make_vk_input(mod_vk, key_up=True)
        return self._send_inputs(mod_down, key_down, key_up, mod_up)


# ======================================================================== #
# Pynput cross-platform fallback                                           #
# ======================================================================== #


class PynputTextInjector:
    """Cross-platform text injector using pynput.

    This is a fallback for platforms where Win32 APIs are unavailable
    (Linux, macOS).  Requires the ``pynput`` package.

    Args:
        char_delay: Seconds to sleep between characters (default 0.005).
    """

    def __init__(self, char_delay: float = 0.005) -> None:
        self._char_delay = char_delay

        try:
            from pynput.keyboard import Controller, Key

            self._controller = Controller()
            self._Key = Key
        except ImportError as exc:
            raise RuntimeError("pynput is required for cross-platform text injection: pip install pynput") from exc
        logger.info("PynputTextInjector initialised (char_delay=%s)", char_delay)

    def type_text(self, text: str) -> InjectionOutcome:
        """Type text using pynput.

        Args:
            text: String to type.

        Returns:
            A submitted-count outcome. pynput's controller returns nothing,
            so this backend can report what it sent and never whether the OS
            or the application took it.
        """
        for char in text:
            self._controller.type(char)
            if self._char_delay > 0:
                time.sleep(self._char_delay)
        return _unobservable(len(text))

    def press_key(self, key: str) -> InjectionOutcome:
        """Press and release a named key.

        Args:
            key: Case-insensitive key name.

        Raises:
            ValueError: If the key name is not recognised.
        """
        pynput_key = self._resolve_pynput_key(key)
        self._controller.press(pynput_key)
        self._controller.release(pynput_key)
        return _unobservable(1)

    def backspace(self, count: int = 1) -> InjectionOutcome:
        """Press Backspace *count* times.

        Args:
            count: Number of backspace presses.
        """
        for _ in range(count):
            self._controller.press(self._Key.backspace)
            self._controller.release(self._Key.backspace)
            if self._char_delay > 0:
                time.sleep(self._char_delay)
        return _unobservable(count)

    def combo_key(self, modifier: str, key: str) -> InjectionOutcome:
        """Press a modifier+key combination.

        Args:
            modifier: Modifier name (``"ctrl"``, ``"alt"``, ``"shift"``).
            key: The key to press.

        Raises:
            ValueError: If the modifier is not recognised.
        """
        mod_map = {
            "ctrl": self._Key.ctrl,
            "control": self._Key.ctrl,
            "alt": self._Key.alt,
            "shift": self._Key.shift,
            "cmd": self._Key.cmd,
        }
        mod = mod_map.get(modifier.lower())
        if mod is None:
            raise ValueError("Unknown modifier: %s" % modifier)

        pynput_key = self._resolve_pynput_key(key)
        with self._controller.pressed(mod):
            self._controller.press(pynput_key)
            self._controller.release(pynput_key)
        return _unobservable(1)

    def _resolve_pynput_key(self, key: str) -> object:
        """Map a key name to a pynput Key enum member or character.

        Args:
            key: Case-insensitive key name.

        Returns:
            pynput Key enum member or single character.

        Raises:
            ValueError: If the key name is unknown.
        """
        key_map = {
            "return": self._Key.enter,
            "enter": self._Key.enter,
            "tab": self._Key.tab,
            "backspace": self._Key.backspace,
            "back": self._Key.backspace,
            "escape": self._Key.esc,
            "esc": self._Key.esc,
            "space": self._Key.space,
            "delete": self._Key.delete,
            "del": self._Key.delete,
            "home": self._Key.home,
            "end": self._Key.end,
            "left": self._Key.left,
            "up": self._Key.up,
            "right": self._Key.right,
            "down": self._Key.down,
            "pageup": self._Key.page_up,
            "pagedown": self._Key.page_down,
            "insert": self._Key.insert,
            "capslock": self._Key.caps_lock,
        }
        resolved = key_map.get(key.lower())
        if resolved is not None:
            return resolved
        if len(key) == 1:
            return key
        raise ValueError("Unknown key name: %s" % key)


# ======================================================================== #
# Factory                                                                   #
# ======================================================================== #


def create_text_injector(char_delay: float = 0.005) -> TextInjectorPort:
    """Create the best available text injector for the current platform.

    On Windows, uses the Win32 ``SendInput`` API.  Elsewhere, falls
    back to pynput.

    Args:
        char_delay: Seconds to pause between individual character presses.

    Returns:
        A :class:`TextInjectorPort` implementation.
    """
    if platform.system() == "Windows":
        try:
            return Win32TextInjector(char_delay=char_delay)
        except Exception:
            logger.warning(
                "Win32TextInjector unavailable, falling back to pynput",
                exc_info=True,
            )
    return PynputTextInjector(char_delay=char_delay)


__all__ = [
    "EMPTY_OUTCOME",
    "ERROR_ACCESS_DENIED",
    "InjectionOutcome",
    "PynputTextInjector",
    "TextInjectorPort",
    "Win32TextInjector",
    "create_text_injector",
]
