"""Abstract recording storage — local filesystem and S3/R2 compatible.

Provides a storage abstraction layer for call recordings. Local filesystem
is the default (backward compatible). S3/R2 storage is opt-in via config
for cloud deployments.

S3 implementation uses boto3 (lazy import, optional dependency).

PHONE-08: S3 object keys hash the call_id with HMAC-SHA256 so an S3 list
does not leak call identifiers or any substring we could use to correlate
a user's call to other metadata. The hash is deterministic per bucket
secret so ``delete`` still resolves to the same key without a DB roundtrip.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import wave
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_SAMPLE_RATE = 16000
_SAMPLE_WIDTH = 2  # 16-bit signed PCM
_NUM_CHANNELS = 1  # mono


class RecordingStorage:
    """Abstract recording storage. Local filesystem by default, S3/R2 for cloud.

    Subclasses implement save/delete for their storage backend. Local
    desktop storage also exposes ``get_url`` for file-backed playback.
    """

    async def save(self, call_id: str, variant: str, audio_data: bytes, sample_rate: int = _SAMPLE_RATE) -> str:
        """Save recording audio data.

        Args:
            call_id: Unique call identifier.
            variant: Recording variant (e.g. "inbound", "outbound", "stereo").
            audio_data: Raw PCM audio bytes.
            sample_rate: PCM sample rate in Hz for the WAV header. Inbound
                telephone legs run at 8 kHz while the outbound (TTS) leg runs at
                16 kHz, so the rate must be passed per variant or the saved WAV
                plays back at the wrong speed/pitch.

        Returns:
            Storage URI identifying the saved recording.
        """
        raise NotImplementedError

    async def get_url(self, call_id: str, variant: str) -> str:
        """Get a URL for streaming/downloading a recording.

        Args:
            call_id: Unique call identifier.
            variant: Recording variant.

        Returns:
            URL string for local file-backed storage.
        """
        raise NotImplementedError

    async def delete(self, call_id: str) -> None:
        """Delete all recordings for a call.

        Args:
            call_id: Unique call identifier.
        """
        raise NotImplementedError


class LocalRecordingStorage(RecordingStorage):
    """Stores recordings on local filesystem (existing behavior).

    Recordings are saved as WAV files under base_dir/call_id_variant.wav.
    """

    def __init__(self, base_dir: str = "data/call_recordings") -> None:
        self._base_dir = Path(base_dir)

    async def save(self, call_id: str, variant: str, audio_data: bytes, sample_rate: int = _SAMPLE_RATE) -> str:
        """Save audio data as a WAV file on the local filesystem.

        Returns:
            file:// URI to the saved WAV.
        """
        if not audio_data:
            logger.info("No audio data for %s/%s, skipping save", call_id, variant)
            return ""

        self._base_dir.mkdir(parents=True, exist_ok=True)
        filename = "%s_%s.wav" % (call_id, variant)
        wav_path = self._base_dir / filename

        try:
            # Run blocking I/O in executor to avoid blocking the event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._write_wav, wav_path, audio_data, sample_rate)

            total_seconds = len(audio_data) / (sample_rate * _SAMPLE_WIDTH * _NUM_CHANNELS)
            logger.info(
                "Saved %s recording: %s (%.1fs, %d bytes)",
                variant,
                wav_path,
                total_seconds,
                len(audio_data),
            )
            return "file://%s" % wav_path.resolve()
        except Exception as exc:
            logger.warning("Failed to save %s recording: %s", variant, exc)
            return ""

    async def get_url(self, call_id: str, variant: str) -> str:
        """Get file:// URL for a local recording."""
        filename = "%s_%s.wav" % (call_id, variant)
        wav_path = self._base_dir / filename
        if wav_path.exists():
            return "file://%s" % wav_path.resolve()
        return ""

    async def delete(self, call_id: str) -> None:
        """Delete all recording files for a call."""
        if not self._base_dir.exists():
            return

        deleted = 0
        for wav_file in self._base_dir.glob("%s_*.wav" % call_id):
            try:
                wav_file.unlink()
                deleted += 1
            except OSError as exc:
                logger.warning("Failed to delete %s: %s", wav_file, exc)

        if deleted:
            logger.info("Deleted %d recording(s) for call %s", deleted, call_id)

    @staticmethod
    def _write_wav(path: Path, audio_data: bytes, sample_rate: int = _SAMPLE_RATE) -> None:
        """Write raw PCM data to a WAV file (blocking)."""
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(_NUM_CHANNELS)
            wf.setsampwidth(_SAMPLE_WIDTH)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)


