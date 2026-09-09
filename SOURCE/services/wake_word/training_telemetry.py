"""
Training Telemetry Service
==========================

Handles collection and upload of training samples to improve the global model.
Respects user opt-in preferences and provides developer controls.

PRIVACY NOTES:
- No PII collected (no user ID, device ID, IP stored on server)
- Audio samples are stripped of metadata before upload
- User must explicitly opt-in
- Local queue allows offline operation
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import numpy as np

from core.constants import SAMPLE_RATE_16K, TIMEOUT_LONG, TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from core.platform import get_data_dir

if TYPE_CHECKING:
    from numpy.typing import NDArray

logger = get_logger(__name__)


class QueuedSampleDict(TypedDict):
    """TypedDict for serialized QueuedSample."""

    id: str
    audio_path: str
    wake_word: str
    feedback: str
    created_at: str
    uploaded: bool
    upload_attempts: int
    last_attempt: str


class QueueStatsDict(TypedDict):
    """TypedDict for queue statistics."""

    pending_count: int
    uploaded_count: int
    total_count: int
    total_size_mb: float
    upload_endpoint_configured: bool
    opted_in: bool


# Configuration
DEFAULT_QUEUE_DIR = get_data_dir() / "training_queue"
_LEGACY_QUEUE_DIR = Path.home().joinpath(".viola", "training_queue")
MAX_QUEUE_SIZE_MB = 100  # Maximum queue size before pruning old samples
RETENTION_DAYS = 30  # Delete samples older than this after upload attempt
_WAKE_DATA_UPLOAD_CONTROL = "wake_data_upload"


def _wake_data_upload_allowed(action: str) -> bool:
    try:
        from services.operator_controls import require_enabled

        decision = require_enabled(_WAKE_DATA_UPLOAD_CONTROL, action=action)
        if not decision.allowed:
            logger.debug("Wake training telemetry blocked: %s", decision.reason)
            return False
        return True
    except Exception:
        logger.exception("Wake training telemetry blocked: operator control check failed")
        return False


async def _wake_data_upload_allowed_async(action: str) -> bool:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(_WAKE_DATA_UPLOAD_CONTROL, action=action)
        if not decision.allowed:
            logger.debug("Wake training telemetry blocked: %s", decision.reason)
            return False
        return True
    except Exception:
        logger.exception("Wake training telemetry blocked: operator control check failed")
        return False


@dataclass
class QueuedSample:
    """A sample queued for upload."""

    id: str
    audio_path: str
    wake_word: str = "viola"
    feedback: str = "positive"  # positive, false_positive, missed
    created_at: str = ""
    uploaded: bool = False
    upload_attempts: int = 0
    last_attempt: str = ""

    def to_dict(self) -> QueuedSampleDict:
        return {
            "id": self.id,
            "audio_path": self.audio_path,
            "wake_word": self.wake_word,
            "feedback": self.feedback,
            "created_at": self.created_at,
            "uploaded": self.uploaded,
            "upload_attempts": self.upload_attempts,
            "last_attempt": self.last_attempt,
        }

    @classmethod
    def from_dict(cls, data: QueuedSampleDict) -> QueuedSample:
        return cls(
            id=data["id"],
            audio_path=data["audio_path"],
            wake_word=data.get("wake_word", "viola"),
            feedback=data.get("feedback", "positive"),
            created_at=data.get("created_at", ""),
            uploaded=data.get("uploaded", False),
            upload_attempts=data.get("upload_attempts", 0),
            last_attempt=data.get("last_attempt", ""),
        )


@dataclass
class UploadResult:
    """Result of upload attempt."""

    success: bool
    uploaded_count: int = 0
    failed_count: int = 0
    error: str = ""


class TrainingTelemetryService:
    """
    Service for collecting and uploading training samples.

    Samples are queued locally and uploaded when:
    1. User has opted in
    2. Network is available
    3. Upload endpoint is configured

    For MVP, samples are stored locally. You need to:
    1. Set up an S3 bucket or similar storage
    2. Configure UPLOAD_ENDPOINT environment variable
    3. Periodically download samples and retrain
    """

    QUEUE_FILE = "queue.json"
    SAMPLES_DIR = "samples"

    @staticmethod
    def _migrate_legacy_queue_dir(queue_dir: Path) -> None:
        if queue_dir.exists() or not _LEGACY_QUEUE_DIR.exists():
            return
        try:
            shutil.copytree(_LEGACY_QUEUE_DIR, queue_dir)
            logger.info(
                "Migrated legacy wake training queue from %s to %s",
                _LEGACY_QUEUE_DIR,
                queue_dir,
            )
        except OSError as exc:
            logger.warning(
                "Could not migrate legacy wake training queue from %s to %s: %s",
                _LEGACY_QUEUE_DIR,
                queue_dir,
                exc,
            )

    def __init__(
        self,
        queue_dir: Path | None = None,
        upload_endpoint: str | None = None,
    ):
        """
        Initialize telemetry service.

        Args:
            queue_dir: Directory for sample queue
            upload_endpoint: URL for uploading samples (None = local only)
        """
        self.queue_dir = Path(queue_dir or DEFAULT_QUEUE_DIR)
        if queue_dir is None:
            self._migrate_legacy_queue_dir(self.queue_dir)
        from config.settings import settings as app_settings

        self.upload_endpoint = upload_endpoint or app_settings.training_upload_endpoint

        # Ensure directories exist
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        (self.queue_dir / self.SAMPLES_DIR).mkdir(exist_ok=True)

        # Load queue
        self._queue: list[QueuedSample] = []
        self._load_queue()

    def _queue_path(self) -> Path:
        return self.queue_dir / self.QUEUE_FILE

    def _samples_path(self) -> Path:
        return self.queue_dir / self.SAMPLES_DIR

    def _load_queue(self) -> None:
        """Load queue from disk."""
        try:
            if self._queue_path().exists():
                with open(self._queue_path()) as f:
                    data = json.load(f)
                self._queue = [QueuedSample.from_dict(s) for s in data.get("samples", [])]
                logger.debug("Loaded %s samples from queue", len(self._queue))
        except Exception as e:
            logger.error("Failed to load queue: %s", e)
            self._queue = []

    def _save_queue(self) -> None:
        """Save queue to disk."""
        try:
            data = {
                "samples": [s.to_dict() for s in self._queue],
                "updated_at": datetime.utcnow().isoformat(),
            }
            with open(self._queue_path(), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save queue: %s", e)

    def is_opted_in(self) -> bool:
        """Check if user has opted in to data sharing."""
        try:
            from ui.settings_manager import get_settings_manager

            settings = get_settings_manager()
            explicit = settings.get("wake_word_training_opt_in", None)
            if explicit is None:
                explicit = settings.get("wake_data_contribute", False)
            return bool(explicit)
        except Exception as e:
            # Default closed if settings are unavailable.
            logger.debug(
                "Failed to check training opt-in setting, defaulting to False: %s",
                e,
                exc_info=True,
            )
            return False

    def set_opt_in(self, value: bool) -> None:
        """Update data sharing preference."""
        try:
            from ui.settings_manager import get_settings_manager

            settings = get_settings_manager()
            settings.set("wake_word_training_opt_in", value)
            settings.set("wake_data_contribute", value)
            logger.info("Training data opt-in set to: %s", value)
        except Exception as e:
            logger.error("Failed to update opt-in setting: %s", e)

    def queue_sample(
        self,
        audio: NDArray[np.int16],
        sample_rate: int = SAMPLE_RATE_16K,
        wake_word: str = "viola",
        feedback: str = "positive",
    ) -> bool:
        """
        Queue a sample for upload.

        Args:
            audio: Audio samples (int16)
            sample_rate: Sample rate
            wake_word: Wake word this sample represents
            feedback: Type of sample (positive, false_positive, missed)

        Returns:
            True if queued successfully
        """
        if not _wake_data_upload_allowed("wake_training_sample_queue"):
            return False

        if not self.is_opted_in():
            logger.debug("Sample not queued - user not opted in")
            return False

        try:
            # Generate unique ID
            sample_id = hashlib.sha256(f"{time.time()}_{np.random.rand()}".encode()).hexdigest()[:16]

            # Save audio file
            audio_filename = f"{sample_id}.wav"
            audio_path = self._samples_path() / audio_filename

            with wave.open(str(audio_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                wf.writeframes(audio.tobytes())

            # Create queue entry
            sample = QueuedSample(
                id=sample_id,
                audio_path=audio_filename,
                wake_word=wake_word,
                feedback=feedback,
                created_at=datetime.utcnow().isoformat(),
            )

            self._queue.append(sample)
            self._save_queue()

            logger.info("Queued sample %s for upload", sample_id)

            # Check queue size and prune if needed
            self._check_queue_size()

            return True

        except Exception as e:
            logger.error("Failed to queue sample: %s", e)
            return False

    def _check_queue_size(self) -> None:
        """Prune old samples if queue is too large."""
        try:
            total_size = sum(
                (self._samples_path() / s.audio_path).stat().st_size
                for s in self._queue
                if (self._samples_path() / s.audio_path).exists()
            )

            max_size_bytes = MAX_QUEUE_SIZE_MB * 1024 * 1024

            if total_size > max_size_bytes:
                # Remove oldest samples until under limit
                sorted_queue = sorted(self._queue, key=lambda s: s.created_at)
                while total_size > max_size_bytes and sorted_queue:
                    oldest = sorted_queue.pop(0)
                    audio_path = self._samples_path() / oldest.audio_path
                    if audio_path.exists():
                        size = audio_path.stat().st_size
                        audio_path.unlink()
                        total_size -= size
                    self._queue.remove(oldest)

                self._save_queue()
                logger.info("Pruned queue to %s samples", len(self._queue))

        except Exception as e:
            logger.warning("Queue size check failed: %s", e)

    def get_pending_count(self) -> int:
        """Get number of samples pending upload."""
        return len([s for s in self._queue if not s.uploaded])

    def get_queue_stats(self) -> QueueStatsDict:
        """Get queue statistics."""
        pending = [s for s in self._queue if not s.uploaded]
        uploaded = [s for s in self._queue if s.uploaded]

        total_size = 0
        try:
            for s in self._queue:
                audio_path = self._samples_path() / s.audio_path
                if audio_path.exists():
                    total_size += audio_path.stat().st_size
        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            pass

        return {
            "pending_count": len(pending),
            "uploaded_count": len(uploaded),
            "total_count": len(self._queue),
            "total_size_mb": total_size / (1024 * 1024),
            "upload_endpoint_configured": bool(self.upload_endpoint),
            "opted_in": self.is_opted_in(),
        }

    async def upload_pending(self) -> UploadResult:
        """
        Upload pending samples to server.

        Returns:
            UploadResult with counts and status
        """
        if not self.is_opted_in():
            return UploadResult(success=True, error="User not opted in")

        if not await _wake_data_upload_allowed_async("wake_training_sample_upload"):
            return UploadResult(success=True, error="Wake data upload disabled by owner safety control")

        if not self.upload_endpoint:
            # No endpoint configured - this is expected for MVP
            return UploadResult(
                success=True,
                error="No upload endpoint configured (samples stored locally)",
            )

        pending = [s for s in self._queue if not s.uploaded]
        if not pending:
            return UploadResult(success=True)

        uploaded = 0
        failed = 0

        try:
            import httpx

            async with httpx.AsyncClient(timeout=TIMEOUT_VERY_LONG) as client:
                for sample in pending:
                    try:
                        audio_path = self._samples_path() / sample.audio_path
                        if not audio_path.exists():
                            sample.uploaded = True  # Mark as uploaded to remove from queue
                            continue

                        # Upload file
                        with open(audio_path, "rb") as f:
                            files = {"audio": (sample.audio_path, f, "audio/wav")}
                            data = {
                                "wake_word": sample.wake_word,
                                "feedback": sample.feedback,
                            }

                            response = await client.post(
                                self.upload_endpoint,
                                files=files,
                                data=data,
                            )
                            response.raise_for_status()

                        sample.uploaded = True
                        sample.upload_attempts += 1
                        sample.last_attempt = datetime.utcnow().isoformat()
                        uploaded += 1

                    except Exception as e:
                        sample.upload_attempts += 1
                        sample.last_attempt = datetime.utcnow().isoformat()
                        failed += 1
                        logger.warning("Failed to upload sample %s: %s", sample.id, e)

                self._save_queue()

        except ImportError:
            logger.debug("httpx not installed for telemetry upload")
            return UploadResult(
                success=False,
                error="httpx not installed. Install with: pip install httpx",
            )
        except Exception as e:
            logger.warning("Telemetry upload failed: %s", e)
            return UploadResult(success=False, error=str(e))

        return UploadResult(
            success=failed == 0,
            uploaded_count=uploaded,
            failed_count=failed,
        )

    def export_samples(self, output_dir: Path) -> int:
        """
        Export all samples to a directory for manual processing.

        This is your developer workflow:
        1. Call export_samples() to get all collected samples
        2. Add them to your training dataset
        3. Retrain the model
        4. Ship updated model in app update

        Args:
            output_dir: Directory to export samples to

        Returns:
            Number of samples exported
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        exported = 0

        for sample in self._queue:
            src = self._samples_path() / sample.audio_path
            if src.exists():
                dst = output_dir / sample.audio_path
                shutil.copy2(src, dst)
                exported += 1

        # Export metadata
        metadata = {
            "exported_at": datetime.utcnow().isoformat(),
            "samples": [s.to_dict() for s in self._queue],
        }
        with open(output_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)

        logger.info("Exported %s samples to %s", exported, output_dir)
        return exported

    def clear_uploaded(self) -> int:
        """
        Clear samples that have been uploaded.

        Returns:
            Number of samples cleared
        """
        to_remove = [s for s in self._queue if s.uploaded]

        for sample in to_remove:
            audio_path = self._samples_path() / sample.audio_path
            if audio_path.exists():
                try:
                    audio_path.unlink()
                except Exception as e:
                    logger.debug("Operation failed: %s", e, exc_info=True)
                    pass
            self._queue.remove(sample)

        self._save_queue()
        logger.info("Cleared %s uploaded samples", len(to_remove))
        return len(to_remove)


