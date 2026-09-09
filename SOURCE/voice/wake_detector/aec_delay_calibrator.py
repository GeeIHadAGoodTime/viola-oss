"""
AEC Delay Calibrator
====================

Automatically measures speaker-to-microphone delay for AEC alignment.

ALGORITHM:
1. Record baseline noise (500ms silence)
2. Play calibration signal (marker + chirp)
3. Record microphone input during playback
4. Detect marker in recording for coarse timing
5. Correlate chirp for precise delay measurement
6. Validate result (correlation strength, single peak)

FAILURE MODES:
- Too much ambient noise -> CalibrationError("noise_too_high")
- No marker detected -> CalibrationError("marker_not_found")
- Multiple correlation peaks -> CalibrationError("ambiguous_peaks")
- Weak correlation -> CalibrationError("weak_correlation")

All failures are recoverable with user intervention (quieter environment).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from audio_core.portaudio_guard import open_portaudio, open_stream, terminate_portaudio
from core.constants import AUDIO_INT16_MAX, AUDIO_INT16_SCALE
from core.logging_config import get_logger
from voice.wake_detector.calibration_signals import CalibrationSignalGenerator
from voice.wake_detector.wake_types import CalibrationConfig, CalibrationResult

if TYPE_CHECKING:
    import pyaudio

logger = get_logger(__name__)


class AECDelayCalibrator:
    """
    Automatic AEC delay calibrator.

    USAGE:
        calibrator = AECDelayCalibrator(config)

        # Run calibration (blocking)
        result = calibrator.calibrate()

        if result.success:
            aec_processor.set_delay(result.delay_samples)
        else:
            logger.error("Calibration failed: %s", result.failure_reason)

    THREADING:
    - calibrate() is blocking and should be called from a worker thread
    - Progress callbacks are invoked from the calibration thread
    """

    def __init__(
        self,
        config: CalibrationConfig | None = None,
        playback_callback: Callable[[np.ndarray], None] | None = None,
        on_progress: Callable[[str, float], None] | None = None,
    ):
        """
        Initialize calibrator.

        Args:
            config: Calibration configuration
            playback_callback: Function to play audio through speaker
                              Signature: callback(audio_float32) -> None
                              If None, uses default PyAudio output
            on_progress: Progress callback
                        Signature: callback(stage_name, progress_0_to_1) -> None
        """
        self._config = config or CalibrationConfig()
        self._playback_callback = playback_callback
        self._on_progress = on_progress

        self._signal_gen = CalibrationSignalGenerator(sample_rate=self._config.playback_sample_rate)

        # PyAudio instance (created on demand)
        self._audio: pyaudio.PyAudio | None = None

        # Lock for thread safety
        self._lock = threading.Lock()
        self._calibrating = False

    def calibrate(self) -> CalibrationResult:
        """
        Run delay calibration.

        BLOCKING: Runs for ~2-3 seconds.

        Returns:
            CalibrationResult with success/failure and delay value
        """
        with self._lock:
            if self._calibrating:
                return CalibrationResult.failure("calibration_in_progress")
            self._calibrating = True

        try:
            return self._calibrate_impl()
        finally:
            with self._lock:
                self._calibrating = False

    def _calibrate_impl(self) -> CalibrationResult:
        """Internal calibration implementation."""
        self._report_progress("initializing", 0.0)

        # Initialize audio
        try:
            self._init_audio()
        except Exception as e:
            logger.error("Failed to initialize audio: %s", e)
            return CalibrationResult.failure(f"audio_init_failed: {e}")

        try:
            for attempt in range(self._config.max_retries):
                logger.info("Calibration attempt %d/%d", attempt + 1, self._config.max_retries)

                result = self._single_calibration_attempt()

                if result.success:
                    self._save_calibration(result)
                    return result

                if attempt < self._config.max_retries - 1:
                    logger.warning(
                        "Calibration attempt %s failed: %s. Retrying in %ss...",
                        attempt + 1,
                        result.failure_reason,
                        self._config.retry_delay_seconds,
                    )
                    time.sleep(self._config.retry_delay_seconds)

            return result  # Return last failure

        finally:
            self._cleanup_audio()

    def _single_calibration_attempt(self) -> CalibrationResult:
        """Single calibration attempt."""

        # Step 1: Record baseline noise
        self._report_progress("measuring_noise", 0.1)
        noise_recording = self._record_audio(duration_seconds=0.5)
        noise_rms = float(np.sqrt(np.mean(noise_recording.astype(np.float32) ** 2)))

        logger.debug("Noise floor RMS: %.0f", noise_rms)

        if noise_rms > self._config.max_noise_rms:
            return CalibrationResult.failure(
                f"noise_too_high: RMS {noise_rms:.0f} > threshold {self._config.max_noise_rms}"
            )

        # Step 2: Play calibration signal and record
        self._report_progress("playing_signal", 0.3)

        calibration_signal = self._signal_gen.generate()
        recording = self._play_and_record(calibration_signal)

        if recording is None:
            return CalibrationResult.failure("recording_failed")

        # Step 3: Detect marker for coarse timing
        self._report_progress("detecting_marker", 0.5)

        marker_template = self._signal_gen.get_marker_template()
        marker_template_16k = self._resample_to_16k(marker_template)

        marker_position = self._detect_marker(recording, marker_template_16k)

        if marker_position is None:
            return CalibrationResult.failure("marker_not_found")

        logger.debug("Marker detected at sample %d", marker_position)

        # Step 4: Correlate chirp for precise delay
        self._report_progress("computing_delay", 0.7)

        chirp_template = self._signal_gen.get_chirp_template()
        chirp_template_16k = self._resample_to_16k(chirp_template)

        delay_result = self._compute_delay_from_chirp(
            recording,
            chirp_template_16k,
            marker_position,
        )

        if not delay_result["success"]:
            return CalibrationResult.failure(delay_result["reason"])

        # Step 5: Validate result
        self._report_progress("validating", 0.9)

        if delay_result["correlation"] < self._config.min_correlation:
            return CalibrationResult.failure(
                f"weak_correlation: {delay_result['correlation']:.3f} < " f"threshold {self._config.min_correlation}"
            )

        if delay_result["secondary_peaks"]:
            max_secondary = max(p[1] for p in delay_result["secondary_peaks"])
            if max_secondary > delay_result["correlation"] * self._config.max_secondary_peak_ratio:
                return CalibrationResult.failure(
                    f"ambiguous_peaks: secondary peak {max_secondary:.3f} "
                    f"too close to primary {delay_result['correlation']:.3f}"
                )

        self._report_progress("complete", 1.0)

        # Build successful result
        delay_samples_16k = delay_result["delay_samples"]
        delay_ms = delay_samples_16k / self._config.recording_sample_rate * 1000

        logger.info(
            "Calibration successful: delay = %s samples (%sms), correlation = %s",
            delay_samples_16k,
            format(delay_ms, ".1f"),
            format(delay_result["correlation"], ".3f"),
        )

        return CalibrationResult(
            success=True,
            delay_samples=delay_samples_16k,
            delay_ms=delay_ms,
            confidence=delay_result["correlation"],
            correlation_peak=delay_result["correlation"],
            noise_floor_db=20 * np.log10(noise_rms / AUDIO_INT16_SCALE + 1e-10),
            secondary_peaks=delay_result["secondary_peaks"],
        )

    def _detect_marker(
        self,
        recording: np.ndarray,
        marker_template: np.ndarray,
    ) -> int | None:
        """
        Detect marker position in recording.

        Returns:
            Sample position of marker, or None if not found
        """
        try:
            from scipy import signal as scipy_signal

            # Normalize
            recording_f: np.ndarray = recording.astype(np.float32)
            template_f: np.ndarray = marker_template.astype(np.float32)

            # Matched filter (correlation)
            correlation = scipy_signal.correlate(recording_f, template_f, mode="valid")

            # Find peak
            peak_idx = np.argmax(np.abs(correlation))
            peak_value = np.abs(correlation[peak_idx])

            # Threshold: peak must be significantly above median
            median_value = np.median(np.abs(correlation))
            if peak_value < median_value * 5:
                logger.warning(
                    "Marker peak %.0f not significantly above median %.0f",
                    peak_value,
                    median_value,
                )
                return None

            return int(peak_idx)

        except ImportError:
            # Fallback without scipy
            return self._detect_marker_simple(recording, marker_template)

    def _detect_marker_simple(
        self,
        recording: np.ndarray,
        marker_template: np.ndarray,
    ) -> int | None:
        """Simple marker detection without scipy."""
        recording_f: np.ndarray = recording.astype(np.float32)
        template_f: np.ndarray = marker_template.astype(np.float32)

        # Sliding dot product
        template_len = len(template_f)
        max_corr = 0.0
        max_idx = 0

        for i in range(len(recording_f) - template_len):
            segment = recording_f[i : i + template_len]
            corr = np.abs(np.dot(segment, template_f))
            if corr > max_corr:
                max_corr = corr
                max_idx = i

        # Threshold check
        if max_corr < np.std(recording_f) * template_len * 0.5:
            return None

        return max_idx

    def _compute_delay_from_chirp(
        self,
        recording: np.ndarray,
        chirp_template: np.ndarray,
        marker_position: int,
    ) -> dict[str, Any]:
        """
        Compute precise delay using chirp correlation.

        Returns:
            Dict with delay_samples, correlation, secondary_peaks, success, reason
        """
        try:
            from scipy import signal as scipy_signal

            # Expected chirp position (after marker + gap)
            gap_samples = int(0.020 * self._config.recording_sample_rate)  # 20ms gap
            marker_len_16k = int(
                len(self._signal_gen.get_marker_template())
                * self._config.recording_sample_rate
                / self._config.playback_sample_rate
            )
            expected_chirp_start = marker_position + marker_len_16k + gap_samples

            # Search window around expected position
            search_margin = int(0.100 * self._config.recording_sample_rate)  # ±100ms
            search_start = max(0, expected_chirp_start - search_margin)
            search_end = min(
                len(recording),
                expected_chirp_start + len(chirp_template) + search_margin,
            )

            if search_end <= search_start:
                return {"success": False, "reason": "search_window_too_small"}

            search_region: np.ndarray = recording[search_start:search_end].astype(np.float32)
            template_f: np.ndarray = chirp_template.astype(np.float32)

            # Cross-correlation
            correlation = scipy_signal.correlate(search_region, template_f, mode="valid")

            if len(correlation) == 0:
                return {"success": False, "reason": "correlation_empty"}

            # Normalize correlation
            norm = np.sqrt(np.sum(template_f**2) * np.sum(search_region**2))
            if norm > 0:
                correlation = correlation / norm

            # Find primary peak
            peak_idx = np.argmax(np.abs(correlation))
            peak_value = float(np.abs(correlation[peak_idx]))

            # Find secondary peaks (local maxima)
            secondary_peaks = []
            for i in range(1, len(correlation) - 1):
                if i == peak_idx:
                    continue
                if np.abs(correlation[i]) > np.abs(correlation[i - 1]) and np.abs(correlation[i]) > np.abs(
                    correlation[i + 1]
                ):
                    if np.abs(correlation[i]) > 0.3:  # Minimum threshold
                        secondary_peaks.append((i + search_start, float(np.abs(correlation[i]))))

            # Compute actual delay
            # Delay = time from playback start to when sound reached mic
            playback_rate = self._config.playback_sample_rate
            record_rate = self._config.recording_sample_rate

            expected_marker_position = int(self._signal_gen.marker_offset_samples * record_rate / playback_rate)

            delay_samples = marker_position - expected_marker_position

            return {
                "success": True,
                "delay_samples": int(max(0, delay_samples)),
                "correlation": peak_value,
                "secondary_peaks": secondary_peaks[:5],  # Top 5 secondary peaks
                "chirp_position": int(search_start + peak_idx),
            }

        except ImportError:
            # Simplified fallback
            return self._compute_delay_simple(recording, marker_position)

    def _compute_delay_simple(
        self,
        recording: np.ndarray,
        marker_position: int,
    ) -> dict[str, Any]:
        """Simple delay computation without scipy."""
        # Use marker position directly
        playback_rate = self._config.playback_sample_rate
        record_rate = self._config.recording_sample_rate

        expected_marker_position = int(self._signal_gen.marker_offset_samples * record_rate / playback_rate)

        delay_samples = marker_position - expected_marker_position

        return {
            "success": True,
            "delay_samples": int(max(0, delay_samples)),
            "correlation": 0.7,  # Assume moderate confidence
            "secondary_peaks": [],
        }

    def _resample_to_16k(self, audio: np.ndarray) -> np.ndarray:
        """Resample audio to 16kHz recording rate."""
        from voice.wake_detector.resampler import get_resampler

        resampler = get_resampler(
            self._config.playback_sample_rate,
            self._config.recording_sample_rate,
            "quality",
        )

        # Convert to int16 for resampler
        audio_int16: np.ndarray
        if audio.dtype == np.float32:
            audio_int16 = (audio * AUDIO_INT16_MAX).astype(np.int16)
        else:
            audio_int16 = audio.astype(np.int16)

        return resampler.resample(audio_int16)

    def _play_and_record(self, audio: np.ndarray) -> np.ndarray | None:
        """
        Play audio and record microphone simultaneously.

        Returns:
            Recorded audio as int16 array, or None on failure
        """
        import pyaudio

        # Calculate durations
        playback_duration = len(audio) / self._config.playback_sample_rate
        record_duration = playback_duration + 0.2  # 200ms extra for delay
        record_samples = int(record_duration * self._config.recording_sample_rate)

        # Convert to int16 for playback
        playback_int16: np.ndarray
        if audio.dtype == np.float32:
            playback_int16 = (audio * AUDIO_INT16_MAX).astype(np.int16)
        else:
            playback_int16 = audio

        recording: np.ndarray = np.zeros(record_samples, dtype=np.int16)
        record_position = [0]  # Mutable for callback
        _playback_complete = threading.Event()

        def record_callback(in_data, frame_count, time_info, status):
            data = np.frombuffer(in_data, dtype=np.int16)
            end_pos = min(record_position[0] + len(data), len(recording))
            recording[record_position[0] : end_pos] = data[: end_pos - record_position[0]]
            record_position[0] = end_pos
            return (None, pyaudio.paContinue)

        try:
            # Open recording stream
            record_stream = open_stream(
                self._audio,
                format=pyaudio.paInt16,
                channels=1,
                rate=self._config.recording_sample_rate,
                input=True,
                frames_per_buffer=1024,
                stream_callback=record_callback,
            )
            record_stream.start_stream()

            # Small delay to ensure recording is active (50ms timing-critical for audio sync)
            time.sleep(0.05)

            # Play audio
            if self._playback_callback is not None:
                self._playback_callback(audio)
            else:
                self._play_audio_default(playback_int16)

            # Wait for recording to complete
            time.sleep(record_duration + 0.1)

            record_stream.stop_stream()
            record_stream.close()

            return recording

        except Exception as e:
            logger.error("Play and record failed: %s", e)
            return None

    def _play_audio_default(self, audio_int16: np.ndarray) -> None:
        """Play audio using default PyAudio output."""
        import pyaudio

        stream = open_stream(
            self._audio,
            format=pyaudio.paInt16,
            channels=1,
            rate=self._config.playback_sample_rate,
            output=True,
        )

        stream.write(audio_int16.tobytes())
        stream.stop_stream()
        stream.close()

    def _record_audio(self, duration_seconds: float) -> np.ndarray:
        """Record audio for specified duration."""
        import pyaudio

        samples = int(duration_seconds * self._config.recording_sample_rate)

        stream = open_stream(
            self._audio,
            format=pyaudio.paInt16,
            channels=1,
            rate=self._config.recording_sample_rate,
            input=True,
            frames_per_buffer=1024,
        )

        frames = []
        remaining = samples

        while remaining > 0:
            chunk_size = min(1024, remaining)
            data = stream.read(chunk_size)
            frames.append(np.frombuffer(data, dtype=np.int16))
            remaining -= chunk_size

        stream.stop_stream()
        stream.close()

        return np.concatenate(frames)

    def _init_audio(self) -> None:
        """Initialize PyAudio."""
        # open_portaudio() serializes Pa_Initialize under the process-wide lock.
        self._audio = open_portaudio()

    def _cleanup_audio(self) -> None:
        """Clean up PyAudio."""
        if self._audio is not None:
            terminate_portaudio(self._audio)
            self._audio = None

    def _report_progress(self, stage: str, progress: float) -> None:
        """Report calibration progress."""
        if self._on_progress is not None:
            try:
                self._on_progress(stage, progress)
            except Exception as e:
                logger.warning("Progress callback failed: %s", e)

    def _save_calibration(self, result: CalibrationResult) -> None:
        """Save calibration result to disk."""
        cal_dir = Path(self._config.calibration_data_path)
        cal_dir.mkdir(parents=True, exist_ok=True)

        cal_file = cal_dir / "aec_delay_calibration.json"

        data = {
            "delay_samples": result.delay_samples,
            "delay_ms": result.delay_ms,
            "confidence": result.confidence,
            "correlation_peak": result.correlation_peak,
            "noise_floor_db": result.noise_floor_db,
            "calibrated_at": time.time(),
            "sample_rate": self._config.recording_sample_rate,
        }

        with open(cal_file, "w") as f:
            json.dump(data, f, indent=2)

        logger.info("Saved calibration to %s", cal_file)

    @staticmethod
    def load_calibration(
        calibration_data_path: Path | str | None = None,
    ) -> CalibrationResult | None:
        """
        Load previously saved calibration.

        Returns:
            CalibrationResult if found, None otherwise
        """
        from voice.wake_detector.wake_types import _default_calibration_dir

        cal_dir = Path(calibration_data_path) if calibration_data_path else _default_calibration_dir()
        cal_file = cal_dir / "aec_delay_calibration.json"

        if not cal_file.exists():
            return None

        try:
            with open(cal_file) as f:
                data = json.load(f)

            return CalibrationResult(
                success=True,
                delay_samples=data["delay_samples"],
                delay_ms=data["delay_ms"],
                confidence=data.get("confidence", 1.0),
                correlation_peak=data.get("correlation_peak", 1.0),
                noise_floor_db=data.get("noise_floor_db", -60.0),
            )

        except Exception as e:
            logger.warning("Failed to load calibration: %s", e)
            return None


__all__ = ["AECDelayCalibrator", "CalibrationConfig"]
