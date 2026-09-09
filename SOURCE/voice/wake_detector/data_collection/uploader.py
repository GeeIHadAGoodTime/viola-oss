"""Upload queue for anonymized wake word clips.

Scans DB for anonymized, un-uploaded clips when the system is idle,
then copies them to a local outbox directory. Future HTTP backend
can be plugged in via the UploadBackend protocol.

Features:
- Session batch ID (random UUID generated per process, never persisted to disk)
- Daily upload cap (configurable)
- Structured outbox with manifest.json per batch
- Flat outbox migration for legacy installs
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from config.settings import settings
from core.logging_config import get_logger

from .database import get_data_collection_db

logger = get_logger(__name__)

_WAKE_DATA_UPLOAD_CONTROL = "wake_data_upload"


def get_settings_manager():
    """Lazy import of SettingsManager to avoid hard ui dependency."""
    from ui.settings_manager import get_settings_manager as _get_sm

    return _get_sm()


def _is_voice_contribution_consented() -> bool:
    """Return True only if the user opted into contributing wake-word voice clips.

    SEC-046: this is the *voice-contribution* consent (``wake_data_contribute``),
    which is distinct from error/crash reporting consent. Wake-word clips are
    biometric voiceprints; they may only leave the device on this explicit
    opt-in. SettingsManager is the runtime source of truth; ``settings`` is the
    fallback. Fails closed (returns False) if neither can be read.
    """
    try:
        sm = get_settings_manager()
        return bool(sm.get("wake_data_contribute", False))
    except (ImportError, AttributeError, OSError, RuntimeError, ValueError):
        try:
            return bool(settings.wake_data_contribute)
        except (AttributeError, RuntimeError):
            return False


CHECK_INTERVAL_SEC = 300  # 5 minutes
BATCH_SIZE = 10
MAX_RETRIES = 3
IDLE_SINCE_TRIGGER_SEC = 60
MANIFEST_VERSION = 1


def _get_session_batch_id() -> str:
    """Return a per-session random batch ID.

    Generated once per process lifetime via ``uuid.uuid4()``.
    Never written to disk -- satisfies the privacy policy promise of
    "no device identifiers".
    """
    global _session_batch_id
    if _session_batch_id is None:
        _session_batch_id = uuid.uuid4().hex
        logger.info("Generated session batch ID for this process")
    return _session_batch_id


# Module-level session ID, reset on every process restart.
_session_batch_id: str | None = None


def reset_device_batch_id() -> str:
    """Reset the session batch ID and return the new value.

    Also removes the legacy persistent ID file if one exists from an
    older version.
    """
    global _session_batch_id
    # Clean up legacy persistent file (if present from previous versions)
    try:
        legacy_path = Path(settings.data_dir) / "wake_data_device_batch_id.txt"
        if legacy_path.exists():
            legacy_path.unlink()
            logger.info("Removed legacy persistent device batch ID file")
    except Exception:
        logger.exception("Failed to remove legacy device batch ID file")
    _session_batch_id = uuid.uuid4().hex
    logger.info("Session batch ID reset")
    return _session_batch_id


def migrate_flat_outbox(outbox_dir: Path) -> int:
    """Move legacy flat WAVs from outbox root into outbox/legacy_flat/.

    Returns the number of files migrated.
    """
    flat_wavs = list(outbox_dir.glob("*.wav"))
    if not flat_wavs:
        return 0

    legacy_dir = outbox_dir / "legacy_flat"
    legacy_dir.mkdir(parents=True, exist_ok=True)

    migrated = 0
    for wav in flat_wavs:
        try:
            shutil.move(str(wav), str(legacy_dir / wav.name))
            migrated += 1
        except Exception:
            logger.exception("Failed to migrate legacy WAV: %s", wav.name)

    if migrated > 0:
        logger.info("Migrated %d legacy flat WAVs to outbox/legacy_flat/", migrated)
    return migrated


@runtime_checkable
class UploadBackend(Protocol):
    """Protocol for upload backends."""

    def upload(self, path: Path) -> bool:
        """Upload a file. Returns True on success."""
        ...


class LocalOutboxBackend:
    """Copies anonymized clips to a structured local outbox directory."""

    def __init__(self) -> None:
        base = Path(settings.data_dir) / "wake_data" / "outbox"
        self._outbox_dir = base
        self._outbox_dir.mkdir(parents=True, exist_ok=True)
        migrate_flat_outbox(self._outbox_dir)

    @property
    def outbox_dir(self) -> Path:
        return self._outbox_dir

    def upload(self, path: Path) -> bool:
        """Legacy single-file upload (backward compatible)."""
        try:
            dest = self._outbox_dir / path.name
            shutil.copy2(str(path), str(dest))
            return True
        except Exception:
            logger.exception("Failed to copy to outbox: %s", path)
            return False

    def upload_batch(
        self,
        clips: list[dict],
        batch_id: str,
        data_dir: Path,
    ) -> list[int]:
        """Upload a batch of clips with manifest to outbox/{batch_id}/.

        Returns list of clip IDs successfully copied.
        """
        session_id = _get_session_batch_id()
        batch_dir = self._outbox_dir / batch_id
        batch_dir.mkdir(parents=True, exist_ok=True)

        manifest_clips = []
        copied_ids = []

        for clip in clips:
            clip_id = clip["id"]
            anon_path = data_dir / "anonymized" / ("%d.wav" % clip_id)
            if not anon_path.exists():
                logger.warning("Anonymized file not found for clip %d", clip_id)
                continue

            dest_name = "%d.wav" % clip_id
            try:
                shutil.copy2(str(anon_path), str(batch_dir / dest_name))
            except Exception:
                logger.exception("Failed to copy clip %d to batch dir", clip_id)
                continue

            manifest_clips.append(
                {
                    "filename": dest_name,
                    "classification": clip["classification"],
                    "classification_method": clip["classification_method"],
                    "confidence_score": clip["confidence_score"],
                    "duration_ms": clip["duration_ms"],
                    "content_hash": clip.get("content_hash", ""),
                    "captured_at": clip.get("created_at", ""),
                }
            )
            copied_ids.append(clip_id)

        if not copied_ids:
            # Clean up empty batch dir
            try:
                batch_dir.rmdir()
            except OSError:
                pass
            return []

        # Write manifest
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "batch_id": batch_id,
            "device_batch_id": session_id,
            "viola_version": getattr(settings, "app_version", "unknown"),
            "model_version": "viola_v2",
            "clip_count": len(copied_ids),
            "clips": manifest_clips,
        }

        manifest_path = batch_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

        return copied_ids


class HttpUploadBackend:
    """Uploads anonymized clips to a remote HTTP endpoint.

    Sends multipart/form-data POST requests with the WAV file.
    Falls back to LocalOutboxBackend on persistent failures.
    """

    def __init__(self, endpoint: str) -> None:
        self._endpoint = endpoint

    def upload(self, path: Path) -> bool:
        """Upload a single file via HTTP POST.

        SEC-046: voice clips are biometric data. Two gates, both required:

        1. Consent must be the *voice-contribution* opt-in
           (``wake_data_contribute``), not the unrelated error-reporting / Sentry
           consent. Conflating the two meant a user who opted into crash reports
           but never opted into voice contribution could still have voice clips
           transmitted. Fail closed if the opt-in can't be confirmed.
        2. The endpoint must be ``https://`` — never send biometric audio over
           plaintext http, even if a misconfiguration slipped a http URL past
           settings validation.
        """
        # Consent gate: the voice-contribution opt-in, not error reporting.
        # The helper is internally fail-closed (returns False on any read error),
        # so a False result here means "no confirmed opt-in" → do not upload.
        if not _is_voice_contribution_consented():
            logger.debug("HTTP upload skipped: voice-contribution consent not granted")
            return False

        # Transport gate: biometric audio must travel over TLS.
        if not self._endpoint.lower().startswith("https://"):
            logger.warning("HTTP upload refused: endpoint is not https (biometric voice clip)")
            return False

        try:
            import httpx

            with open(path, "rb") as f:
                files = {"file": (path.name, f, "audio/wav")}
                response = httpx.post(
                    self._endpoint,
                    files=files,
                    timeout=30.0,
                )
                if response.status_code in (200, 201):
                    return True
                logger.warning(
                    "Upload failed: HTTP %d for %s",
                    response.status_code,
                    path.name,
                )
                return False
        except Exception:
            logger.exception("HTTP upload failed for %s", path.name)
            return False


def _create_upload_backend() -> LocalOutboxBackend | HttpUploadBackend:
    """Create the appropriate upload backend based on config."""
    endpoint = getattr(settings, "wake_data_upload_endpoint", "")
    if endpoint:
        logger.info("Using HTTP upload backend: %s", endpoint)
        return HttpUploadBackend(endpoint)
    return LocalOutboxBackend()


class UploadQueue:
    """Background upload queue for anonymized clips."""

    def __init__(self, backend: UploadBackend | None = None) -> None:
        self._backend: UploadBackend | LocalOutboxBackend | HttpUploadBackend = backend or _create_upload_backend()
        self._timer: threading.Timer | None = None
        self._running = False
        self._retry_counts: dict[int, int] = {}

    def start(self) -> None:
        self._running = True
        self._schedule_check()
        logger.info("Upload queue started (interval=%ds)", CHECK_INTERVAL_SEC)

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_check(self) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(CHECK_INTERVAL_SEC, self._run_cycle)
        self._timer.daemon = True
        self._timer.start()

    def _is_idle(self) -> bool:
        """Check if the system is idle enough for uploading."""
        try:
            from services.operator_controls import require_enabled

            decision = require_enabled(_WAKE_DATA_UPLOAD_CONTROL, action="wake_data_upload")
            if not decision.allowed:
                logger.debug("Wake data upload skipped: %s", decision.reason)
                return False
        except Exception:
            logger.exception("Wake data upload skipped: operator control check failed")
            return False

        # Check if contribution is enabled (SettingsManager is runtime source of truth)
        try:
            sm = get_settings_manager()
            if not sm.get("wake_data_contribute", False):
                return False
        except Exception:
            if not settings.wake_data_contribute:
                return False

        # Check classifier for recent trigger
        try:
            from .classifier import get_trigger_classifier

            classifier = get_trigger_classifier()
            if time.time() - classifier.last_trigger_time < IDLE_SINCE_TRIGGER_SEC:
                return False
        except Exception:
            logger.debug("Trigger classifier unavailable for idle check")

        return True

    def _run_cycle(self) -> None:
        """Run one upload cycle: validate → anonymize → check cap → upload."""
        try:
            if not self._is_idle():
                logger.debug("Upload skipped: system not idle")
                self._schedule_check()
                return

            # Pre-upload validation
            try:
                from .validator import validate_pending

                validate_pending(limit=50)
            except Exception:
                logger.exception("Validation step failed")

            # Anonymize validated clips
            try:
                from .anonymizer import anonymize_pending

                anonymize_pending(limit=50)
            except Exception:
                logger.exception("Anonymization step failed")

            db = get_data_collection_db()

            # Daily cap check
            daily_max = settings.wake_data_max_daily_uploads
            uploads_today = db.count_uploads_today()
            if uploads_today >= daily_max:
                logger.info(
                    "Daily upload cap reached (%d/%d), skipping upload",
                    uploads_today,
                    daily_max,
                )
                # Mark remaining uploadable clips as capped
                remaining = db.get_clips_for_upload(BATCH_SIZE)
                for clip in remaining:
                    db.mark_upload_capped(clip["id"])
                self._schedule_check()
                return

            remaining_quota = daily_max - uploads_today
            batch_limit = min(BATCH_SIZE, remaining_quota)

            # Prioritize previously-capped clips (FIFO carry-over)
            clips = db.get_capped_clips(limit=batch_limit)
            if len(clips) < batch_limit:
                fresh = db.get_clips_for_upload(batch_limit - len(clips))
                clips.extend(fresh)

            if not clips:
                self._schedule_check()
                return

            batch_id = uuid.uuid4().hex[:16]
            data_dir = Path(settings.data_dir) / "wake_clips"

            # Try structured batch upload if backend supports it
            if isinstance(self._backend, LocalOutboxBackend):
                self._upload_batch_structured(db, clips, batch_id, data_dir)
            else:
                self._upload_batch_legacy(db, clips, batch_id, data_dir)

        except Exception:
            logger.exception("Upload cycle failed")
        finally:
            self._schedule_check()

    def _upload_batch_structured(
        self,
        db: object,
        clips: list[dict],
        batch_id: str,
        data_dir: Path,
    ) -> None:
        """Upload via structured batch with manifest."""
        assert isinstance(self._backend, LocalOutboxBackend)

        # Filter out dedup and retried clips
        valid_clips = []
        for clip in clips:
            clip_id = clip["id"]
            if clip["content_hash"] and db.hash_exists(clip["content_hash"]):  # type: ignore[union-attr]
                db.mark_uploaded(clip_id, "dedup_%s" % batch_id)  # type: ignore[union-attr]
                continue
            retries = self._retry_counts.get(clip_id, 0)
            if retries >= MAX_RETRIES:
                logger.warning("Clip %d exceeded max retries, skipping", clip_id)
                continue
            valid_clips.append(clip)

        if not valid_clips:
            return

        copied_ids = self._backend.upload_batch(valid_clips, batch_id, data_dir)
        for clip_id in copied_ids:
            db.mark_uploaded(clip_id, batch_id)  # type: ignore[union-attr]
            # Clear capped flag on successful upload
            self._retry_counts.pop(clip_id, None)

        if copied_ids:
            logger.info("Uploaded %d clips (batch=%s)", len(copied_ids), batch_id)

    def _upload_batch_legacy(
        self,
        db: object,
        clips: list[dict],
        batch_id: str,
        data_dir: Path,
    ) -> None:
        """Upload via legacy single-file backend."""
        uploaded = 0
        for clip in clips:
            clip_id = clip["id"]

            if clip["content_hash"] and db.hash_exists(clip["content_hash"]):  # type: ignore[union-attr]
                db.mark_uploaded(clip_id, "dedup_%s" % batch_id)  # type: ignore[union-attr]
                continue

            retries = self._retry_counts.get(clip_id, 0)
            if retries >= MAX_RETRIES:
                logger.warning("Clip %d exceeded max retries, skipping", clip_id)
                continue

            anon_path = data_dir / "anonymized" / ("%d.wav" % clip_id)
            if not anon_path.exists():
                logger.warning("Anonymized file not found for clip %d", clip_id)
                continue

            if self._backend.upload(anon_path):
                db.mark_uploaded(clip_id, batch_id)  # type: ignore[union-attr]
                self._retry_counts.pop(clip_id, None)
                uploaded += 1
            else:
                self._retry_counts[clip_id] = retries + 1

        if uploaded > 0:
            logger.info("Uploaded %d clips (batch=%s)", uploaded, batch_id)


_upload_queue: UploadQueue | None = None
_uq_lock = threading.Lock()


def get_upload_queue() -> UploadQueue:
    global _upload_queue
    with _uq_lock:
        if _upload_queue is None:
            _upload_queue = UploadQueue()
        return _upload_queue
