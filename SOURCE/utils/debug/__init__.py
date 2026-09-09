"""
Debug utilities for Viola
- Ring-buffer debug trace
- Diagnostics
"""

from __future__ import annotations

import json
import logging
import time
import traceback as tb
from pathlib import Path
from types import TracebackType

from .ring_buffer import DebugRingBuffer, TraceEvent, get_debug_ring_buffer

logger = logging.getLogger(__name__)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in value.items():
            out[str(k)] = _json_safe(v)
        return out
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]
    return str(value)


class CrashDumpGenerator:
    """Generate JSON crash dump artifacts for post-mortem debugging."""

    def __init__(self, dump_dir: Path | None = None) -> None:
        self.dump_dir = dump_dir if dump_dir is not None else (Path.cwd() / "crash_dumps")
        self.dump_dir.mkdir(parents=True, exist_ok=True)

    def generate_dump(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> Path:
        dump_path = self.dump_dir / f"crash_dump_{int(time.time() * 1000)}.json"
        dump_data: dict[str, object] = {
            "exception_type": exc_type.__name__,
            "exception_message": str(exc_value),
            "stack_trace": tb.format_exception(exc_type, exc_value, exc_traceback),
            "state_snapshot": {
                "recent_events": get_debug_ring_buffer().export(),
            },
        }

        try:
            with open(dump_path, "w", encoding="utf-8") as f:
                json.dump(_json_safe(dump_data), f, indent=2, sort_keys=True)
        except Exception:
            logger.exception("Failed to write crash dump: %s", dump_path)
            raise

        return dump_path


__all__ = [
    "CrashDumpGenerator",
    "DebugRingBuffer",
    "TraceEvent",
    "get_debug_ring_buffer",
]
