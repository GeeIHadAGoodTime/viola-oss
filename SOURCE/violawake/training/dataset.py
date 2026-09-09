"""
ViolaWake Dataset Classes
==========================

Dataset and augmentation utilities for training.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from violawake.audio import compute_features, load_audio, pad_or_trim
from violawake.config import CLIP_SAMPLES, SAMPLE_RATE
from violawake.training.augmentation import AugmentationPipeline

if TYPE_CHECKING:
    import torch
    from torch.utils.data import Dataset as TorchDataset

    # Type alias for the base class
    _DatasetBase = TorchDataset[tuple[torch.Tensor, torch.Tensor]]
else:
    # At runtime, get Dataset from torch if available
    try:
        from torch.utils.data import Dataset as _DatasetBase
    except ImportError:
        _DatasetBase = object


class AudioAugmenter:
    """
    Apply augmentations to audio samples.

    Augmentations:
        - Gain variation (+/- 6dB)
        - Background noise at random SNR (5-20 dB)
        - Time shift
        - Normalization
    """

    def __init__(self, noise_files: list[Path]):
        """
        Initialize augmenter.

        Args:
            noise_files: List of paths to noise audio files
        """
        self.noise_files = noise_files
        self.noise_cache: dict[str, np.ndarray] = {}

    def _get_noise(self) -> np.ndarray | None:
        """Get a random noise sample."""
        if not self.noise_files:
            return None

        noise_path = random.choice(self.noise_files)
        path_str = str(noise_path)

        if path_str not in self.noise_cache:
            noise = load_audio(noise_path)
            if noise is not None:
                self.noise_cache[path_str] = noise

        return self.noise_cache.get(path_str)

    def augment(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply random augmentations to audio.

        Args:
            audio: Input audio samples

        Returns:
            Augmented audio samples
        """
        # Gain variation (+/- 6dB)
        if random.random() < 0.5:
            gain_db = random.uniform(-6, 6)
            audio = audio * (10 ** (gain_db / 20))

        # Add noise
        if random.random() < 0.5 and self.noise_files:
            noise = self._get_noise()
            if noise is not None:
                # Match length
                noise = pad_or_trim(noise, len(audio))
                # Random SNR (5-20 dB)
                snr_db = random.uniform(5, 20)
                audio_power = np.mean(audio**2) + 1e-10
                noise_power = np.mean(noise**2) + 1e-10
                scale = np.sqrt(audio_power / (noise_power * (10 ** (snr_db / 10))))
                audio = audio + noise * scale

        # Time shift
        if random.random() < 0.3:
            shift = random.randint(-int(SAMPLE_RATE * 0.1), int(SAMPLE_RATE * 0.1))
            audio = np.roll(audio, shift)

        # Normalize
        max_val: float = float(np.max(np.abs(audio)))
        if max_val > 0:
            audio = audio / max_val * 0.95

        return audio


