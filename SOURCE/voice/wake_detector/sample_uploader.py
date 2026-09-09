"""
Sample Uploader for Contributor Mode.

Handles batch upload of collected samples to developer server.
Upload is triggered by developer request (pull model), not automatic.

This module was previously in telemetry/ but belongs with the listener
package as it handles wake word training sample upload alongside
sample_collector.py and contributor_mode.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.constants import TIMEOUT_SHUTDOWN, TIMEOUT_VERY_LONG
from core.logging_config import get_logger

logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


# No default remote endpoint. Public builds must not phone home unless an owner
# explicitly configures a contribution endpoint and enables the upload control.
DEFAULT_SERVER_URL = ""
_WAKE_DATA_UPLOAD_CONTROL = "wake_data_upload"


def _wake_data_upload_allowed(action: str) -> bool:
    try:
        from services.operator_controls import require_enabled

        decision = require_enabled(_WAKE_DATA_UPLOAD_CONTROL, action=action)
        if not decision.allowed:
            logger.debug("Contributor sample upload blocked: %s", decision.reason)
            return False
        return True
    except Exception:
        logger.exception("Contributor sample upload blocked: operator control check failed")
        return False


async def _wake_data_upload_allowed_async(action: str) -> bool:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async(_WAKE_DATA_UPLOAD_CONTROL, action=action)
        if not decision.allowed:
            logger.debug("Contributor sample upload blocked: %s", decision.reason)
            return False
        return True
    except Exception:
        logger.exception("Contributor sample upload blocked: operator control check failed")
        return False


class SampleUploader:
    """
    Batch uploads samples on developer request.

    Uses pull-based model:
    - Periodically polls server to check if upload is requested
    - If requested, uploads pending samples in batches
    - Gracefully handles server unavailability
    """

    def __init__(
        self,
        samples_dir: Path | str,
        server_url: str | None = None,
    ):
        """
        Initialize sample uploader.

        Args:
            samples_dir: Directory containing samples to upload
            server_url: Server URL (uses default if None)
        """
        self._samples_dir = Path(samples_dir)
        self._server_url = server_url or DEFAULT_SERVER_URL
        self._client: Any = None  # httpx.AsyncClient
        self._upload_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

        # Import SampleCollector for sample tracking
        try:
            from voice.wake_detector.sample_collector import SampleCollector

            self._collector = SampleCollector(self._samples_dir)
        except ImportError:
            logger.warning("SampleCollector not available")
            self._collector = None

        logger.info("SampleUploader initialized (server=%s)", self._server_url)

    async def _get_client(self) -> Any:
        """Get or create HTTP client."""
        if self._client is None:
            try:
                import httpx

                self._client = httpx.AsyncClient(
                    timeout=TIMEOUT_VERY_LONG,
                    headers={"User-Agent": "Viola/1.0 SampleUploader"},
                )
            except ImportError:
                logger.debug("httpx not available, upload disabled")
                return None
        return self._client

    async def close(self) -> None:
        """Close HTTP client and stop background tasks."""
        await self.stop_background_check()

        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception as e:
                logger.debug("Client close failed: %s", e)
            self._client = None

    async def check_upload_requested(self) -> bool:
        """
        Poll server to see if developer has requested uploads.

        Returns:
            True if upload is requested, False otherwise
        """
        if not self._server_url or not await _wake_data_upload_allowed_async("contributor_upload_check"):
            return False

        try:
            client = await self._get_client()
            if client is None:
                return False

            resp = await client.get(f"{self._server_url}/upload-requested")

            if resp.status_code == 404:
                # Endpoint not implemented yet - graceful fallback
                logger.debug("upload-requested endpoint not found")
                return False

            resp.raise_for_status()
            data = resp.json()
            return data.get("requested", False)

        except Exception as e:
            logger.debug("Upload request check failed: %s", e)
            return False

    async def upload_batch(self, limit: int = 50) -> dict[str, Any]:
        """
        Upload a batch of pending samples.

        Args:
            limit: Maximum samples to upload in this batch

        Returns:
            Dict with upload results:
            {
                "uploaded": 45,
                "failed": 5,
                "remaining": 120,
                "errors": [...]
            }
        """
        if self._collector is None:
            return {
                "uploaded": 0,
                "failed": 0,
                "remaining": 0,
                "error": "SampleCollector not available",
            }

        if not self._server_url:
            return {
                "uploaded": 0,
                "failed": 0,
                "remaining": self._collector.get_pending_count(),
                "error": "No contributor upload server configured",
            }

        if not await _wake_data_upload_allowed_async("contributor_sample_upload"):
            return {
                "uploaded": 0,
                "failed": 0,
                "remaining": self._collector.get_pending_count(),
                "error": "Wake data upload disabled by owner safety control",
            }

        pending = self._collector.get_pending_samples()[:limit]
        if not pending:
            return {
                "uploaded": 0,
                "failed": 0,
                "remaining": 0,
            }

        client = await self._get_client()
        if client is None:
            return {
                "uploaded": 0,
                "failed": len(pending),
                "remaining": self._collector.get_pending_count(),
                "error": "HTTP client not available",
            }

        uploaded = []
        failed = []
        errors = []

        for sample_path in pending:
            try:
                # Read sample file
                audio_data = sample_path.read_bytes()

                # Get metadata if available
                metadata = self._get_sample_metadata(sample_path)

                # Request upload URL
                resp = await client.post(
                    f"{self._server_url}/upload-batch",
                    json={
                        "filename": sample_path.name,
                        "metadata": metadata,
                    },
                )

                if resp.status_code == 429:
                    # Quota reached - stop uploading
                    logger.info("Server quota reached, stopping batch upload")
                    break

                resp.raise_for_status()
                upload_info = resp.json()

                # Upload the file
                upload_resp = await client.put(
                    upload_info.get("upload_url", f"{self._server_url}/upload"),
                    content=audio_data,
                    headers={"Content-Type": "audio/wav"},
                    params={"id": upload_info.get("sample_id")},
                )
                upload_resp.raise_for_status()

                uploaded.append(sample_path)
                logger.debug("Uploaded sample: %s", sample_path.name)

            except Exception as e:
                failed.append(sample_path)
                errors.append(f"{sample_path.name}: {e}")
                logger.debug("Failed to upload %s: %s", sample_path.name, e)

        # Mark uploaded samples
        if uploaded:
            self._collector.mark_as_uploaded(uploaded)

        remaining = self._collector.get_pending_count()

        result: dict[str, Any] = {
            "uploaded": len(uploaded),
            "failed": len(failed),
            "remaining": remaining,
        }

        if errors:
            result["errors"] = errors[:5]  # Limit error list

        logger.info(
            "Batch upload complete: %s uploaded, %s failed, %s remaining",
            len(uploaded),
            len(failed),
            remaining,
        )

        return result

    def _get_sample_metadata(self, sample_path: Path) -> dict[str, Any]:
        """
        Get metadata for a sample from the metadata file.

        Args:
            sample_path: Path to the sample file

        Returns:
            Metadata dict if found, empty dict otherwise
        """
        if self._collector is None:
            return {}

        metadata_file = self._samples_dir / "metadata.jsonl"
        if not metadata_file.exists():
            return {}

        try:
            import json

            with open(metadata_file, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("filename") == sample_path.name:
                        return entry
        except Exception as e:
            logger.debug("Failed to read sample metadata: %s", e)

        return {}

    def get_pending_count(self) -> int:
        """Count pending samples."""
        if self._collector is None:
            return 0
        return self._collector.get_pending_count()

    async def start_background_check(self, interval_seconds: int = 300) -> None:
        """
        Start background task that periodically checks if upload is requested.

        Args:
            interval_seconds: Check interval (default 5 minutes)
        """
        if self._upload_task is not None and not self._upload_task.done():
            logger.debug("Background check already running")
            return

        if not self._server_url or not await _wake_data_upload_allowed_async("contributor_background_upload"):
            logger.debug("Background contributor upload not started")
            return

        self._stop_event = asyncio.Event()

        async def _check_loop() -> None:
            while not self._stop_event.is_set():
                try:
                    if await self.check_upload_requested():
                        logger.info("Upload requested by server, starting batch upload")
                        await self.upload_batch()
                except Exception as e:
                    logger.debug("Background upload check error: %s", e)

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=interval_seconds,
                    )
                except TimeoutError:
                    pass  # Normal timeout, continue loop

        self._upload_task = asyncio.create_task(_check_loop())
        self._upload_task.add_done_callback(_log_task_exception)
        logger.info("Started background upload check (interval=%ss)", interval_seconds)

    async def stop_background_check(self) -> None:
        """Stop background upload check task."""
        if self._stop_event is not None:
            self._stop_event.set()

        if self._upload_task is not None:
            try:
                await asyncio.wait_for(self._upload_task, timeout=TIMEOUT_SHUTDOWN)
            except TimeoutError:
                self._upload_task.cancel()
            except Exception as e:
                logger.debug("Error during upload task shutdown: %s", e)
            self._upload_task = None

        logger.debug("Stopped background upload check")


# Global instance
_uploader: SampleUploader | None = None


def get_sample_uploader(
    samples_dir: Path | str | None = None,
    server_url: str | None = None,
) -> SampleUploader:
    """
    Get or create global sample uploader.

    Args:
        samples_dir: Directory containing samples (only used on first call)
        server_url: Server URL (only used on first call)

    Returns:
        Global SampleUploader instance
    """
    global _uploader

    if _uploader is None:
        from config import settings

        if samples_dir is None:
            # PATH-1: contributor samples live under the user data dir.
            from voice.wake_detector.contributor_mode import default_samples_dir

            samples_dir = default_samples_dir()
        if server_url is None:
            server_url = getattr(settings, "contributor_mode_server_url", None)

        _uploader = SampleUploader(samples_dir, server_url)

    return _uploader


def reset_sample_uploader() -> None:
    """Reset the global uploader (for testing)."""
    global _uploader
    _uploader = None


async def background_upload_check(
    uploader: SampleUploader,
    interval_seconds: int = 300,
) -> None:
    """
    Background task that periodically checks if upload is requested.

    This is an alternative entry point for the background check loop.

    Args:
        uploader: SampleUploader instance
        interval_seconds: Check interval (default 5 minutes)
    """
    await uploader.start_background_check(interval_seconds)
