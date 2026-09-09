"""
ViolaWake Audio Processing
===========================

Utilities for loading audio and computing mel spectrograms.
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np

from core.constants import AUDIO_INT16_SCALE
from core.logging_config import get_logger
from violawake.config import (
    CLIP_SAMPLES,
    F_MAX,
    F_MIN,
    FEATURE_TYPE,
    HOP_LENGTH,
    HOP_LENGTH_MEL,
    N_FFT,
    N_FFT_MEL,
    N_MELS,
    N_MELS_MEL,
    PCEN_BIAS,
    PCEN_EPS,
    PCEN_GAIN,
    PCEN_POWER,
    PCEN_TIME_CONSTANT,
    SAMPLE_RATE,
    USE_PCEN,
    WIN_LENGTH,
    WIN_LENGTH_MEL,
)

logger = get_logger(__name__)

# Runtime is ONNX-only. Torch / torchaudio are imported lazily inside
# :func:`load_audio` so packaged runtime never pays the ~500 MB RSS cost.
# Training / analysis scripts keep the multi-format loader for FLAC+MP3.


def _try_import_torchaudio() -> tuple[object, object] | None:
    # Lazy import so packaged runtime does not pay the torchaudio RSS cost.
    try:
        import torchaudio
        import torchaudio.transforms as T
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        logger.debug("Torchaudio unavailable; falling back to wave loader: %s", exc)
        return None
    return (torchaudio, T)


def load_audio(path: Path, target_sr: int = SAMPLE_RATE) -> np.ndarray | None:
    """
    Load audio file and return as numpy array.

    Args:
        path: Path to audio file (.wav, .flac, .mp3)
        target_sr: Target sample rate (default 16kHz)

    Returns:
        Audio samples as float32 numpy array, or None if failed
    """
    # Try torchaudio first (scripts / training), fall back to wave module.
    torchaudio_bundle = _try_import_torchaudio()
    if torchaudio_bundle is not None:
        torchaudio, T = torchaudio_bundle
        try:
            waveform, sr = torchaudio.load(str(path))
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)
            if sr != target_sr:
                resampler = T.Resample(sr, target_sr)
                waveform = resampler(waveform)
            return waveform.squeeze().numpy()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Torchaudio load failed for %s; falling back to wave: %s", path, exc)

    # Fallback to wave module (WAV only)
    try:
        import wave

        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            audio_int16 = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            audio_float = audio_int16.astype(np.float32) / AUDIO_INT16_SCALE
            if sr != target_sr:
                # VP-6: prefer soxr for higher quality resampling
                try:
                    import soxr  # type: ignore[import-untyped]

                    audio_float = soxr.resample(audio_float, sr, target_sr, quality="HQ")
                except ImportError:
                    from scipy import signal

                    audio_float = signal.resample(audio_float, int(len(audio_float) * target_sr / sr))
            return audio_float
    except (EOFError, OSError, RuntimeError, ValueError, wave.Error) as exc:
        logger.debug("Wave audio load failed for %s: %s", path, exc)
        return None


def pad_or_trim(audio: np.ndarray, target_length: int = CLIP_SAMPLES) -> np.ndarray:
    """
    Pad or trim audio to exact length.

    Args:
        audio: Input audio samples
        target_length: Target number of samples (default: 1.5s at 16kHz)

    Returns:
        Audio array with exactly target_length samples
    """
    if len(audio) < target_length:
        # Pad with zeros at the end
        padding = target_length - len(audio)
        audio = np.pad(audio, (0, padding), mode="constant")
    elif len(audio) > target_length:
        # Random crop for training, center crop for inference
        start = random.randint(0, len(audio) - target_length)
        audio = audio[start : start + target_length]
    return audio


def center_crop(audio: np.ndarray, target_length: int = CLIP_SAMPLES) -> np.ndarray:
    """
    Center crop or pad audio to exact length.

    Unlike pad_or_trim, this always takes the center for deterministic inference.

    Args:
        audio: Input audio samples
        target_length: Target number of samples

    Returns:
        Audio array with exactly target_length samples
    """
    if len(audio) < target_length:
        # Pad equally on both sides
        padding = target_length - len(audio)
        left = padding // 2
        right = padding - left
        audio = np.pad(audio, (left, right), mode="constant")
    elif len(audio) > target_length:
        # Center crop
        start = (len(audio) - target_length) // 2
        audio = audio[start : start + target_length]
    return audio


def compute_mel_spectrogram(audio: np.ndarray) -> np.ndarray:
    """
    Compute mel spectrogram features.

    IMPORTANT: Always uses scipy spectrogram (not torchaudio) because the
    viola_v3.onnx model was trained with scipy features. Using torchaudio
    would produce different features and cause 0.0 scores.

    Args:
        audio: Audio samples (should be CLIP_SAMPLES length)

    Returns:
        Mel spectrogram as numpy array (N_MELS x time_frames)
    """
    # Always use scipy - model was trained with scipy features
    # DO NOT use torchaudio even if available - features are incompatible!
    from scipy import signal

    _f, _t, Sxx = signal.spectrogram(audio, fs=SAMPLE_RATE, nperseg=WIN_LENGTH, noverlap=WIN_LENGTH - HOP_LENGTH)
    # Take first N_MELS frequency bins (matches training)
    return np.log(Sxx[:N_MELS, :] + 1e-9)


def _apply_pcen_manual(mel: np.ndarray) -> np.ndarray:
    """
    Apply Per-Channel Energy Normalization (PCEN) manually.

    This is a fallback when librosa.pcen is not available. Implements the
    streaming-compatible PCEN formula from Wang et al. 2017.

    Args:
        mel: Mel spectrogram power array (n_mels, time_frames)

    Returns:
        PCEN-normalized array of same shape
    """
    # IIR smoothing across time axis (axis=1)
    # Compute the smoothing coefficient from time constant and hop rate
    hop_rate = SAMPLE_RATE / HOP_LENGTH_MEL  # frames per second
    s = 1.0 - np.exp(-1.0 / (PCEN_TIME_CONSTANT * hop_rate))

    # Build the smoother via forward IIR pass
    smoother = np.zeros_like(mel)
    smoother[:, 0] = mel[:, 0]
    for t in range(1, mel.shape[1]):
        smoother[:, t] = (1.0 - s) * smoother[:, t - 1] + s * mel[:, t]

    # PCEN formula: ((mel / (eps + smoother)^gain) + bias)^power - bias^power
    gain_applied = (mel / (PCEN_EPS + smoother) ** PCEN_GAIN + PCEN_BIAS) ** PCEN_POWER
    return gain_applied - PCEN_BIAS**PCEN_POWER


def compute_mel_spectrogram_v2(audio: np.ndarray) -> np.ndarray:
    """
    Compute mel spectrogram features using librosa with full speech band coverage.

    Uses 40 mel bins spanning 60-7800 Hz, capturing formants F1/F2/F3 that
    distinguish "Viola" from other words. Optionally applies PCEN instead of
    log compression for better robustness to volume and noise variation.

    Args:
        audio: Audio samples as float32 (should be CLIP_SAMPLES length)

    Returns:
        Feature array (N_MELS_MEL x time_frames), either log-mel or PCEN-mel
    """
    try:
        import librosa  # Lazy import to avoid startup cost for inference
    except ImportError as exc:
        raise RuntimeError(
            "FEATURE_TYPE='mel' or 'mel_pcen' requires librosa-compatible mel/PCEN extraction, "
            "but librosa is not installed. Install librosa for these wake models or use a model "
            "trained with FEATURE_TYPE='linear'."
        ) from exc

    # Compute mel spectrogram with librosa
    mel = librosa.feature.melspectrogram(
        y=audio,
        sr=SAMPLE_RATE,
        n_fft=N_FFT_MEL,
        hop_length=HOP_LENGTH_MEL,
        win_length=WIN_LENGTH_MEL,
        n_mels=N_MELS_MEL,
        fmin=F_MIN,
        fmax=F_MAX,
        power=2.0,  # Power spectrogram (magnitude squared)
    )

    if USE_PCEN:
        # Try librosa.pcen first, fall back to manual implementation
        try:
            # librosa.pcen expects power spectrogram (not log)
            features = librosa.pcen(
                mel * (2**31),  # Scale to int-like range as librosa.pcen expects
                sr=SAMPLE_RATE,
                hop_length=HOP_LENGTH_MEL,
                gain=PCEN_GAIN,
                bias=PCEN_BIAS,
                power=PCEN_POWER,
                time_constant=PCEN_TIME_CONSTANT,
                eps=PCEN_EPS,
            )
        except (AttributeError, TypeError):
            # librosa.pcen not available in this version — use manual impl
            features = _apply_pcen_manual(mel)
    else:
        # Standard log compression
        features = np.log(mel + 1e-9)

    return features


def compute_features(audio: np.ndarray) -> np.ndarray:
    """
    Dispatch function for computing audio features based on FEATURE_TYPE config.

    This is the primary entry point for training scripts. It reads FEATURE_TYPE
    from config and delegates to the appropriate spectrogram function.

    Args:
        audio: Audio samples as float32 (should be CLIP_SAMPLES length)

    Returns:
        Feature array:
            - "linear": (32, time_frames) from legacy scipy spectrogram
            - "mel" or "mel_pcen": (40, time_frames) from librosa mel spectrogram

    Raises:
        ValueError: If FEATURE_TYPE is not recognized
    """
    if FEATURE_TYPE == "linear":
        return compute_mel_spectrogram(audio)
    elif FEATURE_TYPE in ("mel", "mel_pcen"):
        return compute_mel_spectrogram_v2(audio)
    else:
        raise ValueError("Unknown FEATURE_TYPE %r. Must be 'linear', 'mel', or 'mel_pcen'." % FEATURE_TYPE)


def normalize_audio(audio: np.ndarray, target_peak: float = 0.95, max_gain: float = 3.0) -> np.ndarray:
    """
    Normalize audio to target peak amplitude with gain cap.

    Args:
        audio: Input audio samples
        target_peak: Target peak value (0.0 to 1.0)
        max_gain: Maximum amplification factor (prevents quiet AEC
                  residuals from being amplified into false patterns)

    Returns:
        Normalized audio
    """
    max_val: float = float(np.max(np.abs(audio)))
    if max_val < 1e-6:
        return audio
    gain = target_peak / max_val
    gain = min(gain, max_gain)
    return audio * gain


def normalize_audio_rms(audio: np.ndarray, target_rms: float = 0.1) -> np.ndarray:
    """
    Normalize audio to target RMS level.

    This makes the model volume-independent - quiet and loud speech
    will have similar energy levels in the mel spectrogram.

    Args:
        audio: Input audio samples
        target_rms: Target RMS value (typically 0.05-0.2)

    Returns:
        RMS-normalized audio, clipped to [-1, 1]
    """
    current_rms = np.sqrt(np.mean(audio**2))
    if current_rms > 1e-6:  # Avoid division by near-zero
        scale = target_rms / current_rms
        audio = audio * scale
        # Clip to prevent clipping distortion
        audio = np.clip(audio, -1.0, 1.0)
    return audio


def compute_rms(audio: np.ndarray) -> float:
    """Compute RMS energy of audio."""
    return float(np.sqrt(np.mean(audio**2)))


def is_silent(audio: np.ndarray, threshold: float = 0.01) -> bool:
    """Check if audio is mostly silent."""
    return compute_rms(audio) < threshold
