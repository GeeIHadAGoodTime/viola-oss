"""Viola's audio-I/O adapter for the optional DeepFilterNet runtime.

Original implementation under Viola's Apache-2.0 terms. It uses SoundFile for
audio files and SciPy polyphase resampling, without depending on torchaudio.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from scipy.signal import resample_poly


@dataclass(frozen=True)
class AudioMetaData:
    sample_rate: int
    num_frames: int
    num_channels: int
    bits_per_sample: int
    encoding: str


def load_audio(file: str, sr: int | None = None, verbose: bool = True, **kwargs):
    info = sf.info(file)
    subtype_bits = {"PCM_16": 16, "PCM_24": 24, "PCM_32": 32, "FLOAT": 32, "DOUBLE": 64, "PCM_U8": 8, "PCM_S8": 8}
    metadata = AudioMetaData(
        info.samplerate, info.frames, info.channels, subtype_bits.get(info.subtype, 0), info.subtype
    )
    channels_first = kwargs.pop("channels_first", True)
    frames = int(kwargs.pop("num_frames", -1))
    offset = int(kwargs.pop("frame_offset", 0))
    method = kwargs.pop("method", "sinc_fast")
    kwargs.pop("format", None)
    kwargs.pop("normalize", None)
    if kwargs:
        raise ValueError("Unsupported audio options: " + ", ".join(sorted(kwargs)))
    if frames >= 0 and sr is not None:
        frames = math.ceil(frames * info.samplerate / sr)
    data, rate = sf.read(file, start=offset, frames=frames, dtype="float32", always_2d=True)
    audio = torch.from_numpy(data.T.copy())
    if sr is not None and rate != sr:
        audio = resample(audio, rate, sr, method=method)
    return (audio if channels_first else audio.T).contiguous(), metadata


def save_audio(file, audio, sr, output_dir=None, suffix=None, log=False, dtype=torch.int16):
    target = Path(file)
    if suffix is not None:
        target = target.with_name(target.stem + "_" + suffix + target.suffix)
    if output_dir is not None:
        target = Path(output_dir) / target.name
    values = torch.as_tensor(audio).detach().cpu().numpy()
    if values.ndim == 1:
        values = values.reshape(1, -1)
    if values.ndim != 2:
        raise ValueError("Audio must have channel and sample dimensions")
    if np.issubdtype(values.dtype, np.integer):
        values = values.astype(np.float32) / 32768.0
    sf.write(target, values.T, sr, subtype="PCM_16" if dtype == torch.int16 else "FLOAT")


def get_resample_params(method: str):
    if method not in {"sinc_fast", "sinc_best", "kaiser_fast", "kaiser_best"}:
        raise ValueError("Unsupported resampling method: " + method)
    return {"window": ("kaiser", 14.769656459379492 if method.endswith("best") else 8.555504641634386)}


def resample(audio: torch.Tensor, orig_sr: int, new_sr: int, method="sinc_fast") -> torch.Tensor:
    if orig_sr <= 0 or new_sr <= 0:
        raise ValueError("Sample rates must be positive")
    if orig_sr == new_sr:
        return audio
    divisor = math.gcd(orig_sr, new_sr)
    array = audio.detach().cpu().numpy()
    result = resample_poly(array, new_sr // divisor, orig_sr // divisor, axis=-1, **get_resample_params(method))
    return torch.as_tensor(np.ascontiguousarray(result), dtype=audio.dtype, device=audio.device)


def get_test_sample(sr=48000):
    raise RuntimeError("Pass an explicit local test recording; this runtime does not download sample audio")
