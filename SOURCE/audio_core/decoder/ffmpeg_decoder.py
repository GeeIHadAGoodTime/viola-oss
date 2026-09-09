"""
FFmpeg-backed decode-only pipeline.

The FFmpegDecoder orchestrates an ffprobe pre-flight (for codec validation) and
streams decoded PCM frames into a provided BufferManager. The implementation
intentionally avoids device sink coupling; consumers read PCM directly from the
buffer.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

from core.constants import SAMPLE_RATE_48K
from core.logging_config import get_logger
from core.subprocess_utils import popen_silent, run_silent

from .buffer_manager import BufferManager, BufferOverflowError
from .telemetry import DecoderTelemetry


class DecoderStartupError(RuntimeError):
    """Raised when the decoder cannot initialise (e.g., missing binaries)."""


class DecoderIOError(RuntimeError):
    """Raised when FFmpeg encounters runtime I/O errors."""


class CodecNotSupportedError(RuntimeError):
    """Raised when the requested codec is not permitted or unsupported."""


@dataclass(slots=True)
class DecoderConfiguration:
    """Runtime configuration for FFmpegDecoder."""

    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    allowed_codecs: Sequence[str] = ("pcm_s16le", "mp3", "aac", "flac")
    pcm_sample_rate: int = SAMPLE_RATE_48K
    pcm_channels: int = 2
    decode_chunk_size: int = 32 * 1024
    telemetry_source: str = "audio_core.decoder"
    fail_on_missing_codec_metadata: bool = True
    shutdown_grace_period_sec: float = 5.0


@dataclass(slots=True)
class ProbeResult:
    """Subset of ffprobe details required for decode pipeline."""

    codec_name: str
    sample_rate: int
    channels: int
    duration_sec: float | None = None


class FFmpegDecoder:
    """
    High-level decode orchestrator wrapping FFmpeg through subprocess pipes.

    Usage:
        decoder = FFmpegDecoder(config, buffer_manager, telemetry)
        result = decoder.open("file.mp3")
        decoder.start()
        # Consumer thread fetches PCM via buffer_manager.read(...)
        decoder.close()
    """

    def __init__(
        self,
        config: DecoderConfiguration | None = None,
        buffer_manager: BufferManager | None = None,
        telemetry: DecoderTelemetry | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config or DecoderConfiguration()
        self._logger = logger or get_logger("audio_core.decoder.ffmpeg")
        self._buffer = buffer_manager or BufferManager(telemetry=telemetry)
        self._telemetry = telemetry or DecoderTelemetry(source=self._config.telemetry_source)

        self._ffmpeg_path = self._resolve_binary(self._config.ffmpeg_path, "ffmpeg")
        self._ffprobe_path = self._resolve_binary(self._config.ffprobe_path, "ffprobe")
        self._probe: ProbeResult | None = None
        self._source_uri: str | None = None

        self._process: subprocess.Popen[bytes] | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._decode_start_ts = 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def open(self, source_uri: str) -> ProbeResult:
        """
        Probe the input and verify codec support.

        Returns the parsed ProbeResult, caching it for start().
        """
        start = time.perf_counter()

        # SECURITY: Validate source_uri to prevent command injection
        self._validate_source_uri(source_uri)

        self._source_uri = source_uri
        probe = self._run_probe(source_uri)
        self._validate_codec(probe.codec_name)
        self._telemetry.emit_decode_latency(stage="probe", duration_sec=time.perf_counter() - start)
        self._telemetry.emit_decode_start(
            source_uri=source_uri,
            codec=probe.codec_name,
            sample_rate=probe.sample_rate,
            channels=probe.channels,
        )
        self._probe = probe
        return probe

    def start(self) -> None:
        """Launch the FFmpeg subprocess and streaming threads."""
        if self._source_uri is None or self._probe is None:
            raise DecoderStartupError("Decoder.start() called before open()")

        if self._process is not None:
            raise DecoderStartupError("Decoder already running")

        start = time.perf_counter()
        command = self._build_decode_command(self._source_uri)

        self._logger.debug("Launching FFmpeg decoder", extra={"command": command})
        try:
            proc = popen_silent(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:  # pragma: no cover - environment failure
            raise DecoderStartupError(f"Failed to launch FFmpeg: {exc}") from exc

        if proc.stdout is None or proc.stderr is None:
            proc.kill()
            raise DecoderStartupError("FFmpeg pipes not initialised")

        self._process = proc
        self._decode_start_ts = time.time()
        self._stop_event.clear()

        self._stdout_thread = threading.Thread(
            target=self._drain_stdout,
            args=(proc.stdout,),
            name="FFmpegPCMReader",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(proc.stderr,),
            name="FFmpegLogReader",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        self._telemetry.emit_decode_latency(stage="startup", duration_sec=time.perf_counter() - start)

    def close(self) -> None:
        """Terminate decoder subprocess and background threads."""
        if self._process is None:
            return

        self._stop_event.set()
        try:
            if self._stdout_thread:
                self._stdout_thread.join(timeout=self._config.shutdown_grace_period_sec)
            if self._stderr_thread:
                self._stderr_thread.join(timeout=self._config.shutdown_grace_period_sec)
        finally:
            self._terminate_process()

        elapsed = time.time() - self._decode_start_ts
        reason = "normal" if self._process.returncode == 0 else f"return_code_{self._process.returncode}"
        self._telemetry.emit_decode_stop(
            source_uri=self._source_uri or "unknown",
            reason=reason,
            elapsed_sec=elapsed,
        )

        self._process = None
        self._stdout_thread = None
        self._stderr_thread = None
        self._probe = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # ------------------------------------------------------------------ #
    # Internal command helpers
    # ------------------------------------------------------------------ #

    def _resolve_binary(self, configured_path: str | None, fallback: str) -> str:
        candidate = configured_path or shutil.which(fallback)
        if not candidate:
            raise DecoderStartupError(f"{fallback} binary not found in PATH and no override provided")
        return candidate

    def _validate_source_uri(self, source_uri: str) -> None:
        """
        SECURITY: Validate source URI to prevent command injection.

        Args:
            source_uri: Source URI to validate

        Raises:
            DecoderStartupError: If URI is invalid or contains dangerous characters
        """
        if not source_uri:
            raise DecoderStartupError("Source URI cannot be empty")

        # Block dangerous characters that could be used for command injection
        dangerous_chars = [";", "|", "&", "`", "$", "(", ")", "<", ">", "\n", "\r"]
        for char in dangerous_chars:
            if char in source_uri:
                raise DecoderStartupError(
                    f"Invalid source URI: contains dangerous character '{char}'. Only URLs and file paths are allowed."
                )

        # Validate URI format (must be http://, https://, file://, or local file path)
        source_uri_lower = source_uri.lower().strip()

        # Allow common URI schemes
        allowed_schemes = ["http://", "https://", "file://", "rtmp://", "rtsp://"]
        is_url = any(source_uri_lower.startswith(scheme) for scheme in allowed_schemes)

        # Allow local file paths (relative or absolute)
        # But block paths that start with dangerous patterns
        is_local_path = not is_url and (
            source_uri.startswith("/")
            or source_uri.startswith("./")
            or (len(source_uri) > 2 and source_uri[1] == ":" and source_uri[2] in ["/", "\\"])  # Windows drive
        )

        if not (is_url or is_local_path):
            # If it doesn't match URL or local path pattern, it might be malicious
            # Allow simple filenames without path separators
            if "/" not in source_uri and "\\" not in source_uri and ":" not in source_uri:
                # Simple filename - allow it
                return

        # Additional validation: block URLs with embedded commands
        # Check for common command injection patterns in URLs
        if ";" in source_uri or "|" in source_uri or "&" in source_uri:
            raise DecoderStartupError(
                "Invalid source URI: contains command separator characters. Only valid URLs and file paths are allowed."
            )

    def _run_probe(self, source_uri: str) -> ProbeResult:
        # SECURITY: Using list arguments prevents command injection (no shell interpretation)
        # Additional validation is performed in _validate_source_uri()
        command = [
            self._ffprobe_path,
            "-v",
            "error",
            "-show_streams",
            "-select_streams",
            "a:0",
            "-of",
            "json",
            source_uri,  # Safe: list args prevent injection, validated in open()
        ]
        self._logger.debug("Running ffprobe", extra={"command": command})
        try:
            result = run_silent(
                command,
                capture_output=True,
                check=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise DecoderStartupError(f"ffprobe failed: {exc.stderr.strip()}") from exc

        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        if not streams:
            if self._config.fail_on_missing_codec_metadata:
                raise DecoderStartupError("No audio streams found in source")
            return ProbeResult(
                codec_name="unknown",
                sample_rate=self._config.pcm_sample_rate,
                channels=self._config.pcm_channels,
            )

        stream = streams[0]
        codec_name = stream.get("codec_name")
        if codec_name is None:
            if self._config.fail_on_missing_codec_metadata:
                raise DecoderStartupError("ffprobe did not provide codec_name")
            codec_name = "unknown"

        sample_rate_str = stream.get("sample_rate")
        try:
            sample_rate = int(sample_rate_str) if sample_rate_str else self._config.pcm_sample_rate
        except ValueError:
            sample_rate = self._config.pcm_sample_rate

        channels = int(stream.get("channels") or self._config.pcm_channels)
        duration_str = stream.get("duration")
        duration = float(duration_str) if duration_str else None

        return ProbeResult(
            codec_name=codec_name,
            sample_rate=sample_rate,
            channels=channels,
            duration_sec=duration,
        )

    def _validate_codec(self, codec_name: str) -> None:
        if codec_name not in self._config.allowed_codecs:
            raise CodecNotSupportedError(
                f"Codec '{codec_name}' not supported. Allowed codecs: {', '.join(self._config.allowed_codecs)}"
            )

    def _build_decode_command(self, source_uri: str) -> Sequence[str]:
        # SECURITY: Using list arguments prevents command injection (no shell interpretation)
        # Additional validation is performed in _validate_source_uri()
        cmd = [
            self._ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-vn",
            "-i",
            source_uri,  # Safe: list args prevent injection, validated in open()
            "-f",
            "s16le",
            "-ac",
            str(self._config.pcm_channels),
            "-ar",
            str(self._config.pcm_sample_rate),
            "-acodec",
            "pcm_s16le",
            "pipe:1",
        ]
        return cmd

    # ------------------------------------------------------------------ #
    # Stream draining
    # ------------------------------------------------------------------ #

    def _drain_stdout(self, stdout) -> None:
        chunk_size = max(4096, self._config.decode_chunk_size)
        while not self._stop_event.is_set():
            chunk = stdout.read(chunk_size)
            if not chunk:
                break

            try:
                self._buffer.write(chunk, block=True)
            except BufferOverflowError as exc:
                self._logger.warning("Buffer overflow during decode", exc_info=exc)
                self._telemetry.emit_decode_error(
                    source_uri=self._source_uri or "unknown",
                    error_code="buffer_overflow",
                    message=str(exc),
                )
                break

        stdout.close()

    def _drain_stderr(self, stderr) -> None:
        while not self._stop_event.is_set():
            line = stderr.readline()
            if not line:
                break
            self._logger.debug("ffmpeg", extra={"stderr": line.decode("utf-8", "ignore").strip()})
        stderr.close()

    def _terminate_process(self) -> None:
        if self._process is None:
            return

        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=self._config.shutdown_grace_period_sec)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process.stdout = None
        self._process.stderr = None

    # ------------------------------------------------------------------ #
    # Utility
    # ------------------------------------------------------------------ #

    @property
    def buffer(self) -> BufferManager:
        return self._buffer

    @property
    def telemetry(self) -> DecoderTelemetry:
        return self._telemetry

    @property
    def source_uri(self) -> str | None:
        return self._source_uri

    def snapshot_state(self) -> dict[str, object]:
        """Return a diagnostic snapshot for operators/tests."""
        metrics = self._buffer.metrics()
        return {
            "source_uri": self._source_uri,
            "probe": self._probe,
            "running": self.is_running(),
            "buffer_level_percent": metrics.level_percent,
            "underruns": metrics.underrun_count,
            "overflows": metrics.overflow_count,
        }


__all__ = [
    "CodecNotSupportedError",
    "DecoderConfiguration",
    "DecoderIOError",
    "DecoderStartupError",
    "FFmpegDecoder",
    "ProbeResult",
]
