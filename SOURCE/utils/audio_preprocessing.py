"""
Audio preprocessing utilities for improved speech-to-text accuracy.

Includes:
- Noise reduction/suppression
- Audio normalization
- Signal enhancement
"""

from __future__ import annotations

import importlib
import wave
from pathlib import Path
from typing import Protocol, cast

import numpy as np

from core.constants import AUDIO_INT16_SCALE
from core.logging_config import get_logger

logger = get_logger(__name__)


class _NoiseReduceModule(Protocol):
    def reduce_noise(
        self,
        *,
        y: np.ndarray,
        sr: int,
        stationary: bool,
        prop_decrease: float,
    ) -> np.ndarray: ...


class _AudioSegment(Protocol):
    dBFS: float

    def apply_gain(self, gain: float) -> object: ...

    def export(self, out_f: str, *, format: str) -> object: ...


class _AudioSegmentClass(Protocol):
    def from_wav(self, file: str) -> _AudioSegment: ...


class _NormalizeFunc(Protocol):
    def __call__(self, audio: _AudioSegment, *, headroom: float = ...) -> _AudioSegment: ...


class _ScipySignalModule(Protocol):
    def butter(self, N: int, Wn: float, *, btype: str, analog: bool) -> tuple[np.ndarray, np.ndarray]: ...

    def filtfilt(self, b: np.ndarray, a: np.ndarray, x: np.ndarray) -> np.ndarray: ...


