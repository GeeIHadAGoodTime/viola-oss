# WARNING: Do NOT apply noise suppression upstream of ViolaWake.
# ViolaWake's MLP was trained on raw (post-AEC) audio. Noise suppression
# alters the spectral envelope, causing training-serving skew that degrades
# wake word detection. Use noise suppression ONLY in the STT path.
# See: docs/research/MASTER_OSS_ADOPTION_LIST.md — VP-7 notes.

"""Noise Suppression Backend Selection and Configuration.

Provides a unified factory for selecting between DeepFilterNet (heavy,
high quality) and RNNoise (lightweight, ARM-friendly) noise suppression
backends.

Configuration is driven by ``config/settings.py``::

    NOISE_SUPPRESSION_BACKEND=deepfilter   # "deepfilter", "rnnoise", "none"
    NOISE_SUPPRESSION_ENABLED=true

Usage::

    from audio_core.noise_gate import get_noise_gate

    gate = get_noise_gate()
    cleaned = gate.process(raw_audio, sample_rate=16000)
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Literal, Protocol

from core.constants import SAMPLE_RATE_16K
from core.logging_config import get_logger

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)

# ---------------------------------------------------------------------- #
# Protocol — both backends satisfy this                                   #
# ---------------------------------------------------------------------- #


class NoiseSuppressionBackend(Protocol):
    """Common interface for noise suppression backends."""

    @property
    def enabled(self) -> bool: ...

    @enabled.setter
    def enabled(self, value: bool) -> None: ...

    @property
    def is_loaded(self) -> bool: ...

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE_16K) -> np.ndarray: ...

    def release(self) -> None: ...


# ---------------------------------------------------------------------- #
# Factory                                                                  #
# ---------------------------------------------------------------------- #

NoiseBackendChoice = Literal["deepfilter", "rnnoise", "none"]

_global_gate: NoiseSuppressionBackend | None = None
_gate_lock = threading.Lock()


def create_noise_suppressor(
    backend: NoiseBackendChoice = "deepfilter",
    *,
    enabled: bool = True,
) -> NoiseSuppressionBackend:
    """Create a noise suppression backend instance.

    Parameters
    ----------
    backend:
        Which backend to use:
        - ``"deepfilter"`` — DeepFilterNet (GPU/desktop, best quality)
        - ``"rnnoise"`` — RNNoise (ARM/Pi, lightweight)
        - ``"none"`` — no-op passthrough
    enabled:
        Whether suppression is active.

    Returns
    -------
    NoiseSuppressionBackend
        An instance that satisfies the common protocol.
    """
    if backend == "none" or not enabled:
        return _NoOpSuppressor()

    if backend == "rnnoise":
        from audio_core.rnnoise_suppressor import RNNoiseSuppressor

        logger.info("Using RNNoise noise suppression (lightweight / ARM)")
        return RNNoiseSuppressor(enabled=enabled)

    # Default: deepfilter
    from audio_core.noise_suppression import NoiseSuppressor

    logger.info("Using DeepFilterNet noise suppression (desktop)")
    return NoiseSuppressor(enabled=enabled)


def get_noise_gate() -> NoiseSuppressionBackend:
    """Return the global noise-gate singleton, configured from settings.

    Reads ``noise_suppression_backend`` and ``noise_suppression_enabled``
    from ``config.settings`` on first call.
    """
    global _global_gate
    if _global_gate is None:
        with _gate_lock:
            if _global_gate is None:
                from config.settings import settings

                backend_name = getattr(settings, "noise_suppression_backend", "none")
                ns_enabled = getattr(settings, "noise_suppression_enabled", False)
                _global_gate = create_noise_suppressor(
                    backend=backend_name,
                    enabled=ns_enabled,
                )
    return _global_gate


# ---------------------------------------------------------------------- #
# No-op passthrough                                                        #
# ---------------------------------------------------------------------- #


class _NoOpSuppressor:
    """Passthrough that satisfies :class:`NoiseSuppressionBackend`."""

    @property
    def enabled(self) -> bool:
        return False

    @enabled.setter
    def enabled(self, value: bool) -> None:
        pass  # no-op

    @property
    def is_loaded(self) -> bool:
        return False

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE_16K) -> np.ndarray:
        return audio

    def release(self) -> None:
        pass


__all__ = [
    "NoiseBackendChoice",
    "NoiseSuppressionBackend",
    "create_noise_suppressor",
    "get_noise_gate",
]
