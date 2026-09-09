"""Auto-classification of wake word trigger outcomes.

Observes what happens after a wake trigger fires and classifies the
clip as true_positive, false_positive, or ambiguous based on the outcome.

Integration: VoiceCommandHandler calls notify_outcome() at each outcome point.
The classifier updates the database row created by the collector.
"""

from __future__ import annotations

import enum
import threading
import time

from core.logging_config import get_logger

from .database import get_data_collection_db

logger = get_logger(__name__)

# If no outcome received within this time, classify as ambiguous
OBSERVATION_TIMEOUT_SEC = 5.0


class Outcome(enum.Enum):
    """Post-trigger outcome types."""

    NLU_SUCCESS = "nlu_success"
    NLU_FAIL = "nlu_fail"
    SILENCE = "silence"
    CANCEL = "cancel"
    TIMEOUT = "timeout"
    NO_SPEECH = "no_speech"
    IGNORED = "ignored"


# Mapping from outcome to (classification, classification_method)
_OUTCOME_MAP: dict[Outcome, tuple[str, str]] = {
    Outcome.NLU_SUCCESS: ("true_positive", "auto_nlu_success"),
    Outcome.NLU_FAIL: ("ambiguous", "auto_nlu_fail"),
    Outcome.SILENCE: ("false_positive", "auto_silence"),
    Outcome.CANCEL: ("false_positive", "auto_cancel"),
    Outcome.TIMEOUT: ("ambiguous", "auto_timeout"),
    Outcome.NO_SPEECH: ("false_positive", "auto_no_speech"),
    Outcome.IGNORED: ("false_positive", "auto_gpt_ignored"),
}


class _PendingTrigger:
    """Tracks a trigger awaiting classification."""

    __slots__ = ("clip_id", "created_at")

    def __init__(self, clip_id: int) -> None:
        self.clip_id = clip_id
        self.created_at = time.monotonic()


class TriggerClassifier:
    """Classifies wake triggers based on post-trigger outcomes.

    Thread-safe: uses a lock for the pending triggers dict.
    Non-blocking: timeout sweeps run on a background timer.
    """

    def __init__(self) -> None:
        self._pending: dict[int, _PendingTrigger] = {}
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last_trigger_time = 0.0
        self._running = False

    @property
    def last_trigger_time(self) -> float:
        """Unix timestamp of the most recent trigger (for idle checks)."""
        return self._last_trigger_time

    def start(self) -> None:
        self._running = True
        self._schedule_sweep()

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def on_trigger(self, clip_id: int) -> None:
        """Register a new trigger for observation."""
        self._last_trigger_time = time.time()
        with self._lock:
            self._pending[clip_id] = _PendingTrigger(clip_id)
        logger.debug("Classifier tracking trigger clip_id=%d", clip_id)

    def on_outcome(self, clip_id: int, outcome: Outcome) -> None:
        """Classify a trigger based on its outcome."""
        with self._lock:
            pending = self._pending.pop(clip_id, None)

        if pending is None:
            logger.debug(
                "Outcome for unknown clip_id=%d (already classified or timed out)",
                clip_id,
            )
            return

        classification, method = _OUTCOME_MAP[outcome]
        db = get_data_collection_db()
        db.update_classification(clip_id, classification, method)
        logger.info(
            "Classified clip %d as %s (%s)",
            clip_id,
            classification,
            method,
        )

    def _schedule_sweep(self) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(OBSERVATION_TIMEOUT_SEC, self._sweep_timeouts)
        self._timer.daemon = True
        self._timer.start()

    def _sweep_timeouts(self) -> None:
        """Classify any pending triggers that have timed out."""
        now = time.monotonic()
        timed_out: list[int] = []

        with self._lock:
            for clip_id, pending in list(self._pending.items()):
                if now - pending.created_at >= OBSERVATION_TIMEOUT_SEC:
                    timed_out.append(clip_id)
                    del self._pending[clip_id]

        if timed_out:
            db = get_data_collection_db()
            for clip_id in timed_out:
                db.update_classification(clip_id, "ambiguous", "auto_timeout")
                logger.debug("Timeout-classified clip %d as ambiguous", clip_id)

        self._schedule_sweep()


_classifier: TriggerClassifier | None = None
_classifier_lock = threading.Lock()


def get_trigger_classifier() -> TriggerClassifier:
    global _classifier
    with _classifier_lock:
        if _classifier is None:
            _classifier = TriggerClassifier()
        return _classifier


def notify_data_collector(outcome_str: str, clip_id: int | None) -> None:
    """Fire-and-forget notification from VoiceCommandHandler.

    Does nothing if data collection is disabled or clip_id is None.
    """
    if not settings_enabled() or clip_id is None:
        return

    try:
        outcome = Outcome(outcome_str)
    except ValueError:
        logger.warning("Unknown outcome: %s", outcome_str)
        return

    classifier = get_trigger_classifier()
    classifier.on_outcome(clip_id, outcome)


def settings_enabled() -> bool:
    """Check if data collection is enabled in settings."""
    try:
        from config.settings import settings

        return bool(settings.wake_data_collection_enabled)
    except Exception:
        return False
