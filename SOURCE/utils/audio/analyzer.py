"""
Audio Analysis Utilities Module
Audio analysis utilities for perceptual checks.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from core.constants import SAMPLE_RATE_44K
from core.logging_config import get_logger

logger = get_logger(__name__)


class AudioAnalyzer:
    """Audio analysis utilities for perceptual checks"""

    def __init__(self, sample_rate: int = SAMPLE_RATE_44K):
        """
        Initialize audio analyzer.

        Args:
            sample_rate: Audio sample rate (default 44.1kHz)
        """
        self.sample_rate = sample_rate

    def analyze_peak_level(self, audio: np.ndarray) -> dict[str, Any]:
        """
        Analyze peak level.

        Args:
            audio: Audio samples (numpy array)

        Returns:
            Dictionary with peak analysis results
        """
        try:
            if len(audio) == 0:
                return {
                    "peak": 0.0,
                    "peak_db": -float("inf"),
                    "clipped_samples": 0,
                    "clipping_percentage": 0.0,
                }

            # Calculate peak
            peak = float(np.max(np.abs(audio)))
            peak_db = 20 * np.log10(peak) if peak > 0 else -float("inf")

            # Detect clipping (samples at or near 1.0)
            clipped_samples = int(np.sum(np.abs(audio) >= 0.99))
            clipping_percentage = (clipped_samples / len(audio)) * 100.0 if len(audio) > 0 else 0.0

            return {
                "peak": peak,
                "peak_db": float(peak_db),
                "clipped_samples": clipped_samples,
                "clipping_percentage": clipping_percentage,
            }

        except Exception as e:
            logger.error("Error analyzing peak level: %s", e)
            return {
                "peak": 0.0,
                "peak_db": -float("inf"),
                "clipped_samples": 0,
                "clipping_percentage": 0.0,
            }

    def analyze_rms_level(self, audio: np.ndarray) -> dict[str, Any]:
        """
        Analyze RMS level.

        Args:
            audio: Audio samples (numpy array)

        Returns:
            Dictionary with RMS analysis results
        """
        try:
            if len(audio) == 0:
                return {"rms": 0.0, "rms_db": -float("inf"), "rms_variation_db": 0.0}

            # Calculate overall RMS
            if len(audio.shape) > 1:
                # Multi-channel
                rms = np.sqrt(np.mean(np.mean(audio**2, axis=1)))
            else:
                # Mono
                rms = np.sqrt(np.mean(audio**2))

            rms_db = 20 * np.log10(rms) if rms > 0 else -float("inf")

            # Calculate RMS variation (drift detection)
            segment_length = max(int(self.sample_rate * 1), 1)  # 1 second segments
            num_segments = len(audio) // segment_length

            if num_segments >= 2:
                segment_rms: list[float] = []
                for i in range(num_segments):
                    start = i * segment_length
                    end = start + segment_length
                    segment = audio[start:end]
                    if len(segment.shape) > 1:
                        seg_rms = np.sqrt(np.mean(np.mean(segment**2, axis=1)))
                    else:
                        seg_rms = np.sqrt(np.mean(segment**2))
                    if seg_rms > 0:
                        segment_rms.append(20 * np.log10(seg_rms))

                if len(segment_rms) >= 2:
                    min_rms_db = min(segment_rms)
                    max_rms_db = max(segment_rms)
                    rms_variation_db = max_rms_db - min_rms_db
                else:
                    rms_variation_db = 0.0
            else:
                rms_variation_db = 0.0

            return {
                "rms": float(rms),
                "rms_db": float(rms_db),
                "rms_variation_db": float(rms_variation_db),
            }

        except Exception as e:
            logger.error("Error analyzing RMS level: %s", e)
            return {"rms": 0.0, "rms_db": -float("inf"), "rms_variation_db": 0.0}

    def detect_clipping(self, audio: np.ndarray, threshold: float = 0.99) -> dict[str, Any]:
        """
        Detect clipping in audio.

        Args:
            audio: Audio samples (numpy array)
            threshold: Threshold for clipping detection (0.99 = 99% of max)

        Returns:
            Dictionary with clipping detection results
        """
        try:
            if len(audio) == 0:
                return {
                    "clipped_samples": 0,
                    "clipping_percentage": 0.0,
                    "max_clipped_value": 0.0,
                    "has_clipping": False,
                }

            threshold_val = float(np.clip(threshold, 0.0, 1.0))
            clipped_mask = np.abs(audio) >= threshold_val
            clipped_samples = int(np.sum(clipped_mask))
            clipping_percentage = (clipped_samples / len(audio)) * 100.0

            # Find max clipped value
            if clipped_samples > 0:
                clipped_audio = audio[clipped_mask]
                max_clipped_value = float(np.max(np.abs(clipped_audio)))
            else:
                max_clipped_value = 0.0

            return {
                "clipped_samples": clipped_samples,
                "clipping_percentage": clipping_percentage,
                "max_clipped_value": max_clipped_value,
                "has_clipping": clipped_samples > 0,
            }

        except Exception as e:
            logger.error("Error detecting clipping: %s", e)
            return {
                "clipped_samples": 0,
                "clipping_percentage": 0.0,
                "max_clipped_value": 0.0,
                "has_clipping": False,
            }

    def detect_silence(self, audio: np.ndarray, threshold_db: float = -60.0) -> dict[str, Any]:
        """
        Detect silence in audio.

        Args:
            audio: Audio samples (numpy array)
            threshold_db: Silence threshold in dB (default -60dB)

        Returns:
            Dictionary with silence detection results
        """
        try:
            if len(audio) == 0:
                return {
                    "silent_samples": len(audio),
                    "silence_percentage": 100.0,
                    "is_silent": True,
                }

            # Calculate RMS
            if len(audio.shape) > 1:
                rms = np.sqrt(np.mean(np.mean(audio**2, axis=1)))
            else:
                rms = np.sqrt(np.mean(audio**2))

            # Convert threshold to linear
            threshold_linear = 10 ** (threshold_db / 20)

            # Detect silence
            if rms < threshold_linear:
                # Entire audio is silent
                return {
                    "silent_samples": len(audio),
                    "silence_percentage": 100.0,
                    "is_silent": True,
                }

            # Check for silent segments
            segment_length = max(int(self.sample_rate * 0.1), 1)  # 100ms segments
            num_segments = len(audio) // segment_length

            silent_samples = 0
            for i in range(num_segments):
                start = int(i * segment_length)
                end = int(start + segment_length)
                segment = audio[start:end]

                if len(segment.shape) > 1:
                    seg_rms = np.sqrt(np.mean(np.mean(segment**2, axis=1)))
                else:
                    seg_rms = np.sqrt(np.mean(segment**2))

                if seg_rms < threshold_linear:
                    silent_samples += len(segment)

            silence_percentage = (silent_samples / len(audio)) * 100.0

            return {
                "silent_samples": int(silent_samples),
                "silence_percentage": silence_percentage,
                "is_silent": silence_percentage > 50.0,  # More than 50% silent
            }

        except Exception as e:
            logger.error("Error detecting silence: %s", e)
            return {"silent_samples": 0, "silence_percentage": 0.0, "is_silent": False}

    def analyze_audio_quality(self, audio: np.ndarray) -> dict[str, Any]:
        """
        Comprehensive audio quality analysis.

        Args:
            audio: Audio samples (numpy array)

        Returns:
            Dictionary with complete quality analysis
        """
        try:
            peak_analysis = self.analyze_peak_level(audio)
            rms_analysis = self.analyze_rms_level(audio)
            clipping_analysis = self.detect_clipping(audio)
            silence_analysis = self.detect_silence(audio)

            return {
                "peak": peak_analysis,
                "rms": rms_analysis,
                "clipping": clipping_analysis,
                "silence": silence_analysis,
                "quality_score": self._calculate_quality_score(
                    peak_analysis, rms_analysis, clipping_analysis, silence_analysis
                ),
            }

        except Exception as e:
            logger.error("Error analyzing audio quality: %s", e)
            return {
                "peak": {},
                "rms": {},
                "clipping": {},
                "silence": {},
                "quality_score": 0.0,
            }

    def _calculate_quality_score(self, peak: dict, rms: dict, clipping: dict, silence: dict) -> float:
        """
        Calculate overall quality score (0.0 to 1.0).

        Args:
            peak: Peak analysis results
            rms: RMS analysis results
            clipping: Clipping analysis results
            silence: Silence analysis results

        Returns:
            Quality score (0.0 = poor, 1.0 = excellent)
        """
        try:
            score = 1.0

            # Penalize clipping
            if clipping.get("has_clipping", False):
                score -= 0.3

            # Penalize excessive clipping percentage
            clipping_pct = clipping.get("clipping_percentage", 0.0)
            if clipping_pct > 1.0:
                score -= 0.2
            elif clipping_pct > 0.1:
                score -= 0.1

            # Penalize too much silence
            silence_pct = silence.get("silence_percentage", 0.0)
            if silence_pct > 50.0:
                score -= 0.2
            elif silence_pct > 20.0:
                score -= 0.1

            # Penalize RMS variation (drift)
            rms_variation = rms.get("rms_variation_db", 0.0)
            if rms_variation > 3.0:  # More than 3dB variation
                score -= 0.1
            elif rms_variation > 1.0:  # More than 1dB variation
                score -= 0.05

            # Ensure score is in valid range
            return max(0.0, min(1.0, score))

        except Exception as e:
            logger.error("Error calculating quality score: %s", e)
            return 0.5  # Default neutral score
