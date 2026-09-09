"""
Wake Audio Buffer & Replay Pipeline
====================================

Enhanced audio capture system with:
- 60-second rolling buffer for comprehensive pre-event capture
- Full diagnostic context snapshots at capture time
- Replay pipeline for re-running detection on captured audio
- Training data audit and export capabilities

This extends audio_forensics.py with:
- Longer rolling buffers
- Integration with all diagnostic collectors
- Deterministic replay for debugging

Usage:
    from diagnostics.wake_audio_buffer import (
        WakeAudioBuffer,
        WakeReplayPipeline,
        get_wake_audio_buffer,
    )

    buffer = get_wake_audio_buffer()

    # Feed audio continuously
    buffer.feed_audio(samples, diagnostics_context)

    # Capture on trigger with full context
    capture = buffer.capture_trigger_event(
        correlation_id=corr_id,
        score=0.85,
        decision_trace=trace,
    )

    # Replay captured audio
    pipeline = WakeReplayPipeline()
    results = pipeline.replay_capture(capture)
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from config.constants import AUDIO_SAMPLE_RATE
from core.constants import AUDIO_INT16_SCALE
from core.logging_config import get_logger
from core.platform import get_logs_dir

if TYPE_CHECKING:
    from diagnostics.wake_decision_trace import WakeDecisionTrace

logger = get_logger(__name__)

# Optional imports
try:
    import soundfile as sf

    SOUNDFILE_AVAILABLE = True
except ImportError:
    SOUNDFILE_AVAILABLE = False
    sf = None


def is_wake_audio_logging_enabled() -> bool:
    """Return whether diagnostic wake-audio capture is user-enabled."""
    try:
        from ui.settings_manager import get_settings_manager

        manager = get_settings_manager()
        return bool(manager.get("wake_audio_logging_enabled", False))
    except Exception:
        return False


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class AudioFrame:
    """Single audio frame with timing metadata."""

    samples: np.ndarray
    timestamp: float
    frame_index: int
    rms_db: float | None = None
    has_speech: bool | None = None


@dataclass
class DiagnosticsSnapshot:
    """Complete diagnostics state at capture time."""

    # Audio pipeline health
    clipping_ratio: float = 0.0
    noise_floor_db: float = -60.0
    buffer_health_score: float = 1.0

    # AEC state
    aec_reduction_db: float = 0.0
    reference_buffer_fill: float = 0.0
    aec_delay_ms: float = 0.0

    # Playback state
    is_playing: bool = False
    playback_volume: float = 0.0
    playback_rms_db: float = -96.0

    # TTS state
    tts_active: bool = False
    tts_speaking: bool = False

    # Model state
    model_loaded: bool = True
    last_inference_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_collectors(cls) -> DiagnosticsSnapshot:
        """Build snapshot from current collector states."""
        snapshot = cls()

        # Try to get audio health
        try:
            from diagnostics.audio_pipeline_health import get_audio_health_monitor

            health = get_audio_health_monitor()
            health_snap = health.get_health_snapshot()
            snapshot.clipping_ratio = health_snap.clipping.clip_ratio
            snapshot.noise_floor_db = health_snap.noise_floor.current_db
            snapshot.buffer_health_score = health_snap.buffer_health.health_score
        except Exception as _e:
            logger.debug("Audio health collector unavailable for snapshot: %s", _e)

        # Try to get AEC state
        try:
            from diagnostics.aec_effectiveness import get_aec_diagnostics

            aec = get_aec_diagnostics()
            aec_snap = aec.get_effectiveness_snapshot()
            snapshot.aec_reduction_db = aec_snap.reduction.current_reduction_db
            snapshot.reference_buffer_fill = aec_snap.reference_buffer.fill_ratio
            snapshot.aec_delay_ms = aec_snap.delay.current_delay_ms
        except Exception as _e:
            logger.debug("AEC diagnostics collector unavailable for snapshot: %s", _e)

        # Try to get state sync
        try:
            from diagnostics.wake_state_sync import get_state_sync_monitor

            state = get_state_sync_monitor()
            state_snap = state.get_sync_snapshot()
            snapshot.is_playing = state_snap.playback.callback_says_playing
            snapshot.playback_volume = state_snap.volume.current_volume
            snapshot.playback_rms_db = state_snap.playback.last_rms_db
            snapshot.tts_active = state_snap.tts.tts_active
            snapshot.tts_speaking = state_snap.tts.currently_speaking
        except Exception as _e:
            logger.debug("Wake state sync collector unavailable for snapshot: %s", _e)

        return snapshot


@dataclass
class CapturedAudio:
    """Complete captured audio with all context."""

    # Core identity
    capture_id: str
    correlation_id: str | None
    capture_time: float
    event_type: str  # "trigger", "false_positive", "missed", "manual"

    # Audio data
    audio_samples: np.ndarray
    sample_rate: int
    duration_seconds: float

    # Wake detection context
    wake_score: float | None = None
    threshold_used: float | None = None
    decision_accepted: bool | None = None
    rejection_reason: str | None = None

    # Decision trace (serialized)
    decision_trace_json: str | None = None

    # Diagnostics at capture time
    diagnostics: DiagnosticsSnapshot = field(default_factory=DiagnosticsSnapshot)

    # File paths if saved
    audio_path: Path | None = None
    metadata_path: Path | None = None

    # Replay results
    replay_results: list[dict[str, Any]] = field(default_factory=list)

    def get_audio_hash(self) -> str:
        """Get hash of audio content for deduplication."""
        return hashlib.sha256(self.audio_samples.tobytes()).hexdigest()[:16]

    def to_metadata_dict(self) -> dict[str, Any]:
        """Export metadata for JSON sidecar."""
        return {
            "capture_id": self.capture_id,
            "correlation_id": self.correlation_id,
            "capture_time": self.capture_time,
            "event_type": self.event_type,
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "audio_hash": self.get_audio_hash(),
            "wake_score": self.wake_score,
            "threshold_used": self.threshold_used,
            "decision_accepted": self.decision_accepted,
            "rejection_reason": self.rejection_reason,
            "diagnostics": self.diagnostics.to_dict(),
            "audio_path": str(self.audio_path) if self.audio_path else None,
        }


@dataclass
class ReplayResult:
    """Result from replaying captured audio through detection."""

    capture_id: str
    replay_time: float

    # Detection results
    detected: bool
    max_score: float
    all_scores: list[float]
    detection_timestamps: list[float]

    # Comparison with original
    original_score: float | None
    score_delta: float | None

    # Detailed breakdown
    frame_results: list[dict[str, Any]] = field(default_factory=list)

    def matches_original(self, tolerance: float = 0.05) -> bool:
        """Check if replay matches original detection."""
        if self.original_score is None:
            return True  # Can't compare
        if self.score_delta is None:
            return False
        return abs(self.score_delta) < tolerance


# =============================================================================
# Wake Audio Buffer
# =============================================================================


class WakeAudioBuffer:
    """
    60-second rolling audio buffer with diagnostic context.

    Features:
    - Long rolling buffer for comprehensive pre-event capture
    - Frame-level metadata (RMS, speech detection)
    - Automatic diagnostic snapshot on capture
    - Deduplication based on audio hash
    """

    DEFAULT_BUFFER_SECONDS = 60.0
    SAMPLE_RATE = AUDIO_SAMPLE_RATE
    DEFAULT_OUTPUT_DIR = get_logs_dir() / "wake_captures"
    FRAME_SIZE = 512  # Samples per frame for metadata tracking

    def __init__(
        self,
        buffer_seconds: float = DEFAULT_BUFFER_SECONDS,
        output_dir: Path | None = None,
        pre_event_seconds: float = 5.0,
        post_event_seconds: float = 2.0,
        max_captures: int = 100,
        enabled: bool | None = None,
    ):
        """
        Initialize wake audio buffer.

        Args:
            buffer_seconds: Rolling buffer size (default 60s)
            output_dir: Directory for saved captures
            pre_event_seconds: Audio to capture before event
            post_event_seconds: Audio to capture after event
            max_captures: Maximum captures to keep in memory
        """
        self._output_dir = Path(output_dir or self.DEFAULT_OUTPUT_DIR)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._buffer_samples = int(buffer_seconds * self.SAMPLE_RATE)
        self._pre_samples = int(pre_event_seconds * self.SAMPLE_RATE)
        self._post_samples = int(post_event_seconds * self.SAMPLE_RATE)
        self._max_captures = max_captures
        self._enabled_override = enabled

        self._lock = threading.Lock()

        # Rolling sample buffer
        self._sample_buffer: deque[float] = deque(maxlen=self._buffer_samples)

        # Frame metadata buffer (one entry per FRAME_SIZE samples)
        max_frames = self._buffer_samples // self.FRAME_SIZE + 1
        self._frame_metadata: deque[AudioFrame] = deque(maxlen=max_frames)

        # Current frame accumulator
        self._current_frame: list[float] = []
        self._frame_index = 0

        # Pending post-event capture
        self._pending_capture: dict[str, Any] | None = None
        self._pending_samples_remaining = 0
        self._post_buffer: list[np.ndarray] = []

        # Recent captures (in memory)
        self._recent_captures: deque[CapturedAudio] = deque(maxlen=max_captures)

        # Seen audio hashes for deduplication
        self._seen_hashes: set[str] = set()

        # Statistics
        self._total_samples_fed = 0
        self._total_captures = 0
        self._duplicate_captures_skipped = 0

        logger.info(
            "Wake audio buffer initialized: buffer_seconds=%s, pre=%s, post=%s",
            buffer_seconds,
            pre_event_seconds,
            post_event_seconds,
        )

    def _is_enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return is_wake_audio_logging_enabled()

    def feed_audio(
        self,
        samples: np.ndarray,
        rms_db: float | None = None,
        has_speech: bool | None = None,
    ) -> None:
        """
        Feed audio samples into rolling buffer.

        Args:
            samples: Audio samples (int16 or float32)
            rms_db: Optional pre-computed RMS in dB
            has_speech: Optional speech detection flag
        """
        if not self._is_enabled():
            return

        with self._lock:
            # Convert to float if needed
            if samples.dtype == np.int16:
                float_samples = samples.astype(np.float32) / AUDIO_INT16_SCALE
            else:
                float_samples = samples.astype(np.float32)

            flat_samples = float_samples.flatten()

            # Extend sample buffer
            self._sample_buffer.extend(flat_samples)
            self._total_samples_fed += len(flat_samples)

            # Update frame metadata
            self._current_frame.extend(flat_samples)
            while len(self._current_frame) >= self.FRAME_SIZE:
                frame_samples = np.array(self._current_frame[: self.FRAME_SIZE], dtype=np.float32)
                self._current_frame = self._current_frame[self.FRAME_SIZE :]

                # Compute frame RMS if not provided
                frame_rms = rms_db
                if frame_rms is None:
                    rms = np.sqrt(np.mean(frame_samples**2))
                    frame_rms = 20 * np.log10(max(rms, 1e-10))

                frame = AudioFrame(
                    samples=frame_samples,
                    timestamp=time.time(),
                    frame_index=self._frame_index,
                    rms_db=frame_rms,
                    has_speech=has_speech,
                )
                self._frame_metadata.append(frame)
                self._frame_index += 1

            # Handle pending post-event capture
            if self._pending_capture is not None:
                self._post_buffer.append(flat_samples.copy())
                self._pending_samples_remaining -= len(flat_samples)

                if self._pending_samples_remaining <= 0:
                    self._finalize_capture()

    def capture_trigger_event(
        self,
        correlation_id: str | None = None,
        score: float | None = None,
        threshold: float | None = None,
        accepted: bool | None = None,
        rejection_reason: str | None = None,
        decision_trace: WakeDecisionTrace | None = None,
        event_type: str = "trigger",
    ) -> CapturedAudio | None:
        """
        Capture audio around a wake detection event.

        Args:
            correlation_id: Correlation ID for the event
            score: Wake detection score
            threshold: Threshold that was used
            accepted: Whether detection was accepted
            rejection_reason: Why rejected (if applicable)
            decision_trace: Full decision trace object
            event_type: Event type ("trigger", "false_positive", etc.)

        Returns:
            CapturedAudio object or None if capture pending/failed
        """
        if not self._is_enabled():
            return None

        with self._lock:
            now = time.time()

            # Get pre-event audio
            pre_audio = np.array(list(self._sample_buffer)[-self._pre_samples :], dtype=np.float32)

            # Pad if buffer doesn't have enough samples
            if len(pre_audio) < self._pre_samples:
                padding = np.zeros(self._pre_samples - len(pre_audio), dtype=np.float32)
                pre_audio = np.concatenate([padding, pre_audio])

            # Generate capture ID
            capture_id = f"{int(now * 1000)}_{correlation_id or 'none'}"

            # Serialize decision trace
            trace_json = None
            if decision_trace is not None:
                try:
                    trace_json = json.dumps(asdict(decision_trace), default=str)
                except Exception as e:
                    logger.debug("Failed to serialize decision trace: %s", e)

            # Get diagnostics snapshot
            diagnostics = DiagnosticsSnapshot.from_collectors()

            # Set up pending capture
            self._pending_capture = {
                "capture_id": capture_id,
                "correlation_id": correlation_id,
                "capture_time": now,
                "event_type": event_type,
                "pre_audio": pre_audio,
                "wake_score": score,
                "threshold_used": threshold,
                "decision_accepted": accepted,
                "rejection_reason": rejection_reason,
                "decision_trace_json": trace_json,
                "diagnostics": diagnostics,
            }
            self._pending_samples_remaining = self._post_samples
            self._post_buffer = []

            # If no post-capture needed, finalize immediately
            if self._post_samples == 0:
                return self._finalize_capture()

            return None  # Will be available later via get_recent_captures

    def capture_missed_detection(
        self,
        expected_score: float | None = None,
    ) -> CapturedAudio | None:
        """Capture audio when user said wake word but it wasn't detected."""
        return self.capture_trigger_event(
            event_type="missed",
            score=expected_score,
            accepted=False,
            rejection_reason="user_reported_missed",
        )

    def capture_false_positive(
        self,
        correlation_id: str | None = None,
        score: float | None = None,
    ) -> CapturedAudio | None:
        """Capture audio when detection was a false positive."""
        return self.capture_trigger_event(
            correlation_id=correlation_id,
            score=score,
            event_type="false_positive",
            accepted=True,  # It was accepted but shouldn't have been
            rejection_reason="user_reported_false_positive",
        )

    def _finalize_capture(self) -> CapturedAudio:
        """Finalize pending capture and save to disk."""
        if self._pending_capture is None:
            raise RuntimeError("No pending capture to finalize")

        capture_data = self._pending_capture
        self._pending_capture = None

        # Combine pre and post audio
        if self._post_buffer:
            post_audio = np.concatenate(self._post_buffer)[: self._post_samples]
        else:
            post_audio = np.zeros(self._post_samples, dtype=np.float32)

        full_audio = np.concatenate([capture_data["pre_audio"], post_audio])
        self._post_buffer = []

        # Normalize and clip
        full_audio = np.clip(full_audio, -1.0, 1.0)

        # Create capture object
        capture = CapturedAudio(
            capture_id=capture_data["capture_id"],
            correlation_id=capture_data["correlation_id"],
            capture_time=capture_data["capture_time"],
            event_type=capture_data["event_type"],
            audio_samples=full_audio,
            sample_rate=self.SAMPLE_RATE,
            duration_seconds=len(full_audio) / self.SAMPLE_RATE,
            wake_score=capture_data["wake_score"],
            threshold_used=capture_data["threshold_used"],
            decision_accepted=capture_data["decision_accepted"],
            rejection_reason=capture_data["rejection_reason"],
            decision_trace_json=capture_data["decision_trace_json"],
            diagnostics=capture_data["diagnostics"],
        )

        # Check for duplicates
        audio_hash = capture.get_audio_hash()
        if audio_hash in self._seen_hashes:
            self._duplicate_captures_skipped += 1
            logger.debug("Skipping duplicate capture: %s", audio_hash)
            # Still return it, just don't save
            return capture

        self._seen_hashes.add(audio_hash)

        # Save to disk
        self._save_capture(capture)

        # Add to recent captures
        self._recent_captures.append(capture)
        self._total_captures += 1

        logger.info(
            "Wake audio captured: id=%s, type=%s, score=%s",
            capture.capture_id,
            capture.event_type,
            capture.wake_score,
        )

        return capture

    def _save_capture(self, capture: CapturedAudio) -> None:
        """Save capture to disk."""
        if not SOUNDFILE_AVAILABLE:
            return

        # Generate filename
        ts = int(capture.capture_time * 1000)
        score = capture.wake_score or 0
        event_type = capture.event_type
        filename = f"{event_type}_{ts}_{score:.3f}.wav"
        audio_path = self._output_dir / filename

        # Save audio
        try:
            sf.write(str(audio_path), capture.audio_samples, capture.sample_rate)
            capture.audio_path = audio_path
        except Exception as e:
            logger.error("Failed to save audio capture: %s", e)
            return

        # Save metadata sidecar
        meta_path = audio_path.with_suffix(".json")
        try:
            with open(meta_path, "w") as f:
                json.dump(capture.to_metadata_dict(), f, indent=2)
            capture.metadata_path = meta_path
        except Exception as e:
            logger.debug("Failed to save capture metadata: %s", e)

    def get_recent_captures(
        self,
        event_type: str | None = None,
        limit: int = 10,
    ) -> list[CapturedAudio]:
        """Get recent captures, optionally filtered by type."""
        with self._lock:
            captures = list(self._recent_captures)

        if event_type:
            captures = [c for c in captures if c.event_type == event_type]

        return captures[-limit:]

    def get_capture_by_id(self, capture_id: str) -> CapturedAudio | None:
        """Get a specific capture by ID."""
        with self._lock:
            for capture in self._recent_captures:
                if capture.capture_id == capture_id:
                    return capture
        return None

    def get_buffer_stats(self) -> dict[str, Any]:
        """Get buffer statistics."""
        with self._lock:
            buffer_fill = len(self._sample_buffer) / self._buffer_samples * 100
            return {
                "buffer_fill_pct": buffer_fill,
                "buffer_duration_seconds": len(self._sample_buffer) / self.SAMPLE_RATE,
                "total_samples_fed": self._total_samples_fed,
                "total_captures": self._total_captures,
                "recent_captures_count": len(self._recent_captures),
                "duplicate_captures_skipped": self._duplicate_captures_skipped,
                "frames_tracked": len(self._frame_metadata),
                "output_dir": str(self._output_dir),
            }

    def cleanup_old_captures(self, max_age_hours: float = 24.0) -> int:
        """Delete captures older than max_age_hours."""
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0

        for wav_file in self._output_dir.glob("*.wav"):
            try:
                if wav_file.stat().st_mtime < cutoff:
                    wav_file.unlink()
                    meta_file = wav_file.with_suffix(".json")
                    if meta_file.exists():
                        meta_file.unlink()
                    deleted += 1
            except OSError as e:
                logger.debug("Failed to delete %s: %s", wav_file, e)

        if deleted > 0:
            logger.info("Cleaned up %d old wake captures", deleted)

        return deleted


