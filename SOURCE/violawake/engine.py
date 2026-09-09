"""
ViolaWake Runtime Engine
=========================

Main inference engine for wake word detection.
Supports both PyTorch and ONNX models.
"""

from __future__ import annotations

import gc
import importlib
import importlib.util
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, cast

import numpy as np

from core.logging_config import get_logger

# AUD-R1 debug flag — gate multi-user-isolation probe logging behind env.
# See docs/research/AUD_R1_MULTIUSER_AUDIO_ISOLATION.md.
_AUD_R1_DEBUG = os.environ.get("VIOLA_AUD_R1_DEBUG", "") == "1"
_aud_r1_logger = get_logger("viola.wake.aud_r1")
from violawake.audio import (
    center_crop,
    compute_features,
)
from violawake.config import (
    CLIP_SAMPLES,
    DEBOUNCE_SECONDS,
    DEFAULT_MODEL_PATH,
    DEFAULT_THRESHOLD,
    SAMPLE_RATE,
    SILENCE_GATE_RMS,
)

if TYPE_CHECKING:
    from violawake.model import WakeWordModel

logger = get_logger(__name__)

# Check available backends
_TORCH_AVAILABLE = False
_ONNX_AVAILABLE = False

try:
    # Probe only: we drop torch from sys.modules once ONNX is committed
    # (see _release_torch_if_loaded) to free ~400 MB RSS.
    import torch

    _ = torch.__version__
    _TORCH_AVAILABLE = True
except ImportError:
    pass

try:
    import onnxruntime as ort

    _ONNX_AVAILABLE = True
except ImportError:
    pass


# Packages that pull torch at import time and keep live references to
# torch objects. If any of these is loaded, popping torch from sys.modules
# will not free memory (the package's own refs keep torch alive) AND will
# break that package's future re-imports. Skip the pop entirely.
_TORCH_DEPENDENT_RUNTIME_PACKAGES: frozenset[str] = frozenset({"faster_whisper", "torchaudio"})


def _release_torch_if_loaded() -> None:
    """Drop torch from ``sys.modules`` once the ONNX backend is committed.

    Torch is imported at module load as a capability probe. If the running
    engine uses ONNX it never calls ``torch.*`` again — keeping torch
    resident costs ~400 MB RSS on a packaged Windows build.

    No-op when another runtime package (faster_whisper, torchaudio) is
    already holding torch: popping would not free memory (their refs
    keep torch alive) and would break their future re-imports.
    """
    if any(pkg in sys.modules for pkg in _TORCH_DEPENDENT_RUNTIME_PACKAGES):
        logger.debug("ViolaWake: skipping torch release — another package holds torch refs")
        return
    dropped = [
        name
        for name in list(sys.modules)
        if name == "torch" or name.startswith("torch.") or name.startswith("torchaudio")
    ]
    if not dropped:
        return
    for name in dropped:
        sys.modules.pop(name, None)
    gc.collect()
    logger.info("ViolaWake: released %d torch modules post-ONNX commit", len(dropped))


def _wake_audio_consent_granted() -> bool:
    """Return True only when the user has opted in to wake-audio storage.

    Honors the session-level contributor_mode override so a session can
    suppress writes even when the global flag is True.
    """
    try:
        from voice.wake_detector.contributor_mode import session_wake_contributor

        if session_wake_contributor.get() is False:
            return False
    except (ImportError, LookupError, RuntimeError) as exc:
        logger.debug("Wake contributor session override unavailable: %s", exc)
    try:
        from config.settings import settings

        return bool(getattr(settings, "wake_data_contribute", False))
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Wake data contribution setting unavailable: %s", exc)
        return False


def _wake_audio_encrypt_at_rest() -> bool:
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        return bool(sm.get("wake_audio_encrypt_at_rest", False))
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Wake audio encryption setting unavailable: %s", exc)
        return False


