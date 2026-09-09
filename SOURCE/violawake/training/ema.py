"""
ViolaWake Exponential Moving Average
======================================

EMA of model parameters for smoother, more generalizable models.
"""

from __future__ import annotations

import copy

_TORCH_AVAILABLE = False
try:
    import torch
    import torch.nn as nn

    _TORCH_AVAILABLE = True
except ImportError:
    pass


class ExponentialMovingAverage:
    """
    Maintains an exponential moving average of model parameters.

    EMA produces smoother parameter estimates that often generalize better
    than the raw trained parameters, especially for small models.

    Usage::

        ema = ExponentialMovingAverage(model, decay=0.999)

        # During training, after each optimizer step:
        ema.update()

        # For evaluation or export:
        ema.apply(model)
        evaluate(model)
        ema.restore(model)

        # For final export (no restore needed):
        ema.apply(model)
        export(model)

    Args:
        model: The model whose parameters to track.
        decay: EMA decay rate. Higher values give more smoothing.
               Typical range: 0.99 to 0.9999. Default: 0.999.
    """

    def __init__(self, model: nn.Module, decay: float = 0.999):
        if not _TORCH_AVAILABLE:
            raise ImportError("PyTorch required for ExponentialMovingAverage")

        self.decay = decay
        # Deep copy of all parameters as shadow values
        self.shadow: dict[str, torch.Tensor] = {}
        self.backup: dict[str, torch.Tensor] = {}
        self._model_ref = model

        for name, param in model.named_parameters():
            if param.requires_grad:
                self.shadow[name] = param.data.clone()

    def update(self) -> None:
        """
        Update shadow parameters with current model parameters.

        shadow = decay * shadow + (1 - decay) * param
        """
        for name, param in self._model_ref.named_parameters():
            if param.requires_grad and name in self.shadow:
                self.shadow[name].mul_(self.decay).add_(param.data, alpha=1.0 - self.decay)

    def apply(self, model: nn.Module) -> None:
        """
        Copy shadow parameters to model (for evaluation/export).

        Saves original parameters in backup for later restore.
        """
        self.backup = {}
        for name, param in model.named_parameters():
            if param.requires_grad and name in self.shadow:
                self.backup[name] = param.data.clone()
                param.data.copy_(self.shadow[name])

    def restore(self, model: nn.Module) -> None:
        """
        Restore original parameters from backup after apply().

        Call this after evaluation to resume training with original weights.
        """
        for name, param in model.named_parameters():
            if param.requires_grad and name in self.backup:
                param.data.copy_(self.backup[name])
        self.backup = {}