# =============================================================================
# Replay Pipeline
# =============================================================================


class WakeReplayPipeline:
    """
    Re-run wake detection on captured audio for debugging.

    Allows deterministic replay of audio through the detection pipeline
    to verify behavior and debug issues.
    """

    def __init__(
        self,
        detector_factory: Callable[[], Any] | None = None,
    ):
        """
        Initialize replay pipeline.

        Args:
            detector_factory: Optional factory to create detector instance.
                            If not provided, uses default ViolaWakeListener.
        """
        self._detector_factory = detector_factory
        self._detector: Any = None
        self._lock = threading.Lock()

    def _get_detector(self) -> Any:
        """Get or create detector instance."""
        if self._detector is not None:
            return self._detector

        if self._detector_factory:
            self._detector = self._detector_factory()
        else:
            # Try to import default detector
            try:
                from voice.wake_detector.violawake_listener import ViolaWakeListener

                self._detector = ViolaWakeListener()
            except ImportError:
                logger.warning("ViolaWakeListener not available for replay")
                return None

        return self._detector

    def replay_capture(
        self,
        capture: CapturedAudio,
        frame_callback: Callable[[int, float], None] | None = None,
    ) -> ReplayResult:
        """
        Replay captured audio through wake detection.

        Args:
            capture: CapturedAudio to replay
            frame_callback: Optional callback(frame_index, score) for each frame

        Returns:
            ReplayResult with detection results
        """
        with self._lock:
            detector = self._get_detector()
            if detector is None:
                return ReplayResult(
                    capture_id=capture.capture_id,
                    replay_time=time.time(),
                    detected=False,
                    max_score=0.0,
                    all_scores=[],
                    detection_timestamps=[],
                    original_score=capture.wake_score,
                    score_delta=None,
                )

            replay_start = time.time()
            all_scores: list[float] = []
            detection_timestamps: list[float] = []
            frame_results: list[dict[str, Any]] = []

            # Process audio in frames
            frame_size = 512  # Standard frame size
            audio = capture.audio_samples
            num_frames = len(audio) // frame_size

            for i in range(num_frames):
                frame_start = i * frame_size
                frame_end = frame_start + frame_size
                frame = audio[frame_start:frame_end]

                # Get score from detector
                score = 0.0
                try:
                    # Different detectors have different interfaces
                    if hasattr(detector, "process_frame"):
                        result = detector.process_frame(frame)
                        score = result.get("score", 0.0) if isinstance(result, dict) else 0.0
                    elif hasattr(detector, "get_score"):
                        score = detector.get_score(frame)
                    elif hasattr(detector, "detect"):
                        result = detector.detect(frame)
                        score = result.get("score", 0.0) if isinstance(result, dict) else 0.0
                except Exception as e:
                    logger.debug("Frame processing error: %s", e)

                all_scores.append(score)

                # Track detections
                threshold = capture.threshold_used or 0.5
                if score >= threshold:
                    frame_time = frame_start / capture.sample_rate
                    detection_timestamps.append(frame_time)

                frame_results.append(
                    {
                        "frame_index": i,
                        "score": score,
                        "above_threshold": score >= threshold,
                    }
                )

                if frame_callback:
                    frame_callback(i, score)

            # Build result
            max_score = max(all_scores) if all_scores else 0.0
            detected = len(detection_timestamps) > 0
            score_delta = None
            if capture.wake_score is not None:
                score_delta = max_score - capture.wake_score

            result = ReplayResult(
                capture_id=capture.capture_id,
                replay_time=replay_start,
                detected=detected,
                max_score=max_score,
                all_scores=all_scores,
                detection_timestamps=detection_timestamps,
                original_score=capture.wake_score,
                score_delta=score_delta,
                frame_results=frame_results,
            )

            # Store result in capture
            capture.replay_results.append(
                {
                    "replay_time": replay_start,
                    "detected": detected,
                    "max_score": max_score,
                    "score_delta": score_delta,
                }
            )

            logger.info(
                "Replay complete: capture=%s, detected=%s, max_score=%.3f, delta=%.3f",
                capture.capture_id,
                detected,
                max_score,
                score_delta or 0.0,
            )

            return result

    def replay_from_file(self, wav_path: Path) -> ReplayResult | None:
        """
        Replay audio from a WAV file.

        Args:
            wav_path: Path to WAV file

        Returns:
            ReplayResult or None if file can't be loaded
        """
        if not SOUNDFILE_AVAILABLE:
            logger.warning("soundfile not available - cannot load WAV")
            return None

        try:
            audio, sample_rate = sf.read(str(wav_path))
            audio = audio.astype(np.float32)
        except Exception as e:
            logger.error("Failed to load WAV file %s: %s", wav_path, e)
            return None

        # Create synthetic capture
        capture = CapturedAudio(
            capture_id=wav_path.stem,
            correlation_id=None,
            capture_time=time.time(),
            event_type="file_replay",
            audio_samples=audio,
            sample_rate=sample_rate,
            duration_seconds=len(audio) / sample_rate,
        )

        return self.replay_capture(capture)

    def batch_replay(
        self,
        captures: list[CapturedAudio],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[ReplayResult]:
        """
        Replay multiple captures in batch.

        Args:
            captures: List of captures to replay
            progress_callback: Optional callback(completed, total)

        Returns:
            List of ReplayResult objects
        """
        results = []
        total = len(captures)

        for i, capture in enumerate(captures):
            result = self.replay_capture(capture)
            results.append(result)

            if progress_callback:
                progress_callback(i + 1, total)

        return results


# =============================================================================
# Training Data Audit
# =============================================================================


@dataclass
class TrainingDataAudit:
    """Audit results for training data quality."""

    total_samples: int = 0
    positive_samples: int = 0
    negative_samples: int = 0
    ambiguous_samples: int = 0

    # Quality metrics
    samples_with_clipping: int = 0
    samples_with_noise: int = 0
    samples_too_quiet: int = 0
    samples_during_playback: int = 0

    # Score distribution
    score_percentiles: dict[str, float] = field(default_factory=dict)

    # Issues found
    issues: list[str] = field(default_factory=list)


def audit_training_data(captures_dir: Path) -> TrainingDataAudit:
    """
    Audit training data captures for quality issues.

    Args:
        captures_dir: Directory containing capture WAV/JSON files

    Returns:
        TrainingDataAudit with findings
    """
    audit = TrainingDataAudit()

    if not captures_dir.exists():
        audit.issues.append(f"Directory does not exist: {captures_dir}")
        return audit

    metadata_files = list(captures_dir.glob("*.json"))
    scores: list[float] = []

    for meta_path in metadata_files:
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            continue

        audit.total_samples += 1

        # Categorize by event type
        event_type = meta.get("event_type", "unknown")
        if event_type in ("trigger", "accepted"):
            audit.positive_samples += 1
        elif event_type in ("missed", "false_positive"):
            audit.negative_samples += 1
        else:
            audit.ambiguous_samples += 1

        # Check diagnostics
        diag = meta.get("diagnostics", {})

        if diag.get("clipping_ratio", 0) > 0.01:
            audit.samples_with_clipping += 1

        noise_floor = diag.get("noise_floor_db", -60)
        if noise_floor > -30:
            audit.samples_with_noise += 1
        if noise_floor < -50:
            audit.samples_too_quiet += 1

        if diag.get("is_playing", False):
            audit.samples_during_playback += 1

        # Collect scores
        score = meta.get("wake_score")
        if score is not None:
            scores.append(score)

    # Compute score percentiles
    if scores:
        sorted_scores = sorted(scores)
        n = len(sorted_scores)
        audit.score_percentiles = {
            "p10": sorted_scores[int(n * 0.1)] if n > 10 else 0,
            "p25": sorted_scores[int(n * 0.25)] if n > 4 else 0,
            "p50": sorted_scores[int(n * 0.5)] if n > 2 else 0,
            "p75": sorted_scores[int(n * 0.75)] if n > 4 else 0,
            "p90": sorted_scores[int(n * 0.9)] if n > 10 else 0,
        }

    # Generate issue warnings
    if audit.samples_with_clipping > audit.total_samples * 0.1:
        audit.issues.append(f"High clipping rate: {audit.samples_with_clipping}/{audit.total_samples}")

    if audit.samples_during_playback > audit.total_samples * 0.5:
        audit.issues.append(f"Many samples during playback: {audit.samples_during_playback}/{audit.total_samples}")

    if audit.ambiguous_samples > audit.total_samples * 0.2:
        audit.issues.append(f"Many ambiguous samples: {audit.ambiguous_samples}/{audit.total_samples}")

    return audit


# =============================================================================
# Singleton Access
# =============================================================================

_buffer: WakeAudioBuffer | None = None
_buffer_lock = threading.Lock()


def get_wake_audio_buffer(output_dir: Path | None = None) -> WakeAudioBuffer:
    """Get global wake audio buffer singleton."""
    global _buffer
    with _buffer_lock:
        if _buffer is None:
            _buffer = WakeAudioBuffer(output_dir=output_dir)
        return _buffer


def reset_wake_audio_buffer() -> None:
    """Reset global wake audio buffer (for testing)."""
    global _buffer
    with _buffer_lock:
        _buffer = None


__all__ = [
    "AudioFrame",
    "CapturedAudio",
    "DiagnosticsSnapshot",
    "ReplayResult",
    "TrainingDataAudit",
    "WakeAudioBuffer",
    "WakeReplayPipeline",
    "audit_training_data",
    "get_wake_audio_buffer",
    "is_wake_audio_logging_enabled",
    "reset_wake_audio_buffer",
]
