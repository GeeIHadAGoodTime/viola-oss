# voice/synthesis/__init__.py
"""Text-to-Speech (TTS) Synthesis Package.

This is the canonical location for all TTS functionality in Viola.
Other modules should import from here rather than reimplementing TTS interfaces.

Exports:
    - KokoroTTSEngine: Neural TTS engine using Kokoro-82M (default)
    - TTSEngine: Thread-safe TTS engine using pyttsx3 (fallback)
    - LazyTTSEngine: Lazy-loading wrapper that defers initialization
    - TTSPort: Protocol defining the TTS interface
    - LocalTTS: Local TTS implementation wrapper (pyttsx3)
    - TTSFactory: Factory for creating TTS components with fallback support
"""

from __future__ import annotations

from voice.synthesis.engine import TTSEngine
from voice.synthesis.factory import LocalTTS, TTSFactory, TTSPort
from voice.synthesis.kokoro_engine import KokoroTTSEngine
from voice.synthesis.lazy_engine import LazyTTSEngine

__all__ = [
    "KokoroTTSEngine",
    "LazyTTSEngine",
    "LocalTTS",
    "TTSEngine",
    "TTSFactory",
    "TTSPort",
]
