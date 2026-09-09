"""Post-processing for byte-capable TTS voices."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from core.constants import AUDIO_INT16_MAX

_EPS = 1.0e-12
_DB_FLOOR = -120.0


@dataclass(frozen=True, slots=True)
class AudioMetrics:
    """Simple measurement bundle for proof runs and diagnostics."""

    peak_dbfs: float
    rms_dbfs: float
    integrated_lufs: float


def as_float_mono(samples: np.ndarray) -> np.ndarray:
    """Return mono float32 samples in approximately [-1.0, 1.0]."""
    audio = np.asarray(samples)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if np.issubdtype(audio.dtype, np.integer):
        audio = audio.astype(np.float32) / float(AUDIO_INT16_MAX)
    else:
        audio = audio.astype(np.float32, copy=False)
    return np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)


def peak_dbfs(samples: np.ndarray) -> float:
    peak = float(np.max(np.abs(as_float_mono(samples)))) if len(samples) else 0.0
    if peak <= _EPS:
        return _DB_FLOOR
    return 20.0 * float(np.log10(peak))


def rms_dbfs(samples: np.ndarray) -> float:
    audio = as_float_mono(samples)
    if len(audio) == 0:
        return _DB_FLOOR
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    if rms <= _EPS:
        return _DB_FLOOR
    return 20.0 * float(np.log10(rms))


def integrated_lufs(samples: np.ndarray, sample_rate: int) -> float:
    """Approximate ITU-R BS.1770 integrated loudness for mono speech."""
    audio = as_float_mono(samples)
    if len(audio) == 0 or sample_rate <= 0:
        return _DB_FLOOR

    weighted = _apply_k_weighting(audio, sample_rate)
    square = np.square(weighted, dtype=np.float64)
    # Absolute silence gate. For short TTS clips a full relative-gated LUFS
    # meter is overkill; this keeps silence from biasing the proof metric.
    square = square[square > 1.0e-10]
    if square.size == 0:
        return _DB_FLOOR
    mean_square = float(np.mean(square))
    return -0.691 + 10.0 * float(np.log10(max(mean_square, _EPS)))


def measure_audio(samples: np.ndarray, sample_rate: int) -> AudioMetrics:
    """Measure peak, RMS, and integrated LUFS for mono speech samples."""
    audio = as_float_mono(samples)
    return AudioMetrics(
        peak_dbfs=peak_dbfs(audio),
        rms_dbfs=rms_dbfs(audio),
        integrated_lufs=integrated_lufs(audio, sample_rate),
    )


def process_voice(samples: np.ndarray, sample_rate: int, target_lufs: float) -> np.ndarray:
    """Apply Viola's broadcast-style voice chain to mono float PCM."""
    audio = as_float_mono(samples)
    if len(audio) == 0 or sample_rate <= 0:
        return audio

    audio = _high_pass(audio, sample_rate, cutoff_hz=80.0)
    audio = _de_ess(audio, sample_rate)
    audio = _compress(audio, sample_rate, threshold_db=-24.0, ratio=2.5)
    audio = _low_shelf(audio, sample_rate, cutoff_hz=200.0, gain_db=1.5)
    audio = normalize_loudness(audio, sample_rate, target_lufs)
    return peak_limit(audio, ceiling_dbfs=-1.0)


def normalize_loudness(samples: np.ndarray, sample_rate: int, target_lufs: float) -> np.ndarray:
    """Scale audio so measured integrated LUFS lands on ``target_lufs``."""
    audio = as_float_mono(samples)
    current = integrated_lufs(audio, sample_rate)
    if current <= _DB_FLOOR + 1.0:
        return audio
    gain_db = float(target_lufs) - current
    gain = 10.0 ** (gain_db / 20.0)
    return audio * np.float32(gain)


