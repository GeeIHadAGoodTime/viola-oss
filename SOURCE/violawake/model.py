"""
ViolaWake Model Architecture
=============================

Small CNN model for wake word detection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logging_config import get_logger
from violawake.config import HIDDEN_SIZE, N_MELS

logger = get_logger(__name__)


def _verify_feature_config(checkpoint: dict, checkpoint_path: str) -> None:
    """Verify checkpoint feature config matches current config.

    If mismatched, overrides runtime config with checkpoint values
    to prevent inverted scores. Added 2026-03-02 after config drift
    bug caused inverted scores in production.
    """
    from violawake.config import get_feature_config

    saved_config = checkpoint.get("feature_config")
    current_config = get_feature_config()

    if saved_config is None:
        logger.warning(
            "Checkpoint %s has no embedded feature_config. "
            "Using config.py values. Re-save or retrain to embed config.",
            checkpoint_path,
        )
        return

    mismatches = {
        k: (saved_config[k], current_config[k])
        for k in saved_config
        if k in current_config and saved_config[k] != current_config[k]
    }
    if mismatches:
        logger.error(
            "FEATURE CONFIG MISMATCH between checkpoint and config.py! "
            "Checkpoint trained with different settings. Mismatches: %s. "
            "Using CHECKPOINT config to prevent inverted scores.",
            mismatches,
        )
        _apply_checkpoint_config(saved_config)


def _apply_checkpoint_config(saved_config: dict) -> None:
    """Override runtime feature config with checkpoint values.

    Overrides both violawake.config module-level constants AND
    violawake.audio cached imports (from violawake.config import X),
    since audio.py caches values at import time.
    """
    import violawake.audio as audio_mod
    import violawake.config as cfg

    _CONFIG_MAP = {
        "feature_type": "FEATURE_TYPE",
        "use_pcen": "USE_PCEN",
        "n_mels": "N_MELS_MEL",
        "n_fft": "N_FFT_MEL",
        "hop_length": "HOP_LENGTH_MEL",
        "win_length": "WIN_LENGTH_MEL",
        "f_min": "F_MIN",
        "f_max": "F_MAX",
        "sample_rate": "SAMPLE_RATE",
        "clip_samples": "CLIP_SAMPLES",
        "pcen_gain": "PCEN_GAIN",
        "pcen_bias": "PCEN_BIAS",
        "pcen_power": "PCEN_POWER",
        "pcen_time_constant": "PCEN_TIME_CONSTANT",
        "pcen_eps": "PCEN_EPS",
    }

    for key, value in saved_config.items():
        attr_name = _CONFIG_MAP.get(key)
        if attr_name is None:
            continue
        # Override config.py module-level constant
        if hasattr(cfg, attr_name):
            setattr(cfg, attr_name, value)
        # Override audio.py cached import (from violawake.config import X)
        if hasattr(audio_mod, attr_name):
            setattr(audio_mod, attr_name, value)


if TYPE_CHECKING:
    import torch
    import torch.nn as nn

    class WakeWordModel(nn.Module):
        def __init__(self, n_mels: int = N_MELS, hidden_size: int = HIDDEN_SIZE) -> None: ...

        def forward(self, x: torch.Tensor) -> torch.Tensor: ...

        @classmethod
        def from_checkpoint(cls, checkpoint_path: str, device: str = "cpu") -> WakeWordModel: ...

else:
    # Check for PyTorch
    _TORCH_AVAILABLE = False
    try:
        import torch
        import torch.nn as nn

        _TORCH_AVAILABLE = True
    except ImportError:
        pass

    if _TORCH_AVAILABLE:

        class WakeWordModel(nn.Module):
            """
            Small CNN model for wake word detection.

            Architecture:
                - 3 convolutional layers with batch norm and pooling
                - Adaptive pooling for variable-length input
                - 2-layer classifier with dropout

            Input: Mel spectrogram (batch, n_mels, time_frames)
            Output: Wake word probability (batch,) in range [0, 1]

            Total parameters: ~28K (very lightweight)
            """

            def __init__(self, n_mels: int = N_MELS, hidden_size: int = HIDDEN_SIZE):
                super().__init__()

                # CNN feature extractor
                self.conv = nn.Sequential(
                    nn.Conv2d(1, 16, kernel_size=3, padding=1),
                    nn.BatchNorm2d(16),
                    nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(16, 32, kernel_size=3, padding=1),
                    nn.BatchNorm2d(32),
                    nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(32, 64, kernel_size=3, padding=1),
                    nn.BatchNorm2d(64),
                    nn.ReLU(),
                    nn.AdaptiveAvgPool2d((1, 1)),
                )

                # Classifier
                self.classifier = nn.Sequential(
                    nn.Linear(64, hidden_size),
                    nn.ReLU(),
                    nn.Dropout(0.3),
                    nn.Linear(hidden_size, 1),
                    nn.Sigmoid(),
                )

            def forward(self, x):
                """
                Forward pass.

                Args:
                    x: Mel spectrogram tensor (batch, n_mels, time_frames)

                Returns:
                    Wake word probabilities (batch,)
                """
                # Add channel dimension: (batch, n_mels, time) -> (batch, 1, n_mels, time)
                x = x.unsqueeze(1)

                # CNN feature extraction
                x = self.conv(x)  # (batch, 64, 1, 1)
                x = x.view(x.size(0), -1)  # (batch, 64)

                # Classify
                x = self.classifier(x)
                return x.squeeze(-1)

            @classmethod
            def from_checkpoint(cls, checkpoint_path: str, device: str = "cpu"):
                """
                Load model from checkpoint.

                Args:
                    checkpoint_path: Path to .pt file
                    device: Device to load model onto

                Returns:
                    Loaded model in eval mode
                """
                checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)

                # Verify feature config matches (2026-03-02 config drift prevention)
                _verify_feature_config(checkpoint, checkpoint_path)

                # Get config from checkpoint
                config = checkpoint.get("config", {})
                n_mels = config.get("n_mels", N_MELS)

                model = cls(n_mels=n_mels)
                model.load_state_dict(checkpoint["model_state_dict"])
                model.to(device)
                model.eval()

                return model

    else:
        # Stub for when PyTorch is not available
        class WakeWordModel:
            """Stub class when PyTorch is not available."""

            def __init__(self, *args, **kwargs):
                raise ImportError("PyTorch is required for WakeWordModel. " "Install with: pip install torch")
