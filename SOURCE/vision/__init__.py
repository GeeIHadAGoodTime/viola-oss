"""Screen awareness subsystem -- captures and analyzes screen content on demand."""

from __future__ import annotations

from vision.analyzer import ScreenInsightSession, VisionAnalyzer
from vision.privacy_filter import PrivacyBlockedError, PrivacyFilter, get_privacy_filter
from vision.screen_capture import ScreenContext

__all__ = [
    "PrivacyBlockedError",
    "PrivacyFilter",
    "ScreenContext",
    "ScreenInsightSession",
    "VisionAnalyzer",
    "get_privacy_filter",
]