def _derive_wake_audio_key() -> bytes | None:
    """Return a 32-byte AES-GCM key derived from the active user identity.

    Fails closed when user identity is unavailable — sharing an ``anonymous``
    bucket across users would mean every user on a shared machine could decrypt
    each other's wake-audio captures (MULTI-TENANT RULE).
    """
    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
    except (ImportError, LookupError, RuntimeError):
        return None
    if not user_id:
        return None
    try:
        from config.settings import settings

        secret = getattr(settings, "secret_key", "") or getattr(settings, "app_secret", "")
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        secret = ""
    if not secret:
        return None
    import hashlib

    return hashlib.sha256(("wake-audio::%s::%s" % (user_id, secret)).encode("utf-8")).digest()


def _write_wake_audio_encrypted(path: Path, audio: np.ndarray) -> None:
    """Serialize audio as WAV, seal with AES-GCM, and write to disk."""
    import io

    sf_module = _try_import_module("soundfile")
    if sf_module is None:
        raise ImportError("soundfile not available")
    sf = cast(_SoundFileModule, sf_module)
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
    plaintext = buf.getvalue()
    key = _derive_wake_audio_key()
    if key is None:
        raise RuntimeError("wake audio encryption requested but no key material available")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise ImportError("cryptography required for wake audio encryption") from exc
    import os

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    path.write_bytes(b"VWA1" + nonce + ciphertext)


_OWW_AVAILABLE = False

try:
    from openwakeword.utils import AudioFeatures as OWWAudioFeatures

    _OWW_AVAILABLE = True
except ImportError:
    pass


def _resolve_oww_preprocessor_model(filename: str) -> Path:
    """Resolve a bundled OWW preprocessor model (melspectrogram/embedding).

    Resolution order:
      1. The bundled payload at ``models/wake/`` (tracked in git, collected
         into the frozen bundle) — the canonical location.
      2. The openwakeword package's own ``resources/models`` dir (dev
         convenience for environments that previously ran the OWW download).

    NEVER downloads. A fresh install must construct the wake detector with
    zero network access and zero install-dir writes (requal-M3). Missing
    models fail loudly here; the wake facade surfaces it as
    ``live_wake_available: false`` honest health.
    """
    from violawake.config import OWW_PREPROCESSOR_DIR

    candidates = [OWW_PREPROCESSOR_DIR / filename]
    try:
        import openwakeword as _oww_pkg

        candidates.append(Path(_oww_pkg.__file__).parent / "resources" / "models" / filename)
    except ImportError:
        pass

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise FileNotFoundError(
        "OWW preprocessor model %r not found (searched: %s). The wake "
        "detector requires the bundled preprocessor payload at models/wake/ "
        "— runtime download is forbidden (requal-M3)." % (filename, ", ".join(str(c) for c in candidates))
    )


class _OrtInput(Protocol):
    name: str


class _OrtSession(Protocol):
    def get_inputs(self) -> list[_OrtInput]: ...

    def run(self, output_names: object, input_feed: dict[str, np.ndarray]) -> list[object]: ...


class _SoundFileModule(Protocol):
    def write(self, file: str, data: np.ndarray, samplerate: int) -> object: ...


def _try_import_module(name: str) -> ModuleType | None:
    try:
        if importlib.util.find_spec(name) is None:
            return None
    except (ImportError, ValueError):
        return None
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _single_threaded_session_options(ort_local: ModuleType) -> object:
    """SessionOptions pinning ONNX Runtime to one thread for the wake classifier.

    Left at the ONNX Runtime default, a session spins `intra_op_num_threads`
    equal to the machine's core count. On the wake classifier -- a (1, 9, 96)
    temporal CNN, ~100 KB -- the thread pool costs far more than the matmuls it
    parallelises. Measured on an idle 4-core box, 200 inferences each:

        default : 19.19 ms wall / 65.47 ms CPU per call
        pinned  : 14.66 ms wall / 14.96 ms CPU per call

    So pinning is 24% faster in wall time and burns 4.4x less CPU, in a path
    that runs continuously on every user's machine while Viola is listening.
    The two other sessions in this pipeline are already single-threaded --
    OpenWakeWord's melspec + embedding models via `ncpu=1` in _load_onnx_mlp,
    and the Silero VAD via voice/vad/silero_onnx.py:106-109 -- so the classifier
    was the outlier, not the precedent.

    It also makes the wake path's cost *measurable*: with one thread, CPU time
    per inference is invariant to co-tenant load (14.96 ms idle vs 14.69 ms
    under 3x CPU oversubscription), which is what lets the stress guard in
    tests/wake_word/test_wake_stress.py assert on CPU time instead of a
    wall clock that just reports how busy the shared CI runner is (#4805).
    """
    options = ort_local.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    return options