def apply_noise_reduction(
    audio_path: str | Path,
    output_path: str | Path | None = None,
    noise_reduction_strength: float = 0.5,
) -> Path:
    """
    Apply basic noise reduction to an audio file.

    This uses spectral gating to reduce background noise while preserving speech.

    Args:
        audio_path: Path to input WAV file
        output_path: Path for output file (None = overwrite input)
        noise_reduction_strength: Strength of noise reduction (0.0-1.0)

    Returns:
        Path to processed audio file
    """
    try:
        nr_module = importlib.import_module("noisereduce")
        nr = cast(_NoiseReduceModule, nr_module)

        audio_path = Path(audio_path)
        if output_path is None:
            output_path = audio_path
        else:
            output_path = Path(output_path)

        # Read the audio file
        with wave.open(str(audio_path), "rb") as wf:
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            audio_data = wf.readframes(wf.getnframes())

        # Convert to numpy array
        if sample_width == 2:  # 16-bit
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
        else:
            logger.warning("Unsupported sample width: %s, skipping noise reduction", sample_width)
            return audio_path

        # Convert to float for processing
        audio_float = audio_array.astype(np.float32) / AUDIO_INT16_SCALE

        # Apply noise reduction
        # Use the first 1 second as noise profile (or entire clip if shorter)
        noise_sample_duration = min(1.0, len(audio_float) / sample_rate)
        reduced_noise = nr.reduce_noise(
            y=audio_float,
            sr=sample_rate,
            stationary=True,
            prop_decrease=noise_reduction_strength,
        )

        # Convert back to int16
        reduced_int16 = (reduced_noise * AUDIO_INT16_SCALE).astype(np.int16)

        # Write processed audio
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(n_channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(reduced_int16.tobytes())

        logger.info("Applied noise reduction to %s", audio_path)
        return output_path

    except ImportError:
        logger.debug("noisereduce library not available. Install with: pip install noisereduce")
        return Path(audio_path)
    except Exception as e:
        logger.warning("Failed to apply noise reduction: %s", e)
        return Path(audio_path)


def normalize_audio(
    audio_path: str | Path,
    output_path: str | Path | None = None,
    target_level: float = -20.0,
) -> Path:
    """
    Normalize audio levels to improve STT accuracy.

    Args:
        audio_path: Path to input WAV file
        output_path: Path for output file (None = overwrite input)
        target_level: Target loudness in dBFS (typically -20 to -10)

    Returns:
        Path to normalized audio file
    """
    try:
        pydub_module = importlib.import_module("pydub")
        effects_module = importlib.import_module("pydub.effects")
        audio_segment = cast(_AudioSegmentClass, pydub_module.AudioSegment)
        normalize = cast(_NormalizeFunc, effects_module.normalize)

        audio_path = Path(audio_path)
        if output_path is None:
            output_path = audio_path
        else:
            output_path = Path(output_path)

        # Load audio
        audio = audio_segment.from_wav(str(audio_path))

        # Normalize
        normalized = normalize(audio, headroom=0.1)

        # Adjust to target level
        change_in_dBFS = target_level - normalized.dBFS
        normalized = cast(_AudioSegment, normalized.apply_gain(change_in_dBFS))

        # Export
        normalized.export(str(output_path), format="wav")

        logger.info("Normalized audio: %s", audio_path)
        return output_path

    except ImportError:
        logger.debug("pydub library not available. Install with: pip install pydub")
        return Path(audio_path)
    except Exception as e:
        logger.warning("Failed to normalize audio: %s", e)
        return Path(audio_path)


def preprocess_audio_for_stt(
    audio_path: str | Path,
    enable_noise_reduction: bool = True,
    enable_normalization: bool = True,
    noise_strength: float = 0.5,
) -> Path:
    """
    Apply full preprocessing pipeline to audio for optimal STT results.

    This combines noise reduction and normalization.

    Args:
        audio_path: Path to input WAV file (will be modified in-place)
        enable_noise_reduction: Whether to apply noise reduction
        enable_normalization: Whether to normalize audio levels
        noise_strength: Strength of noise reduction (0.0-1.0)

    Returns:
        Path to processed audio file
    """
    audio_path = Path(audio_path)

    # Apply noise reduction first
    if enable_noise_reduction:
        audio_path = apply_noise_reduction(audio_path, noise_reduction_strength=noise_strength)

    # Then normalize
    if enable_normalization:
        audio_path = normalize_audio(audio_path)

    return audio_path


def get_audio_info(audio_path: str | Path) -> dict[str, int | float]:
    """
    Get information about an audio file.

    Returns:
        Dictionary with audio properties
    """
    try:
        audio_path = Path(audio_path)

        with wave.open(str(audio_path), "rb") as wf:
            info = {
                "sample_rate": wf.getframerate(),
                "channels": wf.getnchannels(),
                "sample_width": wf.getsampwidth(),
                "n_frames": wf.getnframes(),
                "duration_seconds": wf.getnframes() / wf.getframerate(),
            }

        return info

    except Exception as e:
        logger.error("Failed to get audio info: %s", e)
        return {}


# Optional: High-pass filter to remove low-frequency noise (rumble, hum)
def apply_highpass_filter(audio_path: str | Path, output_path: str | Path | None = None, cutoff_freq: int = 80) -> Path:
    """
    Apply high-pass filter to remove low-frequency noise.

    Useful for removing:
    - Room rumble
    - AC hum
    - Wind noise

    Args:
        audio_path: Path to input WAV file
        output_path: Path for output file (None = overwrite input)
        cutoff_freq: High-pass cutoff frequency in Hz (typically 80-100)

    Returns:
        Path to filtered audio file
    """
    try:
        scipy_signal_module = importlib.import_module("scipy.signal")
        scipy_signal = cast(_ScipySignalModule, scipy_signal_module)
        butter = scipy_signal.butter
        filtfilt = scipy_signal.filtfilt

        audio_path = Path(audio_path)
        if output_path is None:
            output_path = audio_path
        else:
            output_path = Path(output_path)

        # Read the audio file
        with wave.open(str(audio_path), "rb") as wf:
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            audio_data = wf.readframes(wf.getnframes())

        # Convert to numpy array
        if sample_width == 2:  # 16-bit
            audio_array = np.frombuffer(audio_data, dtype=np.int16)
        else:
            logger.warning("Unsupported sample width: %s", sample_width)
            return audio_path

        # Convert to float
        audio_float = audio_array.astype(np.float32) / AUDIO_INT16_SCALE

        # Design high-pass filter
        nyquist = sample_rate / 2
        normalized_cutoff = cutoff_freq / nyquist
        b, a = butter(4, normalized_cutoff, btype="high", analog=False)

        # Apply filter
        filtered = filtfilt(b, a, audio_float)

        # Convert back to int16
        filtered_int16 = (filtered * AUDIO_INT16_SCALE).astype(np.int16)

        # Write filtered audio
        with wave.open(str(output_path), "wb") as wf:
            wf.setnchannels(n_channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(filtered_int16.tobytes())

        logger.info("Applied high-pass filter (%sHz) to %s", cutoff_freq, audio_path)
        return output_path

    except ImportError:
        logger.debug("scipy library not available. Install with: pip install scipy")
        return Path(audio_path)
    except Exception as e:
        logger.warning("Failed to apply high-pass filter: %s", e)
        return Path(audio_path)