def peak_limit(samples: np.ndarray, ceiling_dbfs: float = -1.0) -> np.ndarray:
    """Fast transparent peak limiter for short TTS chunks."""
    audio = as_float_mono(samples)
    ceiling = 10.0 ** (ceiling_dbfs / 20.0)
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    if peak > ceiling:
        audio = audio * np.float32(ceiling / peak)
    return np.clip(audio, -1.0, 1.0).astype(np.float32, copy=False)


def apply_attack_declick(samples: np.ndarray, sample_rate: int, attack_ms: float = 3.0) -> np.ndarray:
    """Apply a short cosine attack ramp to remove model-start transients."""
    audio = as_float_mono(samples).copy()
    n = min(len(audio), int(sample_rate * attack_ms / 1000.0))
    if n <= 1:
        return audio
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n, dtype=np.float32))
    audio[:n] *= ramp
    return audio


def crossfade_pcm_bytes(previous: bytes | None, current: bytes, sample_rate: int, fade_ms: float = 5.0) -> bytes:
    """Blend the start of ``current`` from the previous PCM tail."""
    if not previous or not current:
        return current
    n = int(sample_rate * fade_ms / 1000.0)
    if n <= 1:
        return current

    prev_i16 = np.frombuffer(previous, dtype=np.int16)
    curr_i16 = np.frombuffer(current, dtype=np.int16).copy()
    n = min(n, len(prev_i16), len(curr_i16))
    if n <= 1:
        return current

    prev_tail = prev_i16[-n:].astype(np.float32)
    curr_head = curr_i16[:n].astype(np.float32)
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    curr_i16[:n] = np.clip(
        prev_tail * (1.0 - ramp) + curr_head * ramp,
        -AUDIO_INT16_MAX - 1,
        AUDIO_INT16_MAX,
    ).astype(np.int16)
    return curr_i16.tobytes()


def silence_pcm_bytes(sample_rate: int, duration_ms: int) -> bytes:
    """Return mono int16 silence for the requested duration."""
    if sample_rate <= 0 or duration_ms <= 0:
        return b""
    samples = round(sample_rate * duration_ms / 1000.0)
    return np.zeros(samples, dtype=np.int16).tobytes()


def _high_pass(samples: np.ndarray, sample_rate: int, cutoff_hz: float) -> np.ndarray:
    from scipy import signal

    cutoff = min(cutoff_hz, sample_rate * 0.45)
    sos = signal.butter(2, cutoff, btype="highpass", fs=sample_rate, output="sos")
    return _apply_sos(samples, sos)


def _low_shelf(samples: np.ndarray, sample_rate: int, cutoff_hz: float, gain_db: float) -> np.ndarray:
    sos = _shelf_sos(sample_rate, cutoff_hz, gain_db, shelf="low")
    return _apply_sos(samples, sos)


