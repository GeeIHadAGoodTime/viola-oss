"""System eSpeak-NG locator for the optional Kokoro phonemizer path.

The open-source core does not redistribute eSpeak-NG. Install it separately
from the operating system or another licensed source when Kokoro is enabled.
"""

from __future__ import annotations

import ctypes.util
import os
from pathlib import Path


def get_library_path() -> str:
    value = os.environ.get("PHONEMIZER_ESPEAK_LIBRARY")
    library = value or ctypes.util.find_library("espeak-ng") or ctypes.util.find_library("espeak")
    if not library:
        raise ImportError("system eSpeak-NG library not found; install it to enable Kokoro")
    return library


def get_data_path() -> str:
    candidates = []
    configured = os.environ.get("PHONEMIZER_ESPEAK_DATA_PATH")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        (
            Path("/usr/share/espeak-ng-data"),
            Path("/usr/local/share/espeak-ng-data"),
            Path("/opt/homebrew/share/espeak-ng-data"),
        )
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise ImportError("system eSpeak-NG data not found; install it to enable Kokoro")


def load_library() -> str:
    return get_library_path()


def make_library_available() -> str:
    return get_library_path()
