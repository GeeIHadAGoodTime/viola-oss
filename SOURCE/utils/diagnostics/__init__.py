"""
Diagnostics and self-check utilities
"""

from __future__ import annotations

from .collector import DiagnosticsCollector
from .self_check import CheckResult, CheckStatus, SelfCheck

__all__ = ["CheckResult", "CheckStatus", "DiagnosticsCollector", "SelfCheck"]