def _apply_k_weighting(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    from scipy import signal

    high_pass_cutoff = min(60.0, sample_rate * 0.25)
    high_shelf_cutoff = min(1500.0, sample_rate * 0.40)
    sos_hp = signal.butter(2, high_pass_cutoff, btype="highpass", fs=sample_rate, output="sos")
    weighted = _apply_sos(samples, sos_hp)
    sos_shelf = _shelf_sos(sample_rate, high_shelf_cutoff, 4.0, shelf="high")
    return _apply_sos(weighted, sos_shelf)


def _de_ess(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    from scipy import signal

    if sample_rate <= 12000:
        return samples
    low = 5000.0
    high = min(8000.0, sample_rate * 0.45)
    if high <= low:
        return samples
    sos = signal.butter(2, [low, high], btype="bandpass", fs=sample_rate, output="sos")
    band = _apply_sos(samples, sos)
    env = _smooth_abs(band, sample_rate, cutoff_hz=35.0)
    threshold = 10.0 ** (-26.0 / 20.0)
    reduction = np.clip((env - threshold) / max(threshold * 2.0, _EPS), 0.0, 1.0) * 0.45
    return (samples - band * reduction).astype(np.float32, copy=False)


def _compress(samples: np.ndarray, sample_rate: int, threshold_db: float, ratio: float) -> np.ndarray:
    env = _smooth_abs(samples, sample_rate, cutoff_hz=18.0)
    level_db = 20.0 * np.log10(np.maximum(env, _EPS))
    over_db = np.maximum(level_db - threshold_db, 0.0)
    gain_db = -over_db * (1.0 - 1.0 / ratio)
    target_gain = 10.0 ** (gain_db / 20.0)
    gain = _smooth_gain(target_gain, sample_rate, attack_ms=8.0, release_ms=90.0)
    return (samples * gain).astype(np.float32, copy=False)


def _smooth_abs(samples: np.ndarray, sample_rate: int, cutoff_hz: float) -> np.ndarray:
    from scipy import signal

    cutoff = min(cutoff_hz, sample_rate * 0.45)
    sos = signal.butter(1, cutoff, btype="lowpass", fs=sample_rate, output="sos")
    return _apply_sos(np.abs(samples).astype(np.float32, copy=False), sos)


def _smooth_gain(gain: np.ndarray, sample_rate: int, attack_ms: float, release_ms: float) -> np.ndarray:
    if len(gain) == 0:
        return gain
    attack = float(np.exp(-1.0 / max(1.0, sample_rate * attack_ms / 1000.0)))
    release = float(np.exp(-1.0 / max(1.0, sample_rate * release_ms / 1000.0)))
    out = np.empty_like(gain, dtype=np.float32)
    current = float(gain[0])
    for i, target in enumerate(gain):
        coeff = attack if target < current else release
        current = coeff * current + (1.0 - coeff) * float(target)
        out[i] = current
    return out


def _shelf_sos(sample_rate: int, cutoff_hz: float, gain_db: float, *, shelf: str) -> np.ndarray:
    omega = 2.0 * np.pi * cutoff_hz / sample_rate
    sin_w = np.sin(omega)
    cos_w = np.cos(omega)
    amp = 10.0 ** (gain_db / 40.0)
    alpha = sin_w / np.sqrt(2.0)
    beta = 2.0 * np.sqrt(amp) * alpha

    if shelf == "low":
        b0 = amp * ((amp + 1.0) - (amp - 1.0) * cos_w + beta)
        b1 = 2.0 * amp * ((amp - 1.0) - (amp + 1.0) * cos_w)
        b2 = amp * ((amp + 1.0) - (amp - 1.0) * cos_w - beta)
        a0 = (amp + 1.0) + (amp - 1.0) * cos_w + beta
        a1 = -2.0 * ((amp - 1.0) + (amp + 1.0) * cos_w)
        a2 = (amp + 1.0) + (amp - 1.0) * cos_w - beta
    else:
        b0 = amp * ((amp + 1.0) + (amp - 1.0) * cos_w + beta)
        b1 = -2.0 * amp * ((amp - 1.0) + (amp + 1.0) * cos_w)
        b2 = amp * ((amp + 1.0) + (amp - 1.0) * cos_w - beta)
        a0 = (amp + 1.0) - (amp - 1.0) * cos_w + beta
        a1 = 2.0 * ((amp - 1.0) - (amp + 1.0) * cos_w)
        a2 = (amp + 1.0) - (amp - 1.0) * cos_w - beta

    return np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]], dtype=np.float64)


def _apply_sos(samples: np.ndarray, sos: np.ndarray) -> np.ndarray:
    from scipy import signal

    if len(samples) < 16:
        return signal.sosfilt(sos, samples).astype(np.float32, copy=False)
    return signal.sosfiltfilt(sos, samples).astype(np.float32, copy=False)


__all__ = [
    "AudioMetrics",
    "apply_attack_declick",
    "as_float_mono",
    "crossfade_pcm_bytes",
    "integrated_lufs",
    "measure_audio",
    "normalize_loudness",
    "peak_dbfs",
    "peak_limit",
    "process_voice",
    "rms_dbfs",
    "silence_pcm_bytes",
]
