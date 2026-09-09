"""
Voice Transcription Module
==========================

Provides speech-to-text functionality for NOVVIOLA.

Key exports:
- ASRPort: Protocol for ASR implementations (async interface)
- ASRFactory: Factory pattern for creating ASR components with fallback support
- WhisperTranscriber: Primary local STT implementation using faster-whisper
- DeterministicTranscriber: Test-only transcriber for deterministic testing

Submodules:
- voice.transcription.factory: ASR factory and fallback chain
- voice.transcription.whisper: Whisper-based local transcription
- voice.transcription.deterministic: Test transcriber for automated testing
"""

from __future__ import annotations

from voice.transcription.deterministic import DeterministicTranscriber
from voice.transcription.factory import (
    ASRFactory,
    ASRFallbackChain,
    ASRPort,
    LocalASR,
)
from voice.transcription.whisper import WhisperTranscriber

__all__ = [
    "ASRFactory",
    "ASRFallbackChain",
    "ASRPort",
    "DeterministicTranscriber",
    "LocalASR",
    "WhisperTranscriber",
]
