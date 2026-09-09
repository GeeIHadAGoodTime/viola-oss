"""Silero VAD via ONNX runtime — no torch dependency.

Provides a singleton ONNX-based Silero VAD model that mirrors the torch
``model(audio_tensor, sample_rate)`` interface used throughout Viola's
voice pipeline.  All four VAD consumers (silero_endpoint, wake_decision_policy,
streaming_stt, continuous_capture) import from here instead of torch.

The model file is resolved in order:
1. ``models/vad/silero_vad.onnx`` relative to the app root
2. Bundled inside the ``openwakeword`` package
3. Bundled inside the ``pipecat`` package
4. Bundled inside the ``faster_whisper`` package
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from core.logging_config import get_logger

logger = get_logger(__name__)

_CONTEXT_SIZE_16K = 64

# AUD-R1 debug flag — gate multi-user-isolation probe logging behind env.
# See docs/research/AUD_R1_MULTIUSER_AUDIO_ISOLATION.md.
import os as _os

_AUD_R1_DEBUG = _os.environ.get("VIOLA_AUD_R1_DEBUG", "") == "1"
_aud_r1_logger = get_logger("viola.vad.aud_r1")
_WINDOW_SAMPLES_16K = 512

# Singleton
_instance: SileroVADOnnx | None = None
_instance_lock = threading.Lock()


def _find_model_path() -> str | None:
    """Locate the Silero VAD ONNX model file."""
    # 1. Project-local (bundled): anchored on the project root — PATH-1,
    # never Path.cwd() (a frozen install's launch cwd is arbitrary; the
    # bundled model lives under _internal = project root when frozen).
    candidate = Path(__file__).resolve().parent.parent.parent / "models" / "vad" / "silero_vad.onnx"
    if candidate.is_file():
        return str(candidate)

    # 2. openwakeword bundle
    try:
        import openwakeword

        p = Path(openwakeword.__file__).parent / "resources" / "models" / "silero_vad.onnx"
        if p.is_file():
            return str(p)
    except Exception:
        pass

    # 3. pipecat bundle
    try:
        import pipecat

        p = Path(pipecat.__file__).parent / "audio" / "vad" / "data" / "silero_vad.onnx"
        if p.is_file():
            return str(p)
    except Exception:
        pass

    # 4. faster_whisper bundle (v6 model — compatible)
    try:
        import faster_whisper

        p = Path(faster_whisper.__file__).parent / "assets" / "silero_vad_v6.onnx"
        if p.is_file():
            return str(p)
    except Exception:
        pass

    return None


class SileroVADOnnx:
    """ONNX runtime wrapper for Silero VAD.

    Drop-in replacement for the torch-based model.  Calling convention:

        prob = model(audio_float32, sample_rate)

    where *audio_float32* is a 1-D numpy float32 array of exactly 512
    samples (16 kHz) and *sample_rate* is 16000.  Returns a float in
    [0, 1] representing speech probability.

    The model is stateful — call ``reset_states()`` between unrelated
    audio segments.
    """

    def __init__(self, model_path: str | None = None) -> None:
        import onnxruntime as ort

        if model_path is None:
            model_path = _find_model_path()
        if model_path is None:
            raise FileNotFoundError("Silero VAD ONNX model not found")

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            model_path,
            providers=["CPUExecutionProvider"],
            sess_options=opts,
        )
        self._bound_user_id: str | None = None
        self.reset_states()
        logger.debug("SileroVAD ONNX loaded from %s", model_path)

    def reset_states(self, batch_size: int = 1) -> None:
        self._state = np.zeros((2, batch_size, 128), dtype=np.float32)
        self._context = np.zeros((batch_size, _CONTEXT_SIZE_16K), dtype=np.float32)

    def _maybe_reset_for_user(self) -> None:
        """Reset VAD state when the ambient user_id changes.

        Reads ``core.user_context.get_current_user_id`` — the contextvar
        the auth middleware populates per request. When no user context
        is set (e.g. calls from the wake-detector read thread), we leave
        state untouched so that thread-boundary crossings do not thrash
        VAD history between the wake thread (no context) and request
        handlers (has context).
        """
        try:
            from core.user_context import get_current_user_id

            current_user: str | None = get_current_user_id()
        except (LookupError, Exception):
            current_user = None

        if current_user is None:
            return  # No context — don't alter state.
        if current_user != self._bound_user_id:
            if self._bound_user_id is not None:
                logger.debug(
                    "SileroVAD user switch (%s -> %s); resetting state",
                    self._bound_user_id,
                    current_user,
                )
            self.reset_states()
            self._bound_user_id = current_user

    def __call__(self, audio: np.ndarray, sr: int) -> float:
        """Run VAD on a 512-sample float32 audio window at 16 kHz.

        Returns speech probability as a Python float.
        """
        self._maybe_reset_for_user()
        if audio.ndim == 1:
            audio = audio[np.newaxis, :]  # (1, 512)

        x = np.concatenate((self._context, audio), axis=1)
        ort_inputs = {
            "input": x,
            "state": self._state,
            "sr": np.array(sr, dtype=np.int64),
        }
        out, state = self._session.run(None, ort_inputs)
        self._state = state
        self._context = x[..., -_CONTEXT_SIZE_16K:]

        # AUD-R1 instrumentation: per-VAD-instance probe emitted when
        # VIOLA_AUD_R1_DEBUG=1 so the multi-user-isolation harness can
        # assert that user A's state norm never appears on user B's
        # VAD instance. Emits a structured line per call; gated by env
        # var to avoid production log spam.
        if _AUD_R1_DEBUG:
            try:
                _aud_r1_logger.info(
                    "AUD_R1 vad_probe id=%d user=%s norm=%.6f",
                    id(self),
                    self._bound_user_id,
                    float(np.linalg.norm(self._state)),
                )
            except Exception:
                pass  # instrumentation must never break production audio path

        return float(out[0, 0]) if out.ndim == 2 else float(out[0])


def create_silero_vad() -> SileroVADOnnx | None:
    """Create a new Silero VAD ONNX instance with independent state.

    Each consumer should hold its own instance to avoid state corruption
    when multiple VAD consumers run concurrently (e.g., wake detection
    overlaps with continuous capture during TTS playback).

    Returns None if the model cannot be loaded.
    """
    try:
        return SileroVADOnnx()
    except Exception:
        logger.warning(
            "SileroVAD ONNX unavailable, VAD consumers will use RMS fallback",
            exc_info=True,
        )
        return None


def get_silero_vad() -> SileroVADOnnx | None:
    """Get or create the singleton Silero VAD ONNX instance.

    .. deprecated:: Use :func:`create_silero_vad` for new consumers that
       may run concurrently with other VAD users.

    Returns None if the model cannot be loaded.
    """
    global _instance
    if _instance is not None:
        return _instance

    with _instance_lock:
        if _instance is not None:
            return _instance
        _instance = create_silero_vad()
        return _instance
