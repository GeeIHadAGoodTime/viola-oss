"""Compatibility exports for the renamed hub audio controller.

Older ProcTap and Spotify-CDP code imports ``source_audio_controller``. The
canonical implementation now lives in ``hub_audio_controller``; keep this shim
dynamic so tests that patch the canonical module still exercise the callers.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_HUB_MODULE = "audio_core.capture.hub_audio_controller"


def _hub() -> Any:
    return import_module(_HUB_MODULE)


def register_external_audio_pid(pid: int) -> None:
    _hub().register_external_audio_pid(pid)


def unregister_external_audio_pid(pid: int) -> None:
    _hub().unregister_external_audio_pid(pid)


def exclude_pid(pid: int) -> None:
    _hub().exclude_pid(pid)


def include_pid(pid: int) -> None:
    _hub().include_pid(pid)


def find_audio_child_pid() -> int | None:
    return _hub().find_audio_child_pid()


def find_all_audio_pids() -> list[int]:
    return _hub().find_all_audio_pids()


def __getattr__(name: str) -> Any:
    if name in {"_external_audio_pids", "_excluded_pids"}:
        return getattr(_hub(), name)
    raise AttributeError(name)


__all__ = [
    "exclude_pid",
    "find_all_audio_pids",
    "find_audio_child_pid",
    "include_pid",
    "register_external_audio_pid",
    "unregister_external_audio_pid",
]