class WakeWordDataset(_DatasetBase):
    """
    PyTorch Dataset for wake word training.

    Loads audio files, applies augmentation, and computes mel spectrograms.
    Requires PyTorch to be installed for actual use.

    Supports two augmentation modes:
    - Basic: ``AudioAugmenter`` (gain, noise, time shift, normalize)
    - Advanced: ``AugmentationPipeline`` (time/freq domain, environmental,
      degradation transforms)

    When ``advanced_augmenter`` is provided and ``use_advanced_augmentation``
    is True, the advanced pipeline takes precedence over the basic augmenter.
    """

    positive_files: list[Path]
    negative_files: list[Path]
    augmenter: AudioAugmenter | None
    advanced_augmenter: AugmentationPipeline | None
    use_advanced_augmentation: bool
    augmentation_factor: int
    samples: list[tuple[Path, int]]
    _torch_module: object

    def __init__(
        self,
        positive_files: list[Path],
        negative_files: list[Path],
        augmenter: AudioAugmenter | None = None,
        augmentation_factor: int = 3,
        advanced_augmenter: AugmentationPipeline | None = None,
        use_advanced_augmentation: bool = True,
    ):
        """
        Initialize dataset.

        Args:
            positive_files: Paths to positive (wake word) audio files
            negative_files: Paths to negative audio files
            augmenter: Optional basic augmenter for data augmentation
            augmentation_factor: How many copies of each positive sample
            advanced_augmenter: Optional advanced AugmentationPipeline.
                If provided and use_advanced_augmentation is True, this
                takes precedence over the basic augmenter.
            use_advanced_augmentation: Whether to use the advanced pipeline
                when an advanced_augmenter is provided. Defaults to True.

        Raises:
            ImportError: If PyTorch is not installed
        """
        # Import torch at init time to allow the class to be defined without torch
        if "torch" not in sys.modules:
            try:
                import torch as _torch

                self._torch_module = _torch
            except ImportError as e:
                raise ImportError("PyTorch required. Install with: pip install torch") from e
        else:
            import torch as _torch

            self._torch_module = _torch

        # Call parent __init__ if it's a real Dataset (not object)
        if _DatasetBase is not object:
            super().__init__()

        self.positive_files = positive_files
        self.negative_files = negative_files
        self.augmenter = augmenter
        self.advanced_augmenter = advanced_augmenter
        self.use_advanced_augmentation = use_advanced_augmentation
        self.augmentation_factor = augmentation_factor

        # Create samples list
        self.samples = []

        # Add positives (with augmentation factor)
        for f in positive_files:
            for _ in range(augmentation_factor):
                self.samples.append((f, 1))

        # Add negatives (balance dataset)
        n_positives = len(positive_files) * augmentation_factor
        n_negatives = min(len(negative_files), n_positives * 2)
        neg_subset = random.sample(negative_files, n_negatives) if len(negative_files) > n_negatives else negative_files
        for f in neg_subset:
            self.samples.append((f, 0))

        random.shuffle(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, torch.Tensor]:
        """Get a sample by index."""
        path, label = self.samples[idx]

        # Load audio
        audio = load_audio(path)
        if audio is None:
            audio = np.zeros(CLIP_SAMPLES)

        # Pad/trim
        audio = pad_or_trim(audio, CLIP_SAMPLES)

        # Determine whether to augment this sample
        should_augment = label == 1 or random.random() < 0.3

        # Advanced augmentation takes precedence when available and enabled
        _use_advanced = self.advanced_augmenter is not None and self.use_advanced_augmentation

        if should_augment and _use_advanced:
            # Time-domain augmentation via advanced pipeline
            # (pipeline has its own per-transform probability checks)
            audio = self.advanced_augmenter.augment(audio)
        elif should_augment and self.augmenter is not None:
            # Fallback to basic augmenter
            audio = self.augmenter.augment(audio)

        # Re-pad/trim after augmentation (time stretch can change length)
        if len(audio) != CLIP_SAMPLES:
            audio = pad_or_trim(audio, CLIP_SAMPLES)

        # Compute features (respects FEATURE_TYPE config)
        # Shape is (n_mels, time_frames) from compute_features
        mel = compute_features(audio)

        # Apply frequency-domain augmentation on spectrogram if advanced pipeline
        if should_augment and _use_advanced:
            # SpecAugment/FrequencyWarp expect (n_frames, n_mels), but our mel
            # is (n_mels, time_frames). Transpose before and after.
            mel_transposed = mel.T  # -> (time_frames, n_mels)
            mel_transposed = self.advanced_augmenter.augment_spectrogram(mel_transposed)
            mel = mel_transposed.T  # -> (n_mels, time_frames)

        # Use the cached torch module
        import torch

        return torch.from_numpy(mel).float(), torch.tensor(label).float()

    def set_external_noise(self, noise_dir: Path) -> None:
        """
        Load external noise files into the advanced augmenter's BackgroundMixer.

        Args:
            noise_dir: Directory containing WAV noise files

        Raises:
            RuntimeError: If no advanced augmenter is configured
        """
        if self.advanced_augmenter is None:
            raise RuntimeError("Cannot set external noise: no advanced_augmenter configured")

        from violawake.training.augmentation import load_noise_dataset

        noise_paths = load_noise_dataset(noise_dir)
        if noise_paths:
            self.advanced_augmenter.set_backgrounds([str(p) for p in noise_paths])

    @property
    def num_positives(self) -> int:
        """Number of positive samples."""
        return sum(1 for _, label in self.samples if label == 1)

    @property
    def num_negatives(self) -> int:
        """Number of negative samples."""
        return sum(1 for _, label in self.samples if label == 0)
