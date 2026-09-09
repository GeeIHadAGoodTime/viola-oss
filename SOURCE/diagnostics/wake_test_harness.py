"""
Wake Word Test Harness & Regression Framework
==============================================

Comprehensive testing infrastructure for wake word detection:
- Synthetic test case generation (noise, clipping, interference)
- Golden sample regression testing
- Threshold sensitivity analysis
- Performance benchmarking
- Automated test scenarios

Usage:
    from diagnostics.wake_test_harness import (
        WakeTestHarness,
        SyntheticTestGenerator,
        RegressionTestRunner,
        run_regression_suite,
    )

    # Generate synthetic test cases
    generator = SyntheticTestGenerator()
    noisy_audio = generator.add_noise(clean_audio, snr_db=10)

    # Run regression tests
    runner = RegressionTestRunner()
    results = runner.run_all()

    # Full test harness
    harness = WakeTestHarness()
    report = harness.run_full_test_suite()
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from config.constants import AUDIO_SAMPLE_RATE
from core.logging_config import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)

# Optional soundfile
try:
    import soundfile as sf

    SOUNDFILE_AVAILABLE = True
except ImportError:
    SOUNDFILE_AVAILABLE = False
    sf = None


# =============================================================================
# Test Result Types
# =============================================================================


class TestStatus(Enum):
    """Test execution status."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class TestCase:
    """Individual test case definition."""

    test_id: str
    name: str
    description: str
    category: str  # "synthetic", "regression", "performance", "threshold"

    # Test audio
    audio_path: Path | None = None
    audio_samples: np.ndarray | None = None
    sample_rate: int = AUDIO_SAMPLE_RATE

    # Expected outcome
    expected_detection: bool = True
    expected_score_min: float | None = None
    expected_score_max: float | None = None

    # Test parameters
    parameters: dict[str, Any] = field(default_factory=dict)

    # Metadata
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class TestResult:
    """Result from running a test case."""

    test_id: str
    status: TestStatus
    execution_time_ms: float

    # Detection results
    detected: bool = False
    score: float = 0.0
    all_scores: list[float] = field(default_factory=list)

    # Comparison with expected
    detection_correct: bool = False
    score_in_range: bool = False

    # Details
    message: str = ""
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "test_id": self.test_id,
            "status": self.status.value,
            "execution_time_ms": self.execution_time_ms,
            "detected": self.detected,
            "score": self.score,
            "detection_correct": self.detection_correct,
            "score_in_range": self.score_in_range,
            "message": self.message,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class TestSuiteResult:
    """Results from running a test suite."""

    suite_name: str
    started_at: float
    completed_at: float
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0

    results: list[TestResult] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """Calculate success rate."""
        if self.total_tests == 0:
            return 0.0
        return self.passed / self.total_tests * 100

    @property
    def duration_seconds(self) -> float:
        """Total suite duration."""
        return self.completed_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "suite_name": self.suite_name,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
            "total_tests": self.total_tests,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "success_rate": self.success_rate,
            "results": [r.to_dict() for r in self.results],
            "summary": self.summary,
        }


# =============================================================================
# Synthetic Test Generator
# =============================================================================


