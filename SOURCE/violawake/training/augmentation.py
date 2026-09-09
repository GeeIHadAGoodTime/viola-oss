"""
Training Data Augmentation
===========================

Audio augmentation pipeline for robust wake word model training.

AUGMENTATION TYPES:
1. Time Domain:
   - Time stretching (speed variations)
   - Pitch shifting
   - Volume perturbation
   - Additive noise

2. Frequency Domain:
   - SpecAugment (frequency/time masking)
   - Frequency warping

3. Environmental:
   - Room impulse response convolution
   - Background noise mixing
   - Reverberation

4. Signal Degradation:
   - Low-pass filtering (phone quality)
   - Quantization noise
   - Clipping simulation

USAGE:
    from violawake.training.augmentation import (
        AugmentationPipeline,
        AugmentationConfig,
    )

    pipeline = AugmentationPipeline(config)
    augmented = pipeline.augment(audio)
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.constants import AUDIO_INT16_SCALE, SAMPLE_RATE_16K
from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class AugmentationConfig:
    """Configuration for audio augmentation."""

    # Enable/disable categories
    enable_time_domain: bool = True
    enable_frequency_domain: bool = True
    enable_environmental: bool = True
    enable_degradation: bool = True

    # Time domain
    time_stretch_range: tuple[float, float] = (0.9, 1.1)
    pitch_shift_range: tuple[float, float] = (-2.0, 2.0)  # semitones
    volume_range: tuple[float, float] = (0.7, 1.3)
    noise_snr_range: tuple[float, float] = (20.0, 40.0)  # dB

    # Frequency domain (SpecAugment)
    freq_mask_max_bands: int = 10
    freq_mask_num: int = 2
    time_mask_max_frames: int = 20
    time_mask_num: int = 2

    # Environmental
    reverb_decay_range: tuple[float, float] = (0.1, 0.5)
    background_volume_range: tuple[float, float] = (0.05, 0.2)
    use_real_rir: bool = False  # Use real RIR files instead of simple comb filter

    # Degradation
    lowpass_cutoff_range: tuple[float, float] = (3000.0, 7000.0)
    clip_threshold_range: tuple[float, float] = (0.3, 0.9)

    # Probability of applying each augmentation
    augmentation_prob: float = 0.5

    # Reproducibility
    seed: int | None = None


class TimeStretch:
    """Time stretching without pitch change."""

    def __init__(self, rate_range: tuple[float, float] = (0.9, 1.1)):
        self._rate_range = rate_range

    def __call__(self, audio: np.ndarray, rate: float | None = None) -> np.ndarray:
        """
        Apply time stretching.

        Args:
            audio: Input audio
            rate: Stretch rate (None = random from range)

        Returns:
            Time-stretched audio
        """
        if rate is None:
            rate = random.uniform(*self._rate_range)

        if abs(rate - 1.0) < 0.01:
            return audio

        # Simple resampling-based stretch
        original_length = len(audio)
        target_length = int(original_length / rate)

        if target_length == original_length:
            return audio

        # Use linear interpolation for simplicity
        indices = np.linspace(0, original_length - 1, target_length)
        stretched = np.interp(indices, np.arange(original_length), audio)

        return stretched.astype(np.float32)


class PitchShift:
    """Pitch shifting using simple resampling."""

    def __init__(self, semitone_range: tuple[float, float] = (-2.0, 2.0)):
        self._semitone_range = semitone_range

    def __call__(
        self,
        audio: np.ndarray,
        semitones: float | None = None,
    ) -> np.ndarray:
        """
        Apply pitch shift.

        Args:
            audio: Input audio
            semitones: Shift amount (None = random)

        Returns:
            Pitch-shifted audio (same length as input)
        """
        if semitones is None:
            semitones = random.uniform(*self._semitone_range)

        if abs(semitones) < 0.1:
            return audio

        # Pitch shift ratio
        ratio = 2 ** (semitones / 12.0)

        # Resample to change pitch
        new_length = int(len(audio) / ratio)
        if new_length < 1:
            return audio

        indices = np.linspace(0, len(audio) - 1, new_length)
        resampled: np.ndarray = np.asarray(np.interp(indices, np.arange(len(audio)), audio))

        # Stretch back to original length
        if len(resampled) != len(audio):
            indices = np.linspace(0, len(resampled) - 1, len(audio))
            resampled = np.asarray(np.interp(indices, np.arange(len(resampled)), resampled))

        return resampled.astype(np.float32)


class VolumePerturb:
    """Volume perturbation."""

    def __init__(self, range_: tuple[float, float] = (0.7, 1.3)):
        self._range = range_

    def __call__(self, audio: np.ndarray, gain: float | None = None) -> np.ndarray:
        """Apply volume change."""
        if gain is None:
            gain = random.uniform(*self._range)

        return (audio * gain).astype(np.float32)


class AdditiveNoise:
    """Add Gaussian noise at specified SNR."""

    def __init__(self, snr_range: tuple[float, float] = (20.0, 40.0)):
        self._snr_range = snr_range

    def __call__(self, audio: np.ndarray, snr_db: float | None = None) -> np.ndarray:
        """
        Add noise at specified SNR.

        Args:
            audio: Input audio
            snr_db: Signal-to-noise ratio in dB

        Returns:
            Noisy audio
        """
        if snr_db is None:
            snr_db = random.uniform(*self._snr_range)

        # Signal power
        signal_power = np.mean(audio**2)
        if signal_power < 1e-10:
            return audio

        # Noise power for target SNR
        snr_linear = 10 ** (snr_db / 10)
        noise_power = signal_power / snr_linear

        # Generate noise
        noise = np.random.randn(len(audio)).astype(np.float32)
        noise *= np.sqrt(noise_power)

        return (audio + noise).astype(np.float32)


class SpecAugment:
    """
    SpecAugment: frequency and time masking on spectrograms.

    Reference: Park et al., "SpecAugment: A Simple Data Augmentation
    Method for Automatic Speech Recognition"
    """

    def __init__(
        self,
        freq_mask_param: int = 10,
        freq_mask_num: int = 2,
        time_mask_param: int = 20,
        time_mask_num: int = 2,
    ):
        self._freq_mask_param = freq_mask_param
        self._freq_mask_num = freq_mask_num
        self._time_mask_param = time_mask_param
        self._time_mask_num = time_mask_num

    def __call__(self, spectrogram: np.ndarray) -> np.ndarray:
        """
        Apply SpecAugment to spectrogram.

        Args:
            spectrogram: Input [n_frames, n_mels]

        Returns:
            Augmented spectrogram
        """
        spec = spectrogram.copy()
        n_frames, n_mels = spec.shape

        # Frequency masking
        for _ in range(self._freq_mask_num):
            f = random.randint(0, self._freq_mask_param)
            f0 = random.randint(0, max(0, n_mels - f))
            spec[:, f0 : f0 + f] = 0

        # Time masking
        for _ in range(self._time_mask_num):
            t = random.randint(0, self._time_mask_param)
            t0 = random.randint(0, max(0, n_frames - t))
            spec[t0 : t0 + t, :] = 0

        return spec


class FrequencyWarp:
    """Frequency warping for speaker variation simulation."""

    def __init__(self, warp_range: tuple[float, float] = (0.9, 1.1)):
        self._warp_range = warp_range

    def __call__(
        self,
        spectrogram: np.ndarray,
        factor: float | None = None,
    ) -> np.ndarray:
        """
        Apply frequency warping.

        Args:
            spectrogram: Input [n_frames, n_mels]
            factor: Warp factor (None = random)

        Returns:
            Warped spectrogram
        """
        if factor is None:
            factor = random.uniform(*self._warp_range)

        if abs(factor - 1.0) < 0.01:
            return spectrogram

        n_frames, n_mels = spectrogram.shape
        warped = np.zeros_like(spectrogram)

        # Warp frequency axis
        orig_bins = np.arange(n_mels)
        warped_bins = np.power(orig_bins / n_mels, factor) * n_mels

        for t in range(n_frames):
            warped[t] = np.interp(orig_bins, warped_bins, spectrogram[t])

        return warped


class SimpleReverb:
    """Simple reverberation using exponential decay."""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE_16K,
        decay_range: tuple[float, float] = (0.1, 0.5),
    ):
        self._sample_rate = sample_rate
        self._decay_range = decay_range

    def __call__(
        self,
        audio: np.ndarray,
        decay: float | None = None,
    ) -> np.ndarray:
        """
        Apply simple reverberation.

        Args:
            audio: Input audio
            decay: RT60-like decay factor

        Returns:
            Reverberant audio
        """
        if decay is None:
            decay = random.uniform(*self._decay_range)

        # Simple comb filter reverb
        delay_samples = int(0.03 * self._sample_rate)  # 30ms
        feedback = decay * 0.5

        output = audio.copy()
        for i in range(delay_samples, len(audio)):
            output[i] += feedback * output[i - delay_samples]

        # Normalize to prevent clipping
        max_val: float = float(np.max(np.abs(output)))
        if max_val > 1.0:
            output = output / max_val * 0.95

        return output.astype(np.float32)


class RealRoomReverb:
    """
    Convolution reverb using real Room Impulse Response (RIR) files.

    Applies reverb by convolving audio with a randomly selected RIR,
    producing more realistic room acoustics than the simple comb filter.
    """

    def __init__(self, rir_dir: Path | None = None):
        self._rir_files: list[Path] = []
        self._rir_cache: dict[str, np.ndarray] = {}
        if rir_dir is not None:
            self.load_rirs(rir_dir)

    def load_rirs(self, rir_dir: Path) -> None:
        """Load RIR file paths from directory."""
        self._rir_files = load_noise_dataset(rir_dir)
        logger.info("RealRoomReverb loaded %d RIR files from %s", len(self._rir_files), rir_dir)

    def _load_rir(self, path: Path) -> np.ndarray | None:
        """Load a single RIR WAV file."""
        path_str = str(path)
        if path_str in self._rir_cache:
            return self._rir_cache[path_str]
        try:
            import wave

            with wave.open(str(path), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                audio = np.frombuffer(frames, dtype=np.int16)
                rir = audio.astype(np.float32) / AUDIO_INT16_SCALE
                # Normalize RIR to unit energy
                energy = np.sqrt(np.sum(rir**2))
                if energy > 1e-8:
                    rir = rir / energy
                self._rir_cache[path_str] = rir
                return rir
        except (EOFError, OSError, RuntimeError, ValueError, wave.Error) as exc:
            logger.debug("Failed to load RIR file '%s': %s", path, exc)
            return None

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        """
        Apply convolution reverb using a random RIR.

        Args:
            audio: Input audio (float32)

        Returns:
            Reverberant audio (same length as input)
        """
        if not self._rir_files:
            return audio

        rir_path = random.choice(self._rir_files)
        rir = self._load_rir(rir_path)
        if rir is None:
            return audio

        try:
            from scipy.signal import fftconvolve

            output = fftconvolve(audio, rir, mode="full")[: len(audio)]
        except ImportError:
            # Fallback: direct convolution (slower)
            output = np.convolve(audio, rir, mode="full")[: len(audio)]

        # Normalize to prevent clipping
        max_val: float = float(np.max(np.abs(output)))
        if max_val > 1.0:
            output = output / max_val * 0.95

        return output.astype(np.float32)


class BackgroundMixer:
    """Mix background noise/music into audio."""

    def __init__(
        self,
        background_files: list[str | Path] | None = None,
        volume_range: tuple[float, float] = (0.05, 0.2),
    ):
        self._background_files = background_files or []
        self._volume_range = volume_range
        self._backgrounds: list[np.ndarray] = []

    def load_backgrounds(self) -> None:
        """Load background files into memory."""
        for path in self._background_files:
            try:
                # Simple WAV loading
                bg = self._load_wav(path)
                if bg is not None:
                    self._backgrounds.append(bg)
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("Failed to load background %s: %s", path, exc)

    def _load_wav(self, path: str | Path) -> np.ndarray | None:
        """Load WAV file."""
        try:
            import wave

            with wave.open(str(path), "rb") as wav:
                frames = wav.readframes(wav.getnframes())
                audio = np.frombuffer(frames, dtype=np.int16)
                return audio.astype(np.float32) / AUDIO_INT16_SCALE
        except (EOFError, OSError, RuntimeError, ValueError, wave.Error) as exc:
            logger.debug("Failed to load WAV file '%s': %s", path, exc)
            return None

    def __call__(
        self,
        audio: np.ndarray,
        volume: float | None = None,
    ) -> np.ndarray:
        """
        Mix background into audio.

        Args:
            audio: Input audio
            volume: Background volume (None = random)

        Returns:
            Mixed audio
        """
        if not self._backgrounds:
            # Generate random noise as fallback
            return self._add_random_background(audio, volume)

        if volume is None:
            volume = random.uniform(*self._volume_range)

        # Select random background
        bg = random.choice(self._backgrounds)

        # Get segment of same length
        if len(bg) > len(audio):
            start = random.randint(0, len(bg) - len(audio))
            bg_segment = bg[start : start + len(audio)]
        else:
            # Loop if too short
            repeats = (len(audio) // len(bg)) + 1
            bg_segment = np.tile(bg, repeats)[: len(audio)]

        return (audio + volume * bg_segment).astype(np.float32)

    def _add_random_background(
        self,
        audio: np.ndarray,
        volume: float | None = None,
    ) -> np.ndarray:
        """Add random noise as background."""
        if volume is None:
            volume = random.uniform(*self._volume_range)

        # Pink noise approximation
        white = np.random.randn(len(audio))

        # Simple low-pass for pink-ish noise
        from scipy.signal import butter, filtfilt

        try:
            b, a = butter(2, 0.1)
            pink = filtfilt(b, a, white)
        except ImportError:
            pink = white

        pink = pink / np.max(np.abs(pink)) * volume

        return (audio + pink).astype(np.float32)


class LowPassFilter:
    """Low-pass filter for phone quality simulation."""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE_16K,
        cutoff_range: tuple[float, float] = (3000.0, 7000.0),
    ):
        self._sample_rate = sample_rate
        self._cutoff_range = cutoff_range

    def __call__(
        self,
        audio: np.ndarray,
        cutoff: float | None = None,
    ) -> np.ndarray:
        """
        Apply low-pass filter.

        Args:
            audio: Input audio
            cutoff: Cutoff frequency in Hz

        Returns:
            Filtered audio
        """
        if cutoff is None:
            cutoff = random.uniform(*self._cutoff_range)

        nyquist = self._sample_rate / 2
        if cutoff >= nyquist:
            return audio

        try:
            from scipy.signal import butter, filtfilt

            normalized_cutoff = cutoff / nyquist
            b, a = butter(4, normalized_cutoff, btype="low")
            filtered = filtfilt(b, a, audio)
            return filtered.astype(np.float32)

        except ImportError:
            # Simple moving average as fallback
            window_size = max(1, int(self._sample_rate / cutoff / 2))
            kernel = np.ones(window_size) / window_size
            filtered = np.convolve(audio, kernel, mode="same")
            return filtered.astype(np.float32)


class ClippingSimulator:
    """Simulate audio clipping/distortion."""

    def __init__(self, threshold_range: tuple[float, float] = (0.3, 0.9)):
        self._threshold_range = threshold_range

    def __call__(
        self,
        audio: np.ndarray,
        threshold: float | None = None,
    ) -> np.ndarray:
        """
        Apply clipping.

        Args:
            audio: Input audio
            threshold: Clip threshold (0-1)

        Returns:
            Clipped audio
        """
        if threshold is None:
            threshold = random.uniform(*self._threshold_range)

        return np.clip(audio, -threshold, threshold).astype(np.float32)


class QuantizationNoise:
    """Add quantization noise to simulate low-quality ADC."""

    def __init__(self, bits_range: tuple[int, int] = (8, 14)):
        self._bits_range = bits_range

    def __call__(self, audio: np.ndarray, bits: int | None = None) -> np.ndarray:
        """
        Apply quantization.

        Args:
            audio: Input audio (float32)
            bits: Bit depth

        Returns:
            Quantized audio
        """
        if bits is None:
            bits = random.randint(*self._bits_range)

        levels = 2**bits
        quantized = np.round(audio * (levels / 2)) / (levels / 2)
        return quantized.astype(np.float32)


def load_noise_dataset(noise_dir: Path) -> list[Path]:
    """
    Recursively find all WAV files in a directory.

    Args:
        noise_dir: Directory to search for .wav files

    Returns:
        List of paths to WAV files found
    """
    if not noise_dir.exists():
        logger.warning("Noise directory does not exist: %s", noise_dir)
        return []

    wav_files = sorted(noise_dir.rglob("*.wav"))
    logger.info("Found %d WAV files in %s", len(wav_files), noise_dir)
    return wav_files


class AugmentationPipeline:
    """
    Complete augmentation pipeline for wake word training.
    """

    def __init__(self, config: AugmentationConfig | None = None):
        """
        Initialize augmentation pipeline.

        Args:
            config: Augmentation configuration
        """
        self._config = config or AugmentationConfig()

        if self._config.seed is not None:
            random.seed(self._config.seed)
            np.random.seed(self._config.seed)

        # Time domain augmentations
        self._time_stretch = TimeStretch(self._config.time_stretch_range)
        self._pitch_shift = PitchShift(self._config.pitch_shift_range)
        self._volume = VolumePerturb(self._config.volume_range)
        self._noise = AdditiveNoise(self._config.noise_snr_range)

        # Frequency domain
        self._spec_augment = SpecAugment(
            freq_mask_param=self._config.freq_mask_max_bands,
            freq_mask_num=self._config.freq_mask_num,
            time_mask_param=self._config.time_mask_max_frames,
            time_mask_num=self._config.time_mask_num,
        )
        self._freq_warp = FrequencyWarp()

        # Environmental
        self._reverb = SimpleReverb(decay_range=self._config.reverb_decay_range)
        self._real_reverb: RealRoomReverb | None = None
        self._background = BackgroundMixer(volume_range=self._config.background_volume_range)

        # Degradation
        self._lowpass = LowPassFilter(cutoff_range=self._config.lowpass_cutoff_range)
        self._clip = ClippingSimulator(threshold_range=self._config.clip_threshold_range)
        self._quantize = QuantizationNoise()

        logger.info(
            "AugmentationPipeline initialized: time=%s, freq=%s, env=%s, degrade=%s",
            self._config.enable_time_domain,
            self._config.enable_frequency_domain,
            self._config.enable_environmental,
            self._config.enable_degradation,
        )

    def augment(
        self,
        audio: np.ndarray,
        augmentations: list[str] | None = None,
    ) -> np.ndarray:
        """
        Apply augmentations to audio.

        Args:
            audio: Input audio (float32)
            augmentations: Specific augmentations to apply (None = random)

        Returns:
            Augmented audio
        """
        # Convert to float32 if needed
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / AUDIO_INT16_SCALE
        elif audio.dtype != np.float32:
            audio = audio.astype(np.float32)

        result = audio.copy()

        # Time domain augmentations
        if self._config.enable_time_domain:
            if self._should_apply():
                result = self._time_stretch(result)

            if self._should_apply():
                result = self._pitch_shift(result)

            if self._should_apply():
                result = self._volume(result)

            if self._should_apply():
                result = self._noise(result)

        # Environmental augmentations
        if self._config.enable_environmental:
            if self._should_apply():
                if self._config.use_real_rir and self._real_reverb is not None:
                    result = self._real_reverb(result)
                else:
                    result = self._reverb(result)

            if self._should_apply():
                result = self._background(result)

        # Degradation augmentations
        if self._config.enable_degradation:
            if self._should_apply():
                result = self._lowpass(result)

            if self._should_apply():
                result = self._clip(result)

            if self._should_apply():
                result = self._quantize(result)

        return result

    def augment_spectrogram(self, spectrogram: np.ndarray) -> np.ndarray:
        """
        Apply frequency-domain augmentations to spectrogram.

        Args:
            spectrogram: Input [n_frames, n_mels]

        Returns:
            Augmented spectrogram
        """
        result = spectrogram.copy()

        if self._config.enable_frequency_domain:
            if self._should_apply():
                result = self._spec_augment(result)

            if self._should_apply():
                result = self._freq_warp(result)

        return result

    def _should_apply(self) -> bool:
        """Check if augmentation should be applied."""
        return random.random() < self._config.augmentation_prob

    def set_backgrounds(self, paths: list[str | Path]) -> None:
        """Set background noise files."""
        self._background._background_files = list(paths)
        self._background.load_backgrounds()

    def set_rir_files(self, rir_dir: Path) -> None:
        """
        Load real RIR files for convolution reverb.

        Args:
            rir_dir: Directory containing RIR WAV files
        """
        self._real_reverb = RealRoomReverb(rir_dir)
        if self._real_reverb._rir_files:
            self._config.use_real_rir = True
            logger.info(
                "Real RIR reverb enabled with %d files",
                len(self._real_reverb._rir_files),
            )
        else:
            logger.warning("No RIR files found in %s, keeping simple reverb", rir_dir)


def create_training_augmentor(
    mild: bool = False,
    aggressive: bool = False,
) -> AugmentationPipeline:
    """
    Create augmentation pipeline with preset configs.

    Args:
        mild: Use mild augmentation (for fine-tuning)
        aggressive: Use aggressive augmentation (for robustness)

    Returns:
        Configured AugmentationPipeline
    """
    if mild:
        config = AugmentationConfig(
            time_stretch_range=(0.95, 1.05),
            pitch_shift_range=(-1.0, 1.0),
            volume_range=(0.85, 1.15),
            noise_snr_range=(30.0, 50.0),
            freq_mask_max_bands=5,
            freq_mask_num=1,
            time_mask_max_frames=10,
            time_mask_num=1,
            augmentation_prob=0.3,
        )
    elif aggressive:
        config = AugmentationConfig(
            time_stretch_range=(0.8, 1.2),
            pitch_shift_range=(-4.0, 4.0),
            volume_range=(0.5, 1.5),
            noise_snr_range=(10.0, 30.0),
            freq_mask_max_bands=15,
            freq_mask_num=3,
            time_mask_max_frames=30,
            time_mask_num=3,
            reverb_decay_range=(0.2, 0.7),
            lowpass_cutoff_range=(2000.0, 6000.0),
            augmentation_prob=0.7,
        )
    else:
        config = AugmentationConfig()  # Default moderate

    return AugmentationPipeline(config)


__all__ = [
    "AdditiveNoise",
    "AugmentationConfig",
    "AugmentationPipeline",
    "BackgroundMixer",
    "ClippingSimulator",
    "FrequencyWarp",
    "LowPassFilter",
    "PitchShift",
    "QuantizationNoise",
    "RealRoomReverb",
    "SimpleReverb",
    "SpecAugment",
    "TimeStretch",
    "VolumePerturb",
    "create_training_augmentor",
    "load_noise_dataset",
]
