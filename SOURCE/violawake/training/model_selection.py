"""
ViolaWake Model Selection
==========================

Proper model selection with composite scoring for wake word training.

This module implements robust model selection that balances multiple criteria:
- Quiet performance (baseline wake word detection)
- Music robustness (detection during music playback)
- False positive rejection (not triggering on music alone)
- Temporal consistency (stable detection over time)

DO NOT just pick the model with highest recall - that leads to poor generalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import numpy as np

from core.console import console
from core.logging_config import get_logger
from violawake.audio import center_crop, compute_features, pad_or_trim
from violawake.config import CLIP_SAMPLES, HOP_LENGTH, SAMPLE_RATE

if TYPE_CHECKING:
    from violawake.model import WakeWordModel

logger = get_logger(__name__)


# ============================================================
# Validation Data Types
# ============================================================


class ValidationDataDict(TypedDict):
    """Structure for validation data sets."""

    quiet: list[tuple[np.ndarray, int]]  # (audio, label) pairs
    music_positive: list[tuple[np.ndarray, int]]  # Viola during music
    music_negative: list[tuple[np.ndarray, int]]  # Music only (should reject)


# ============================================================
# Metrics Dataclass
# ============================================================


@dataclass
class ValidationMetrics:
    """
    Metrics computed from a validation set.

    Holds confusion matrix counts and provides computed metric properties.
    """

    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        """
        Precision: TP / (TP + FP).

        How many of the positive predictions were actually positive.
        """
        denom = self.true_positives + self.false_positives
        if denom == 0:
            return 0.0
        return self.true_positives / denom

    @property
    def recall(self) -> float:
        """
        Recall: TP / (TP + FN).

        How many of the actual positives were correctly predicted.
        """
        denom = self.true_positives + self.false_negatives
        if denom == 0:
            return 0.0
        return self.true_positives / denom

    @property
    def f1(self) -> float:
        """
        F1 Score: Harmonic mean of precision and recall.

        Balances precision and recall into a single metric.
        """
        p = self.precision
        r = self.recall
        if p + r == 0:
            return 0.0
        return 2 * (p * r) / (p + r)


# ============================================================
# Model Evaluation Dataclass
# ============================================================


@dataclass
class ModelEvaluation:
    """
    Complete evaluation of a model checkpoint.

    Combines metrics from multiple validation scenarios plus temporal consistency.
    """

    checkpoint_path: Path
    quiet_metrics: ValidationMetrics
    music_positive_metrics: ValidationMetrics
    music_negative_metrics: ValidationMetrics
    temporal_consistency: float

    # Thresholds for minimum acceptable performance
    MIN_QUIET_RECALL = 0.95
    MIN_MUSIC_NEGATIVE_PRECISION = 0.99

    def compute_composite_score(self) -> float | None:
        """
        Compute composite score for model selection.

        Returns:
            Composite score in range [0, 1], or None if thresholds not met.

        The composite score balances:
        - 25% quiet_f1: Baseline wake word detection quality
        - 35% music_positive_f1: Wake word detection during music
        - 25% music_negative_precision: Must not false trigger on music
        - 15% temporal_consistency: Stable detection over time

        Hard rejections:
        - quiet_recall < 0.95: Must not regress baseline performance
        - music_negative_precision < 0.99: Must not false trigger on music
        """
        # Check hard rejection thresholds
        if self.quiet_metrics.recall < self.MIN_QUIET_RECALL:
            logger.debug(
                "Checkpoint %s rejected: quiet_recall %.3f < %.3f",
                self.checkpoint_path.name,
                self.quiet_metrics.recall,
                self.MIN_QUIET_RECALL,
            )
            return None

        if self.music_negative_metrics.precision < self.MIN_MUSIC_NEGATIVE_PRECISION:
            logger.debug(
                "Checkpoint %s rejected: music_negative_precision %.3f < %.3f",
                self.checkpoint_path.name,
                self.music_negative_metrics.precision,
                self.MIN_MUSIC_NEGATIVE_PRECISION,
            )
            return None

        # Compute weighted composite score
        score = (
            0.25 * self.quiet_metrics.f1
            + 0.35 * self.music_positive_metrics.f1
            + 0.25 * self.music_negative_metrics.precision
            + 0.15 * self.temporal_consistency
        )

        logger.debug(
            "Checkpoint %s composite score: %.4f",
            self.checkpoint_path.name,
            score,
        )

        return score

    def print_summary(self) -> None:
        """Print human-readable evaluation summary."""
        console(f"\n{'='*60}")
        console(f"Model Evaluation: {self.checkpoint_path.name}")
        console(f"{'='*60}")

        console("\nQuiet Environment (clean Viola):")
        console(f"  Precision: {self.quiet_metrics.precision:.4f}")
        console(f"  Recall:    {self.quiet_metrics.recall:.4f}")
        console(f"  F1 Score:  {self.quiet_metrics.f1:.4f}")
        console(
            f"  TP/FP/TN/FN: {self.quiet_metrics.true_positives}/"
            f"{self.quiet_metrics.false_positives}/"
            f"{self.quiet_metrics.true_negatives}/"
            f"{self.quiet_metrics.false_negatives}"
        )

        console("\nMusic + Viola (positive during playback):")
        console(f"  Precision: {self.music_positive_metrics.precision:.4f}")
        console(f"  Recall:    {self.music_positive_metrics.recall:.4f}")
        console(f"  F1 Score:  {self.music_positive_metrics.f1:.4f}")
        console(
            f"  TP/FP/TN/FN: {self.music_positive_metrics.true_positives}/"
            f"{self.music_positive_metrics.false_positives}/"
            f"{self.music_positive_metrics.true_negatives}/"
            f"{self.music_positive_metrics.false_negatives}"
        )

        console("\nMusic Only (should reject):")
        console(f"  Precision: {self.music_negative_metrics.precision:.4f}")
        console(f"  Recall:    {self.music_negative_metrics.recall:.4f}")
        console(f"  F1 Score:  {self.music_negative_metrics.f1:.4f}")
        console(
            f"  TP/FP/TN/FN: {self.music_negative_metrics.true_positives}/"
            f"{self.music_negative_metrics.false_positives}/"
            f"{self.music_negative_metrics.true_negatives}/"
            f"{self.music_negative_metrics.false_negatives}"
        )

        console(f"\nTemporal Consistency: {self.temporal_consistency:.4f}")

        composite = self.compute_composite_score()
        if composite is not None:
            console(f"\nComposite Score: {composite:.4f}")
        else:
            console("\n[REJECTED] Does not meet minimum thresholds:")
            if self.quiet_metrics.recall < self.MIN_QUIET_RECALL:
                console(f"  - quiet_recall {self.quiet_metrics.recall:.3f} " f"< {self.MIN_QUIET_RECALL}")
            if self.music_negative_metrics.precision < self.MIN_MUSIC_NEGATIVE_PRECISION:
                console(
                    f"  - music_negative_precision "
                    f"{self.music_negative_metrics.precision:.3f} "
                    f"< {self.MIN_MUSIC_NEGATIVE_PRECISION}"
                )

        console(f"{'='*60}\n")


# ============================================================
# Helper Functions
# ============================================================


def _compute_metrics_from_predictions(
    predictions: list[float],
    labels: list[int],
    threshold: float = 0.5,
) -> ValidationMetrics:
    """
    Compute validation metrics from model predictions.

    Args:
        predictions: Model output scores (0 to 1)
        labels: Ground truth labels (0 or 1)
        threshold: Classification threshold

    Returns:
        ValidationMetrics with confusion matrix counts
    """
    tp = fp = tn = fn = 0

    for pred, label in zip(predictions, labels, strict=True):
        predicted_positive = pred >= threshold
        actual_positive = label == 1

        if actual_positive and predicted_positive:
            tp += 1
        elif not actual_positive and predicted_positive:
            fp += 1
        elif not actual_positive and not predicted_positive:
            tn += 1
        else:
            fn += 1

    return ValidationMetrics(
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
    )


def _run_inference(
    model: WakeWordModel,
    audio_samples: list[np.ndarray],
    device: str = "cpu",
) -> list[float]:
    """
    Run inference on a batch of audio samples.

    Args:
        model: Loaded WakeWordModel
        audio_samples: List of audio arrays
        device: Device to run on

    Returns:
        List of prediction scores
    """
    import torch

    model.eval()
    predictions = []

    with torch.no_grad():
        for audio in audio_samples:
            # Mirror inference preprocessing (engine.py:263-270):
            # normalize_audio() removed — training does not normalize,
            # inference does not normalize. See pipeline audit 2026-03-02.
            audio = center_crop(audio, CLIP_SAMPLES)
            mel = compute_features(audio)
            mel_tensor = torch.from_numpy(mel).float().unsqueeze(0).to(device)

            # Run inference
            score = model(mel_tensor)
            predictions.append(float(score.cpu().numpy()))

    return predictions


# ============================================================
# Main Functions
# ============================================================


def measure_temporal_consistency(
    model: WakeWordModel,
    audio_samples: list[np.ndarray],
    threshold: float = 0.5,
    device: str = "cpu",
) -> float:
    """
    Measure temporal consistency of model predictions.

    Simulates sliding window inference and measures how many consecutive
    frames score above the threshold. A good model should produce stable
    predictions over time, not oscillate rapidly.

    Args:
        model: Loaded WakeWordModel
        audio_samples: List of audio samples (should be positive samples)
        threshold: Score threshold for detection
        device: Device to run on

    Returns:
        Average ratio of consecutive frames above threshold (0 to 1)
    """
    import torch

    if not audio_samples:
        return 0.0

    model.eval()

    # Window parameters for sliding inference
    # Use half the clip length as window to allow multiple overlapping frames
    # even on clip-length audio samples
    window_samples = CLIP_SAMPLES // 2  # 0.75 seconds
    hop_samples = HOP_LENGTH * 4  # ~40ms hops for temporal analysis

    consistency_scores = []

    with torch.no_grad():
        for audio in audio_samples:
            # normalize_audio() removed — training does not normalize,
            # inference does not normalize. See pipeline audit 2026-03-02.

            # Ensure minimum length for analysis
            if len(audio) < window_samples:
                audio = pad_or_trim(audio, CLIP_SAMPLES)

            # Sliding window inference
            # Use the approach from evaluate_fa_hr.py: iterate through all audio
            # and pad windows that extend past the end
            frame_scores = []
            for start in range(0, len(audio), hop_samples):
                window = audio[start : start + window_samples]
                # Pad if window extends past audio end
                window = pad_or_trim(window, window_samples)
                mel = compute_features(window)
                mel_tensor = torch.from_numpy(mel).float().unsqueeze(0).to(device)
                score = float(model(mel_tensor).cpu().numpy())
                frame_scores.append(score)

            if len(frame_scores) < 2:
                continue

            # Count consecutive frames above threshold
            above_threshold = [s >= threshold for s in frame_scores]
            consecutive_count = 0
            max_consecutive = 0

            for is_above in above_threshold:
                if is_above:
                    consecutive_count += 1
                    max_consecutive = max(max_consecutive, consecutive_count)
                else:
                    consecutive_count = 0

            # Ratio of frames in longest consecutive run
            consistency = max_consecutive / len(above_threshold)
            consistency_scores.append(consistency)

    if not consistency_scores:
        return 0.0

    avg_consistency = float(np.mean(consistency_scores))
    logger.debug(
        "Temporal consistency: %.3f (averaged over %d samples, %d frames each)",
        avg_consistency,
        len(consistency_scores),
        len(frame_scores) if frame_scores else 0,
    )

    return avg_consistency


def evaluate_checkpoint(
    checkpoint_path: Path,
    val_data: ValidationDataDict,
    model_class: type[WakeWordModel],
    device: str = "cpu",
) -> ModelEvaluation:
    """
    Evaluate a model checkpoint on validation data.

    Args:
        checkpoint_path: Path to .pt checkpoint file
        val_data: Dictionary with quiet, music_positive, music_negative data
        model_class: WakeWordModel class to instantiate
        device: Device to run on

    Returns:
        ModelEvaluation with all metrics
    """
    logger.info("Evaluating checkpoint: %s", checkpoint_path.name)

    # Load model from checkpoint
    model = model_class.from_checkpoint(str(checkpoint_path), device=device)

    # Evaluate on quiet data
    quiet_audio = [sample[0] for sample in val_data["quiet"]]
    quiet_labels = [sample[1] for sample in val_data["quiet"]]
    quiet_predictions = _run_inference(model, quiet_audio, device)
    quiet_metrics = _compute_metrics_from_predictions(quiet_predictions, quiet_labels)

    logger.debug(
        "Quiet metrics - P: %.3f, R: %.3f, F1: %.3f",
        quiet_metrics.precision,
        quiet_metrics.recall,
        quiet_metrics.f1,
    )

    # Evaluate on music + viola (positive during playback)
    music_pos_audio = [sample[0] for sample in val_data["music_positive"]]
    music_pos_labels = [sample[1] for sample in val_data["music_positive"]]
    music_pos_predictions = _run_inference(model, music_pos_audio, device)
    music_pos_metrics = _compute_metrics_from_predictions(music_pos_predictions, music_pos_labels)

    logger.debug(
        "Music positive metrics - P: %.3f, R: %.3f, F1: %.3f",
        music_pos_metrics.precision,
        music_pos_metrics.recall,
        music_pos_metrics.f1,
    )

    # Evaluate on music only (should reject)
    music_neg_audio = [sample[0] for sample in val_data["music_negative"]]
    music_neg_labels = [sample[1] for sample in val_data["music_negative"]]
    music_neg_predictions = _run_inference(model, music_neg_audio, device)
    music_neg_metrics = _compute_metrics_from_predictions(music_neg_predictions, music_neg_labels)

    logger.debug(
        "Music negative metrics - P: %.3f, R: %.3f, F1: %.3f",
        music_neg_metrics.precision,
        music_neg_metrics.recall,
        music_neg_metrics.f1,
    )

    # Measure temporal consistency (on positive quiet samples)
    positive_quiet = [audio for audio, label in val_data["quiet"] if label == 1]
    temporal_consistency = measure_temporal_consistency(model, positive_quiet, device=device)

    return ModelEvaluation(
        checkpoint_path=checkpoint_path,
        quiet_metrics=quiet_metrics,
        music_positive_metrics=music_pos_metrics,
        music_negative_metrics=music_neg_metrics,
        temporal_consistency=temporal_consistency,
    )


def select_best_model(
    checkpoints_dir: Path,
    val_data: ValidationDataDict,
    model_class: type[WakeWordModel],
    device: str = "cpu",
    checkpoint_pattern: str = "*.pt",
) -> Path:
    """
    Select the best model checkpoint based on composite scoring.

    Evaluates all checkpoints in the directory and returns the one with
    the highest composite score that meets minimum thresholds.

    Args:
        checkpoints_dir: Directory containing .pt checkpoint files
        val_data: Validation data dictionary
        model_class: WakeWordModel class to instantiate
        device: Device to run on
        checkpoint_pattern: Glob pattern for checkpoint files

    Returns:
        Path to best checkpoint

    Raises:
        ValueError: If no checkpoint meets minimum thresholds
        FileNotFoundError: If no checkpoints found in directory
    """
    checkpoints = list(checkpoints_dir.glob(checkpoint_pattern))

    if not checkpoints:
        raise FileNotFoundError(
            "No checkpoints found in %s matching pattern %s",
            checkpoints_dir,
            checkpoint_pattern,
        )

    logger.info(
        "Evaluating %d checkpoints from %s",
        len(checkpoints),
        checkpoints_dir,
    )

    evaluations: list[tuple[ModelEvaluation, float]] = []

    for ckpt in checkpoints:
        try:
            evaluation = evaluate_checkpoint(ckpt, val_data, model_class, device)
            composite = evaluation.compute_composite_score()

            if composite is not None:
                evaluations.append((evaluation, composite))
                logger.info(
                    "Checkpoint %s: composite score %.4f",
                    ckpt.name,
                    composite,
                )
            else:
                logger.warning(
                    "Checkpoint %s rejected (thresholds not met)",
                    ckpt.name,
                )

        except Exception as e:
            logger.exception("Failed to evaluate checkpoint %s", ckpt.name)
            continue

    if not evaluations:
        raise ValueError(
            "No checkpoint meets minimum thresholds. "
            f"Required: quiet_recall >= {ModelEvaluation.MIN_QUIET_RECALL}, "
            f"music_negative_precision >= {ModelEvaluation.MIN_MUSIC_NEGATIVE_PRECISION}"
        )

    # Sort by composite score (descending)
    evaluations.sort(key=lambda x: x[1], reverse=True)

    best_eval, best_score = evaluations[0]

    logger.info(
        "Selected best model: %s with composite score %.4f",
        best_eval.checkpoint_path.name,
        best_score,
    )

    # Print summary of best model
    best_eval.print_summary()

    return best_eval.checkpoint_path
