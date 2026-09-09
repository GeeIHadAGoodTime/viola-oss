"""Live dictation subsystem -- streams speech to text at the cursor position."""

from __future__ import annotations

from voice.dictation.command_parser import DictationAction, DictationCommand
from voice.dictation.controller import DictationController, DictationState
from voice.dictation.streaming_stt import StreamingSTTEngine, StreamingSTTFactory
from voice.dictation.text_injector import TextInjectorPort, create_text_injector

__all__ = [
    "DictationAction",
    "DictationCommand",
    "DictationController",
    "DictationState",
    "StreamingSTTEngine",
    "StreamingSTTFactory",
    "TextInjectorPort",
    "create_text_injector",
]
