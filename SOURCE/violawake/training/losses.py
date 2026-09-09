"""
ViolaWake Training Losses
==========================

Custom loss functions for wake word model training.
"""

from __future__ import annotations

_TORCH_AVAILABLE = False
try:
    import torch
    import torch.nn as nn

    _TORCH_AVAILABLE = True
except ImportError:
    pass


if _TORCH_AVAILABLE:

    class FocalLoss(nn.Module):
        """
        Focal Loss for binary classification.

        Focuses training on hard-to-classify examples by down-weighting easy ones.
        Particularly useful for wake word detection where there is class imbalance
        (many more negatives than positives).

        FL(p_t) = -alpha_t * (1 - p_t)^gamma * log(p_t)

        With label smoothing, targets are adjusted:
            0 -> label_smoothing / 2
            1 -> 1 - label_smoothing / 2

        Args:
            gamma: Focusing parameter. Higher values focus more on hard examples.
                   gamma=0 is equivalent to cross-entropy. Default: 2.0.
            alpha: Weighting factor for positive class. Compensates for class
                   imbalance by favoring the minority (positive) class.
                   Default: 0.75.
            label_smoothing: Smoothing factor for targets. Targets become
                   {smoothing/2, 1 - smoothing/2}. Default: 0.05.
        """

        def __init__(
            self,
            gamma: float = 2.0,
            alpha: float = 0.75,
            label_smoothing: float = 0.05,
        ):
            super().__init__()
            self.gamma = gamma
            self.alpha = alpha
            self.label_smoothing = label_smoothing

        def forward(self, inputs: torch.Tensor, targets: torch.Tensor) -> torch.Tensor:
            """
            Compute focal loss.

            Args:
                inputs: Model predictions (sigmoid outputs, values in [0, 1]).
                targets: Ground truth labels (0 or 1).

            Returns:
                Scalar loss value (mean over batch).
            """
            # Apply label smoothing
            if self.label_smoothing > 0:
                targets = targets * (1.0 - self.label_smoothing) + self.label_smoothing / 2.0

            # Clamp predictions for numerical stability
            p = torch.clamp(inputs, min=1e-7, max=1.0 - 1e-7)

            # Compute focal weights
            # For positive targets: p_t = p, for negative targets: p_t = 1 - p
            p_t = targets * p + (1.0 - targets) * (1.0 - p)
            focal_weight = (1.0 - p_t) ** self.gamma

            # Compute alpha weights
            # alpha for positives, (1-alpha) for negatives
            alpha_t = targets * self.alpha + (1.0 - targets) * (1.0 - self.alpha)

            # Compute binary cross-entropy (element-wise)
            bce = -(targets * torch.log(p) + (1.0 - targets) * torch.log(1.0 - p))

            # Combine
            loss = alpha_t * focal_weight * bce

            return loss.mean()

else:

    class FocalLoss:  # type: ignore[no-redef]
        """Stub when PyTorch is not available."""

        def __init__(self, *args, **kwargs):
            raise ImportError("PyTorch required for FocalLoss")
