from __future__ import annotations

import torch

class Resample:
    def __init__(self, orig_freq: int, new_freq: int) -> None: ...
    def __call__(self, waveform: torch.Tensor) -> torch.Tensor: ...

class MelSpectrogram:
    def __init__(
        self,
        *,
        sample_rate: int,
        n_fft: int,
        hop_length: int,
        win_length: int,
        n_mels: int,
        power: float,
    ) -> None: ...
    def __call__(self, waveform: torch.Tensor) -> torch.Tensor: ...