class S3RecordingStorage(RecordingStorage):
    """Stores recordings in S3/R2 compatible object storage.

    Uses boto3 (lazy import, optional dependency). Compatible with:
    - AWS S3
    - Cloudflare R2 (via endpoint_url)
    - MinIO (via endpoint_url)
    - Any S3-compatible storage

    Recordings are stored as WAV files at ``prefix/<hmac(call_id)>/variant.wav``.
    The ``hmac(call_id)`` is a HMAC-SHA256 over the call_id with a per-bucket
    ``key_hash_secret`` (see PHONE-08) so an S3 list cannot be used to
    enumerate call identifiers. The mapping from call_id to hashed key is
    deterministic — callers reuse the same ``S3RecordingStorage`` to delete a
    recording without needing a DB lookup.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "recordings/",
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        key_hash_secret: str = "",
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/" if prefix else ""
        self._endpoint_url = endpoint_url
        self._access_key = access_key
        self._secret_key = secret_key
        if not key_hash_secret:
            # We refuse to silently fall back to plaintext call_id keys —
            # that re-introduces the PHONE-08 leak.
            raise ValueError("S3RecordingStorage requires key_hash_secret to avoid leaking call_id in object keys.")
        self._key_hash_secret = key_hash_secret.encode("utf-8")
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazy-initialize the boto3 S3 client."""
        if self._client is None:
            try:
                import boto3
            except ImportError:
                raise ImportError(
                    "boto3 is required for S3 recording storage. " "Install with: pip install boto3"
                ) from None

            kwargs: dict[str, Any] = {
                "service_name": "s3",
            }
            if self._endpoint_url:
                kwargs["endpoint_url"] = self._endpoint_url
            if self._access_key:
                kwargs["aws_access_key_id"] = self._access_key
            if self._secret_key:
                kwargs["aws_secret_access_key"] = self._secret_key

            self._client = boto3.client(**kwargs)
        return self._client

    def _object_key(self, call_id: str, variant: str) -> str:
        """Build the S3 object key for a recording.

        The call_id is hashed with HMAC-SHA256 so an S3 list doesn't
        expose call identifiers (PHONE-08).
        """
        digest = hmac.new(self._key_hash_secret, call_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return "%s%s/%s.wav" % (self._prefix, digest, variant)

    def _delete_prefix(self, call_id: str) -> str:
        digest = hmac.new(self._key_hash_secret, call_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return "%s%s/" % (self._prefix, digest)

    async def save(self, call_id: str, variant: str, audio_data: bytes, sample_rate: int = _SAMPLE_RATE) -> str:
        """Save audio data as a WAV to S3/R2.

        Returns:
            s3:// URI identifying the saved recording.
        """
        if not audio_data:
            logger.info("No audio data for %s/%s, skipping save", call_id, variant)
            return ""

        key = self._object_key(call_id, variant)

        # Build WAV in memory
        import io

        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wf:
            wf.setnchannels(_NUM_CHANNELS)
            wf.setsampwidth(_SAMPLE_WIDTH)
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)
        wav_bytes = wav_buffer.getvalue()

        try:
            loop = asyncio.get_running_loop()
            client = self._get_client()
            await loop.run_in_executor(
                None,
                lambda: client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=wav_bytes,
                    ContentType="audio/wav",
                ),
            )

            total_seconds = len(audio_data) / (sample_rate * _SAMPLE_WIDTH * _NUM_CHANNELS)
            logger.info(
                "Saved %s recording to S3: s3://%s/%s (%.1fs, %d bytes)",
                variant,
                self._bucket,
                key,
                total_seconds,
                len(audio_data),
            )
            return "s3://%s/%s" % (self._bucket, key)
        except Exception as exc:
            logger.warning("Failed to save %s recording to S3: %s", variant, exc)
            return ""

    async def get_url(self, call_id: str, variant: str) -> str:
        """Refuse raw provider bearer URLs for cloud call recordings."""
        raise RuntimeError("S3 recording URLs must be issued through authenticated cloud storage routes")

    async def delete(self, call_id: str) -> None:
        """Delete all recordings for a call from S3."""
        prefix = self._delete_prefix(call_id)
        try:
            loop = asyncio.get_running_loop()
            client = self._get_client()

            # List objects with the call_id prefix
            response = await loop.run_in_executor(
                None,
                lambda: client.list_objects_v2(Bucket=self._bucket, Prefix=prefix),
            )

            objects = response.get("Contents", [])
            if not objects:
                return

            # Delete all matching objects
            delete_keys = [{"Key": obj["Key"]} for obj in objects]
            await loop.run_in_executor(
                None,
                lambda: client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": delete_keys},
                ),
            )

            logger.info("Deleted %d recording(s) for call %s from S3", len(delete_keys), call_id)
        except Exception as exc:
            logger.warning("Failed to delete recordings for call %s from S3: %s", call_id, exc)


def create_recording_storage(
    storage_type: str = "local",
    base_dir: str = "data/call_recordings",
    s3_bucket: str = "",
    s3_endpoint: str = "",
    s3_prefix: str = "recordings/",
    s3_access_key: str = "",
    s3_secret_key: str = "",
    s3_key_hash_secret: str = "",
) -> RecordingStorage:
    """Factory function to create the appropriate recording storage backend.

    Args:
        storage_type: "local" or "s3".
        base_dir: Base directory for local storage.
        s3_bucket: S3 bucket name (required for s3 type).
        s3_endpoint: S3 endpoint URL (for R2/MinIO).
        s3_prefix: Key prefix for S3 objects.
        s3_access_key: AWS access key ID.
        s3_secret_key: AWS secret access key.
        s3_key_hash_secret: HMAC secret used to hash call_ids into
            opaque S3 keys (PHONE-08). Required when ``storage_type="s3"``;
            the helper raises if it's missing.

    Returns:
        A RecordingStorage instance.
    """
    if storage_type == "s3":
        if not s3_bucket:
            raise ValueError("s3_bucket is required for S3 recording storage")
        if not s3_key_hash_secret:
            raise ValueError(
                "s3_key_hash_secret is required for S3 recording storage " "(set TELNYX_RECORDING_S3_KEY_HASH_SECRET)."
            )
        return S3RecordingStorage(
            bucket=s3_bucket,
            prefix=s3_prefix,
            endpoint_url=s3_endpoint,
            access_key=s3_access_key,
            secret_key=s3_secret_key,
            key_hash_secret=s3_key_hash_secret,
        )

    return LocalRecordingStorage(base_dir=base_dir)
