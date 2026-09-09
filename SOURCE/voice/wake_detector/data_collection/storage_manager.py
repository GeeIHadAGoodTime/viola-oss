"""Storage management and retention policies for wake word clips.

Handles pruning of old clips, near-miss cap enforcement, outbox
pruning, and total storage cap monitoring. Runs on an hourly
background timer.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from config.settings import settings
from core.logging_config import get_logger

from .database import get_data_collection_db

logger = get_logger(__name__)

PRUNE_INTERVAL_SEC = 3600  # 1 hour


def _get_dir_size_mb(directory: Path) -> float:
    """Calculate directory size in MB."""
    if not directory.exists():
        return 0.0
    total = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
    return total / (1024 * 1024)


def _get_outbox_dir() -> Path:
    """Get outbox directory path (platform-aware, matches LocalOutboxBackend)."""
    return Path(settings.data_dir) / "wake_data" / "outbox"


class StorageManager:
    """Manages disk usage and retention for wake clips."""

    def __init__(self) -> None:
        self._data_dir = Path(settings.data_dir) / "wake_clips"
        self._timer: threading.Timer | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        # Run outbox pruning on startup
        try:
            self._prune_outbox()
        except Exception:
            logger.exception("Startup outbox pruning failed")
        self._schedule_prune()
        logger.info("Storage manager started (interval=%ds)", PRUNE_INTERVAL_SEC)

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_prune(self) -> None:
        if not self._running:
            return
        self._timer = threading.Timer(PRUNE_INTERVAL_SEC, self._run_prune)
        self._timer.daemon = True
        self._timer.start()

    def _run_prune(self) -> None:
        """Execute pruning cycle."""
        try:
            self.prune()
        except Exception:
            logger.exception("Pruning cycle failed")
        finally:
            self._schedule_prune()

    def prune(self) -> dict:
        """Run all pruning policies. Returns stats on what was pruned."""
        stats = {
            "near_miss_pruned": 0,
            "trigger_pruned": 0,
            "cap_pruned": 0,
            "outbox_pruned": 0,
        }

        # Priority 1: Near-miss cap
        stats["near_miss_pruned"] = self._prune_near_miss_cap()

        # Priority 2: Old uploaded triggers past retention
        stats["trigger_pruned"] = self._prune_old_triggers()

        # Priority 3: Outbox cap
        stats["outbox_pruned"] = self._prune_outbox()

        # Priority 4: Total cap enforcement
        stats["cap_pruned"] = self._enforce_total_cap()

        total = sum(stats.values())
        if total > 0:
            logger.info("Pruned %d clips: %s", total, stats)

        return stats

    def _prune_near_miss_cap(self) -> int:
        """Prune oldest near-misses if exceeding cap."""
        cap_mb = settings.wake_data_near_miss_cap_mb
        near_miss_dir = self._data_dir / "near_misses"
        current_mb = _get_dir_size_mb(near_miss_dir)

        if current_mb <= cap_mb:
            return 0

        db = get_data_collection_db()
        pruned = 0

        while current_mb > cap_mb * 0.9:  # Prune to 90% of cap
            clips = db.get_oldest_near_misses(limit=50)
            if not clips:
                break

            for clip in clips:
                self._delete_clip_files(clip)
                db.delete_clip(clip["id"])
                pruned += 1

            current_mb = _get_dir_size_mb(near_miss_dir)

        return pruned

    def _prune_old_triggers(self) -> int:
        """Prune triggers older than retention period that are already uploaded."""
        retention_days = settings.wake_data_retention_days
        cutoff = time.time() - (retention_days * 86400)

        db = get_data_collection_db()
        clips = db.get_old_uploaded_triggers(before_timestamp=cutoff)

        pruned = 0
        for clip in clips:
            self._delete_clip_files(clip)
            db.delete_clip(clip["id"])
            pruned += 1

        return pruned

    def _prune_outbox(self) -> int:
        """Delete oldest batch dirs when outbox exceeds max_outbox_size_mb."""
        outbox_dir = _get_outbox_dir()
        cap_mb = settings.wake_data_max_outbox_size_mb
        current_mb = _get_dir_size_mb(outbox_dir)

        if current_mb <= cap_mb:
            return 0

        # Get batch directories sorted by modification time (oldest first)
        batch_dirs = sorted(
            [d for d in outbox_dir.iterdir() if d.is_dir()],
            key=lambda d: d.stat().st_mtime,
        )

        pruned = 0
        for batch_dir in batch_dirs:
            if current_mb <= cap_mb * 0.9:
                break
            try:
                dir_mb = _get_dir_size_mb(batch_dir)
                shutil.rmtree(batch_dir)
                current_mb -= dir_mb
                pruned += 1
            except Exception:
                logger.exception("Failed to remove outbox batch: %s", batch_dir.name)

        if pruned > 0:
            logger.info("Pruned %d outbox batch directories", pruned)

        return pruned

    def _enforce_total_cap(self) -> int:
        """Enforce total storage cap. Prune near-misses first, then old uploaded triggers."""
        cap_mb = settings.wake_data_total_cap_mb
        outbox_mb = _get_dir_size_mb(_get_outbox_dir())
        clip_mb = _get_dir_size_mb(self._data_dir)
        current_mb = clip_mb + outbox_mb

        if current_mb <= cap_mb:
            return 0

        db = get_data_collection_db()
        pruned = 0

        # First pass: prune near-misses
        while current_mb > cap_mb * 0.9:
            clips = db.get_oldest_near_misses(limit=50)
            if not clips:
                break
            for clip in clips:
                self._delete_clip_files(clip)
                db.delete_clip(clip["id"])
                pruned += 1
            current_mb = _get_dir_size_mb(self._data_dir) + outbox_mb

        # Second pass: prune old uploaded triggers (not unreviewed)
        if current_mb > cap_mb * 0.9:
            cutoff = time.time() - (7 * 86400)  # At least 7 days old
            clips = db.get_old_uploaded_triggers(before_timestamp=cutoff)
            for clip in clips:
                if current_mb <= cap_mb * 0.9:
                    break
                self._delete_clip_files(clip)
                db.delete_clip(clip["id"])
                pruned += 1
                current_mb = _get_dir_size_mb(self._data_dir) + outbox_mb

        return pruned

    def _delete_clip_files(self, clip: dict) -> None:
        """Delete audio files for a clip (original + anonymized)."""
        audio_path = self._data_dir / clip["audio_path"]
        if audio_path.exists():
            try:
                audio_path.unlink()
            except OSError:
                logger.warning("Could not delete: %s", audio_path)

        anon_path = self._data_dir / "anonymized" / ("%d.wav" % clip["id"])
        if anon_path.exists():
            try:
                anon_path.unlink()
            except OSError:
                logger.warning("Could not delete: %s", anon_path)

    def get_storage_stats(self) -> dict:
        """Return storage usage breakdown."""
        triggers_mb = _get_dir_size_mb(self._data_dir / "triggers")
        near_miss_mb = _get_dir_size_mb(self._data_dir / "near_misses")
        anon_mb = _get_dir_size_mb(self._data_dir / "anonymized")
        outbox_mb = _get_dir_size_mb(_get_outbox_dir())
        total_mb = triggers_mb + near_miss_mb + anon_mb + outbox_mb

        return {
            "total_mb": round(total_mb, 2),
            "triggers_mb": round(triggers_mb, 2),
            "near_misses_mb": round(near_miss_mb, 2),
            "anonymized_mb": round(anon_mb, 2),
            "outbox_mb": round(outbox_mb, 2),
            "total_cap_mb": settings.wake_data_total_cap_mb,
            "near_miss_cap_mb": settings.wake_data_near_miss_cap_mb,
            "max_outbox_size_mb": settings.wake_data_max_outbox_size_mb,
        }


_storage_mgr: StorageManager | None = None
_sm_lock = threading.Lock()


def get_storage_manager() -> StorageManager:
    global _storage_mgr
    with _sm_lock:
        if _storage_mgr is None:
            _storage_mgr = StorageManager()
        return _storage_mgr