class SyntheticTestGenerator:
    """
    Generate synthetic test audio with controlled degradations.

    Supports:
    - Additive noise at various SNR levels
    - Clipping/distortion
    - Echo/reverb simulation
    - Music interference
    - Gain variations
    - Sample rate conversion artifacts
    """

    def __init__(self, sample_rate: int = AUDIO_SAMPLE_RATE):
        """Initialize generator."""
        self._sample_rate = sample_rate
        self._rng = np.random.default_rng(42)  # Reproducible

    def add_white_noise(
        self,
        audio: np.ndarray,
        snr_db: float = 20.0,
    ) -> np.ndarray:
        """
        Add white noise at specified SNR.

        Args:
            audio: Clean audio samples
            snr_db: Signal-to-noise ratio in dB

        Returns:
            Audio with added noise
        """
        signal_power = np.mean(audio**2)
        noise_power = signal_power / (10 ** (snr_db / 10))
        noise = self._rng.normal(0, np.sqrt(noise_power), len(audio)).astype(np.float32)
        return audio + noise

    def add_pink_noise(
        self,
        audio: np.ndarray,
        snr_db: float = 20.0,
    ) -> np.ndarray:
        """
        Add pink (1/f) noise at specified SNR.

        Args:
            audio: Clean audio samples
            snr_db: Signal-to-noise ratio in dB

        Returns:
            Audio with added pink noise
        """
        # Generate pink noise using Voss-McCartney algorithm
        n = len(audio)
        num_rows = 16
        array = self._rng.standard_normal((num_rows, n // num_rows + 1))
        pink = np.cumsum(array, axis=0)[-1, :n].astype(np.float32)

        # Normalize to target SNR
        signal_power = np.mean(audio**2)
        noise_power = signal_power / (10 ** (snr_db / 10))
        pink = pink * np.sqrt(noise_power / np.mean(pink**2))

        return audio + pink

    def add_clipping(
        self,
        audio: np.ndarray,
        clip_ratio: float = 0.1,
    ) -> np.ndarray:
        """
        Add hard clipping to simulate overdriven input.

        Args:
            audio: Audio samples
            clip_ratio: Fraction of samples to clip (0-1)

        Returns:
            Clipped audio
        """
        # Find threshold that clips the specified ratio
        sorted_abs = np.sort(np.abs(audio))
        threshold_idx = int(len(sorted_abs) * (1 - clip_ratio))
        threshold = sorted_abs[threshold_idx]

        return np.clip(audio, -threshold, threshold)

    def add_soft_clipping(
        self,
        audio: np.ndarray,
        drive: float = 2.0,
    ) -> np.ndarray:
        """
        Add soft clipping (tanh saturation).

        Args:
            audio: Audio samples
            drive: Drive amount (higher = more saturation)

        Returns:
            Soft-clipped audio
        """
        return np.tanh(audio * drive) / np.tanh(drive)

    def add_echo(
        self,
        audio: np.ndarray,
        delay_ms: float = 100.0,
        decay: float = 0.3,
    ) -> np.ndarray:
        """
        Add echo/reflection.

        Args:
            audio: Audio samples
            delay_ms: Echo delay in milliseconds
            decay: Echo amplitude (0-1)

        Returns:
            Audio with echo
        """
        delay_samples = int(delay_ms * self._sample_rate / 1000)
        echo = np.zeros(len(audio) + delay_samples, dtype=np.float32)
        echo[: len(audio)] = audio
        echo[delay_samples:] += audio * decay
        return echo[: len(audio)]

    def add_reverb(
        self,
        audio: np.ndarray,
        room_size: float = 0.5,
        damping: float = 0.5,
    ) -> np.ndarray:
        """
        Add simple reverb effect.

        Args:
            audio: Audio samples
            room_size: Room size (0-1)
            damping: High-frequency damping (0-1)

        Returns:
            Audio with reverb
        """
        # Simple multi-tap delay reverb
        delays_ms = [23, 37, 53, 79, 97, 127]
        decays = [0.7, 0.6, 0.5, 0.4, 0.3, 0.2]

        result = audio.copy()
        for delay_ms, decay in zip(delays_ms, decays):
            adjusted_delay = delay_ms * room_size
            adjusted_decay = decay * (1 - damping * 0.5)
            result = self.add_echo(result, adjusted_delay, adjusted_decay)

        return result

    def add_music_interference(
        self,
        audio: np.ndarray,
        interference_level: float = 0.3,
        frequency_hz: float = 440.0,
    ) -> np.ndarray:
        """
        Add simulated music interference (sine wave + harmonics).

        Args:
            audio: Audio samples
            interference_level: Interference amplitude (0-1)
            frequency_hz: Base frequency

        Returns:
            Audio with music interference
        """
        t = np.arange(len(audio)) / self._sample_rate

        # Fundamental + harmonics
        interference = np.sin(2 * np.pi * frequency_hz * t)
        interference += 0.5 * np.sin(2 * np.pi * frequency_hz * 2 * t)
        interference += 0.25 * np.sin(2 * np.pi * frequency_hz * 3 * t)

        # Normalize and scale
        interference = interference.astype(np.float32)
        interference = interference / np.max(np.abs(interference)) * interference_level

        return audio + interference

    def apply_gain(
        self,
        audio: np.ndarray,
        gain_db: float = 0.0,
    ) -> np.ndarray:
        """
        Apply gain adjustment.

        Args:
            audio: Audio samples
            gain_db: Gain in dB (positive = louder)

        Returns:
            Gained audio
        """
        gain_linear = 10 ** (gain_db / 20)
        return audio * gain_linear

    def simulate_aec_residual(
        self,
        audio: np.ndarray,
        residual_level: float = 0.1,
        delay_ms: float = 50.0,
    ) -> np.ndarray:
        """
        Simulate AEC residual echo (imperfect cancellation).

        Args:
            audio: Audio samples (as if reference leaking through)
            residual_level: Level of residual echo (0-1)
            delay_ms: Echo delay in milliseconds

        Returns:
            Audio with AEC residual
        """
        # Create reference-like signal
        reference = self.add_music_interference(
            np.zeros_like(audio),
            interference_level=1.0,
        )

        # Add delayed attenuated version as residual
        delay_samples = int(delay_ms * self._sample_rate / 1000)
        residual = np.zeros_like(audio)
        if delay_samples < len(audio):
            residual[delay_samples:] = reference[:-delay_samples] * residual_level

        return audio + residual

    def generate_test_variants(
        self,
        base_audio: np.ndarray,
    ) -> dict[str, np.ndarray]:
        """
        Generate standard test variants from base audio.

        Returns dict of variant_name -> audio
        """
        return {
            "clean": base_audio.copy(),
            "snr_30db": self.add_white_noise(base_audio, 30),
            "snr_20db": self.add_white_noise(base_audio, 20),
            "snr_10db": self.add_white_noise(base_audio, 10),
            "snr_5db": self.add_white_noise(base_audio, 5),
            "clipped_5pct": self.add_clipping(base_audio, 0.05),
            "clipped_20pct": self.add_clipping(base_audio, 0.20),
            "echo_100ms": self.add_echo(base_audio, 100, 0.3),
            "echo_200ms": self.add_echo(base_audio, 200, 0.5),
            "music_low": self.add_music_interference(base_audio, 0.1),
            "music_high": self.add_music_interference(base_audio, 0.5),
            "gain_minus10db": self.apply_gain(base_audio, -10),
            "gain_minus20db": self.apply_gain(base_audio, -20),
            "reverb_small": self.add_reverb(base_audio, 0.3, 0.5),
            "reverb_large": self.add_reverb(base_audio, 0.8, 0.3),
            "aec_residual_low": self.simulate_aec_residual(base_audio, 0.05),
            "aec_residual_high": self.simulate_aec_residual(base_audio, 0.2),
        }


# =============================================================================
# Regression Test Runner
# =============================================================================


class RegressionTestRunner:
    """
    Run regression tests against golden samples.

    Loads test cases from a directory structure:
        golden_samples/
            positive/     # Expected to trigger
                sample1.wav
                sample1.json
            negative/     # Expected NOT to trigger
                noise1.wav
                noise1.json
    """

    DEFAULT_GOLDEN_DIR = Path("tests/wake_word/golden_samples")

    def __init__(
        self,
        golden_dir: Path | None = None,
        detector_factory: Callable[[], Any] | None = None,
    ):
        """
        Initialize regression runner.

        Args:
            golden_dir: Directory containing golden samples
            detector_factory: Factory to create detector instances
        """
        self._golden_dir = Path(golden_dir or self.DEFAULT_GOLDEN_DIR)
        self._detector_factory = detector_factory
        self._detector: Any = None
        self._lock = threading.Lock()

    def _get_detector(self) -> Any:
        """Get or create detector instance."""
        if self._detector is not None:
            return self._detector

        if self._detector_factory:
            self._detector = self._detector_factory()
        else:
            try:
                from voice.wake_detector.violawake_listener import ViolaWakeListener

                self._detector = ViolaWakeListener()
            except ImportError:
                logger.warning("ViolaWakeListener not available")
                return None

        return self._detector

    def load_test_cases(self) -> list[TestCase]:
        """Load test cases from golden directory."""
        test_cases: list[TestCase] = []

        if not self._golden_dir.exists():
            logger.warning("Golden directory does not exist: %s", self._golden_dir)
            return test_cases

        # Load positive samples
        positive_dir = self._golden_dir / "positive"
        if positive_dir.exists():
            for wav_file in positive_dir.glob("*.wav"):
                meta_file = wav_file.with_suffix(".json")
                meta = {}
                if meta_file.exists():
                    with open(meta_file) as f:
                        meta = json.load(f)

                test_cases.append(
                    TestCase(
                        test_id=f"positive_{wav_file.stem}",
                        name=wav_file.stem,
                        description=meta.get("description", "Positive sample"),
                        category="regression",
                        audio_path=wav_file,
                        expected_detection=True,
                        expected_score_min=meta.get("expected_score_min", 0.5),
                        parameters=meta,
                        tags=["positive", "regression"],
                    )
                )

        # Load negative samples
        negative_dir = self._golden_dir / "negative"
        if negative_dir.exists():
            for wav_file in negative_dir.glob("*.wav"):
                meta_file = wav_file.with_suffix(".json")
                meta = {}
                if meta_file.exists():
                    with open(meta_file) as f:
                        meta = json.load(f)

                test_cases.append(
                    TestCase(
                        test_id=f"negative_{wav_file.stem}",
                        name=wav_file.stem,
                        description=meta.get("description", "Negative sample"),
                        category="regression",
                        audio_path=wav_file,
                        expected_detection=False,
                        expected_score_max=meta.get("expected_score_max", 0.5),
                        parameters=meta,
                        tags=["negative", "regression"],
                    )
                )

        return test_cases

    def run_test(self, test_case: TestCase) -> TestResult:
        """Run a single test case."""
        start_time = time.time()

        # Load audio
        if test_case.audio_samples is not None:
            audio = test_case.audio_samples
        elif test_case.audio_path and SOUNDFILE_AVAILABLE:
            try:
                audio, _ = sf.read(str(test_case.audio_path))
                audio = audio.astype(np.float32)
            except Exception as e:
                return TestResult(
                    test_id=test_case.test_id,
                    status=TestStatus.ERROR,
                    execution_time_ms=(time.time() - start_time) * 1000,
                    error=f"Failed to load audio: {e}",
                )
        else:
            return TestResult(
                test_id=test_case.test_id,
                status=TestStatus.SKIPPED,
                execution_time_ms=(time.time() - start_time) * 1000,
                message="No audio available",
            )

        # Get detector
        detector = self._get_detector()
        if detector is None:
            return TestResult(
                test_id=test_case.test_id,
                status=TestStatus.SKIPPED,
                execution_time_ms=(time.time() - start_time) * 1000,
                message="Detector not available",
            )

        # Process audio
        all_scores: list[float] = []
        try:
            frame_size = 512
            num_frames = len(audio) // frame_size

            for i in range(num_frames):
                frame = audio[i * frame_size : (i + 1) * frame_size]

                score = 0.0
                if hasattr(detector, "process_frame"):
                    result = detector.process_frame(frame)
                    score = result.get("score", 0.0) if isinstance(result, dict) else 0.0
                elif hasattr(detector, "get_score"):
                    score = detector.get_score(frame)

                all_scores.append(score)

        except Exception as e:
            return TestResult(
                test_id=test_case.test_id,
                status=TestStatus.ERROR,
                execution_time_ms=(time.time() - start_time) * 1000,
                error=f"Detection error: {e}",
            )

        # Analyze results
        max_score = max(all_scores) if all_scores else 0.0
        threshold = 0.5  # Default threshold
        detected = max_score >= threshold

        # Check against expectations
        detection_correct = detected == test_case.expected_detection

        score_in_range = True
        if test_case.expected_score_min is not None:
            score_in_range = score_in_range and (max_score >= test_case.expected_score_min)
        if test_case.expected_score_max is not None:
            score_in_range = score_in_range and (max_score <= test_case.expected_score_max)

        # Determine status
        if detection_correct and score_in_range:
            status = TestStatus.PASSED
            message = "Detection matched expectations"
        else:
            status = TestStatus.FAILED
            messages = []
            if not detection_correct:
                messages.append(f"Expected detection={test_case.expected_detection}, got {detected}")
            if not score_in_range:
                messages.append(
                    f"Score {max_score:.3f} out of range "
                    f"[{test_case.expected_score_min}, {test_case.expected_score_max}]"
                )
            message = "; ".join(messages)

        return TestResult(
            test_id=test_case.test_id,
            status=status,
            execution_time_ms=(time.time() - start_time) * 1000,
            detected=detected,
            score=max_score,
            all_scores=all_scores,
            detection_correct=detection_correct,
            score_in_range=score_in_range,
            message=message,
        )

    def run_all(
        self,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> TestSuiteResult:
        """Run all regression tests."""
        test_cases = self.load_test_cases()

        suite = TestSuiteResult(
            suite_name="regression",
            started_at=time.time(),
            completed_at=0,
            total_tests=len(test_cases),
        )

        for i, test_case in enumerate(test_cases):
            result = self.run_test(test_case)
            suite.results.append(result)

            if result.status == TestStatus.PASSED:
                suite.passed += 1
            elif result.status == TestStatus.FAILED:
                suite.failed += 1
            elif result.status == TestStatus.SKIPPED:
                suite.skipped += 1
            else:
                suite.errors += 1

            if progress_callback:
                progress_callback(i + 1, len(test_cases))

        suite.completed_at = time.time()

        logger.info(
            "Regression suite complete: %d/%d passed (%.1f%%)",
            suite.passed,
            suite.total_tests,
            suite.success_rate,
        )

        return suite


# =============================================================================
# Threshold Sensitivity Analyzer
# =============================================================================


class ThresholdAnalyzer:
    """
    Analyze detection sensitivity across threshold values.

    Useful for finding optimal thresholds that maximize
    true positive rate while minimizing false positives.
    """

    def __init__(
        self,
        detector_factory: Callable[[], Any] | None = None,
    ):
        """Initialize analyzer."""
        self._detector_factory = detector_factory

    def analyze_threshold_sensitivity(
        self,
        positive_samples: list[np.ndarray],
        negative_samples: list[np.ndarray],
        thresholds: list[float] | None = None,
    ) -> dict[str, Any]:
        """
        Analyze detection performance across threshold values.

        Args:
            positive_samples: Audio samples that should trigger
            negative_samples: Audio samples that should NOT trigger
            thresholds: List of thresholds to test

        Returns:
            Analysis results with TPR/FPR at each threshold
        """
        if thresholds is None:
            thresholds = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]

        # Get scores for all samples
        positive_scores = self._get_max_scores(positive_samples)
        negative_scores = self._get_max_scores(negative_samples)

        results = {
            "thresholds": [],
            "true_positive_rates": [],
            "false_positive_rates": [],
            "accuracy": [],
            "f1_scores": [],
        }

        for threshold in thresholds:
            # True positives: positive samples above threshold
            tp = sum(1 for s in positive_scores if s >= threshold)
            fn = len(positive_scores) - tp

            # False positives: negative samples above threshold
            fp = sum(1 for s in negative_scores if s >= threshold)
            tn = len(negative_scores) - fp

            # Calculate metrics
            tpr = tp / max(tp + fn, 1)  # Sensitivity/Recall
            fpr = fp / max(fp + tn, 1)  # 1 - Specificity
            accuracy = (tp + tn) / max(tp + tn + fp + fn, 1)

            precision = tp / max(tp + fp, 1)
            f1 = 2 * precision * tpr / max(precision + tpr, 1e-10)

            results["thresholds"].append(threshold)
            results["true_positive_rates"].append(tpr)
            results["false_positive_rates"].append(fpr)
            results["accuracy"].append(accuracy)
            results["f1_scores"].append(f1)

        # Find optimal threshold (highest F1)
        best_idx = np.argmax(results["f1_scores"])
        results["optimal_threshold"] = results["thresholds"][best_idx]
        results["optimal_f1"] = results["f1_scores"][best_idx]

        return results

    def _get_max_scores(self, samples: list[np.ndarray]) -> list[float]:
        """Get maximum detection score for each sample."""
        scores = []

        detector = None
        if self._detector_factory:
            detector = self._detector_factory()
        else:
            try:
                from voice.wake_detector.violawake_listener import ViolaWakeListener

                detector = ViolaWakeListener()
            except ImportError:
                pass

        if detector is None:
            return [0.0] * len(samples)

        for audio in samples:
            frame_scores = []
            frame_size = 512
            num_frames = len(audio) // frame_size

            for i in range(num_frames):
                frame = audio[i * frame_size : (i + 1) * frame_size]
                try:
                    if hasattr(detector, "process_frame"):
                        result = detector.process_frame(frame)
                        score = result.get("score", 0.0) if isinstance(result, dict) else 0.0
                    elif hasattr(detector, "get_score"):
                        score = detector.get_score(frame)
                    else:
                        score = 0.0
                    frame_scores.append(score)
                except Exception:
                    frame_scores.append(0.0)

            scores.append(max(frame_scores) if frame_scores else 0.0)

        return scores


# =============================================================================
# Performance Benchmarker
# =============================================================================


@dataclass
class PerformanceBenchmark:
    """Performance benchmark results."""

    total_frames: int = 0
    total_time_ms: float = 0.0
    avg_frame_time_ms: float = 0.0
    max_frame_time_ms: float = 0.0
    min_frame_time_ms: float = 0.0
    p95_frame_time_ms: float = 0.0
    p99_frame_time_ms: float = 0.0

    frames_over_budget: int = 0
    budget_ms: float = 32.0  # 512 samples at 16kHz

    realtime_factor: float = 0.0  # <1.0 means faster than realtime


class PerformanceBenchmarker:
    """
    Benchmark wake detection performance.

    Measures:
    - Per-frame processing time
    - Percentile latencies
    - Real-time factor
    - Budget violations
    """

    def __init__(
        self,
        detector_factory: Callable[[], Any] | None = None,
    ):
        """Initialize benchmarker."""
        self._detector_factory = detector_factory

    def run_benchmark(
        self,
        audio: np.ndarray,
        frame_size: int = 512,
        iterations: int = 1,
    ) -> PerformanceBenchmark:
        """
        Run performance benchmark.

        Args:
            audio: Audio samples to process
            frame_size: Frame size in samples
            iterations: Number of times to process (for averaging)

        Returns:
            PerformanceBenchmark results
        """
        detector = None
        if self._detector_factory:
            detector = self._detector_factory()
        else:
            try:
                from voice.wake_detector.violawake_listener import ViolaWakeListener

                detector = ViolaWakeListener()
            except ImportError:
                pass

        if detector is None:
            return PerformanceBenchmark()

        frame_times: list[float] = []
        num_frames = len(audio) // frame_size

        for _ in range(iterations):
            for i in range(num_frames):
                frame = audio[i * frame_size : (i + 1) * frame_size]

                start = time.perf_counter()
                try:
                    if hasattr(detector, "process_frame"):
                        detector.process_frame(frame)
                    elif hasattr(detector, "get_score"):
                        detector.get_score(frame)
                except Exception as _e:
                    logger.debug("Detector frame error during benchmark (frame skipped): %s", _e)
                elapsed_ms = (time.perf_counter() - start) * 1000
                frame_times.append(elapsed_ms)

        if not frame_times:
            return PerformanceBenchmark()

        # Calculate statistics
        frame_times_arr = np.array(frame_times)
        budget_ms = frame_size / AUDIO_SAMPLE_RATE * 1000  # Time available

        benchmark = PerformanceBenchmark(
            total_frames=len(frame_times),
            total_time_ms=np.sum(frame_times_arr),
            avg_frame_time_ms=np.mean(frame_times_arr),
            max_frame_time_ms=np.max(frame_times_arr),
            min_frame_time_ms=np.min(frame_times_arr),
            p95_frame_time_ms=float(np.percentile(frame_times_arr, 95)),
            p99_frame_time_ms=float(np.percentile(frame_times_arr, 99)),
            frames_over_budget=int(np.sum(frame_times_arr > budget_ms)),
            budget_ms=budget_ms,
            realtime_factor=np.mean(frame_times_arr) / budget_ms,
        )

        return benchmark


# =============================================================================
# Full Test Harness
# =============================================================================


class WakeTestHarness:
    """
    Complete test harness combining all test capabilities.

    Provides:
    - Synthetic test generation
    - Regression testing
    - Threshold analysis
    - Performance benchmarking
    - Report generation
    """

    def __init__(
        self,
        golden_dir: Path | None = None,
        output_dir: Path | None = None,
        detector_factory: Callable[[], Any] | None = None,
    ):
        """
        Initialize test harness.

        Args:
            golden_dir: Directory for golden samples
            output_dir: Directory for test outputs
            detector_factory: Factory for creating detectors
        """
        self._golden_dir = Path(golden_dir or RegressionTestRunner.DEFAULT_GOLDEN_DIR)
        self._output_dir = Path(output_dir or "logs/wake_tests")
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._detector_factory = detector_factory

        self._generator = SyntheticTestGenerator()
        self._regression = RegressionTestRunner(golden_dir, detector_factory)
        self._threshold = ThresholdAnalyzer(detector_factory)
        self._benchmark = PerformanceBenchmarker(detector_factory)

    def run_full_test_suite(
        self,
        include_synthetic: bool = True,
        include_regression: bool = True,
        include_threshold: bool = True,
        include_benchmark: bool = True,
    ) -> dict[str, Any]:
        """
        Run complete test suite.

        Returns:
            Dictionary with all test results
        """
        report: dict[str, Any] = {
            "started_at": time.time(),
            "completed_at": 0,
            "suites": {},
        }

        # Regression tests
        if include_regression:
            logger.info("Running regression tests...")
            regression_result = self._regression.run_all()
            report["suites"]["regression"] = regression_result.to_dict()

        # Synthetic tests (if base audio available)
        if include_synthetic:
            logger.info("Running synthetic tests...")
            synthetic_result = self._run_synthetic_suite()
            report["suites"]["synthetic"] = synthetic_result

        # Threshold analysis
        if include_threshold:
            logger.info("Running threshold analysis...")
            threshold_result = self._run_threshold_analysis()
            report["suites"]["threshold"] = threshold_result

        # Performance benchmark
        if include_benchmark:
            logger.info("Running performance benchmark...")
            benchmark_result = self._run_performance_benchmark()
            report["suites"]["benchmark"] = asdict(benchmark_result)

        report["completed_at"] = time.time()
        report["duration_seconds"] = report["completed_at"] - report["started_at"]

        # Save report
        report_path = self._output_dir / f"test_report_{int(time.time())}.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        logger.info("Test report saved: %s", report_path)

        return report

    def _run_synthetic_suite(self) -> dict[str, Any]:
        """Run synthetic test suite."""
        # Try to load a base sample
        positive_dir = self._golden_dir / "positive"
        base_audio = None

        if positive_dir.exists() and SOUNDFILE_AVAILABLE:
            wav_files = list(positive_dir.glob("*.wav"))
            if wav_files:
                try:
                    base_audio, _ = sf.read(str(wav_files[0]))
                    base_audio = base_audio.astype(np.float32)
                except Exception as _e:
                    logger.debug("Failed to load base audio sample %s: %s", wav_files[0], _e)

        if base_audio is None:
            return {"skipped": True, "reason": "No base audio available"}

        # Generate variants
        variants = self._generator.generate_test_variants(base_audio)

        results = {
            "base_file": str(wav_files[0]) if wav_files else None,
            "variants_tested": len(variants),
            "variant_results": {},
        }

        for variant_name, audio in variants.items():
            test_case = TestCase(
                test_id=f"synthetic_{variant_name}",
                name=variant_name,
                description=f"Synthetic variant: {variant_name}",
                category="synthetic",
                audio_samples=audio,
                expected_detection=True,  # Derived from positive sample
            )
            result = self._regression.run_test(test_case)
            results["variant_results"][variant_name] = result.to_dict()

        return results

    def _run_threshold_analysis(self) -> dict[str, Any]:
        """Run threshold sensitivity analysis."""
        positive_samples: list[np.ndarray] = []
        negative_samples: list[np.ndarray] = []

        if not SOUNDFILE_AVAILABLE:
            return {"skipped": True, "reason": "soundfile not available"}

        # Load positive samples
        positive_dir = self._golden_dir / "positive"
        if positive_dir.exists():
            for wav_file in positive_dir.glob("*.wav"):
                try:
                    audio, _ = sf.read(str(wav_file))
                    positive_samples.append(audio.astype(np.float32))
                except Exception as _e:
                    logger.debug("Skipping unreadable positive sample %s: %s", wav_file, _e)

        # Load negative samples
        negative_dir = self._golden_dir / "negative"
        if negative_dir.exists():
            for wav_file in negative_dir.glob("*.wav"):
                try:
                    audio, _ = sf.read(str(wav_file))
                    negative_samples.append(audio.astype(np.float32))
                except Exception as _e:
                    logger.debug("Skipping unreadable negative sample %s: %s", wav_file, _e)

        if not positive_samples or not negative_samples:
            return {
                "skipped": True,
                "reason": "Need both positive and negative samples",
            }

        return self._threshold.analyze_threshold_sensitivity(
            positive_samples,
            negative_samples,
        )

    def _run_performance_benchmark(self) -> PerformanceBenchmark:
        """Run performance benchmark."""
        # Generate synthetic audio for benchmarking
        duration_seconds = 10
        num_samples = duration_seconds * AUDIO_SAMPLE_RATE
        benchmark_audio = np.random.randn(num_samples).astype(np.float32) * 0.1

        return self._benchmark.run_benchmark(benchmark_audio, iterations=3)


# =============================================================================
# Convenience Functions
# =============================================================================


def run_regression_suite(
    golden_dir: Path | None = None,
) -> TestSuiteResult:
    """Run regression test suite."""
    runner = RegressionTestRunner(golden_dir)
    return runner.run_all()


def run_full_test_suite(
    golden_dir: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run complete test suite."""
    harness = WakeTestHarness(golden_dir, output_dir)
    return harness.run_full_test_suite()


__all__ = [
    "PerformanceBenchmark",
    "PerformanceBenchmarker",
    "RegressionTestRunner",
    "SyntheticTestGenerator",
    "TestCase",
    "TestResult",
    "TestStatus",
    "TestSuiteResult",
    "ThresholdAnalyzer",
    "WakeTestHarness",
    "run_full_test_suite",
    "run_regression_suite",
]
