"""Context controls for browser-spoke TTS broadcast routing."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_skip_spoke_tts_broadcast: ContextVar[bool] = ContextVar(
    "skip_spoke_tts_broadcast",
    default=False,
)


def should_skip_spoke_tts_broadcast() -> bool:
    """Return whether the current context should suppress spoke TTS fan-out."""
    return _skip_spoke_tts_broadcast.get()


@contextmanager
def suppress_spoke_tts_broadcast() -> Iterator[None]:
    """Suppress audio-stream TTS fan-out for the current async context."""
    token = _skip_spoke_tts_broadcast.set(True)
    try:
        yield
    finally:
        _skip_spoke_tts_broadcast.reset(token)
