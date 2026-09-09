"""
Audio capture providers for multi-room streaming.

Captures the hub device's audio output as 48 kHz stereo 16-bit PCM.
"""

from __future__ import annotations

from .factory import get_capture_provider

__all__ = ["get_capture_provider"]
