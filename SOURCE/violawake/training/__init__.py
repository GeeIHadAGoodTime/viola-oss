"""
ViolaWake Training Module
==========================

Training utilities for wake word models.

Usage:
    from violawake.training import WakeWordDataset, AudioAugmenter, train_model
"""

from __future__ import annotations

from violawake.training.dataset import AudioAugmenter, WakeWordDataset
from violawake.training.trainer import evaluate_model, train_model

__all__ = [
    "AudioAugmenter",
    "WakeWordDataset",
    "evaluate_model",
    "train_model",
]
