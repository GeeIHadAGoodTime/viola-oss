from __future__ import annotations

from core.logging_config import get_logger

from .ai_interpreter import IntentInterpreter

# Re-export private functions for backward compatibility with tests
from .interpreter_helpers import _clamp, _detect_source, _parse_hms
from .pipeline_contracts import IntentTTSPort, MusicPort
from .types import Intent, IntentName, IntentResult

logger = get_logger(__name__)

__all__ = [
    "Intent",
    "IntentInterpreter",
    "IntentName",
    "IntentResult",
    "IntentTTSPort",
    "MusicPort",
    "_clamp",
    "_detect_source",
    "_parse_hms",
]
