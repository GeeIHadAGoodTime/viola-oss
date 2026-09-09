"""
Wake Word Decision Module
=========================

This module provides the central policy for wake word detection decisions.

Public API:
    WakeContext: Context data for wake decision evaluation
    WakeDecisionResult: Result of a wake decision
    WakePolicyConfig: Configuration for the wake policy
    WakeDecisionPolicy: Main policy class (from parent module)
    get_wake_policy: Get the singleton policy instance
    reset_wake_policy: Reset the singleton policy
    create_wake_policy: Create a new policy instance
"""

from __future__ import annotations

from voice.wake_detector.wake.config import WakePolicyConfig
from voice.wake_detector.wake.context import WakeContext, WakeDecisionResult

__all__ = [
    "WakeContext",
    "WakeDecisionResult",
    "WakePolicyConfig",
]