class TelemetryUploadScheduler:
    """
    Background scheduler for telemetry uploads.

    Runs in a background thread and periodically attempts to upload
    pending samples when network is available.
    """

    def __init__(
        self,
        telemetry_service: TrainingTelemetryService,
        interval_minutes: int = 30,
    ):
        self.telemetry = telemetry_service
        self.interval_seconds = interval_minutes * 60
        self._stop_event = _threading.Event()
        self._thread: _threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> bool:
        """Start the background upload scheduler."""
        if self._thread is not None and self._thread.is_alive():
            return False

        self._stop_event.clear()
        self._thread = _threading.Thread(
            target=self._run,
            daemon=True,
            name="telemetry-upload-scheduler",
        )
        self._thread.start()
        logger.info("Telemetry upload scheduler started")
        return True

    def stop(self) -> None:
        """Stop the scheduler."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_LONG)
            self._thread = None
        logger.info("Telemetry upload scheduler stopped")

    def _run(self) -> None:
        """Background thread main loop."""
        # Create event loop for this thread
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            while not self._stop_event.wait(self.interval_seconds):
                if not self.telemetry.is_opted_in():
                    continue

                pending = self.telemetry.get_pending_count()
                if pending == 0:
                    continue

                logger.debug("Attempting to upload %s training samples...", pending)

                try:
                    result = self._loop.run_until_complete(self.telemetry.upload_pending())

                    if result.success and result.uploaded_count > 0:
                        logger.info("Uploaded %s training samples", result.uploaded_count)
                        self.telemetry.clear_uploaded()
                    elif result.error:
                        logger.debug("Upload: %s", result.error)

                except Exception as e:
                    logger.warning("Telemetry upload failed: %s", e)
        finally:
            self._loop.close()
            self._loop = None

    def trigger_upload_now(self) -> None:
        """Trigger an immediate upload attempt (non-blocking)."""
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self.telemetry.upload_pending(),
                self._loop,
            )


# Background scheduler singleton
_upload_scheduler: TelemetryUploadScheduler | None = None


def start_telemetry_scheduler(interval_minutes: int = 30) -> TelemetryUploadScheduler:
    """Start the background telemetry upload scheduler."""
    global _upload_scheduler

    if _upload_scheduler is None:
        telemetry = get_training_telemetry()
        _upload_scheduler = TelemetryUploadScheduler(telemetry, interval_minutes)

    _upload_scheduler.start()
    return _upload_scheduler


def stop_telemetry_scheduler() -> None:
    """Stop the background telemetry upload scheduler."""
    global _upload_scheduler

    if _upload_scheduler is not None:
        _upload_scheduler.stop()
        _upload_scheduler = None


# Thread-safe singleton
import threading as _threading

_telemetry_service: TrainingTelemetryService | None = None
_telemetry_service_lock = _threading.Lock()


def get_training_telemetry() -> TrainingTelemetryService:
    """Get the singleton telemetry service (thread-safe)."""
    global _telemetry_service
    if _telemetry_service is None:
        with _telemetry_service_lock:
            if _telemetry_service is None:
                _telemetry_service = TrainingTelemetryService()
    return _telemetry_service
