#!/usr/bin/env python3
"""Headless subprocess spawning for detached (pythonw) daemons.

Why this exists
---------------
A console-less parent process (``pythonw.exe`` -- scheduled tasks, ``--watch``
daemons, detached background processes) has NO console to lend a child. When such
a parent spawns a CONSOLE program (``ssh.exe``, ``git.exe``, ``docker.exe``,
``python.exe`` ...) via ``subprocess`` WITHOUT a "no window" flag, Windows
allocates a BRAND-NEW visible console window for the child because there is no
console to inherit. So every daemon tick flashes a terminal on the desktop
(#261, 2026-07-07: the ``blackboard_sync --watch`` daemon flashed once per 20s
tick spawning ``ssh``).

``CREATE_NO_WINDOW`` (0x08000000) gives the child a console that is simply
INVISIBLE -- the correct default for a background spawn: output still pipes back
normally, nothing pops up on screen.

ONE source
----------
Every daemon / pythonw-launched / detached-background spawn site imports from
here so the flag is applied identically everywhere. The ``daemon-subprocess-headless``
ratchet gate (``scripts/check_daemon_subprocess_headless.py``) fails if an
in-scope daemon spawns a subprocess without this flag.

Usage
-----
    from utils.proc import NO_WINDOW_KWARGS, run_hidden

    # Spread into any subprocess call (keeps every other kwarg explicit):
    subprocess.run(argv, capture_output=True, text=True, **NO_WINDOW_KWARGS)
    subprocess.Popen(argv, **NO_WINDOW_KWARGS)

    # Or the thin wrapper (identical signature to subprocess.run):
    run_hidden(argv, capture_output=True, text=True)

On non-Windows platforms the flag does not exist; ``NO_WINDOW_KWARGS`` is empty
and ``run_hidden`` is a plain ``subprocess.run``, so the same call is portable.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# subprocess.CREATE_NO_WINDOW is a Windows-only attribute. 0x08000000 is its
# documented value; prefer the stdlib attribute when present so any future
# platform change tracks automatically. Zero (a no-op flag) off Windows.
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if sys.platform == "win32" else 0

# Spread these into subprocess.run / Popen / call / check_output on a detached
# daemon so the child gets an INVISIBLE console instead of a fresh visible window.
# Empty off Windows so the same spread is a portable no-op.
NO_WINDOW_KWARGS: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def run_hidden(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run`` with the no-window flag folded in on Windows.

    Any explicit ``creationflags`` the caller passes are OR-ed with
    ``CREATE_NO_WINDOW`` (so a caller can still add ``CREATE_NEW_PROCESS_GROUP``
    etc. and still get the invisible console). Off Windows this is a plain
    ``subprocess.run``.
    """
    if sys.platform == "win32":
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | CREATE_NO_WINDOW
    return subprocess.run(*args, **kwargs)
