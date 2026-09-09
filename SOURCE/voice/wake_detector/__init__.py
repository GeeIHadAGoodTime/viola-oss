"""
Wake Detector Module
====================

Unified wake word detection subsystem for NOVVIOLA.

This module provides the core wake word detection functionality including:
- ViolaWake listener implementation
- Central WakeDecisionPolicy for threshold and gating decisions
- AEC (Acoustic Echo Cancellation) processing
- VAD (Voice Activity Detection) processing
- Barge-in detection
- Contributor mode for training data collection

Public API
----------
Main Classes:
    WakeDecisionPolicy: Central authority for wake word decisions
    WakeContext: Context data for wake decision evaluation
    WakeDecisionResult: Result of a wake decision
    WakePolicyConfig: Configuration for the wake policy
    ViolaWakeListener: Primary wake word listener implementation

Factory Functions:
    get_wake_policy: Get the singleton policy instance
    reset_wake_policy: Reset the singleton policy
    create_wake_listener: Create a wake listener instance
    wire_aec_reference: Wire AEC reference from audio sink

Usage
-----
    from voice.wake_detector import (
        WakeDecisionPolicy,
        WakeContext,
        get_wake_policy,
        create_wake_listener,
    )

    # Get the singleton policy
    policy = get_wake_policy()

    # Create wake context
    context = WakeContext(
        wake_score=0.75,
        mic_rms=1200,
        loopback_rms=3500,
    )

    # Make trigger decision
    decision = policy.final_trigger_decision(context)
"""

from __future__ import annotations

# High-level facade (was voice/wake_detector.py, moved to avoid module/package conflict)
from voice.wake_detector.facade import (
    AdaptiveGainSchedulerProtocol,
    SupportsAdaptiveGain,
    WakeDecisionPolicyProtocol,
    WakeDetector,
    WakeDetectorPort,
)

# Core data classes from wake submodule
from voice.wake_detector.wake.config import WakePolicyConfig
from voice.wake_detector.wake.context import WakeContext, WakeDecisionResult

# Main policy class and factory functions
from voice.wake_detector.wake_decision_policy import (
    WakeDecisionPolicy,
    create_wake_policy,
    get_wake_policy,
    reset_wake_policy,
)

# Factory functions for wake listeners
from voice.wake_detector.wake_factory import create_wake_listener, wire_aec_reference

__all__ = [
    "AdaptiveGainSchedulerProtocol",
    "SupportsAdaptiveGain",
    "WakeContext",
    "WakeDecisionPolicy",
    "WakeDecisionPolicyProtocol",
    "WakeDecisionResult",
    "WakeDetector",
    "WakeDetectorPort",
    "WakePolicyConfig",
    "create_wake_listener",
    "create_wake_policy",
    "get_wake_policy",
    "reset_wake_policy",
    "wire_aec_reference",
]
