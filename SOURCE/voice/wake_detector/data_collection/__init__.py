"""Wake word data collection pipeline.

Provides automatic classification of wake triggers as TP/FP, near-miss
capture, anonymization, upload queue, and model hot-swap.

Usage:
    from voice.wake_detector.data_collection import get_data_collector

    collector = get_data_collector()
    clip_id = collector.save_trigger_clip(audio, score, threshold)
    # ... later, after command outcome ...
    collector.notify_outcome(clip_id, "nlu_success")
"""

from __future__ import annotations

from .classifier import (
    Outcome,
    TriggerClassifier,
    get_trigger_classifier,
    notify_data_collector,
)
from .collector import ClipCollector, get_clip_collector
from .database import DataCollectionDB, get_data_collection_db
from .uploader import migrate_flat_outbox, reset_device_batch_id
from .validator import validate_clip, validate_pending

__all__ = [
    "ClipCollector",
    "DataCollectionDB",
    "Outcome",
    "TriggerClassifier",
    "get_clip_collector",
    "get_data_collection_db",
    "get_trigger_classifier",
    "migrate_flat_outbox",
    "notify_data_collector",
    "reset_device_batch_id",
    "validate_clip",
    "validate_pending",
]