class ViolaWake:
    """
    Wake word detection engine for "Viola".

    Provides a simple interface for detecting the wake word in audio streams.

    Usage:
        engine = ViolaWake("path/to/model.onnx")

        # For each audio chunk:
        score = engine.process_audio(audio_chunk)
        if score > engine.threshold:
            handle_wake_word()

        # Or use the callback interface:
        engine.set_callback(my_callback_function)
        engine.process_audio(audio_chunk)  # Calls callback if detected

    Attributes:
        threshold: Detection threshold (default 0.5)
        debounce_seconds: Minimum time between triggers (default 1.0)
    """

    def __init__(
        self,
        model_path: str | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        device: str = "cpu",
    ):
        """
        Initialize the wake word engine.

        Args:
            model_path: Path to .onnx or .pt model file
                       If None, uses default model path
            threshold: Detection threshold (0.0 to 1.0)
            debounce_seconds: Minimum time between triggers
            device: Device for PyTorch inference ("cpu" or "cuda")
        """
        self.threshold = threshold
        self.debounce_seconds = debounce_seconds
        self._device = device

        # State
        self._last_trigger_time: float = 0
        self._last_score: float = 0.0  # Most recent score from process_audio
        self._callback: Callable[[], None] | None = None
        self._fp_logging_enabled: bool = False
        self._fp_log_dir: Path | None = None

        # Temporal smoothing: require N consecutive high scores to trigger
        # Window=1 (single-frame trigger) is optimal: music FPs score max 0.037
        # (far below threshold), so multi-frame gating only hurts TP (−2.6%)
        # without filtering any real FPs. See logs/tp_optimization_log.jsonl.
        self._temporal_window: int = 1
        self._score_history: list[bool] = []

        # Track active user for multi-tenant isolation — score history leaks
        # decisions across users if a second user wakes mid-window otherwise.
        self._bound_user_id: str | None = None

        # Load model. Reject None explicitly when no default is configured —
        # otherwise str(None) would coalesce to the literal "None" and crash
        # later inside Path(...).exists() with a confusing error.
        if model_path is None:
            if DEFAULT_MODEL_PATH is None:
                raise ValueError("ViolaWake requires a model_path (None passed and DEFAULT_MODEL_PATH is unset)")
            model_path = str(DEFAULT_MODEL_PATH)
        self._model_path = Path(model_path)
        self._backend: str = ""
        self._model: WakeWordModel | None = None
        self._session: _OrtSession | None = None
        self._oww_preprocessor: Any | None = None
        self._oww_temporal_seq_len: int = 0

        self._load_model()

    def _load_model(self) -> None:
        """Load the model based on file extension and wake_inference_backend setting.

        Backend selection priority:
        1. Explicit setting ("onnx", "onnx_mlp", or "pytorch") — use the requested backend
        2. "auto" — ONNX on ARM (lightweight), file extension on x86
        3. Fallback — use file extension (.onnx → ONNX, .pt → PyTorch)
        """
        if not self._model_path.exists():
            raise FileNotFoundError(f"Model not found: {self._model_path}")

        backend_pref = self._resolve_backend_preference()
        suffix = self._model_path.suffix.lower()

        # Determine which backend to use
        use_onnx = False
        if backend_pref in {"onnx", "onnx_mlp"}:
            use_onnx = True
        elif backend_pref == "pytorch":
            use_onnx = False
        else:
            # auto or unknown — use file extension
            use_onnx = suffix == ".onnx"

        is_oww_model = suffix == ".onnx" and self._is_embedding_input_onnx()

        if use_onnx and (backend_pref == "onnx_mlp" or is_oww_model):
            self._load_onnx_mlp()
        elif use_onnx:
            self._load_onnx()
        elif suffix == ".pt" or backend_pref == "pytorch":
            self._load_pytorch()
        else:
            raise ValueError(f"Unsupported model format: {suffix}")

    def _is_embedding_input_onnx(self) -> bool:
        """Return True when the ONNX input expects OWW embeddings, not mel features.

        OWW-style models consume either:
        - `(batch, 96)` for MLP classifiers
        - `(batch, seq_len, 96)` for temporal classifiers

        Plain mel-spectrogram models instead take `(batch, n_mels, time_frames)`.
        Checking the declared ONNX input shape is more reliable than filename
        heuristics because user-trained files may not include naming markers.
        """
        if self._model_path.suffix.lower() != ".onnx":
            return False

        try:
            import onnxruntime as ort_local
        except ImportError:
            return False

        try:
            probe_session = ort_local.InferenceSession(
                str(self._model_path),
                sess_options=_single_threaded_session_options(ort_local),
                providers=["CPUExecutionProvider"],
            )
            inputs = probe_session.get_inputs()
            if not inputs:
                return False
            input_shape = inputs[0].shape
            return len(input_shape) in {2, 3} and input_shape[-1] == 96
        except (OSError, RuntimeError, ValueError):
            return False

    @staticmethod
    def _resolve_backend_preference() -> str:
        """Resolve the backend preference from settings and platform."""
        try:
            from config.settings import settings

            pref = settings.wake_inference_backend
        except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
            pref = "auto"

        if pref in {"onnx", "onnx_mlp", "pytorch"}:
            return pref

        # Auto: prefer ONNX on ARM (lightweight, no PyTorch needed)
        try:
            from core.platform import is_arm

            if is_arm():
                return "onnx"
        except (ImportError, RuntimeError) as exc:
            logger.debug("Unable to detect ARM wake backend preference: %s", exc)

        return "auto"  # Fall through to file-extension logic

    def _load_onnx(self) -> None:
        """Load model via ONNX Runtime."""
        try:
            import onnxruntime as ort_local
        except ImportError as exc:
            raise ImportError(
                "ONNX Runtime required for .onnx models. "
                "Install with: pip install onnxruntime. "
                "Original error: %s (%s)" % (exc, type(exc).__name__)
            ) from exc
        inference_session_factory: Callable[..., _OrtSession] = cast(
            Callable[..., _OrtSession], ort_local.InferenceSession
        )
        self._session = inference_session_factory(
            str(self._model_path),
            sess_options=_single_threaded_session_options(ort_local),
            providers=["CPUExecutionProvider"],
        )
        self._backend = "onnx"
        _release_torch_if_loaded()

    def _load_onnx_mlp(self) -> None:
        """Load OWW embedding extractor plus ONNX classifier (MLP or temporal CNN)."""
        if not _OWW_AVAILABLE:
            raise ImportError(
                "openwakeword is required for OWW-based models. " "Install with: pip install openwakeword"
            )

        try:
            import onnxruntime as ort_local
        except ImportError as exc:
            raise ImportError(
                "ONNX Runtime required for .onnx models. "
                "Install with: pip install onnxruntime. "
                "Original error: %s (%s)" % (exc, type(exc).__name__)
            ) from exc

        # Viola only needs OWW's *preprocessor* (AudioFeatures: melspec +
        # embedding extraction) — never the third-party pretrained wake
        # models that the full OWWModel() constructor would load (alexa,
        # hey_jarvis, ...). Construct AudioFeatures directly on the bundled
        # payloads. NO runtime download: a fresh install must bring up the
        # detector offline, and the install dir is read-only (requal-M3 —
        # the old download_models() bootstrap wrote 17 files / 18.3 MB into
        # _internal and made first-run wake depend on the network).
        melspec_path = _resolve_oww_preprocessor_model("melspectrogram.onnx")
        embedding_path = _resolve_oww_preprocessor_model("embedding_model.onnx")
        self._oww_preprocessor = OWWAudioFeatures(
            melspec_model_path=str(melspec_path),
            embedding_model_path=str(embedding_path),
            inference_framework="onnx",
            ncpu=1,
        )
        if not hasattr(self._oww_preprocessor, "onnx_execution_provider"):
            self._oww_preprocessor.onnx_execution_provider = "CPUExecutionProvider"

        inference_session_factory: Callable[..., _OrtSession] = cast(
            Callable[..., _OrtSession], ort_local.InferenceSession
        )
        self._session = inference_session_factory(
            str(self._model_path),
            sess_options=_single_threaded_session_options(ort_local),
            providers=["CPUExecutionProvider"],
        )
        self._backend = "onnx_mlp"

        # Detect temporal models from ONNX input shape: (batch, seq_len, 96) = temporal
        input_shape = self._session.get_inputs()[0].shape
        self._oww_temporal_seq_len: int = 0
        if len(input_shape) == 3 and input_shape[-1] == 96:
            self._oww_temporal_seq_len = input_shape[1] if isinstance(input_shape[1], int) else 9

        _release_torch_if_loaded()

    def _load_pytorch(self) -> None:
        """Load model via PyTorch."""
        if not _TORCH_AVAILABLE:
            raise ImportError("PyTorch required for .pt models. " "Install with: pip install torch")
        from violawake.model import WakeWordModel

        self._model = WakeWordModel.from_checkpoint(str(self._model_path), device=self._device)
        self._backend = "pytorch"

    def _maybe_reset_for_user(self) -> None:
        """Clear score history + debounce when the ambient user changes.

        Reads the auth-middleware-populated contextvar
        ``core.user_context.get_current_user_id``. When no context is set
        (wake detector thread default), leave state untouched to avoid
        thrashing between the context-free read thread and request paths.
        """
        try:
            from core.user_context import get_current_user_id

            current_user: str | None = get_current_user_id()
        except (ImportError, LookupError, RuntimeError):
            current_user = None

        if current_user is None:
            return  # No context — don't alter state.
        if current_user != self._bound_user_id:
            if self._bound_user_id is not None:
                logger.debug(
                    "ViolaWake user switch (%s -> %s); clearing history + debounce",
                    self._bound_user_id,
                    current_user,
                )
            self._score_history.clear()
            self._last_trigger_time = 0
            self._bound_user_id = current_user

    def process_audio(self, audio: np.ndarray) -> float:
        """
        Process an audio chunk and return wake word probability.

        If a callback is set and the wake word is detected (above threshold),
        the callback will be invoked (respecting debounce).

        Args:
            audio: Audio samples as numpy array (float32, 16kHz)
                  Should be approximately CLIP_SAMPLES in length

        Returns:
            Wake word probability (0.0 to 1.0)
        """
        self._maybe_reset_for_user()

        # Skip silent/near-silent audio (filter only true silence, not quiet speech)
        # Threshold from config (SILENCE_GATE_RMS). Lowered 0.005→0.001 (2026-03-06)
        # because 0.005 rejected 29/113 legitimate whisper/quiet speech eval files.
        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < SILENCE_GATE_RMS:
            return 0.0

        # normalize_audio() REMOVED — training does not normalize,
        # so inference must not either. Removing this improved TP
        # by +0.07 across all checkpoints (pipeline audit 2026-03-02).
        # Previously: audio = normalize_audio(audio, target_peak=0.95, max_gain=3.0)
        audio = center_crop(audio, CLIP_SAMPLES)

        if self._backend == "onnx_mlp":
            score = self._infer(audio)
        else:
            # Compute features (respects FEATURE_TYPE config)
            mel = compute_features(audio)
            score = self._infer(mel)
        self._last_score = score  # Store for callback access

        # Temporal smoothing: track consecutive high scores
        frame_positive = score > self.threshold
        self._score_history.append(frame_positive)

        # Keep only recent history
        if len(self._score_history) > self._temporal_window:
            self._score_history.pop(0)

        # AUD-R1 instrumentation: per-engine-instance probe so the
        # multi-user-isolation harness can assert user A's wake history
        # never leaks onto user B's engine. Gated by VIOLA_AUD_R1_DEBUG=1.
        if _AUD_R1_DEBUG:
            try:
                _aud_r1_logger.info(
                    "AUD_R1 wake_probe id=%d user=%s history_len=%d score=%.4f",
                    id(self),
                    self._bound_user_id,
                    len(self._score_history),
                    float(score),
                )
            except (RuntimeError, TypeError, ValueError) as exc:
                _aud_r1_logger.debug("AUD_R1 wake_probe logging failed: %s", exc)

        # Check for trigger: need N consecutive positive frames
        consecutive_positives = 0
        for is_positive in reversed(self._score_history):
            if is_positive:
                consecutive_positives += 1
            else:
                break

        should_trigger = consecutive_positives >= self._temporal_window

        if should_trigger:
            current_time = time.time()
            if current_time - self._last_trigger_time >= self.debounce_seconds:
                self._last_trigger_time = current_time
                self._score_history.clear()  # Reset after trigger

                # Log detection if enabled
                if self._fp_logging_enabled:
                    self._log_detection(audio, score)

                # Call callback
                if self._callback is not None:
                    logger.info(
                        "[STAGE3] threshold_pass: PASS score=%.3f threshold=%.3f",
                        score,
                        self.threshold,
                    )
                    self._callback()

        return score

    def _infer(self, features: np.ndarray) -> float:
        """Run model inference on the configured backend input."""
        if self._backend == "onnx":
            # ONNX Runtime inference
            # Input shape: (batch, n_mels, time_frames)
            assert self._session is not None  # Guaranteed when backend is onnx
            mel_input: np.ndarray = features[np.newaxis, :, :].astype(np.float32)
            inputs = self._session.get_inputs()
            if not inputs:
                raise RuntimeError("ONNX session has no inputs")
            outputs = self._session.run(None, {inputs[0].name: mel_input})
            if not outputs:
                raise RuntimeError("ONNX session returned no outputs")
            first = outputs[0]
            if isinstance(first, np.ndarray):
                return float(first.flatten()[0])
            raise RuntimeError(f"Unexpected ONNX output type: {type(first)!r}")

        elif self._backend == "onnx_mlp":
            assert self._oww_preprocessor is not None
            assert self._session is not None

            clip = np.clip(features, -1.0, 1.0)
            audio_int16 = (clip * 32767).astype(np.int16)
            if len(audio_int16) < CLIP_SAMPLES:
                audio_int16 = np.pad(audio_int16, (0, CLIP_SAMPLES - len(audio_int16)))
            else:
                audio_int16 = audio_int16[:CLIP_SAMPLES]

            embeddings = self._oww_preprocessor.embed_clips(audio_int16.reshape(1, -1), ncpu=1)

            if self._oww_temporal_seq_len > 0:
                # Temporal model: keep frame sequence (1, seq_len, 96)
                seq = embeddings[0].astype(np.float32)  # (N, 96)
                sl = self._oww_temporal_seq_len
                if seq.shape[0] < sl:
                    seq = np.pad(seq, ((0, sl - seq.shape[0]), (0, 0)))
                elif seq.shape[0] > sl:
                    seq = seq[-sl:]
                model_input = seq.reshape(1, sl, -1)
            else:
                # MLP model: mean-pool to (1, 96)
                model_input = embeddings.mean(axis=1)[0].astype(np.float32).reshape(1, -1)

            inputs = self._session.get_inputs()
            if not inputs:
                raise RuntimeError("ONNX session has no inputs")
            outputs = self._session.run(None, {inputs[0].name: model_input})
            if not outputs:
                raise RuntimeError("ONNX session returned no outputs")
            return float(np.asarray(outputs[0]).flatten()[0])

        elif self._backend == "pytorch":
            # PyTorch inference
            import torch

            assert self._model is not None  # Guaranteed when backend is pytorch
            with torch.no_grad():
                mel_tensor = torch.from_numpy(features).float().unsqueeze(0)
                mel_tensor = mel_tensor.to(self._device)
                output = self._model(mel_tensor)
                return float(output.item())

        return 0.0

    def set_callback(self, callback: Callable[[], None] | None) -> None:
        """
        Set callback function to be called on wake word detection.

        Args:
            callback: Function to call when wake word detected,
                     or None to disable callbacks
        """
        self._callback = callback

    def set_threshold(self, threshold: float) -> None:
        """
        Set detection threshold.

        Args:
            threshold: New threshold (0.0 to 1.0)
        """
        self.threshold = max(0.0, min(1.0, threshold))

    def set_fp_logging(self, enabled: bool, log_dir: str | None = None) -> None:
        """
        Enable/disable false positive logging.

        When enabled, saves audio chunks that triggered detection
        for later review and retraining.

        Args:
            enabled: Whether to enable logging
            log_dir: Directory to save audio files
                    (default: <user data dir>/violawake/false_positives/)
        """
        self._fp_logging_enabled = enabled
        if log_dir:
            self._fp_log_dir = Path(log_dir)
        else:
            # PATH-1: runtime WRITES anchor on the user data dir, never the
            # bundled (read-only / install-dir) violawake_data tree.
            from violawake.config import wake_runtime_data_dir

            self._fp_log_dir = wake_runtime_data_dir() / "false_positives"

        if enabled:
            self._fp_log_dir.mkdir(parents=True, exist_ok=True)

    def _log_detection(self, audio: np.ndarray, score: float) -> None:
        """Log a detection — consent-gated, UUID-named, optionally encrypted.

        Privacy guarantees:
          * Never writes when wake_data_contribute consent is disabled.
          * Filename is a random UUID with no score or timestamp to avoid
            side-channel leakage about when / how strongly a user woke.
          * When ``wake_audio_encrypt_at_rest`` is enabled, payload is
            sealed with AES-GCM using a user-derived key.
        """
        _ = score  # score MUST NOT leak into the filename
        if self._fp_log_dir is None:
            return

        if not _wake_audio_consent_granted():
            return

        try:
            import uuid

            self._fp_log_dir.mkdir(parents=True, exist_ok=True)
            filename_stem = uuid.uuid4().hex

            if _wake_audio_encrypt_at_rest():
                _write_wake_audio_encrypted(self._fp_log_dir / ("%s.wav.enc" % filename_stem), audio)
                return

            sf_module = _try_import_module("soundfile")
            if sf_module is None:
                raise ImportError("soundfile not available")
            sf = cast(_SoundFileModule, sf_module)

            filename = "%s.wav" % filename_stem
            path = self._fp_log_dir / filename
            sf.write(str(path), audio, SAMPLE_RATE)
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("Failed to write false positive audio: %s", exc)

    def reset(self) -> None:
        """Reset engine state (clear debounce timer and score history)."""
        self._last_trigger_time = 0
        self._score_history.clear()

    def fork_runtime_state(self) -> ViolaWake:
        """Return an isolated runtime engine sharing loaded model resources.

        The ONNX/PyTorch model artifacts are expensive and read-only for
        inference, but wake detection state is per audio stream. A fork keeps
        the loaded model/session references while giving the caller fresh score
        history, debounce, callback, and user binding state.
        """
        fork = object.__new__(type(self))
        fork.threshold = self.threshold
        fork.debounce_seconds = self.debounce_seconds
        fork._device = self._device
        fork._last_trigger_time = 0.0
        fork._last_score = 0.0
        fork._callback = None
        fork._fp_logging_enabled = self._fp_logging_enabled
        fork._fp_log_dir = self._fp_log_dir
        fork._temporal_window = self._temporal_window
        fork._score_history = []
        fork._bound_user_id = None
        fork._model_path = self._model_path
        fork._backend = self._backend
        fork._model = self._model
        fork._session = self._session
        fork._oww_preprocessor = self._oww_preprocessor
        fork._oww_temporal_seq_len = self._oww_temporal_seq_len
        return fork

    def set_temporal_window(self, window: int) -> None:
        """
        Set temporal smoothing window.

        Args:
            window: Number of consecutive high-score frames required to trigger.
                   Default is 2. Set to 1 to disable temporal smoothing.
        """
        self._temporal_window = max(1, window)
        self._score_history.clear()

    @property
    def model_path(self) -> Path:
        """Path to loaded model."""
        return self._model_path

    @property
    def backend(self) -> str:
        """Model backend ('onnx', 'onnx_mlp', or 'pytorch')."""
        return self._backend

    @property
    def sample_rate(self) -> int:
        """Required audio sample rate."""
        return SAMPLE_RATE

    @property
    def clip_samples(self) -> int:
        """Expected audio chunk size in samples."""
        return CLIP_SAMPLES

    @property
    def last_score(self) -> float:
        """Most recent wake word probability score from process_audio.

        This is set BEFORE the callback is invoked, so callbacks can
        access the actual score that triggered the detection.
        """
        return self._last_score

    def __repr__(self) -> str:
        return f"ViolaWake(model={self._model_path.name}, " f"backend={self._backend}, threshold={self.threshold})"
