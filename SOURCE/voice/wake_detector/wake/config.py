"""
Wake Policy Configuration
=========================

Configuration dataclass for wake decision policy.

5-gate pipeline configuration:
  1. Zero-Input Guard
  2. Score Threshold (0.90 quiet / 0.90 playback floor)
  3. VAD Gate
  4. Cooldown (2000ms)
  5. Listening Gate
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import AppConfig


@dataclass
class WakePolicyConfig:
    """
    Configuration for the 5-gate wake decision policy.

    All removed gates (echo veto, confirmation window,
    SNR scaling, barge-in, etc.) were deliberately
    stripped on 2026-02-10. They were compensating for model/AEC
    weaknesses and are replaced by model retraining.
    """

    # --- Gate 2: Score Threshold ---
    # Raised from 0.50 to 0.80 (false activation flood 2026-03-11),
    # then aligned to the launch AppConfig default 0.90 after fixture battery.
    base_threshold: float = 0.90

    # --- Gate 3: Cooldown ---
    cooldown_ms: int = 2000

    # --- Gate 1: Zero-Input Guard ---
    zero_input_rms_threshold: float = 1.0  # int16 RMS below this = garbage

    # --- Gate 2: Playback boost ---
    # During active playback (loopback_rms > playback_presence_rms_threshold),
    # keep at least the launch base threshold. The 2026-06-05 fixture battery
    # showed 0.95 missed too many real "Viola" clips during playback context,
    # while 0.90 kept current music/near-miss fixtures rejected.
    playback_threshold: float = 0.90

    # --- Score tracking window (for 2-of-3 confirmation) ---
    confirmation_window: int = 3  # number of recent scores to track

    # --- Misc (retained for diagnostics compatibility) ---
    playback_presence_rms_threshold: float = 150.0  # used by diagnostics only

    @classmethod
    def from_app_config(cls, config: AppConfig) -> WakePolicyConfig:
        """Create policy config from AppConfig."""
        return cls(
            base_threshold=getattr(config, "wake_sensitivity", 0.90),
            cooldown_ms=getattr(config, "wake_cooldown_ms", 2000),
            zero_input_rms_threshold=getattr(config, "wake_zero_input_rms", 1.0),
        )
