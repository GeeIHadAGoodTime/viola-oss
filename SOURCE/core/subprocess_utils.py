"""Subprocess helpers that avoid visible console windows on Windows."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

_IS_WINDOWS = sys.platform == "win32"

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _IS_WINDOWS else 0


def no_window_creationflags(existing: int = 0) -> int:
    """Return creation flags with Windows console-window suppression applied."""

    if _IS_WINDOWS and NO_WINDOW:
        return int(existing) | NO_WINDOW
    return int(existing)


def silent_subprocess_kwargs(kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return subprocess kwargs that do not open a console on Windows.

    Explicit stdout/stderr pipes or handles are preserved.  When a caller is not
    capturing output, stdio is redirected to DEVNULL so console programs cannot
    attach to an inherited interactive stream.
    """

    resolved = dict(kwargs or {})
    resolved["creationflags"] = no_window_creationflags(resolved.get("creationflags", 0))
    if "input" not in resolved:
        resolved.setdefault("stdin", subprocess.DEVNULL)

    if not resolved.get("capture_output"):
        resolved.setdefault("stdout", subprocess.DEVNULL)
        resolved.setdefault("stderr", subprocess.DEVNULL)

    return resolved


def run_silent(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a subprocess without opening a visible console window on Windows."""

    return subprocess.run(cmd, **silent_subprocess_kwargs(kwargs))


def popen_silent(cmd: Any, **kwargs: Any) -> subprocess.Popen:
    """Start a subprocess without opening a visible console window on Windows."""

    return subprocess.Popen(cmd, **silent_subprocess_kwargs(kwargs))
