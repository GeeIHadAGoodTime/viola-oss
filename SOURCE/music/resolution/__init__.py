"""
music/resolution/__init__.py

Pluggable resolution orchestration for parallel and batch operations.
"""

from __future__ import annotations

from .error_handler import ErrorHandlingResult, ErrorKind, ResolutionErrorHandler
from .orchestrator import ResolutionOrchestrator, ResolutionResult

# Conditional import for YouTubeMusicResolver (may not exist yet)
try:
    from .youtube_resolver import YouTubeMusicResolver

    __all__ = [
        "ErrorHandlingResult",
        "ErrorKind",
        "ResolutionErrorHandler",
        "ResolutionOrchestrator",
        "ResolutionResult",
        "YouTubeMusicResolver",
    ]
except ImportError:
    __all__ = [
        "ErrorHandlingResult",
        "ErrorKind",
        "ResolutionErrorHandler",
        "ResolutionOrchestrator",
        "ResolutionResult",
    ]
