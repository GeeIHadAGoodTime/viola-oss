"""
Unified Voice Pipeline.

Top-level exports are loaded lazily so importing a small submodule such as
``voice.synthesis.tts_wire`` does not initialize Whisper, wake detection, or
the full voice pipeline.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voice.audio_manager import AudioDeviceManager
    from voice.pipeline import VoicePipeline
    from voice.spoke_handler import SpokeVoiceHandler
    from voice.synthesizer import Synthesizer
    from voice.transcriber import Transcriber
    from voice.wake_detector import WakeDetector

_EXPORTS: dict[str, tuple[str, str]] = {
    "AudioDeviceManager": ("voice.audio_manager", "AudioDeviceManager"),
    "Synthesizer": ("voice.synthesizer", "Synthesizer"),
    "Transcriber": ("voice.transcriber", "Transcriber"),
    "VoicePipeline": ("voice.pipeline", "VoicePipeline"),
    "WakeDetector": ("voice.wake_detector", "WakeDetector"),
}

_OPTIONAL_EXPORTS: dict[str, tuple[str, str]] = {
    "SpokeVoiceHandler": ("voice.spoke_handler", "SpokeVoiceHandler"),
}

__all__ = [
    "AudioDeviceManager",
    "SpokeVoiceHandler",
    "Synthesizer",
    "Transcriber",
    "VoicePipeline",
    "WakeDetector",
]


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    optional = False
    if target is None:
        target = _OPTIONAL_EXPORTS.get(name)
        optional = target is not None
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = target
    try:
        value = getattr(import_module(module_name), attr_name)
    except ImportError:
        if not optional:
            raise
        value = None
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
