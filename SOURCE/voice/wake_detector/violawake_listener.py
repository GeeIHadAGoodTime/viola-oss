"""
ViolaWake Listener
==================

Standalone wake word listener using the proprietary ViolaWake engine.

This is the primary wake word detection path when temporal_cnn.onnx is
available. CORRECTION (#4786, 2026-08-06): this docstring previously claimed
"NO OpenWakeWord dependency - only requires PyAudio" -- that was stale/wrong.
Ground-truthed against the actual shipped model
(violawake_data/trained_models/temporal_cnn.onnx): its ONNX graph declares
input "embeddings" with shape (batch, 9, 96), the OpenWakeWord embedding
shape, so violawake.engine.ViolaWake._load_model() always routes it through
_load_onnx_mlp(), which DOES require the openwakeword package (it constructs
openwakeword.utils.AudioFeatures to turn raw audio into those embeddings
before the classifier runs). See requirements_linux.txt / requirements_macos.txt
and utils/dependency_manager.py's VOICE_DEPENDENCIES for the pin.

AEC Integration (2025-12-06):
    - Applies frame-by-frame AEC before wake scoring
    - Uses loopback reference from WASAPISink
    - Includes playback gating to suppress false positives during music

Layered Defense Integration (2025-12-06):
    - Uses central WakeDecisionPolicy for all trigger decisions
    - VAD gating as first-line filter during playback
    - Confirmation window for stable detection
    - All magic numbers moved to config
"""

from __future__ import annotations

# CRITICAL: Import onnxruntime FIRST to avoid DLL load order issues
try:
    from importlib import import_module

    _ONNXRUNTIME_PRELOADED = import_module("onnxruntime")
except ImportError:
    pass

import logging
import re
import secrets
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from voice.wake_detector.aec_delay_calibrator import CalibrationResult
    from voice.wake_detector.device_profile_manager import DeviceProfile

import numpy as np

# PyAudio (PortAudio bindings) is the mic capture backend for the live wake
# listener. It is OS-portable (PortAudio backs it via ALSA/Pulse/PipeWire on
# Linux, WASAPI/MME on Windows), but the *wheel* may legitimately be absent on a
# given box — e.g. an import path that never starts live capture, or a partial
# install. Importing it UNCONDITIONALLY at module top makes the whole wake
# package fail to import on such a box, taking down everything that merely
# references the wake module (settings UI, diagnostics, the offline wake
# battery). Guard it so the module imports cleanly; the runtime path that
# actually opens a stream (ViolaWakeListener._init) checks `pyaudio is None` and
# fails closed there with a clear message instead of an import-time crash.
try:
    import pyaudio  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on boxes without PortAudio
    pyaudio = None  # type: ignore[assignment]

from audio_core.portaudio_guard import GuardedStream, open_portaudio, open_stream, terminate_portaudio
from config import AppConfig
from config.constants import AUDIO_SAMPLE_RATE
from core.constants import AUDIO_INT16_SCALE, SAMPLE_RATE_48K, TIMEOUT_MEDIUM
from core.logging_config import get_logger
from core.platform import get_logs_dir, get_temp_dir
from diagnostics.aec_effectiveness import get_aec_diagnostics

# Comprehensive wake diagnostics (lazy-loaded singletons)
from diagnostics.audio_pipeline_health import get_audio_health_monitor
from diagnostics.score_history import get_score_history
from diagnostics.wake_analytics import get_wake_analytics
from diagnostics.wake_audio_buffer import get_wake_audio_buffer
from diagnostics.wake_decision_trace import get_decision_tracer
from diagnostics.wake_metrics import get_wake_metrics
from services.liveness import WorkSignal

# Failure modes the PyAudio/PortAudio device layer can raise while tearing down
# or re-opening a capture stream (USB re-enumeration, "device busy", a driver
# reset, a host-API table freed underneath us). Enumerated deliberately rather
# than caught as a blind `except Exception` so a genuine programming error still
# propagates and is seen, while every real *device* failure stays recoverable:
#   OSError                - PortAudio errors surface as OSError from pyaudio
#   ImportError            - open_portaudio() imports pyaudio lazily
#   AttributeError         - pyaudio absent, or a stream torn down concurrently
#   ValueError             - invalid device index / unsupported stream format
#   MemoryError            - frame-buffer allocation failure
#   RuntimeError           - PortAudio guard/lock layer failures
#   IndexError, KeyError   - device-info lookups inside _get_input_device()
AUDIO_DEVICE_ERRORS: tuple[type[Exception], ...] = (
    AttributeError,
    ImportError,
    IndexError,
    KeyError,
    MemoryError,
    OSError,
    RuntimeError,
    ValueError,
)

# How long the device watcher waits for the read loop to close the old capture
# stream. Sized above the longest command recording (~7s onset+timeout) so an
# in-flight utterance normally completes and hands off cleanly; exceeding it is
# not fatal, because the pending flag is sticky and the reader still services
# the swap at its next safe point.
_DEVICE_CHANGE_QUIESCE_TIMEOUT = 12.0

# How long the read loop waits for the watcher to finish rebuilding the shared
# device table before re-opening anyway. Re-opening early is safe (this binding
# runs its own fresh Pa_Initialize); waiting forever would not be.
_DEVICE_CHANGE_RESUME_TIMEOUT = 15.0


try:
    from voice.wake_detector.wake_decision_policy import VAD_BLOCK_THRESHOLD as _VAD_GATE_STOP_AT
except ImportError:  # a stubbed policy module (tests)
    _VAD_GATE_STOP_AT = 0.05


def _fp_collecting_and_cleanup(collector: Any) -> bool:
    """The false-positive collector's switch; also runs its hourly retention cleanup."""
    cleanup = getattr(collector, "run_periodic_cleanup", None)
    if callable(cleanup):
        try:
            cleanup()
        except Exception:  # noqa: BLE001, RUF100 - cleanup is best effort
            logger.debug("FP collector cleanup failed", exc_info=True)
    return bool(collector.is_collecting)


def _is_wake_audio_logging_enabled() -> bool:
    try:
        from diagnostics.wake_audio_buffer import is_wake_audio_logging_enabled

        return bool(is_wake_audio_logging_enabled())
    except Exception:
        return False


def _resolve_custom_wake_model_path() -> Path | None:
    """Return the user's custom-trained wake-word model path if enabled.

    Reads `use_custom_wake_word` + `custom_wake_word_name` from the
    SettingsManager; falls back to `wake_word_model` if set directly;
    delegates model-path lookup to `WakeWordManager`.  Returns None
    when no custom word is active or the setting infra is unavailable.
    """

    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        active_model = sm.get("wake_word_active_model", "")
        if active_model and Path(active_model).is_file():
            return Path(active_model)

        if not sm.get("use_custom_wake_word", False):
            return None

        name = sm.get("custom_wake_word_name", "")
        if name:
            from scripts.wake_word_manager import WakeWordManager

            path = WakeWordManager().get_model_path(name)
            if path and Path(path).is_file():
                return Path(path)

        # Fallback to explicit model path if user pointed at one directly.
        explicit = sm.get("wake_word_model", "")
        if explicit and Path(explicit).is_file():
            return Path(explicit)
    except Exception:
        # Settings/manager not initialised (CLI context) — silently fall
        # through to the built-in model.
        return None
    return None


from voice.wake_detector.aec_processor import (
    AEC_FRAME_SAMPLES,
    AEC_SAMPLE_RATE,
    AECProcessor,
    create_aec_processor,
)
from voice.wake_detector.contributor_mode import get_contributor_manager
from voice.wake_detector.fp_collector import get_fp_collector
from voice.wake_detector.spoke_wake_diag import (
    DIAG_ENABLED as _SCORE_LOG_ENABLED,
    log_hub_inference,
)
from voice.wake_detector.wake_decision_policy import (
    WakeContext,
    WakeDecisionPolicy,
    get_wake_policy,
)

logger = get_logger(__name__)


def _viola_self_speech_active() -> bool:
    """True while Viola's own TTS is speaking (or just stopped). Fails open."""
    try:
        from voice.wake_detector.wake_decision_policy import viola_self_speech_active

        return bool(viola_self_speech_active())
    except Exception:  # noqa: BLE001, RUF100 - self-speech probe must never break wake detection
        return False


# Minimum seconds between per-frame AEC DEBUG metric lines. The wake loop runs
# ~60-100 frames/sec; logging every frame (or every 10th) floods the log with
# tens of thousands of lines/session and burns string-format + file-I/O on the
# shared interpreter that also paints the UI. One line every few seconds is
# plenty for diagnosing AEC behavior.
_AEC_LOG_INTERVAL_S = 5.0


def _sanitize_voice_temp_user_id(user_id: str) -> str:
    """Return a filesystem-safe user_id segment for voice temp files."""
    sanitized = re.sub(r"[^0-9A-Za-z]", "_", user_id)
    return sanitized or "default"


def _get_voice_temp_dir() -> Path:
    """Return the per-user temp directory for transient wake recordings."""
    deployment_mode = "user"
    try:
        from config.settings import settings

        deployment_mode = getattr(settings, "deployment_mode", "user")
    except Exception:
        deployment_mode = "user"

    try:
        from core.user_context import get_current_user_id

        user_id = get_current_user_id()
    except LookupError:
        if deployment_mode == "cloud":
            # Cloud: no anonymous processing — skip creating temp dir
            return get_temp_dir() / "viola_commands" / "anonymous"
        try:
            from core.user_context import get_device_user_id

            user_id = get_device_user_id()
        except Exception:
            user_id = "default"  # mt-ok: fallback for temp dir naming when user_context unavailable

    temp_dir = get_temp_dir() / "viola_commands" / _sanitize_voice_temp_user_id(user_id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    return temp_dir


# --------------------------------------------------------------------------- #
# AEC Metrics for Instrumentation                                              #
# --------------------------------------------------------------------------- #


@dataclass
class AECFrameMetrics:
    """Per-frame metrics for AEC debugging and monitoring."""

    mic_rms: float
    loopback_rms: float
    post_aec_rms: float
    aec_applied: bool
    aec_delay_ms: float
    correlation: float
    gating_active: bool
    wake_score: float | None = None


# Echo gating logic is handled by WakeDecisionPolicy.
# Call policy.update_audio_context() to update gating state based on loopback RMS and correlation.


# --------------------------------------------------------------------------- #
# Microphone capture thread                                                    #
# --------------------------------------------------------------------------- #


def _begin_fine_timer() -> bool:
    """Ask Windows for 1 ms timer resolution while the wake loop runs. True when set.

    Under GIL contention every hand-back of the GIL waits on a timed
    condition, and at the default 15.6 ms timer resolution each wait rounds up
    to a whole tick. The wake loop gives the GIL up several times per 80 ms
    chunk (each ONNX call, the blocking read), so with two busy Python threads
    it ran at 2-3x real time and the microphone dropped 50-67% of the audio
    (wake capture harness: 0 to 1 of 12 real "Viola" clips woke). With 1 ms
    resolution the same load dropped nothing once the capture thread was in,
    and 11 of 12 woke (the 12th falls in the engine warm-up). The multiroom
    stamper already does this while it plays. Also opts this process out of
    Windows 11's timer-resolution throttling for hidden windows (Viola runs
    hidden in the tray).
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:  # noqa: BLE001, RUF100 - an optimisation
        logger.debug("timeBeginPeriod(1) unavailable", exc_info=True)
        return False
    try:
        from ctypes import wintypes

        class _PowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG), ("StateMask", wintypes.ULONG)]

        # PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4, ProcessPowerThrottling = 4
        state = _PowerThrottlingState(1, 0x4, 0)
        kernel32 = ctypes.windll.kernel32
        kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), 4, ctypes.byref(state), ctypes.sizeof(state))
    except Exception:  # noqa: BLE001, RUF100 - older Windows: resolution still requested
        logger.debug("Timer-resolution throttling opt-out unavailable", exc_info=True)
    return True


def _end_fine_timer(was_set: bool) -> None:
    if not was_set:
        return
    try:
        import ctypes

        ctypes.windll.winmm.timeEndPeriod(1)
    except Exception:  # noqa: BLE001, RUF100
        logger.debug("timeEndPeriod(1) failed", exc_info=True)


class _PumpError:
    """A read error the capture thread hands to the consumer (raised there)."""

    __slots__ = ("exc",)

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class _MicPump:
    """Reads the microphone on its own thread so no audio is dropped while Viola is busy.

    Why (1.0.5 build-7 laptop report, A0.3 run 3): "Viola, pause" / "Viola,
    next" never woke while a long agent task ran (scores 0.60-0.815 against
    0.988 idle). The wake loop read the stream and then did all the work for
    that chunk on the same thread. PortAudio's MME input keeps only 240 ms of
    audio at this stream's 80 ms buffer size (measured: a longer gap between
    reads loses the rest, silently, no overflow flag). Every blocking read
    gives up the GIL, and getting it back while other Python threads are busy
    took 60-180 ms median and up to 1.3 s on a test PC ("Slow audio read:
    460ms" on the laptop). So while Viola worked, the loop fell behind and the
    audio it scored had pieces cut out of it, and a chopped "Viola" scores low.

    This thread only reads (it runs at raised OS priority and holds the GIL
    for microseconds per chunk) and queues the bytes; the wake loop, the Stage
    2 check and the command recorder consume the queue. If the consumer falls
    more than ``MAX_BACKLOG_S`` behind, it skips to the newest
    ``KEEP_AFTER_DROP_S`` in one jump, counted in ``drop_epoch`` so the
    consumer treats it as one gap.
    """

    MAX_BACKLOG_S = 4.0
    KEEP_AFTER_DROP_S = 1.0
    STALL_WARN_S = 0.2

    def __init__(self, owner: ViolaWakeListener) -> None:
        self._owner = owner
        self._cond = threading.Condition()
        self._queue: deque[bytes | _PumpError] = deque()
        self._max_chunks = max(1, round(self.MAX_BACKLOG_S * owner.SAMPLE_RATE / owner.CHUNK_SIZE))
        self._keep_after_drop = max(1, round(self.KEEP_AFTER_DROP_S * owner.SAMPLE_RATE / owner.CHUNK_SIZE))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.drop_epoch = 0
        self.dropped_chunks = 0
        self._last_stall_warning = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-mic-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    def flush(self) -> None:
        """Forget queued audio (the stream was reopened; that audio is stale)."""
        with self._cond:
            self._queue.clear()

    def available_chunks(self) -> int:
        with self._cond:
            return sum(1 for item in self._queue if not isinstance(item, _PumpError))

    def read(self, block: bool = True, timeout: float = 1.0) -> bytes | None:
        """The next chunk (raises the stream's read error). None: nothing yet / stopped."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._queue:
                if not block or self._stop.is_set():
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            item = self._queue.popleft()
        if isinstance(item, _PumpError):
            raise item.exc
        return item

    @staticmethod
    def _raise_priority() -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 2)  # THREAD_PRIORITY_HIGHEST
        except Exception:  # noqa: BLE001, RUF100 - priority is an optimisation
            logger.debug("Could not raise the wake capture thread priority", exc_info=True)

    def _run(self) -> None:
        self._raise_priority()
        owner = self._owner
        last_read = time.monotonic()
        while not self._stop.is_set():
            stream = owner._stream
            if stream is None or owner._device_change_pending.is_set():
                time.sleep(0.01)
                last_read = time.monotonic()
                continue
            try:
                data = stream.read(owner.CHUNK_SIZE, exception_on_overflow=False)
            except Exception as exc:  # noqa: BLE001, RUF100 - the consumer decides (reopen / give up)
                if self._stop.is_set():
                    break
                if owner._stream is stream:
                    with self._cond:
                        self._queue.append(_PumpError(exc))
                        self._cond.notify_all()
                time.sleep(0.05)
                last_read = time.monotonic()
                continue
            now = time.monotonic()
            gap = now - last_read
            last_read = now
            if gap > self.STALL_WARN_S + 0.08 and now - self._last_stall_warning > 30.0:
                self._last_stall_warning = now
                logger.warning(
                    "[TIMING] Wake capture thread waited %.0f ms between reads; the microphone keeps "
                    "about 240 ms, so some audio may be missing",
                    gap * 1000,
                )
            if owner._stream is not stream:
                continue  # read from a stream that was just replaced: stale
            with self._cond:
                self._queue.append(data)
                if len(self._queue) > self._max_chunks:
                    # Far behind: skip to the newest second in one jump. One
                    # gap (one engine warm-up) per jump; trimming a chunk at a
                    # time would make every chunk a gap and nothing would score.
                    excess = len(self._queue) - self._keep_after_drop
                    for _ in range(excess):
                        self._queue.popleft()
                    self.dropped_chunks += excess
                    self.drop_epoch += 1
                self._cond.notify_all()


class ViolaWakeListener:
    """
    Standalone wake word listener using ViolaWake engine.

    Uses temporal_cnn.onnx model with mel spectrogram input.
    No OpenWakeWord dependency required.

    AEC Support:
        Call set_aec_reference_source() with a callback that provides
        playback audio frames for echo cancellation.
    """

    # Audio constants
    SAMPLE_RATE = AUDIO_SAMPLE_RATE
    CHANNELS = 1
    CHUNK_SIZE = 1280  # 80ms at 16kHz
    # 16-bit signed PCM. Resolved from pyaudio at runtime so the class still
    # *defines* on a box without pyaudio (guarded import above). pyaudio.paInt16
    # is the constant 8; we mirror it as a literal so the class body never
    # touches a possibly-None pyaudio at definition time, and _init() verifies the
    # two agree (and refuses to open the stream on a mismatch) once pyaudio is
    # confirmed present.
    FORMAT = 8  # == pyaudio.paInt16

    def __init__(
        self,
        config: AppConfig,
        on_wake_word_detected: Callable[[], None],
        threshold: float = 0.80,
        model_path: str | Path | None = None,
        infer_interval: int | None = None,
        force_wake: bool = False,
    ):
        """
        Initialize ViolaWake listener.

        Args:
            config: Application configuration
            on_wake_word_detected: Callback when wake word is detected
            threshold: Detection threshold (0.0-1.0)
            model_path: Path to temporal_cnn.onnx model (auto-detected if None)
            infer_interval: Audio chunks between inference runs (auto-detected if None)
            force_wake: If True, bypass all policy layers (debug mode)
        """
        self.config = config
        self.on_wake_word_detected = on_wake_word_detected
        self.threshold = threshold
        # Debug: bypass all policy layers - check both param and env var
        from config.settings import settings as app_settings

        self._force_wake = force_wake or app_settings.force_wake
        if self._force_wake:
            logger.warning("🔧 FORCE_WAKE mode enabled - policy layers bypassed!")
        self._base_threshold = threshold  # Store original for gating adjustments

        # Find model path.  Resolution order (first match wins):
        #   1. Explicit model_path passed to constructor
        #   2. User's custom trained wake word (from SettingsManager +
        #      WakeWordManager) when `use_custom_wake_word` is True
        #   3. Version-aware default ViolaWake model
        #   4. Hardcoded fallback
        if model_path is None:
            model_path = _resolve_custom_wake_model_path()

        if model_path is None:
            from config.wake_config import get_violawake_model_path

            version = getattr(config, "wake_model_version", None)
            model_path = get_violawake_model_path(version)
            if model_path is None:
                # Fallback: bundled default model, anchored on the project
                # root (PATH-1 — never cwd-relative; in a frozen install the
                # bundle lives under _internal, not the launch cwd).
                from violawake.config import DEFAULT_MODEL_PATH

                model_path = DEFAULT_MODEL_PATH
            logger.info(
                "Using ViolaWake model: %s (version=%s)",
                model_path,
                version or "latest",
            )
        else:
            logger.info("Using ViolaWake model: %s", model_path)
        self._model_path = Path(model_path)
        # Inference interval: how many audio chunks between inference runs
        # MLP models can use interval=2 (faster inference), others use 5
        if infer_interval is not None:
            self._infer_interval = infer_interval
        else:
            # Auto-detect based on model filename
            model_name = self._model_path.name.lower()
            self._infer_interval = 2 if "mlp" in model_name else 5

        # Audio state
        self._audio: pyaudio.PyAudio | None = None
        self._stream: GuardedStream | None = None
        # Capture thread (see _MicPump): used for real PortAudio streams, or
        # when a test or harness sets _force_capture_pump.
        self._mic_pump: _MicPump | None = None
        self._force_capture_pump = False
        # Per-loop cache of settings-backed switches (see _loop_flag).
        self._loop_flags: dict[str, tuple[float, Any]] = {}
        self._loop_flag_refreshing: set[str] = set()
        self._loop_flag_queue: deque[tuple[str, Callable[[], Any]]] = deque()
        self._loop_flag_wake = threading.Event()
        self._loop_flag_thread: threading.Thread | None = None
        self._loop_tasks: deque[Callable[[], None]] = deque()
        self._is_initialized = False
        # _init() may call cleanup() on failure while already holding this
        # lock. Keep teardown exclusive without deadlocking that same thread.
        self._state_lock = threading.RLock()
        self._run_stop_event: threading.Event | None = None
        self._engine_lock = threading.RLock()
        self._engine_callback: Callable[[], None] | None = None
        self._device_name: str = "unknown"
        self._device_index: int | None = None

        # Recording state - prevents wake detection during command recording
        self._recording_command = False
        self._recording_lock = threading.Lock()

        # --- Device-change handoff -------------------------------------- #
        # An audio device change (headset connected, USB interface plugged in,
        # Windows default endpoint changed) has to move this listener onto the
        # new microphone. The tear-down must NOT happen on the watcher's thread:
        # this listener's stream is read from the wake loop's thread and, during
        # a command, from the async-bridge thread. Closing a PortAudio stream
        # under a live reader is a use-after-free in C, not a Python exception.
        #
        # So the watcher only raises a flag and waits; the close and re-open
        # happen on the thread that does the reading, at a point where it holds
        # the exclusive stream claim. These events are the handshake:
        #   pending  - watcher asks for a device swap (sticky until serviced)
        #   quiesced - reader confirms the old stream is closed
        #   resume   - watcher says the device table is rebuilt, re-open now
        self._device_change_pending = threading.Event()
        self._device_change_quiesced = threading.Event()
        self._device_change_resume = threading.Event()
        # Set only while run()'s read loop is actually spinning, so a suspend
        # request knows whether there is a reader to hand off to at all.
        self._loop_active = threading.Event()

        # Data collection: last trigger clip ID for correlation
        self._last_data_clip_id: int | None = None

        # ViolaWake engine - CRITICAL: Load BEFORE pyaudiowpatch to avoid DLL conflicts
        # When pyaudiowpatch loads first, it can break onnxruntime's InferenceSession creation.
        # Pre-loading the engine here ensures onnxruntime DLLs are loaded first.
        self._engine = None
        try:
            if self._model_path.exists():
                self._engine = self._load_engine(self._model_path)
                logger.info("ViolaWake engine pre-loaded: %s", self._engine)
            else:
                logger.error("ViolaWake model not found during init: %s", self._model_path)
        except Exception as e:
            logger.exception(
                "Failed to pre-load ViolaWake engine: %s (type=%s)",
                e,
                type(e).__name__,
            )
            # Engine will be None - _init() will handle the error
        self._streaming = False
        # (sensitivity level, Stage 2 on) the wake rule was last logged for.
        self._logged_wake_rule: tuple[float, bool] | None = None
        self._configure_cadence()

        # --- Passive AEC delay calibration ---
        self._passive_calibrator: Any = None
        self._aec_adapter: Any = None  # Set via set_aec_adapter()
        self._mic_ring: deque[np.ndarray] = deque(maxlen=500)  # ~5s of 10ms frames
        # Preroll frames: last N audio frames before wake onset, for recording context
        self._preroll_frames: deque[np.ndarray] = deque(maxlen=8)  # 8 frames = 640ms at 80ms/frame
        # Stage 2 wake check: raw frames read after a verified wake word, handed
        # to the command recorder so a "Viola, ..." said in one breath is whole.
        self._command_preroll: list[bytes] = []
        self._command_preroll_since: float | None = None
        self._command_preroll_lock = threading.Lock()
        self._verify_frame_count = 0
        # Stage 2 early start: the decode begins while the detector's agreement
        # run is still building (wake_verifier.EarlyStartTracker); the wake that
        # run confirms uses its result instead of waiting for a fresh decode.
        self._early_tracker: Any = None
        self._early_decode: Any = None
        self._wake_fired_frame = -1
        self._last_passive_cal_frame: int = 0
        self._passive_cal_interval_frames: int = 375  # ~30s at 80ms/frame

        # --- AEC Integration ---
        self._aec_enabled = getattr(config, "wake_aec_enabled", True)
        self._aec_processor: AECProcessor | None = None
        self._aec_reference_source: Callable[[int, int | None], np.ndarray] | None = None
        self._aec_delay_ms = getattr(config, "wake_aec_delay_ms", 50)

        # --- AEC Delay Calibration ---
        self._calibration_result: CalibrationResult | None = None
        # PATH-1: calibration writes land under the user data dir by default.
        from voice.wake_detector.wake_types import _default_calibration_dir

        self._calibration_data_path = getattr(config, "wake_calibration_data_path", None) or str(
            _default_calibration_dir()
        )
        self._delay_calibration_on_startup = getattr(config, "wake_aec_delay_calibration_on_startup", False)

        # --- Per-Device Profile & Auto-Calibration ---
        self._device_id: str = "unknown"
        self._device_profile: DeviceProfile | None = None
        self._needs_auto_calibration: bool = False
        self._auto_calibration_buffer_mic: list[np.ndarray] = []
        self._auto_calibration_buffer_playback: list[np.ndarray] = []
        self._auto_calibration_frames_needed: int = 0

        # --- Wake Decision Policy (central authority) ---
        self._policy = get_wake_policy(config)

        # Track current audio metrics for policy decisions
        self._current_loopback_rms = 0.0
        self._current_correlation = 0.0
        self._current_post_aec_rms = 0.0

        # Metrics instrumentation (enabled via config)
        self._metrics_enabled = getattr(config, "wake_aec_metrics_enabled", False)
        self._recent_metrics: deque[AECFrameMetrics] = deque(maxlen=100)
        self._metrics_lock = threading.Lock()
        # Per-frame AEC DEBUG line is throttled to once per N seconds. Emitting
        # it on every frame (or every 10th) means tens of thousands of
        # string-format + file-I/O calls per session on the SHARED interpreter
        # that also paints the UI — pure GIL contention for a diagnostic. The
        # rolling _recent_metrics deque still captures per-frame numbers for the
        # diagnostics API; only the log line is rate-limited.
        self._last_aec_log_ts: float = 0.0

        # Frame tap: external consumers (e.g. ContinuousMicCapture) receive
        # a copy of every raw audio chunk without opening their own stream.
        self._frame_tap: Callable[[bytes], None] | None = None
        # Same chunk after echo cancellation; see set_processed_frame_tap.
        self._processed_frame_tap: Callable[[bytes], None] | None = None

        # Liveness that cannot be faked: advanced ONLY by run()'s loop body, once
        # per audio frame actually read off the device. Every consumer that wants
        # to know whether wake detection is really happening -- the supervisor,
        # /health, the UI badge -- reads this one signal, so none of them can be
        # satisfied by a thread that exists but has stopped reading audio.
        #
        # A frame is ~80ms. stall_after is set far above that so ordinary jitter,
        # a stream reopen, and the read-error backoff cannot trip it, while a
        # genuinely dead loop is still caught in seconds. startup_grace covers
        # ONNX model load plus device open on a cold machine.
        self.detection_signal = WorkSignal(
            "wake_detection_loop",
            stall_after=15.0,
            startup_grace=90.0,
        )
        # Set when the capture device demonstrably failed to open. Distinct from
        # the signal above: the signal reports "no work lately", which is
        # ambiguous during a cold start, whereas this is the unambiguous fact
        # that there is no microphone to detect with. Health must never report
        # listening while this is set, however early in startup it happens.
        self._capture_open_failed = threading.Event()

        # Initialize AEC processor if enabled
        if self._aec_enabled:
            self._init_aec_processor()
            self._load_or_calibrate_delay()

        # --- Contributor Mode ---
        self._contributor_manager = get_contributor_manager(config)

        logger.info(
            "ViolaWakeListener created (threshold=%s, aec_enabled=%s, policy_threshold=%.2f, cooldown=%dms)",
            threshold,
            self._aec_enabled,
            self._policy._config.base_threshold,
            self._policy._config.cooldown_ms,
        )

    def _load_engine(self, model_path: Path) -> Any:
        """Create a ViolaWake engine for an already selected model path."""

        from violawake import ViolaWake

        return ViolaWake(
            model_path=str(model_path),
            threshold=self.threshold,
            debounce_seconds=2.0,
        )

    def _configure_cadence(self) -> None:
        """Pick the scoring cadence for the loaded engine.

        Temporal openWakeWord-embedding models (the shipped temporal_cnn.onnx)
        are scored on EVERY 80 ms chunk through the engine's incremental
        embedding stream, and the engine wakes on its k-of-n agreement rule
        (wake plan Stage 1). Any other model keeps the old path: re-score the
        1.5 s ring buffer every ``_infer_interval`` chunks, single-frame rule.
        """
        engine = self._engine
        self._streaming = bool(engine is not None and getattr(engine, "supports_streaming", False) is True)
        if self._streaming:
            self._infer_interval = 1
            # The rule depends on the Stage 2 check (STREAM_OPERATING_POINTS_CHECKED).
            # The loop sets engine.checked before every chunk; set it here too, or
            # this line reports the unchecked rule (1.0.5: "7 of 7 at 0.79" at
            # Balanced while 6 of 7 at 0.85 was the rule actually applied).
            checked = self._stage2_check_on()
            engine.checked = checked
            k, n = engine.agreement
            logger.info(
                "Wake scoring cadence: every chunk (80 ms, streaming embeddings), wake on %d of %d frames "
                "at frame threshold %.2f (sensitivity level %.2f, Stage 2 check %s)",
                k,
                n,
                engine.stream_threshold,
                engine.threshold,
                "on" if checked else "off",
            )
            self._logged_wake_rule = (round(float(engine.threshold), 4), checked)
        else:
            logger.info(
                "Wake scoring cadence: every %d chunks (%d ms), single frame",
                self._infer_interval,
                80 * self._infer_interval,
            )

    def reload_model(self, model_path: str | Path) -> dict[str, object]:
        """Validate and swap the active wake-word model without restarting audio capture."""

        candidate = Path(model_path).expanduser()
        if not candidate.is_file():
            return {
                "reloaded": False,
                "reason": "model_not_found",
                "model_path": str(candidate),
            }

        try:
            new_engine = self._load_engine(candidate)
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            logger.warning("Wake model reload rejected for %s: %s", candidate, exc)
            return {
                "reloaded": False,
                "reason": "invalid_model",
                "model_path": str(candidate),
            }

        with self._engine_lock:
            if self._engine_callback is not None:
                new_engine.set_callback(self._engine_callback)
            previous_path = self._model_path
            self._engine = new_engine
            self._model_path = candidate
            model_name = candidate.name.lower()
            self._infer_interval = 2 if "mlp" in model_name else 5
            self._configure_cadence()

        logger.info("Wake model hot-reloaded: %s -> %s", previous_path, candidate)
        self._emit_status_event(
            "wake_model_reloaded",
            {
                "model_path": str(candidate),
                "previous_model_path": str(previous_path),
                "mode": "violawake",
            },
        )
        return {
            "reloaded": True,
            "model_path": str(candidate),
            "previous_model_path": str(previous_path),
        }

    def set_frame_tap(self, callback: Callable[[bytes], None] | None) -> None:
        """Register a callback that receives every raw audio chunk."""
        self._frame_tap = callback

    def set_processed_frame_tap(self, callback: Callable[[bytes], None] | None) -> None:
        """Register a callback that receives every chunk AFTER echo cancellation.

        The echo canceller and its reference buffer belong to this listener and
        run exactly once per chunk. A consumer that needs echo-cancelled audio
        (the barge-in monitor) must take it from here rather than run the
        shared canceller a second time: the reference is a queue, so a second
        reader steals half of it and the adaptive filter diverges.
        """
        self._processed_frame_tap = callback

    def _emit_processed_frame(self, processed_int16: np.ndarray) -> None:
        tap = self._processed_frame_tap
        if tap is None:
            return
        try:
            tap(processed_int16.astype(np.int16, copy=False).tobytes())
        except Exception:  # noqa: BLE001, RUF100 - never let a tap error stop the wake loop
            pass

    def _init_aec_processor(self) -> None:
        """Initialize AEC processor based on config."""
        try:
            backend = getattr(self.config, "wake_aec_backend", "auto")
            filter_length_ms = getattr(self.config, "wake_aec_filter_length_ms", 200)
            denoise = getattr(self.config, "wake_aec_denoise_enabled", True)

            delay_samples = int(self._aec_delay_ms * self.SAMPLE_RATE / 1000)
            self._aec_processor = create_aec_processor(
                enabled=True,
                backend=backend,
                frame_samples=AEC_FRAME_SAMPLES,
                filter_length_ms=filter_length_ms,
                enable_denoise=denoise,
                delay_samples=delay_samples,
            )
            logger.info(
                "AEC processor initialized: %s (delay=%dms / %d samples)",
                self._aec_processor.name,
                self._aec_delay_ms,
                delay_samples,
            )
        except Exception as e:
            logger.warning("Failed to initialize AEC processor: %s", e)
            self._aec_processor = None

    def _propagate_delay_to_aec(self) -> None:
        """Push the current ``_aec_delay_ms`` to the running AEC processor.

        Called after calibration (startup, runtime, or auto-calibration)
        so the adaptive filter sees reference samples aligned to what the
        mic actually captured. Without this, measured delay was stored
        but never reached the filter, leaving ERLE at 0 dB.
        """
        if self._aec_processor is None:
            return
        delay_samples = int(self._aec_delay_ms * self.SAMPLE_RATE / 1000)
        try:
            self._aec_processor.set_delay_samples(delay_samples)
            logger.info(
                "AEC delay propagated: %.1fms / %d samples",
                self._aec_delay_ms,
                delay_samples,
            )
        except Exception:
            logger.exception("Failed to propagate AEC delay to processor")

    def _load_or_calibrate_delay(self) -> None:
        """Load saved calibration from device profile or mark for auto-calibration."""
        from voice.wake_detector.device_profile_manager import (
            get_device_fingerprint,
            load_profile,
        )

        # Get device fingerprint for per-device calibration
        try:
            self._device_id = get_device_fingerprint()
        except Exception as e:
            logger.warning("Could not get device fingerprint: %s", e)
            self._device_id = "unknown"

        # Try to load existing device profile
        self._device_profile = load_profile(self._device_id)

        if self._device_profile is not None and self._device_profile.aec_delay_ms is not None:
            # Use saved calibration from device profile
            self._aec_delay_ms = self._device_profile.aec_delay_ms
            self._calibration_result = type(
                "CalibrationResult",
                (),
                {
                    "success": True,
                    "delay_ms": self._device_profile.aec_delay_ms,
                    "confidence": self._device_profile.aec_calibration_confidence,
                },
            )()
            logger.info(
                "Loaded AEC calibration for device %s: %.1fms delay (confidence=%.2f)",
                self._device_id[:8],
                self._device_profile.aec_delay_ms,
                self._device_profile.aec_calibration_confidence,
            )
            self._needs_auto_calibration = False
            self._propagate_delay_to_aec()
            return

        # No saved calibration - mark for auto-calibration
        logger.info(
            "No AEC calibration for device %s, using default delay: %dms (auto-calibration pending)",
            self._device_id[:8],
            self._aec_delay_ms,
        )
        self._needs_auto_calibration = True
        self._auto_calibration_buffer_playback: list[np.ndarray] = []
        self._auto_calibration_buffer_mic: list[np.ndarray] = []
        self._auto_calibration_frames_needed = int(2.0 * self.SAMPLE_RATE / AEC_FRAME_SAMPLES)  # 2 seconds

    def run_calibration(self) -> bool:
        """
        Run AEC delay calibration.

        This should be called when the user wants to calibrate the AEC delay.
        It will play calibration signals and measure the speaker-to-mic delay.

        Returns:
            True if calibration succeeded, False otherwise
        """

        from voice.wake_detector.aec_delay_calibrator import AECDelayCalibrator
        from voice.wake_detector.wake_types import CalibrationConfig

        logger.info("Running AEC delay calibration...")

        config = CalibrationConfig(
            playback_sample_rate=SAMPLE_RATE_48K,
            recording_sample_rate=self.SAMPLE_RATE,
            calibration_data_path=self._calibration_data_path,
        )

        def on_progress(stage: str, progress: float) -> None:
            logger.info("Calibration: %s (%.0f%%)", stage, progress * 100)
            # Emit status event for UI
            self._emit_status_event(
                "calibration_progress",
                {"stage": stage, "progress": progress},
            )

        calibrator = AECDelayCalibrator(
            config=config,
            on_progress=on_progress,
        )

        result = calibrator.calibrate()

        if result.success:
            self._calibration_result = result
            self._aec_delay_ms = result.delay_ms
            self._propagate_delay_to_aec()

            logger.info(
                "Calibration successful: %sms delay (confidence=%s)",
                format(result.delay_ms, ".1f"),
                format(result.confidence, ".2f"),
            )

            # Emit success event
            self._emit_status_event(
                "calibration_complete",
                {
                    "success": True,
                    "delay_ms": result.delay_ms,
                    "confidence": result.confidence,
                },
            )

            return True
        else:
            logger.warning("Calibration failed: %s", result.failure_reason)

            # Emit failure event
            self._emit_status_event(
                "calibration_complete",
                {
                    "success": False,
                    "reason": result.failure_reason,
                },
            )

            return False

    def get_calibration_status(self) -> dict[str, Any]:
        """
        Get current calibration status.

        Returns:
            Dict with calibration information
        """
        if self._calibration_result is None:
            return {
                "calibrated": False,
                "delay_ms": self._aec_delay_ms,
                "source": "default",
            }

        return {
            "calibrated": True,
            "delay_ms": self._calibration_result.delay_ms,
            "confidence": self._calibration_result.confidence,
            "source": "measured",
        }

    def set_aec_reference_source(
        self,
        callback: Callable[[int, int | None], np.ndarray] | None,
    ) -> None:
        """
        Set the AEC reference source callback.

        This callback should return playback audio frames for echo cancellation.
        Signature: callback(frame_samples: int, target_rate: int | None) -> np.ndarray

        Args:
            callback: Function that returns reference audio, or None to disable
        """
        self._aec_reference_source = callback
        if callback is not None:
            logger.info("AEC reference source connected")
        else:
            logger.info("AEC reference source disconnected")

    def set_aec_adapter(self, adapter: Any) -> None:
        """Set the ChunkStamperAECAdapter for passive delay calibration.

        The adapter provides ``get_recent_reference()`` for cross-correlation
        and ``set_buffer_target()`` to update the delay adaptively.
        """
        self._aec_adapter = adapter
        if adapter is not None:
            logger.info("AEC adapter connected for passive calibration")

    def has_aec_reference(self) -> bool:
        """Check if AEC reference is available."""
        return self._aec_reference_source is not None and self._aec_processor is not None

    def set_playback_context(self, is_playing: bool, volume: int = 80) -> None:
        """
        Update playback context for the wake decision policy.

        Called by audio pipeline when playback starts/stops.

        Args:
            is_playing: Whether media is currently playing
            volume: Current playback volume (0-100)
        """
        self._policy.set_playback_active(is_playing, volume)
        # During Viola's OWN local playback the AEC reference is Viola's known
        # output, so the AEC backend may downshift off its heavy per-frame FDAF
        # FFT to a cheap native canceller — freeing the shared interpreter's GIL
        # for the UI/video loop. The reference source here is Viola's WASAPI
        # loopback; this is not a phone-call mic or external room source.
        if self._aec_processor is not None:
            try:
                self._aec_processor.set_local_playback_active(is_playing)
            except (AttributeError, ImportError, OSError, RuntimeError, ValueError, MemoryError):
                logger.debug("AEC set_local_playback_active failed", exc_info=True)
        logger.debug("Playback context updated: playing=%s, volume=%d", is_playing, volume)

    def get_policy(self) -> WakeDecisionPolicy:
        """Get the wake decision policy (for external access)."""
        return self._policy

    def _apply_aec_to_chunk(self, mic_int16: np.ndarray, frame_count: int) -> np.ndarray:
        """
        Apply AEC processing to a microphone audio chunk.

        Processes the chunk in AEC_FRAME_SAMPLES-sized frames (10ms at 16kHz).
        Also updates the playback gating state.

        Args:
            mic_int16: Microphone audio as int16 numpy array
            frame_count: Current frame number for logging

        Returns:
            Processed audio as int16 numpy array (same size as input)
        """
        # If AEC is not available, return unchanged
        if self._aec_processor is None or self._aec_reference_source is None:
            # Log periodically to help diagnose AEC wiring issues
            if frame_count % 500 == 0:  # Log every ~5 seconds at 100 frames/sec
                logger.debug(
                    "AEC_NOT_WIRED: processor=%s, source=%s at frame %d",
                    "present" if self._aec_processor else "None",
                    "present" if self._aec_reference_source else "None",
                    frame_count,
                )
            return mic_int16

        # Log AEC active status on first frame
        if frame_count == 1:
            logger.info("AEC_WIRED: Processing audio with echo cancellation active")

        # Sync the AEC backend's local-playback downshift from the policy, the
        # single source of truth for whether Viola's OWN player is playing
        # (driven by music.player.on_state_change / the poller — not the mic).
        # Doing it here (once per chunk, a cheap bool compare) guarantees the
        # signal reaches the AEC regardless of which caller updated the policy,
        # so the heavy per-frame FDAF FFT is downshifted during playback no
        # matter the wiring route. set_local_playback_active is a no-op when
        # unchanged, so this is effectively free.
        try:
            self._aec_processor.set_local_playback_active(bool(self._policy.is_playback_active))
        except (AttributeError, ImportError, OSError, RuntimeError, ValueError, MemoryError):
            if frame_count == 1:
                logger.debug("AEC local-playback sync unavailable", exc_info=True)

        try:
            processed_frames = []
            chunk_len = len(mic_int16)

            # Process in AEC_FRAME_SAMPLES-sized frames (10ms at 16kHz = 160 samples)
            for i in range(0, chunk_len, AEC_FRAME_SAMPLES):
                frame_end = min(i + AEC_FRAME_SAMPLES, chunk_len)
                mic_frame = mic_int16[i:frame_end]

                # Pad if needed
                if len(mic_frame) < AEC_FRAME_SAMPLES:
                    mic_frame = np.pad(mic_frame, (0, AEC_FRAME_SAMPLES - len(mic_frame)))

                # Get reference frame from loopback
                ref_frame = self._aec_reference_source(AEC_FRAME_SAMPLES, AEC_SAMPLE_RATE)

                # --- TEMPORARY DIAG: Log ref_frame RMS every 500th outer frame ---
                if frame_count % 500 == 0 and i == 0:
                    ref_rms_diag = float(np.sqrt(np.mean(ref_frame.astype(np.float32) ** 2)))
                    ref_nonzero = int(np.count_nonzero(ref_frame))
                    logger.debug(
                        "AEC_REF_DIAG: frame=%d ref_rms=%.1f ref_nonzero=%d/%d ref_min=%d ref_max=%d",
                        frame_count,
                        ref_rms_diag,
                        ref_nonzero,
                        len(ref_frame),
                        int(np.min(ref_frame)),
                        int(np.max(ref_frame)),
                    )
                # --- END TEMPORARY DIAG ---

                # Compute correlation for gating
                correlation = self._compute_correlation(mic_frame, ref_frame)
                loopback_rms = float(np.sqrt(np.mean(ref_frame.astype(np.float32) ** 2)))

                # Store for policy decisions
                self._current_loopback_rms = loopback_rms
                self._current_correlation = correlation

                # Compute mic_rms for this frame
                mic_rms = float(np.sqrt(np.mean(mic_frame.astype(np.float32) ** 2)))

                # Apply AEC FIRST so we can compute actual post_aec_rms
                processed_frame = self._aec_processor.process_frame(mic_frame, ref_frame)

                # --- TEMPORARY: WAV capture for AEC signal analysis ---
                if not hasattr(self, "_aec_capture_mic"):
                    self._aec_capture_mic = []
                    self._aec_capture_ref = []
                    self._aec_capture_out = []
                    self._aec_capture_count = 0
                    self._aec_capture_flushed = False
                if not self._aec_capture_flushed:
                    self._aec_capture_mic.append(mic_frame.copy())
                    self._aec_capture_ref.append(ref_frame.copy())
                    self._aec_capture_out.append(processed_frame[: frame_end - i].copy())
                    self._aec_capture_count += 1
                    if self._aec_capture_count >= 1000:
                        self._flush_aec_capture()
                # --- END TEMPORARY ---

                processed_frames.append(processed_frame[: frame_end - i])

                # Compute post-AEC RMS for policy VAD estimation
                post_aec_rms = float(np.sqrt(np.mean(processed_frame.astype(np.float32) ** 2)))

                # Update policy with audio context including actual post_aec_rms
                self._policy.update_audio_context(
                    loopback_rms=loopback_rms,
                    correlation=correlation,
                    mic_rms=mic_rms,
                    post_aec_rms=post_aec_rms,
                )

                # --- Diagnostic: AEC effectiveness ---
                try:
                    aec_diag = get_aec_diagnostics()
                    aec_diag.record_frame(
                        mic_rms=mic_rms,
                        reference_rms=loopback_rms,
                        post_aec_rms=post_aec_rms,
                        correlation=correlation,
                        gating_active=self._policy._echo_state.echo_gating_active,
                    )
                except Exception as diag_err:
                    if frame_count == 1:
                        logger.debug("AEC diagnostics unavailable: %s", diag_err)

                # Record metrics into the rolling diagnostics deque at the
                # existing low cadence, but throttle the DEBUG *log line* to at
                # most once per _AEC_LOG_INTERVAL_S seconds. Emitting it per
                # frame (~60/sec) cost tens of thousands of string-format +
                # file-I/O calls per session on the shared interpreter — pure
                # GIL contention with the UI paint loop for a diagnostic.
                if self._metrics_enabled and frame_count % 10 == 0:
                    metrics = AECFrameMetrics(
                        mic_rms=mic_rms,
                        loopback_rms=loopback_rms,
                        post_aec_rms=post_aec_rms,
                        aec_applied=True,
                        aec_delay_ms=self._aec_delay_ms,
                        correlation=correlation,
                        gating_active=self._policy._echo_state.echo_gating_active,  # Use policy state
                    )
                    self._record_metrics(metrics)

                    now = time.monotonic()
                    if now - self._last_aec_log_ts >= _AEC_LOG_INTERVAL_S:
                        self._last_aec_log_ts = now
                        self._log_metrics(metrics, frame_count)

                # Auto-calibration: buffer audio when calibration is pending
                if getattr(self, "_needs_auto_calibration", False) and loopback_rms > 500:
                    self._auto_calibration_buffer_mic.append(mic_frame.copy())
                    self._auto_calibration_buffer_playback.append(ref_frame.copy())

                    # Check if we have enough audio
                    if len(self._auto_calibration_buffer_mic) >= self._auto_calibration_frames_needed:
                        self._run_auto_calibration()

            return np.concatenate(processed_frames)

        except Exception as e:
            logger.warning("AEC processing error: %s", e)
            return mic_int16

    def _compute_correlation(self, mic: np.ndarray, ref: np.ndarray) -> float:
        """
        Compute normalized cross-correlation between mic and reference.

        High correlation indicates the mic is picking up the playback audio
        (speaker bleed), which helps identify when gating should be active.

        Returns value in range [-1, 1], where 1 = identical signals.
        """
        try:
            mic_f: np.ndarray = mic.astype(np.float32)
            ref_f: np.ndarray = ref.astype(np.float32)

            # Normalize
            mic_norm = np.linalg.norm(mic_f)
            ref_norm = np.linalg.norm(ref_f)

            if mic_norm < 1e-6 or ref_norm < 1e-6:
                return 0.0

            # Normalized dot product
            correlation = float(np.dot(mic_f, ref_f) / (mic_norm * ref_norm))
            return max(-1.0, min(1.0, correlation))

        except Exception as e:
            logger.debug("Correlation calculation failed: %s", e)
            return 0.0

    def _run_auto_calibration(self) -> None:
        """
        Run auto-calibration using buffered audio.

        Called automatically when enough playback audio has been captured.
        Uses correlation between playback and mic to measure speaker-to-mic delay.
        Saves result to device profile for future sessions.
        """
        if not getattr(self, "_needs_auto_calibration", False):
            return

        logger.info(
            "Running auto-calibration with %d frames of audio...",
            len(self._auto_calibration_buffer_mic),
        )

        try:
            from voice.wake_detector.device_profile_manager import (
                DeviceProfile,
                get_device_display_name,
                save_profile,
            )
            from voice.wake_detector.startup_calibration import StartupCalibrator

            # Concatenate buffered audio
            mic_audio = np.concatenate(self._auto_calibration_buffer_mic)
            playback_audio = np.concatenate(self._auto_calibration_buffer_playback)

            # Run calibration
            calibrator = StartupCalibrator(sample_rate=self.SAMPLE_RATE)
            result = calibrator.calibrate_with_audio(
                played_audio=playback_audio,
                recorded_audio=mic_audio,
                playback_sample_rate=AEC_SAMPLE_RATE,
                recording_sample_rate=self.SAMPLE_RATE,
            )

            if result.success:
                # Update AEC delay
                self._aec_delay_ms = result.delay_ms
                self._calibration_result = result
                self._propagate_delay_to_aec()

                # Save to device profile
                if self._device_profile is None:
                    self._device_profile = DeviceProfile(
                        device_id=self._device_id,
                        device_name=get_device_display_name(),
                    )

                self._device_profile.aec_delay_ms = result.delay_ms
                self._device_profile.aec_delay_samples = result.delay_samples
                self._device_profile.aec_calibration_confidence = result.confidence
                self._device_profile.aec_calibrated_at = __import__("time").time()

                save_profile(self._device_profile)

                logger.info(
                    "Auto-calibration successful for device %s: %.1fms delay (confidence=%.3f)",
                    self._device_id[:8],
                    result.delay_ms,
                    result.confidence,
                )

                # Emit event for UI
                self._emit_status_event(
                    "auto_calibration_complete",
                    {
                        "success": True,
                        "delay_ms": result.delay_ms,
                        "confidence": result.confidence,
                        "device_id": self._device_id,
                    },
                )
            else:
                logger.warning(
                    "Auto-calibration failed: %s (will retry on next playback)",
                    result.failure_reason,
                )

        except Exception as e:
            logger.warning("Auto-calibration error: %s", e, exc_info=True)

        finally:
            # Clear auto-calibration state regardless of result
            self._needs_auto_calibration = False
            self._auto_calibration_buffer_mic = []
            self._auto_calibration_buffer_playback = []

    def _run_passive_calibration(self, frame_count: int) -> None:
        """Measure speaker-to-mic delay using passive cross-correlation.

        Runs every ~30s during music playback when DTD is not active.
        Updates the AEC adapter's buffer_target if confidence is sufficient.
        """
        if self._aec_adapter is None:
            return

        # Only during active playback with audible reference
        if self._current_loopback_rms < 200:
            return

        # Skip during double-talk (speech over music = bad correlation)
        if self._aec_processor is not None:
            diag = self._aec_processor.get_diagnostics()
            if diag.get("dtd_ratio", 0.0) > 0.3:
                return

        try:
            if self._passive_calibrator is None:
                from audio_core.calibration.passive_calibrate import PassiveCalibrator

                self._passive_calibrator = PassiveCalibrator()

            # Grab ~200ms of reference from adapter ring buffer (3200 samples at 16kHz)
            ref_samples = 3200
            reference = self._aec_adapter.get_recent_reference(ref_samples)
            if len(reference) < 1600:  # Need at least 100ms
                return

            # Grab ~500ms of mic audio from our ring buffer (8000 samples at 16kHz)
            mic_chunks = list(self._mic_ring)
            if len(mic_chunks) < 50:  # Need at least ~500ms
                return
            mic = np.concatenate(mic_chunks[-50:])  # Last ~500ms

            result = self._passive_calibrator.measure(reference, mic)

            if result.confident:
                self._aec_adapter.set_buffer_target(result.delay_ms)

        except Exception as e:
            logger.debug("[AEC_CALIBRATE] passive measurement error: %s", e)

    def _record_metrics(self, metrics: AECFrameMetrics) -> None:
        """Record metrics for later retrieval."""
        with self._metrics_lock:
            self._recent_metrics.append(metrics)

    def _log_metrics(self, metrics: AECFrameMetrics, frame_count: int) -> None:
        """Log AEC metrics for debugging."""
        logger.debug(
            "[AEC] frame=%d mic_rms=%.0f loopback_rms=%.0f post_aec_rms=%.0f corr=%.2f gating=%s delay=%dms",
            frame_count,
            metrics.mic_rms,
            metrics.loopback_rms,
            metrics.post_aec_rms,
            metrics.correlation,
            "active" if metrics.gating_active else "open",
            int(metrics.aec_delay_ms),
        )

    # --- TEMPORARY: AEC signal capture for diagnosis ---
    def _flush_aec_capture(self) -> None:
        """Flush captured AEC signals to wav files for offline analysis."""
        import wave

        logs_dir = get_logs_dir()
        logs_dir.mkdir(exist_ok=True)
        for name, buf in [
            ("mic_raw", self._aec_capture_mic),
            ("ref_raw", self._aec_capture_ref),
            ("aec_out", self._aec_capture_out),
        ]:
            data = np.concatenate(buf)
            path = logs_dir / f"{name}.wav"
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(data.tobytes())
            logger.info(
                "AEC_CAPTURE: Wrote %s (%d samples, %.1fs)",
                path,
                len(data),
                len(data) / 16000,
            )
        self._aec_capture_flushed = True
        logger.info("AEC_CAPTURE: Done. Run: python logs/analyze_aec_signals.py")

    # --- END TEMPORARY ---

    def get_aec_metrics(self) -> list[dict]:
        """
        Get recent AEC metrics for monitoring/debugging.

        Returns:
            List of metric dictionaries from recent frames
        """
        with self._metrics_lock:
            return [
                {
                    "mic_rms": m.mic_rms,
                    "loopback_rms": m.loopback_rms,
                    "post_aec_rms": m.post_aec_rms,
                    "aec_applied": m.aec_applied,
                    "aec_delay_ms": m.aec_delay_ms,
                    "correlation": m.correlation,
                    "gating_active": m.gating_active,
                    "wake_score": m.wake_score,
                }
                for m in self._recent_metrics
            ]

    def get_aec_snapshot(self) -> dict[str, Any]:
        """Assemble a comprehensive AEC diagnostic snapshot for AI tuning.

        Collects data from the FDAF filter, reference adapter, passive
        calibrator, wake policy, and AECDiagnostics into a single dict.
        Any sub-source that is unavailable returns null fields with a
        companion error string.
        """
        import datetime

        snapshot: dict[str, Any] = {"captured_at": datetime.datetime.now(datetime.UTC).isoformat()}

        # --- 1. Filter diagnostics (ViolaAEC inner) ---
        filter_diag: dict[str, Any] = {}
        filter_diag_error: str | None = None
        try:
            if self._aec_processor is not None:
                # ViolaAECAdapter wraps ViolaAEC as _inner
                inner = getattr(self._aec_processor, "_inner", None)
                if inner is not None and hasattr(inner, "get_diagnostics"):
                    filter_diag = inner.get_diagnostics()
                else:
                    # Fallback: processor-level diagnostics (less detailed)
                    filter_diag = self._aec_processor.get_diagnostics()
            else:
                filter_diag_error = "no_aec_processor"
        except Exception as e:
            filter_diag_error = str(e)

        # --- 2. Identity ---
        identity: dict[str, Any] = {
            "backend": (getattr(self._aec_processor, "name", "none") if self._aec_processor else "none"),
            "filter_length_taps": filter_diag.get("filter_length_taps"),
            "filter_length_ms": filter_diag.get("filter_length_ms"),
            "fft_size": filter_diag.get("fft_size"),
            "frame_size": AEC_FRAME_SAMPLES,
            "sample_rate_hz": AEC_SAMPLE_RATE,
            "ref_adapter": (
                type(self._aec_adapter).__name__
                if self._aec_adapter is not None
                else ("callback" if self._aec_reference_source is not None else "none")
            ),
            "mic_device": getattr(self, "_device_id", "unknown"),
        }

        # --- 3. Parameters ---
        param_keys = [
            "mu",
            "dtd_threshold",
            "dtd_holdoff_ms",
            "warmup_ms",
            "echo_scale",
            "dtd_mode",
            "dtd_gain",
            "dtd_leak",
            "regularization_eps",
            "power_smoothing_alpha",
            "baseline_smoothing_alpha",
        ]
        parameters = {k: filter_diag.get(k) for k in param_keys}

        # --- 4. Instantaneous ---
        # Playback context from policy
        is_playing = False
        try:
            is_playing = self._policy.is_playback_active
            # is_playback_active may be a property or callable
            if callable(is_playing):
                is_playing = is_playing()
        except Exception:
            logger.debug("is_playback_active check failed in AEC snapshot")

        buffer_target_ms: float | None = None
        try:
            if self._aec_adapter is not None and hasattr(self._aec_adapter, "get_buffer_target_ms"):
                buffer_target_ms = self._aec_adapter.get_buffer_target_ms()
        except Exception:
            logger.debug("get_buffer_target_ms check failed in AEC snapshot")

        # Correlation from the listener's own tracked value
        correlation = self._current_correlation

        instantaneous: dict[str, Any] = {
            "mic_rms": filter_diag.get("mic_rms"),
            "ref_rms": filter_diag.get("ref_rms"),
            "output_rms": filter_diag.get("output_rms"),
            "erle_db": filter_diag.get("erle_db"),
            "erle_instant": filter_diag.get("erle_instant"),
            "correlation": correlation,
            "dtd_active": (filter_diag.get("dtd_holdoff_remaining", 0) or 0) > 0,
            "dtd_holdoff_remaining": filter_diag.get("dtd_holdoff_remaining"),
            "error_to_baseline_ratio": filter_diag.get("error_to_baseline_ratio"),
            "warmup_remaining_frames": filter_diag.get("warmup_remaining_frames"),
            "filter_energy": filter_diag.get("filter_energy"),
            "ref_active": filter_diag.get("ref_active"),
            "frames_processed": filter_diag.get("frames_processed"),
            "baseline_error_power": filter_diag.get("baseline_error_power"),
            "is_playing": is_playing,
            "buffer_target_ms": buffer_target_ms,
        }
        if filter_diag_error:
            instantaneous["_filter_error"] = filter_diag_error

        # --- 5. Recent history (60-second rolling stats) ---
        recent_history: dict[str, Any] = {
            "erle_db_avg": filter_diag.get("erle_60s_avg"),
            "erle_db_min": filter_diag.get("erle_60s_min"),
            "erle_db_max": filter_diag.get("erle_60s_max"),
            "dtd_ratio": filter_diag.get("dtd_ratio"),
            "ref_activity_ratio": filter_diag.get("ref_active"),
            "frames_in_window": filter_diag.get("frames_processed"),
        }

        # --- 6. Delay calibration ---
        delay_cal: dict[str, Any] = {}
        try:
            if self._passive_calibrator is not None:
                last_result = getattr(self._passive_calibrator, "_last_result", None)
                if last_result is not None:
                    delay_cal = {
                        "current_delay_ms": getattr(last_result, "delay_ms", None),
                        "last_confidence": getattr(last_result, "peak_correlation", None),
                        "last_method": "passive",
                    }
            if not delay_cal:
                cal_status = self.get_calibration_status()
                delay_cal = {
                    "current_delay_ms": cal_status.get("delay_ms", self._aec_delay_ms),
                    "last_confidence": cal_status.get("confidence"),
                    "last_method": cal_status.get("source", "default"),
                }
        except Exception as e:
            delay_cal = {"_error": str(e)}

        # --- 7. Frame history (from AECDiagnostics singleton) ---
        frame_history_data: dict[str, Any] = {}
        try:
            aec_diag = get_aec_diagnostics()
            raw_frames = aec_diag.get_frame_history(last_n=90)
            # Downsample to ~30 entries (every 3rd)
            sampled = raw_frames[::3] if len(raw_frames) > 30 else raw_frames
            frame_history_data = {
                "count": len(sampled),
                "mic_rms": [f["mic_rms"] for f in sampled],
                "ref_rms": [f["reference_rms"] for f in sampled],
                "post_aec_rms": [f["post_aec_rms"] for f in sampled],
                "correlation": [f["correlation"] for f in sampled],
                "gating": [f["gating_active"] for f in sampled],
            }
        except Exception as e:
            frame_history_data = {"_error": str(e), "count": 0}

        # --- 8. Assessment ---
        status = "inactive"
        issues: list[str] = []
        frames = filter_diag.get("frames_processed", 0) or 0
        ref_rms = filter_diag.get("ref_rms", 0.0) or 0.0
        erle_avg = filter_diag.get("erle_60s_avg", 0.0) or 0.0
        dtd_ratio = filter_diag.get("dtd_ratio", 0.0) or 0.0
        warmup_left = filter_diag.get("warmup_remaining_frames", 0) or 0
        e2b_ratio = filter_diag.get("error_to_baseline_ratio", 0.0) or 0.0
        dtd_thresh = filter_diag.get("dtd_threshold", 10.0) or 10.0

        if self._aec_processor is None:
            status = "error"
            issues.append("No AEC processor initialized")
        elif frames == 0 or ref_rms < 0.0001:
            status = "inactive"
            if ref_rms < 0.0001:
                issues.append("Reference signal too weak")
        elif warmup_left > 0 or (erle_avg < 3.0 and frames < 2000):
            status = "converging"
        elif dtd_ratio > 0.5 or erle_avg < 6.0:
            status = "degraded"
            if dtd_ratio > 0.3:
                issues.append("High DTD ratio — filter adaptation limited")
            if erle_avg < 3.0 and frames > 2000:
                issues.append("Poor ERLE after warmup")
        else:
            status = "active"

        if buffer_target_ms is not None and (buffer_target_ms < 10 or buffer_target_ms > 300):
            issues.append("Unusual delay estimate: %.0fms" % buffer_target_ms)
        if e2b_ratio > dtd_thresh * 0.8 and e2b_ratio > 0:
            issues.append("Near DTD threshold (ratio=%.1f, thresh=%.1f)" % (e2b_ratio, dtd_thresh))

        tuning_context = "ERLE=%.1fdB, ref_rms=%.4f, dtd=%.0f%%, delay=%sms, %s" % (
            erle_avg,
            ref_rms,
            dtd_ratio * 100,
            ("%.0f" % buffer_target_ms) if buffer_target_ms is not None else "?",
            status,
        )

        assessment: dict[str, Any] = {
            "status": status,
            "issues": issues,
            "tuning_context": tuning_context,
        }

        snapshot["identity"] = identity
        snapshot["parameters"] = parameters
        snapshot["instantaneous"] = instantaneous
        snapshot["recent_history"] = recent_history
        snapshot["delay_calibration"] = delay_cal
        snapshot["frame_history"] = frame_history_data
        snapshot["assessment"] = assessment

        return snapshot

    def _capture_allowed_locked(self) -> bool:
        """Check this attempt's cancellation while native open owns the lock."""
        event = self._run_stop_event
        return event is None or not event.is_set()

    def _init(self) -> bool:
        """Initialize audio stream and ViolaWake engine."""
        with self._state_lock:
            if not self._capture_allowed_locked():
                return False
            if self._is_initialized:
                return True

            # Fail closed if PortAudio bindings are unavailable. The module
            # imports cleanly without pyaudio (guarded import at top), but live
            # mic capture genuinely needs it — surface a clear error here rather
            # than an AttributeError deep in stream setup. On Linux install
            # `pyaudio` (already in requirements_linux.txt) plus the system
            # libportaudio2 / portaudio19-dev package.
            if pyaudio is None:
                logger.error(
                    "PyAudio (PortAudio bindings) is not installed — wake-word "
                    "mic capture cannot start. Install 'pyaudio' and the system "
                    "PortAudio runtime (libportaudio2 on Linux)."
                )
                return False

            # FORMAT is mirrored as the literal 8 in the class body so the class
            # defines without pyaudio. Now that pyaudio is confirmed present,
            # assert the literal still equals pyaudio.paInt16 — guards against a
            # future pyaudio build changing the constant out from under us before
            # we open the stream with self.FORMAT below.
            if getattr(pyaudio, "paInt16", self.FORMAT) != self.FORMAT:
                logger.error(
                    "wake-word audio FORMAT (%s) does not match pyaudio.paInt16 "
                    "(%s); refusing to open the mic stream with a mismatched "
                    "sample format",
                    self.FORMAT,
                    pyaudio.paInt16,
                )
                return False

            try:
                # Check if engine was pre-loaded in constructor
                if self._engine is None:
                    # Engine not pre-loaded - model may not exist or failed to load
                    if not self._model_path.exists():
                        logger.error("ViolaWake model not found: %s", self._model_path)
                        return False

                    # Try loading now (may fail due to DLL conflicts if pyaudiowpatch loaded first)
                    self._engine = self._load_engine(self._model_path)
                    logger.info("ViolaWake engine loaded in _init: %s", self._engine)
                else:
                    logger.info("Using pre-loaded ViolaWake engine: %s", self._engine)

                # Initialize PyAudio (Pa_Initialize serialized under the lock)
                self._audio = open_portaudio()

                # Find input device
                device_index = self._get_input_device()

                # Log device info
                if device_index is not None:
                    dev_info = self._audio.get_device_info_by_index(device_index)
                else:
                    dev_info = self._audio.get_default_input_device_info()
                self._device_index = device_index
                self._device_name = dev_info.get("name", "unknown")
                logger.info(
                    "AUDIO_DEVICE: index=%s name='%s' rate=%s",
                    device_index,
                    dev_info.get("name"),
                    dev_info.get("defaultSampleRate"),
                )

                # Open audio stream. open_stream() (not a raw self._audio.open())
                # so the stream carries its own lock: this capture stream is read
                # on the wake-detector thread while mute/settings/shutdown reach
                # cleanup() from theirs (facade.stop() does not join this thread),
                # and a raw stream lets that close free the buffer mid-read.
                self._stream = open_stream(
                    self._audio,
                    format=self.FORMAT,
                    channels=self.CHANNELS,
                    rate=self.SAMPLE_RATE,
                    input=True,
                    frames_per_buffer=self.CHUNK_SIZE,
                    input_device_index=device_index,
                )

                self._is_initialized = True
                logger.info("ViolaWake listener initialized successfully")

                # Emit ready event with AEC status
                aec_backend = self._aec_processor.name if self._aec_processor else "none"
                self._emit_status_event(
                    "wake_ready",
                    {
                        "enabled": True,
                        "status": "ready",
                        "threshold": self.threshold,
                        "models": ["temporal_cnn"],
                        "mode": "violawake",
                        "aec_enabled": self._aec_enabled,
                        "aec_backend": aec_backend,
                        "aec_delay_ms": self._aec_delay_ms,
                    },
                )

                return True

            except Exception as e:
                logger.exception(
                    "Failed to initialize ViolaWake listener: %s (type=%s)",
                    e,
                    type(e).__name__,
                )
                self.cleanup()
                return False

    def _get_input_device(self) -> int | None:
        """Get input device index.

        NOTE: We prefer the default MME device (returning None) because WASAPI
        devices often have higher ambient noise levels and clipping issues that
        cause the wake word model to fail. The debug script works correctly with
        the default device, so we match that behavior.

        Guard: if the Windows default recording device is VB-CABLE Output (the
        loopback capture endpoint), we scan for and select the first real physical
        microphone instead. Without this guard, the wake detector hears music
        playback routed through VB-CABLE and processes song lyrics as commands.
        """
        if self._audio is None:
            return None

        # Check config for specific device
        config_device = getattr(self.config, "input_device", None)

        if config_device:
            raw_config_device = str(config_device).strip()

            # The settings UI persists device indices as strings. Honor those
            # directly so the live listener uses the exact device the user
            # selected instead of trying to interpret "1" as a name fragment.
            if raw_config_device.lstrip("-").isdigit():
                configured_index = int(raw_config_device)
                if configured_index >= 0:
                    try:
                        info = self._audio.get_device_info_by_index(configured_index)
                        if info.get("maxInputChannels", 0) > 0:
                            logger.info(
                                "Using configured input device: [%s] %s",
                                configured_index,
                                info["name"],
                            )
                            return configured_index
                        logger.warning(
                            "Configured input device index %s is not input-capable, using default",
                            configured_index,
                        )
                    except Exception:
                        logger.warning(
                            "Configured input device index %s not found, using default",
                            configured_index,
                        )
                else:
                    logger.info("Configured input device requests system default")
                    return None
            else:
                # If user specified a device name, try to find it
                target_name = raw_config_device.lower()
                for i in range(self._audio.get_device_count()):
                    info = self._audio.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) <= 0:
                        continue
                    name = info.get("name", "").lower()
                    if target_name in name or name in target_name:
                        logger.info(
                            "Using configured input device: [%s] %s",
                            i,
                            info["name"],
                        )
                        return i
                logger.warning("Configured device '%s' not found, using default", config_device)

        # Guard: if default recording device is VB-CABLE Output, find a real microphone.
        # VB-CABLE Output is a loopback capture device — it records whatever plays through
        # VB-CABLE Input. If it is the Windows default recording device, opening device=None
        # would capture music playback, causing wake word false triggers from song lyrics.
        try:
            default_info = self._audio.get_default_input_device_info()
            default_name = default_info.get("name", "").lower()
            _is_vb_cable = (
                "vb-cable" in default_name
                or "cable output" in default_name
                or ("vb" in default_name and "cable" in default_name)
            )
            if _is_vb_cable:
                logger.warning(
                    "Default recording device is VB-CABLE ('%s') — scanning for physical microphone to prevent music loopback",
                    default_info.get("name"),
                )
                for i in range(self._audio.get_device_count()):
                    info = self._audio.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) <= 0:
                        continue
                    name = info.get("name", "").lower()
                    if "vb-cable" in name or "cable output" in name or ("vb" in name and "cable" in name):
                        continue
                    logger.info(
                        "Using physical microphone to avoid VB-CABLE loopback: [%d] %s",
                        i,
                        info.get("name"),
                    )
                    return i
                logger.error(
                    "No physical microphone found — wake detector will use VB-CABLE (music loopback risk!)",
                )
        except Exception:
            logger.debug("Could not check default input device for VB-CABLE guard")

        # Use default device (MME) - this is what the debug script uses and it works correctly.
        # WASAPI devices have been found to cause issues with wake word detection due to
        # higher ambient noise levels and potential clipping.
        logger.info("Using default input device (MME)")
        return None

    def _emit_status_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Emit a status event via debug events (for UI)."""
        try:
            from ui.qt_native.debug_events import emit_debug_event

            if emit_debug_event is not None:
                # Use source="backend" so VoiceOrchestratorBridge accepts it
                emit_debug_event(event_type, data, source="backend")
                logger.debug("Emitted %s via debug events", event_type)
        except Exception as e:
            logger.debug("Could not emit debug event: %s", e)

    def _reopen_audio_stream(self) -> bool:
        """Fully tear down and re-open the PyAudio input stream.

        Used for recovery from a dead/errored capture stream (both the
        stream-health silence path and the read-error path). A single
        transient device failure must NOT permanently kill wake detection,
        so callers retry this rather than exiting the loop.

        Returns True if a fresh stream was opened, False otherwise. On
        failure ``self._stream`` is left as None so the caller can retry.
        """
        with self._state_lock:
            if not self._capture_allowed_locked():
                return False
            return self._reopen_audio_stream_locked()

    def _reopen_audio_stream_locked(self) -> bool:
        """Recover capture only after the cancellation check under state lock."""
        try:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except AUDIO_DEVICE_ERRORS as close_err:
                    # Best-effort teardown: the old stream is already suspect,
                    # so a close failure must not abort the reopen. Log it at
                    # warning (a reopen is rare, so this is signal not spam) —
                    # a repeated close failure usually means a leaked handle.
                    logger.warning(
                        "[STREAM_HEALTH] Error closing old stream during reopen (continuing): %s",
                        close_err,
                        exc_info=True,
                    )
                self._stream = None
            # Terminate and reinitialize PyAudio to clear corrupted PortAudio state.
            if self._audio is not None:
                try:
                    terminate_portaudio(self._audio)
                except AUDIO_DEVICE_ERRORS as term_err:
                    # Same rationale: a failed Pa_Terminate must not block the
                    # fresh Pa_Initialize below, but it is worth surfacing since
                    # it leaks a PortAudio instance.
                    logger.warning(
                        "[STREAM_HEALTH] Error terminating PyAudio during reopen (continuing): %s",
                        term_err,
                        exc_info=True,
                    )
                self._audio = None
            self._audio = open_portaudio()
            device_index = self._get_input_device()
            self._stream = open_stream(
                self._audio,
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.SAMPLE_RATE,
                input=True,
                frames_per_buffer=self.CHUNK_SIZE,
                input_device_index=device_index,
            )
            logger.info("[STREAM_HEALTH] Audio stream reopened successfully")
            if self._mic_pump is not None:
                self._mic_pump.flush()
            return True
        except AUDIO_DEVICE_ERRORS as reopen_err:
            logger.exception(
                "[STREAM_HEALTH] Failed to reopen audio stream: %s (%s)",
                reopen_err,
                type(reopen_err).__name__,
            )
            # Leave self._stream as None; caller retries on the next iteration.
            return False

    # ------------------------------------------------------------------ #
    # Device-change handoff (see the __init__ note on the handshake)
    # ------------------------------------------------------------------ #

    def suspend_for_device_change(self) -> None:
        """Ask the read loop to close the capture stream, and wait until it has.

        Called from the device-watch thread. Deliberately does not touch
        ``self._stream``: the wake loop, or the async-bridge thread mid-command,
        may be blocked inside ``stream.read()`` right now, and closing it here is
        the race this handshake exists to avoid.

        If no read loop is running there is no reader to race, so the stream is
        closed inline instead.

        The pending flag is **sticky**. When the reader is busy with a command
        recording and does not reach a safe point before the wait expires, this
        returns and logs, and the reader still services the swap at its next safe
        point. A device change is never silently dropped.
        """
        if not self._loop_active.is_set():
            with self._state_lock:
                self._close_stream_locked()
            return

        self._device_change_resume.clear()
        self._device_change_quiesced.clear()
        self._device_change_pending.set()

        if not self._device_change_quiesced.wait(timeout=_DEVICE_CHANGE_QUIESCE_TIMEOUT):
            logger.warning(
                "[DEVICE_CHANGE] Wake capture did not reach a safe hand-off point within %.0fs "
                "(a command recording is probably in flight); the swap stays queued and "
                "will be serviced when the reader is next free",
                _DEVICE_CHANGE_QUIESCE_TIMEOUT,
            )

    def resume_after_device_change(self) -> None:
        """Release the read loop to re-open on the new device."""
        self._device_change_resume.set()

    def _close_stream_locked(self) -> None:
        """Close the capture stream and drop the PortAudio instance.

        Caller must hold ``self._state_lock`` OR own the exclusive stream claim
        (the recording flag). Errors are logged, never raised: this runs on the
        recovery path and a failed close must not prevent the re-open.
        """
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except AUDIO_DEVICE_ERRORS as close_err:
                logger.warning(
                    "[DEVICE_CHANGE] Error closing capture stream (continuing): %s",
                    close_err,
                    exc_info=True,
                )
            self._stream = None

        if self._audio is not None:
            try:
                terminate_portaudio(self._audio)
            except AUDIO_DEVICE_ERRORS as term_err:
                logger.warning(
                    "[DEVICE_CHANGE] Error terminating PyAudio (continuing): %s",
                    term_err,
                    exc_info=True,
                )
            self._audio = None

    def _service_device_change(self) -> None:
        """Close and re-open the capture stream on the reader's own thread.

        Runs on the wake read loop's thread, holding the exclusive stream claim,
        so no other thread can be inside a read while this closes the stream.
        """
        logger.info("[DEVICE_CHANGE] Moving wake capture to the current default device")
        self._close_stream_locked()
        self._device_change_quiesced.set()

        # Wait for the watcher to finish rebuilding the shared device table.
        # A timeout here is not fatal: re-opening through open_portaudio() runs a
        # fresh Pa_Initialize for this binding either way.
        if not self._device_change_resume.wait(timeout=_DEVICE_CHANGE_RESUME_TIMEOUT):
            logger.warning(
                "[DEVICE_CHANGE] No resume signal within %.0fs; re-opening capture anyway",
                _DEVICE_CHANGE_RESUME_TIMEOUT,
            )

        self._device_change_pending.clear()
        self._device_change_quiesced.clear()
        self._device_change_resume.clear()

        if self._reopen_audio_stream():
            logger.info(
                "[DEVICE_CHANGE] Wake capture now on device index=%s name='%s'",
                self._device_index,
                self._device_name,
            )
            self._emit_status_event(
                "wake_ready",
                {
                    "enabled": True,
                    "status": "ready",
                    "reason": "device_changed",
                    "device_name": self._device_name,
                },
            )
        else:
            # The old swallowed-exception-then-silence path. Say so where the
            # user can see it, and leave the loop's own retry to keep trying.
            logger.error(
                "AUDIO DEVICE LOST: could not open a microphone after the device changed. "
                "Wake detection is deaf until a working input device is available."
            )
            self._emit_status_event(
                "wake_ready",
                {
                    "enabled": True,
                    "status": "error",
                    "reason": "input_device_lost",
                },
            )

    def _claim_stream_for_device_change(self) -> bool:
        """Take exclusive ownership of the stream if a swap is pending and free.

        Returns True when the caller now owns the stream and must call
        :meth:`_release_stream_claim` when done.
        """
        if not self._device_change_pending.is_set():
            return False
        with self._recording_lock:
            if self._recording_command:
                # A command recording owns the stream; try again next iteration.
                return False
            # Reuse the recording flag as the exclusive-ownership token so a
            # wake->record scheduled on the bridge thread cannot start reading
            # the stream we are about to close.
            self._recording_command = True
            return True

    def _release_stream_claim(self) -> None:
        with self._recording_lock:
            self._recording_command = False

    def run(self, stop_event: threading.Event) -> None:
        """
        Main listening loop.

        Args:
            stop_event: Event to signal when to stop listening
        """
        with self._state_lock:
            self._run_stop_event = stop_event
        # A fresh attempt starts now: give model load and device open their
        # cold-start allowance, measured from here rather than from whenever
        # this listener object happened to be constructed.
        self.detection_signal.restart_window()
        self._capture_open_failed.clear()

        # Announce that this loop owns a capture stream, so a mid-session device
        # change is routed here and the suspend/resume handshake below actually
        # gets driven. Registering also starts the device watch (see
        # audio_core/device_change.register_stream_owner), which is what keeps a
        # hot-plug from going unnoticed for the life of the process.
        from audio_core.device_change import register_stream_owner, unregister_stream_owner

        try:
            # Registration can append the owner before starting the watcher,
            # so even a watcher-start failure must pass through this finally.
            register_stream_owner(self)
            self._run_registered(stop_event)
        finally:
            # Cover initialization and loop setup as well as the read loop.
            # Stop receiving device handoffs before tearing down capture.
            self._loop_active.clear()
            unregister_stream_owner(self)
            logger.info("ViolaWake detection stopped")
            self.cleanup()

    def _run_registered(self, stop_event: threading.Event) -> None:
        """Initialize and listen while run() owns registration and teardown."""
        with self._state_lock:
            # Serialize the final cancellation check with native open and the
            # cleanup used by mute/stop. A stopped pending worker cannot open
            # after cleanup has already returned to its caller.
            if stop_event.is_set():
                return
            initialized = self._init()
            if initialized and stop_event.is_set():
                return
        if not initialized:
            logger.error("Failed to initialize ViolaWake listener")
            # Record the DEFINITE fact that capture never opened. The detection
            # signal alone would eventually report stalled, but only after its
            # cold-start allowance expired -- and for those first seconds the UI
            # would say "listening" about a microphone that was never opened.
            # A failed open is not "still starting up", it is known, so health
            # reflects it immediately rather than waiting out a grace period.
            self._capture_open_failed.set()
            # Park until shutdown. Deliberately does NOT mark the detection
            # signal: this loop is not detecting anything, and it must report
            # stalled so the supervisor rebuilds it instead of the runtime
            # sitting here permanently deaf while /health says listening.
            while not stop_event.is_set():
                time.sleep(TIMEOUT_MEDIUM)
            return

        logger.info("🎤 ViolaWake detection started")
        self._warm_wake_verifier()
        logger.info("    Model: %s", self._model_path)
        logger.info("    Threshold: %s", self.threshold)
        logger.info(
            "    AEC: %s",
            self._aec_processor.name if self._aec_processor else "disabled",
        )
        # CRITICAL: Log callback status at startup
        callback_status = (
            "WIRED" if (self.on_wake_word_detected and callable(self.on_wake_word_detected)) else "NOT_WIRED"
        )
        callback_type = type(self.on_wake_word_detected).__name__ if self.on_wake_word_detected else "None"
        logger.info("    Callback: %s (type=%s)", callback_status, callback_type)
        if self._force_wake:
            logger.warning("    ⚠️ FORCE_WAKE enabled - ALL policy layers bypassed!")

        # Import CLIP_SAMPLES from violawake
        from violawake import CLIP_SAMPLES

        # Ring buffer for efficient sliding window (avoids O(n) np.roll per frame)
        # Uses a write index that wraps around, only flattens when processing
        audio_buffer: np.ndarray = np.zeros(CLIP_SAMPLES, dtype=np.float32)
        buffer_write_idx = 0  # Where to write next chunk
        frame_count = 0

        # Stream health monitoring - use sliding window percentage instead of consecutive count
        # This handles streams that are "mostly dead" but have occasional noise spikes
        STREAM_HEALTH_WINDOW = 100  # Check last 100 frames (~8 seconds at 80ms/frame)
        STREAM_HEALTH_DEAD_PERCENT = 0.95  # Restart if 95%+ frames carry no data at all
        recent_frame_dead: list[bool] = []  # Sliding window of dead-frame flags
        stream_health_restart_count = 0
        # Earliest monotonic time another stream-health restart may fire. No
        # health check may thrash a working device: even a genuinely dead stream
        # gets a widening backoff rather than an unbounded restart loop.
        stream_health_next_allowed = 0.0
        stream_health_last_warning = 0.0

        # Read-error recovery: a transient device failure must not permanently
        # kill wake detection. Track consecutive read failures and only exit
        # the loop after a run of them (each attempt reopens the stream).
        consecutive_read_errors = 0
        MAX_CONSECUTIVE_READ_ERRORS = 10

        # Track last wake score for metrics
        last_wake_score: float | None = None
        last_mic_rms: float = 0.0
        # Keep a reference to the most recent post-AEC int16 chunk so
        # the on_detection callback can feed it to Silero VAD.
        last_processed_int16: np.ndarray | None = None

        # Confirmation gate: track recent inference scores (for 2-of-3 check)
        recent_inference_scores: list[float] = []
        CONFIRMATION_WINDOW = self._policy._config.confirmation_window

        # Track-transition blanking: suppress inference when loopback_rms
        # jumps sharply (track skip/change), giving AEC time to re-converge.
        TRANSITION_BLANK_FRAMES = 6  # ~480ms at 80ms/frame
        TRANSITION_RMS_RATIO = 2.5  # loopback jump > 2.5x = transition
        prev_loopback_rms: float = 0.0
        transition_blank_remaining: int = 0

        # Streaming cadence (Stage 1): the engine scores every chunk, but the
        # threshold refresh (quiet hours reads settings) and the track-change
        # check keep the old 400 ms grid, so their cost and timing are exactly
        # what shipped. A blank of N grid steps covers N * 5 chunks.
        LEGACY_GRID_CHUNKS = 5
        effective_threshold: float | None = None
        # The Stage 2 check is on: the engine maps the level with its "checked"
        # operating points (refreshed with the threshold, on the 400 ms grid).
        stage2_checked = False
        # The looser music rule while Viola's own music plays (None = off).
        music_rule: tuple[int, int, float] | None = None
        # Any gap in the audio (first frame, command recording, device change,
        # read error) resets the engine's embedding stream so stale audio from
        # before the gap is never scored.
        stream_gap = True

        # Set up detection callback with policy-based decision
        def on_detection():
            nonlocal last_wake_score, last_mic_rms, last_processed_int16, stream_gap

            # STAGE4: Callback invoked - engine threshold exceeded
            logger.info("[STAGE4] callback: on_detection called (raw trigger)")
            # An early Stage 2 decode belongs to this trigger or to nobody.
            early_decode = self._take_early_decode(frame_count)

            # Track raw trigger (model fired) BEFORE policy decision
            get_wake_metrics().record_raw_trigger()

            # Get the actual score from the engine's last_score property.
            # This is set BEFORE the callback is invoked, so it contains
            # the score that triggered this detection (not a stale value).
            with self._engine_lock:
                engine = self._engine
                actual_score = engine.last_score if engine is not None else 0.0
                # Streaming path: the per-frame threshold the engine's k-of-n
                # rule used for this level (the policy re-checks against it).
                streaming_engine = engine is not None and getattr(engine, "supports_streaming", False) is True
                frame_threshold = engine.stream_threshold if streaming_engine else None
                agreement_rule = tuple(engine.agreement) if streaming_engine else None
                # The rule that confirmed this wake: the normal one, or the
                # looser music rule (then Stage 2 must hear "Viola").
                trigger = getattr(engine, "last_trigger", None) if streaming_engine else None
                music_path = False
                if trigger is not None and isinstance(getattr(trigger, "score", None), float):
                    actual_score = trigger.score
                    frame_threshold = trigger.frame_threshold
                    agreement_rule = (trigger.k, trigger.n)
                    music_path = bool(trigger.music_path)

            # --- Contributor Mode Intercept ---
            # If contributor mode is active, save as sample and DON'T trigger wake
            if self._contributor_manager.on_wake_detection(
                audio=audio_buffer.copy(),
                score=actual_score,
                threshold=self._base_threshold,
                context={
                    "mic_rms": last_mic_rms,
                    "loopback_rms": self._current_loopback_rms,
                    "post_aec_rms": self._current_post_aec_rms,
                    "correlation": self._current_correlation,
                    "playback_active": self._policy.is_playback_active,
                    "frame_count": frame_count,
                },
            ):
                # Sample saved, don't trigger wake - mode is active
                return

            # Build context for policy decision
            # Run VAD on the same audio buffer the model scored on,
            # not just the latest mic chunk.  The wake model scores a
            # 1.5s ring buffer, but by the time the callback fires the
            # most recent 80ms chunk may be post-utterance silence.
            # Using the buffer ensures the VAD sees the speech that
            # actually triggered the model.
            _buf_int16 = (np.clip(audio_buffer, -1.0, 1.0) * 32767).astype(np.int16)
            _buf_float32 = _buf_int16.astype(np.float32)
            policy_mic_rms = max(last_mic_rms, float(np.sqrt(np.mean(_buf_float32 * _buf_float32))))
            # Stop scanning the 1.5 s buffer once the VAD gate is satisfied.
            vad_on_buffer = self._policy.compute_vad_confidence(audio_int16=_buf_int16, stop_at=_VAD_GATE_STOP_AT)
            vad_on_chunk = self._policy.estimate_vad_from_audio(
                audio_int16=last_processed_int16,
                mic_rms=last_mic_rms,
                loopback_rms=self._current_loopback_rms,
                post_aec_rms=self._current_post_aec_rms,
            )
            # Use whichever saw speech — the buffer catches the wake
            # word even when the trailing chunk is silence.
            vad_confidence = max(vad_on_buffer, vad_on_chunk)
            logger.info(
                "[VAD_GATE] score=%.3f vad_used=%.3f (chunk=%.3f buffer=%.3f)",
                actual_score,
                vad_confidence,
                vad_on_chunk,
                vad_on_buffer,
            )

            context = WakeContext(
                wake_score=actual_score,
                base_threshold=self._base_threshold,
                mic_rms=policy_mic_rms,
                loopback_rms=self._current_loopback_rms,
                post_aec_rms=max(self._current_post_aec_rms, policy_mic_rms),
                correlation=self._current_correlation,
                vad_confidence=vad_confidence,
                recent_scores=list(recent_inference_scores),
                frame_count=frame_count,
                self_speech_active=_viola_self_speech_active(),
                score_threshold=frame_threshold,
                agreement=agreement_rule,
                effective_threshold=effective_threshold,
            )

            # Use policy for authoritative decision
            decision = self._policy.final_trigger_decision(context)

            # STAGE5: Policy decision made
            policy_result = "ALLOW" if decision.should_trigger else "DENY"
            policy_reason = decision.blocking_reason or "none"
            logger.info(
                "[STAGE5] policy: %s reason=%s (passed=%s failed=%s)",
                policy_result,
                policy_reason,
                decision.layers_passed,
                decision.layers_failed,
            )

            # --- Diagnostic: Record trigger in analytics (both accepted and rejected) ---
            try:
                analytics = get_wake_analytics()
                blocking_layer = decision.layers_failed[0] if decision.layers_failed else None
                analytics.record_trigger(
                    accepted=decision.should_trigger,
                    score=actual_score,
                    threshold=decision.effective_threshold,
                    blocking_layer=blocking_layer,
                    playback_active=self._policy.is_playback_active,
                    volume=self._policy._playback_volume,
                    tts_active=context.self_speech_active,
                )
            except Exception as diag_err:
                logger.debug("Wake analytics record failed: %s", diag_err)

            # --- Diagnostic: Build decision trace ---
            try:
                tracer = get_decision_tracer()
                tracer.start_trace(correlation_id=f"wake_{frame_count}")
                tracer.record_audio_input(
                    mic_rms=last_mic_rms,
                    mic_rms_db=20 * np.log10(max(last_mic_rms, 1e-10)),
                    clipping_detected=last_mic_rms > 30000,
                    noise_floor_db=-60.0,  # Would need actual tracking
                )
                aec_diag_data = self._aec_processor.get_diagnostics() if self._aec_processor is not None else {}
                tracer.record_aec_state(
                    reference_active=self._current_loopback_rms > 100,
                    reference_rms=self._current_loopback_rms,
                    reduction_db=aec_diag_data.get("erle_db", 0.0),
                    delay_ms=self._aec_delay_ms,
                    correlation=self._current_correlation,
                )
                tracer.record_model_output(
                    score=actual_score,
                    inference_time_ms=0.0,  # Would need timing
                )
                tracer.record_threshold_breakdown(
                    base_threshold=self._base_threshold,
                    effective_threshold=decision.effective_threshold,
                    adjustments={"echo_gating": self._policy._echo_state.echo_gating_active},
                )
                for layer in decision.layers_passed:
                    tracer.record_layer_verdict(layer, passed=True)
                for layer in decision.layers_failed:
                    tracer.record_layer_verdict(layer, passed=False, reason=decision.blocking_reason)
                tracer.finalize_trace(
                    accepted=decision.should_trigger,
                    rejection_reason=(decision.blocking_reason if not decision.should_trigger else None),
                )
            except Exception as diag_err:
                logger.debug("Decision trace recording failed: %s", diag_err)

            # --- Diagnostic: Capture audio on trigger ---
            if (decision.should_trigger or actual_score > 0.6) and self._loop_flag(
                "wake_audio_logging", _is_wake_audio_logging_enabled
            ):
                try:
                    audio_buf = get_wake_audio_buffer()
                    event_type = "trigger" if decision.should_trigger else "near_miss"
                    audio_buf.capture_trigger_event(
                        correlation_id=f"wake_{frame_count}",
                        score=actual_score,
                        threshold=decision.effective_threshold,
                        accepted=decision.should_trigger,
                        rejection_reason=(decision.blocking_reason if not decision.should_trigger else None),
                        event_type=event_type,
                    )
                except Exception as diag_err:
                    logger.debug("Audio capture failed: %s", diag_err)

            # FORCE_WAKE mode: bypass all policy layers
            should_fire = decision.should_trigger
            if self._force_wake and not should_fire:
                logger.warning(
                    "[STAGE5] FORCE_WAKE: Bypassing policy DENY (would have blocked: %s)",
                    decision.blocking_reason,
                )
                should_fire = True

            if not should_fire:
                logger.debug(
                    "Wake blocked by policy: %s (passed=%s, failed=%s)",
                    decision.blocking_reason,
                    decision.layers_passed,
                    decision.layers_failed,
                )
                return

            # Stage 2: a second listen (Whisper) before the chime. Runs only
            # after every policy gate allowed the wake. Soft for a confident
            # score; stricter below the sensitivity level and on the music path.
            if not self._force_wake:
                reads_before = self._verify_frame_count
                verified = self._verify_wake_candidate(
                    np.concatenate([audio_buffer[buffer_write_idx:], audio_buffer[:buffer_write_idx]]),
                    actual_score,
                    early=early_decode,
                    level=decision.effective_threshold if streaming_engine else None,
                    require_token=music_path,
                )
                if self._verify_frame_count != reads_before:
                    # The check read microphone audio this loop never fed the
                    # engine: a gap. Reset the streaming features and the
                    # k-of-n window before the next chunk (debounce is kept),
                    # whether the wake passed or not (a passed wake can still
                    # be dropped downstream without a command recording).
                    stream_gap = True
                if not verified:
                    return

            logger.info(
                "🎤 VIOLA detected! (ViolaWake) score=%s threshold=%s level=%s%s",
                format(context.wake_score, ".3f"),
                format(
                    context.score_threshold if context.score_threshold is not None else decision.effective_threshold,
                    ".3f",
                ),
                format(decision.effective_threshold, ".3f"),
                " (music path)" if music_path else "",
            )

            # Telemetry: record wake activation, off the wake path (it is a
            # counter, and on a busy machine its imports and file reads delayed
            # the chime).
            during_playback = bool(self._policy.is_playback_active)

            def _record_activation() -> None:
                try:
                    from admin.instrumentation import record_wake_activation

                    record_wake_activation(during_playback=during_playback)
                except Exception:
                    logger.debug("Wake activation telemetry recording failed (non-critical)")

            self._run_off_loop(_record_activation)

            # Emit event with full metrics
            self._emit_status_event(
                "wake_detected",
                {
                    "model": "temporal_cnn",
                    "threshold": decision.effective_threshold,
                    "score": context.wake_score,
                    "mode": "violawake",
                    "aec_active": self._aec_reference_source is not None,
                    "gating_active": self._policy._echo_state.echo_gating_active,  # Use policy state
                    "vad_confidence": vad_confidence,
                    "layers_passed": decision.layers_passed,
                },
            )

            # --- Data Collection: save trigger clip ---
            try:
                from config.settings import settings as _dc_settings
                from voice.wake_detector.data_collection.collector import (
                    wake_data_collection_allowed,
                )

                if _dc_settings.wake_data_collection_enabled and wake_data_collection_allowed():
                    from voice.wake_detector.data_collection.classifier import (
                        get_trigger_classifier,
                    )
                    from voice.wake_detector.data_collection.collector import (
                        get_clip_collector,
                    )

                    dc_clip_id = get_clip_collector().save_trigger_clip(
                        audio_buffer.copy(),
                        actual_score,
                        decision.effective_threshold,
                    )
                    get_trigger_classifier().on_trigger(dc_clip_id)
                    self._last_data_clip_id = dc_clip_id
            except Exception:
                logger.debug("Wake data collection clip save failed (optional)")

            # Call user callback with audio ducking
            try:
                if not self.on_wake_word_detected:
                    logger.error("[STAGE6] response: FAILED - on_wake_word_detected is None/falsy")
                    return
                if not callable(self.on_wake_word_detected):
                    logger.error(
                        "[STAGE6] response: FAILED - on_wake_word_detected not callable (type=%s)",
                        type(self.on_wake_word_detected).__name__,
                    )
                    return
                if self.on_wake_word_detected and callable(self.on_wake_word_detected):
                    # Duck audio during wake word processing
                    from contextlib import nullcontext

                    duck_ctx = nullcontext()
                    try:
                        from utils.audio_ducking import duck_context

                        duck_ctx = duck_context()
                    except Exception as duck_err:
                        logger.debug("Audio ducking unavailable: %s", duck_err)

                    with duck_ctx:
                        self.on_wake_word_detected()
                        # STAGE6: User callback completed successfully
                        logger.info("[STAGE6] response: UI triggered - wake callback executed")
            except Exception as e:
                logger.error("Wake callback error: %s", e)
                logger.error("[STAGE6] response: FAILED - callback raised exception")

        self._engine_callback = on_detection
        with self._engine_lock:
            if self._engine is not None:
                self._engine.set_callback(on_detection)

        logger.info(
            "WAKE_LOOP_STARTING: about to enter main audio read loop (stop_event.is_set=%s)",
            stop_event.is_set(),
        )

        # From here until the loop exits there IS a reader thread, so a device
        # change must hand off through the flag rather than close the stream
        # from the watcher's thread.
        self._loop_active.set()
        self._start_capture_pump()
        fine_timer = _begin_fine_timer()
        if self._mic_pump is not None and self._mic_pump.running:
            # The scoring runs three ONNX calls per chunk, and each one gives up
            # the GIL; at normal priority getting it back while other Python
            # threads are busy left this loop seconds behind (wake capture
            # harness, docs in _MicPump). Raised priority gets it back first.
            _MicPump._raise_priority()
        pump_epoch = self._mic_pump.drop_epoch if self._mic_pump is not None else 0
        last_backlog_warning = 0.0
        # #4786: removed a stray `self._register_for_device_changes()` call
        # here -- that method was never defined anywhere in this class (or
        # anywhere else in the repo), so every call to run() crashed the wake
        # loop immediately after WAKE_LOOP_STARTING with
        # "AttributeError: 'ViolaWakeListener' object has no attribute
        # '_register_for_device_changes'" (reproduced live in Build Linux
        # (AppImage) run 31087373708). Introduced in ce57c9d2c ("wip(voice):
        # work-coupled liveness signals + device-change handling") alongside
        # `register_stream_owner(self)`, which already performs the real
        # device-change registration at the top of run() (see above) --
        # this call was a leftover from before that consolidation and was
        # never wired to an implementation. No replacement call is needed;
        # register_stream_owner(self) already runs before self._init() above.

        try:
            while not stop_event.is_set():
                if frame_count == 0:
                    logger.info("WAKE_LOOP_ENTRY: first iteration of audio read loop")

                # Skip wake detection while recording a command
                with self._recording_lock:
                    if self._recording_command:
                        stream_gap = True
                        time.sleep(
                            0.05
                        )  # 50ms sleep to avoid busy loop (shorter than TIMEOUT_SHORT for responsiveness)
                        continue

                # Service a queued device change here, before the next read.
                # This is the safe hand-off point: this thread is the reader, so
                # closing the stream here cannot race a read, and the claim below
                # locks out a command recording on the bridge thread meanwhile.
                if self._device_change_pending.is_set():
                    if self._claim_stream_for_device_change():
                        stream_gap = True
                        try:
                            self._service_device_change()
                        finally:
                            self._release_stream_claim()
                        continue

                frame_count += 1

                # Startup diagnostic - log immediately at frame 1
                if frame_count == 1:
                    aec_source_status = "wired" if self._aec_reference_source else "NO_REF"
                    aec_proc_status = "active" if self._aec_processor else "NONE"
                    logger.info(
                        "[AEC_STARTUP] Wake listener started: aec_source=%s, aec_processor=%s, policy_enabled=True",
                        aec_source_status,
                        aec_proc_status,
                    )
                    if not self._aec_reference_source:
                        logger.warning(
                            "[AEC_STARTUP] AEC reference source NOT wired! False positives during playback likely."
                        )

                # Heartbeat logging with AEC status
                if frame_count % 250 == 0:
                    aec_status = "wired" if self._aec_reference_source else "no_ref"
                    gate_status = "gating" if self._policy._echo_state.echo_gating_active else "open"
                    logger.debug(
                        "[violawake] heartbeat frame=%d aec=%s gate=%s",
                        frame_count,
                        aec_status,
                        gate_status,
                    )
                    # AEC health heartbeat — always log when ref is active
                    if self._aec_processor is not None:
                        hb = self._aec_processor.get_diagnostics()
                        if hb.get("ref_active", 0) > 0.1:
                            logger.info(
                                "[AEC_HEALTH] erle=%.1fdB dtd_ratio=%.2f filter_energy=%.2e ref_active=%.2f frames=%d",
                                hb.get("erle_db", 0.0),
                                hb.get("dtd_ratio", 0.0),
                                hb.get("filter_energy", 0.0),
                                hb.get("ref_active", 0.0),
                                hb.get("frames_processed", 0),
                            )

                # --- TIMING DIAGNOSTIC ---
                read_start = time.time()

                # Pre-read diagnostic
                if frame_count <= 3:
                    logger.info(
                        "WAKE_PRE_READ: frame=%d stream_active=%s stream_stopped=%s chunk=%d rate=%d",
                        frame_count,
                        self._stream.is_active() if self._stream else "NO_STREAM",
                        self._stream.is_stopped() if self._stream else "NO_STREAM",
                        self.CHUNK_SIZE,
                        self.SAMPLE_RATE,
                    )

                # Read audio chunk
                try:
                    audio_data = self._mic_read()
                    if audio_data is None:
                        # Nothing from the capture thread within a second
                        # (device change in progress, or stopping).
                        frame_count -= 1
                        continue
                    if stop_event.is_set():
                        # A read may complete after cancellation/cleanup. Do
                        # not count or forward that late frame to consumers.
                        break
                    read_duration = time.time() - read_start
                    # Post-read diagnostic for first few frames
                    if frame_count <= 3:
                        logger.info(
                            "WAKE_POST_READ: frame=%d bytes=%d read_ms=%.1f",
                            frame_count,
                            len(audio_data),
                            read_duration * 1000,
                        )
                    # STAGE1: Audio input received - include timing info
                    if frame_count % 125 == 1:  # Log every ~10 seconds (125 frames * 80ms)
                        logger.info(
                            "[STAGE1] audio_in: frame=%d bytes=%d read_ms=%.1f samples=%d",
                            frame_count,
                            len(audio_data),
                            read_duration * 1000,
                            len(audio_data) // 2,
                        )
                    pump = self._mic_pump
                    if pump is not None and pump.running:
                        if pump.drop_epoch != pump_epoch:
                            # The capture thread dropped old audio (this loop
                            # was more than MAX_BACKLOG_S behind): a gap.
                            pump_epoch = pump.drop_epoch
                            stream_gap = True
                        backlog = pump.available_chunks()
                        # (not during start-up: the first chunks pay one-time imports)
                        if (
                            frame_count > 125
                            and backlog * 0.08 >= 1.0
                            and time.monotonic() - last_backlog_warning > 30.0
                        ):
                            last_backlog_warning = time.monotonic()
                            logger.warning(
                                "[TIMING] Wake detection is %.1f s behind the microphone (busy machine); "
                                "no audio lost, it is catching up",
                                backlog * 0.08,
                            )
                    # TIMING: Warn if read takes too long (expected ~80ms for blocking read)
                    if read_duration > 0.15 and frame_count > 10 and not (pump is not None and pump.running):
                        logger.warning(
                            "[TIMING] Slow audio read: %.0fms (expected ~80ms) frame=%d",
                            read_duration * 1000,
                            frame_count,
                        )
                except Exception as e:
                    # A transient read error (device hiccup, USB reconnect,
                    # a failed prior reopen that left the stream None) must not
                    # permanently kill wake detection. Attempt to recover the
                    # stream with bounded retries; only give up after several
                    # consecutive failures.
                    consecutive_read_errors += 1
                    logger.error(
                        "Audio read error (%d/%d): %s",
                        consecutive_read_errors,
                        MAX_CONSECUTIVE_READ_ERRORS,
                        e,
                    )
                    if consecutive_read_errors >= MAX_CONSECUTIVE_READ_ERRORS:
                        logger.error(
                            "Audio read failed %d times in a row — stopping wake loop",
                            consecutive_read_errors,
                        )
                        break
                    # Recover the stream, then retry on the next iteration.
                    stream_gap = True
                    self._reopen_audio_stream()
                    time.sleep(0.1)  # brief backoff to let the device settle
                    continue
                else:
                    # Successful read — reset the recovery counter.
                    consecutive_read_errors = 0
                    # The single honest statement that wake detection is alive:
                    # a real audio frame just came off the real device. Nothing
                    # outside this loop body may advance this signal.
                    self.detection_signal.mark()

                # Fan out raw audio to external consumers (e.g. ContinuousMicCapture)
                _tap = self._frame_tap
                if _tap is not None:
                    try:
                        _tap(audio_data)
                    except Exception:
                        pass  # Never let a tap error kill the wake loop
                # A verified wake's command audio keeps accumulating until the
                # command recorder takes it (no-op otherwise).
                self._extend_command_preroll(audio_data)

                # Convert to int16 for AEC processing
                mic_int16 = np.frombuffer(audio_data, dtype=np.int16)

                # Convert to float32 ONCE and reuse (avoid 3 separate conversions)
                mic_float32 = mic_int16.astype(np.float32)
                mic_rms = float(np.sqrt(np.mean(mic_float32**2)))
                last_mic_rms = mic_rms

                # WAKE_AUDIO_DIAG: periodic diagnostic every ~2s (25 frames * 80ms).
                # DEBUG, not INFO: at INFO it alone wrote ~450 lines per 15 min.
                # "[STAGE1] audio_in" (every ~10 s) stays at INFO as the
                # audio-is-flowing proof the live voice oracle reads.
                if frame_count % 25 == 0:
                    stream_active = self._stream is not None and self._stream.is_active()
                    peak_amplitude = int(np.max(np.abs(mic_int16)))
                    logger.debug(
                        "WAKE_AUDIO_DIAG: stream_active=%s, frames_read=%d, "
                        "peak_amplitude=%d, rms=%.1f, device=%s (index=%s)",
                        stream_active,
                        frame_count,
                        peak_amplitude,
                        mic_rms,
                        self._device_name,
                        self._device_index,
                    )

                # DEBUG: Log raw audio stats on early frames and periodically
                if frame_count in (1, 2, 3, 10, 50) or frame_count % 500 == 0:
                    min_val = int(np.min(mic_int16))
                    max_val = int(np.max(mic_int16))
                    nonzero = int(np.count_nonzero(mic_int16))
                    logger.debug(
                        "[AUDIO_DEBUG] frame=%d mic_rms=%.1f min=%d max=%d nonzero=%d/%d",
                        frame_count,
                        mic_rms,
                        min_val,
                        max_val,
                        nonzero,
                        len(mic_int16),
                    )

                # --- Stream Health Monitoring ---
                # A frame is evidence the stream is DEAD only when it carries no
                # data at all -- every sample exactly zero. Working audio hardware
                # always delivers a noise floor, so an all-zero buffer means the
                # stream stopped producing, which is the thing this check exists
                # to catch.
                #
                # This used to ask whether the frame was QUIET (RMS < 5.0 on the
                # int16 scale, about -76 dBFS) and treat that as death. A quiet
                # room sits below that threshold, so a perfectly working
                # microphone was torn down and re-opened every 100 frames (~8s)
                # for as long as nobody spoke: 2,282 restarts in a single
                # measured session, on a stream that was delivering real frames
                # (743 of 1280 samples nonzero). Loudness was never the question.
                is_frame_dead = not bool(np.any(mic_int16))
                recent_frame_dead.append(is_frame_dead)

                # Keep only the last STREAM_HEALTH_WINDOW frames
                if len(recent_frame_dead) > STREAM_HEALTH_WINDOW:
                    recent_frame_dead.pop(0)

                # Check if we should restart (only after window is full)
                if len(recent_frame_dead) == STREAM_HEALTH_WINDOW:
                    dead_count = sum(recent_frame_dead)
                    silence_percent = dead_count / STREAM_HEALTH_WINDOW

                    if silence_percent >= STREAM_HEALTH_DEAD_PERCENT and time.monotonic() >= stream_health_next_allowed:
                        # Skip restart during DI playback: mic silence is
                        # expected because music goes through ChunkStamper,
                        # not speakers.  PyAudio restart holds the GIL for
                        # ~1s, which blocks the DI injection thread and
                        # causes audible stuttering (Bug #33 root cause 2).
                        try:
                            from audio_core.streaming.pipeline_wiring import (
                                _direct_injection_active,
                            )

                            if _direct_injection_active:
                                logger.debug(
                                    "[STREAM_HEALTH] Suppressed restart: DI active "
                                    "(silence is expected during direct injection)"
                                )
                                recent_frame_dead.clear()
                                stream_gap = True
                                continue
                        except ImportError:
                            pass

                        stream_health_restart_count += 1
                        # Widening backoff: even a stream that really is dead
                        # must not be restarted in a tight loop. Caps at 60s.
                        stream_health_next_allowed = time.monotonic() + min(60.0, 2.0**stream_health_restart_count)
                        now = time.monotonic()
                        should_warn = stream_health_restart_count == 1 or now - stream_health_last_warning >= 60.0
                        log_fn = logger.warning if should_warn else logger.debug
                        if should_warn:
                            stream_health_last_warning = now
                        log_fn(
                            "[STREAM_HEALTH] Audio stream %.0f%% silent over %d frames (~%.1fs). Restarting stream (count=%d).",
                            silence_percent * 100,
                            STREAM_HEALTH_WINDOW,
                            STREAM_HEALTH_WINDOW * 0.08,
                            stream_health_restart_count,
                        )
                        # Restart the stream with FULL PyAudio restart.
                        # Just closing/reopening the stream doesn't clear corrupted
                        # PortAudio state (common with USB webcam mics on Windows).
                        # A failed reopen leaves self._stream None; the read-error
                        # path below retries rather than dying, so we don't exit here.
                        stream_gap = True
                        if self._reopen_audio_stream():
                            recent_frame_dead.clear()  # Reset tracking after restart

                # --- Diagnostic: Audio pipeline health ---
                # Sampled every 5th chunk (400 ms): a diagnostic (clipping,
                # noise floor) that cost a seventh of the loop when busy.
                try:
                    if frame_count % 5 == 0:
                        audio_health = get_audio_health_monitor()
                        audio_health.process_frame(audio=mic_int16, timestamp=time.time())
                except Exception as diag_err:
                    if frame_count == 1:
                        logger.debug("Audio health diagnostics unavailable: %s", diag_err)

                # --- Diagnostic: Continuous audio buffer for captures ---
                if self._loop_flag("wake_audio_logging", _is_wake_audio_logging_enabled):
                    try:
                        diag_audio_buffer = get_wake_audio_buffer()
                        mic_rms_db = 20 * np.log10(max(mic_rms, 1e-10))
                        diag_audio_buffer.feed_audio(mic_int16, rms_db=mic_rms_db)
                    except Exception as diag_err:
                        if frame_count == 1:
                            logger.debug("Audio buffer diagnostics unavailable: %s", diag_err)

                # Feed mic ring buffer for passive calibration
                self._mic_ring.append(mic_int16.copy())

                # Periodic passive AEC delay calibration (~every 30s after warmup)
                if (
                    frame_count > 1000
                    and frame_count - self._last_passive_cal_frame >= self._passive_cal_interval_frames
                ):
                    self._last_passive_cal_frame = frame_count
                    self._run_passive_calibration(frame_count)

                # Apply AEC frame-by-frame
                processed_int16 = self._apply_aec_to_chunk(mic_int16, frame_count)
                self._emit_processed_frame(processed_int16)

                # WARNING: Do NOT apply noise suppression here.
                # ViolaWake's MLP was trained on raw (post-AEC) audio.
                # Noise suppression alters the spectral envelope, causing
                # training-serving skew that degrades wake word detection.
                # Use noise suppression ONLY in the STT/dictation path.

                # Store for VAD in on_detection callback
                last_processed_int16 = processed_int16

                # Convert processed audio to float32 ONCE
                processed_float32: np.ndarray = processed_int16.astype(np.float32)
                post_aec_rms = float(np.sqrt(np.mean(processed_float32**2)))

                # Update current metrics for policy decisions
                self._current_post_aec_rms = post_aec_rms

                # Normalize for ViolaWake engine (reuse the float32 we already have)
                audio_chunk = processed_float32 * (1.0 / AUDIO_INT16_SCALE)

                # Ring buffer update - O(chunk_size) instead of O(buffer_size)
                chunk_len = len(audio_chunk)
                end_idx = buffer_write_idx + chunk_len

                if end_idx <= CLIP_SAMPLES:
                    # Simple case: chunk fits without wrap
                    audio_buffer[buffer_write_idx:end_idx] = audio_chunk
                else:
                    # Wrap around: split the chunk
                    first_part = CLIP_SAMPLES - buffer_write_idx
                    audio_buffer[buffer_write_idx:] = audio_chunk[:first_part]
                    audio_buffer[: chunk_len - first_part] = audio_chunk[first_part:]

                buffer_write_idx = end_idx % CLIP_SAMPLES

                # A gap in the audio: drop the engine's streamed features so
                # nothing from before the gap is scored (warm-up ~1.4 s).
                if stream_gap:
                    stream_gap = False
                    self._reset_early_check()
                    if self._streaming:
                        with self._engine_lock:
                            if self._engine is not None and self._streaming:
                                self._engine.reset_stream()

                # Process according to the configured inference interval
                # (every chunk on the streaming path)
                if frame_count % self._infer_interval == 0:
                    on_legacy_grid = frame_count % LEGACY_GRID_CHUNKS == 0 or not self._streaming
                    # The 1.5 s window oldest-first, built only when something
                    # needs it (an early Stage 2 start, a diagnostics capture).
                    # Copying 24,000 samples every 80 ms was a seventh of this
                    # loop's time on a busy machine, where every numpy call
                    # waits for the GIL (wake capture harness, see _MicPump).
                    contiguous_cache: list[np.ndarray] = []

                    def contiguous(
                        _idx: int = buffer_write_idx, _cache: list[np.ndarray] = contiguous_cache
                    ) -> np.ndarray:
                        if not _cache:
                            _cache.append(
                                audio_buffer
                                if _idx == 0
                                else np.concatenate([audio_buffer[_idx:], audio_buffer[:_idx]])
                            )
                        return _cache[0]

                    # Use policy for effective threshold calculation (refreshed
                    # on the 400 ms grid; quiet hours reads settings)
                    if on_legacy_grid or effective_threshold is None:
                        context = WakeContext(
                            base_threshold=self._base_threshold,
                            loopback_rms=self._current_loopback_rms,
                            correlation=self._current_correlation,
                        )
                        # Quiet hours and the Stage 2 switch come from settings:
                        # re-read once a second, not every 400 ms. The playback
                        # state is part of the key, so music starting or stopping
                        # still applies on the next grid step.
                        playing_now = bool(self._policy.is_playback_active) or (
                            context.loopback_rms
                            > getattr(self._policy._config, "playback_presence_rms_threshold", 150.0)
                        )
                        effective_threshold = self._loop_flag(
                            "effective_threshold_%s_%s" % (playing_now, self._base_threshold),
                            lambda: self._policy.effective_threshold(context),
                            ttl=1.0,
                        ) or self._policy.effective_threshold(context)
                        stage2_checked = bool(self._loop_flag("stage2_checked", self._stage2_check_on, ttl=1.0))
                        music_rule = self._music_wake_rule(effective_threshold, stage2_checked)
                        if self._streaming:
                            self._log_wake_rule_change(self._base_threshold, stage2_checked)

                    # DIAGNOSTIC: Check audio buffer health before engine
                    if frame_count % 125 == 5:  # Log occasionally
                        contiguous_buffer = contiguous()
                        buf_rms = float(np.sqrt(np.mean(contiguous_buffer**2)))
                        buf_max = float(np.max(np.abs(contiguous_buffer)))
                        buf_nonzero = np.count_nonzero(contiguous_buffer)
                        logger.debug(
                            "[DIAG_BUFFER] rms=%.6f max=%.6f nonzero=%d/%d",
                            buf_rms,
                            buf_max,
                            buf_nonzero,
                            len(contiguous_buffer),
                        )

                    # --- Track-transition blanking ---
                    # Checked on the 400 ms grid in both modes, so the jump test
                    # and the blank length are exactly what shipped.
                    if on_legacy_grid:
                        cur_lb = self._current_loopback_rms
                        if prev_loopback_rms > 100 and cur_lb > 100:
                            ratio = max(cur_lb, prev_loopback_rms) / min(cur_lb, prev_loopback_rms)
                            if ratio > TRANSITION_RMS_RATIO:
                                transition_blank_remaining = TRANSITION_BLANK_FRAMES * (
                                    LEGACY_GRID_CHUNKS if self._streaming else 1
                                )
                                logger.debug(
                                    "[TRANSITION_BLANK] loopback jump %.0f→%.0f (%.1fx), blanking %d frames",
                                    prev_loopback_rms,
                                    cur_lb,
                                    ratio,
                                    TRANSITION_BLANK_FRAMES,
                                )
                        prev_loopback_rms = cur_lb

                    blanked = transition_blank_remaining > 0
                    if blanked:
                        transition_blank_remaining -= 1
                    if self._streaming:
                        # Every chunk feeds the engine's embedding stream (even
                        # while blanked, so features stay continuous); the
                        # engine applies the k-of-n rule and fires on_detection.
                        with self._engine_lock:
                            engine = self._engine
                            if engine is None or getattr(engine, "supports_streaming", False) is not True:
                                # None, or a hot reload swapped in a legacy model
                                # between frames; the next frame uses its cadence.
                                last_wake_score = 0.0
                            else:
                                engine.threshold = effective_threshold
                                engine.checked = stage2_checked
                                engine.music_rule = music_rule
                                last_wake_score = engine.process_chunk(audio_chunk, suppress=blanked)
                                self._maybe_start_early_check(engine, last_wake_score, contiguous, frame_count)
                    elif blanked:
                        last_wake_score = 0.0
                    else:
                        with self._engine_lock:
                            engine = self._engine
                            if engine is None:
                                last_wake_score = 0.0
                            else:
                                engine.threshold = effective_threshold
                                last_wake_score = engine.process_audio(contiguous())

                    # Track recent scores for confirmation gate (2-of-3)
                    if last_wake_score is not None:
                        recent_inference_scores.append(last_wake_score)
                        # Keep only the last CONFIRMATION_WINDOW scores
                        if len(recent_inference_scores) > CONFIRMATION_WINDOW:
                            recent_inference_scores.pop(0)

                    # --- JSONL score log + WAV capture (hub side) ---
                    if _SCORE_LOG_ENABLED and last_wake_score is not None:
                        # Normalize mic_rms to float [-1,1] scale for consistency with spoke
                        log_hub_inference(
                            last_wake_score,
                            last_mic_rms / AUDIO_INT16_SCALE,
                            last_wake_score >= effective_threshold,
                            audio_buffer=contiguous(),
                        )

                    # --- FP Collector: save high-scoring audio for retraining ---
                    # Only while collecting: its switch is a settings read plus
                    # a file check, read off this loop every 2 s together with
                    # the collector's retention cleanup.
                    if last_wake_score is not None:
                        collector = get_fp_collector()
                        if self._loop_flag("fp_collecting", lambda: _fp_collecting_and_cleanup(collector)):
                            collector.check_and_save(
                                score=last_wake_score,
                                audio_buffer=contiguous(),
                                ref_buffer=None,  # Reference buffer integration deferred
                            )

                    # --- Data Collection: near-miss capture ---
                    if last_wake_score is not None:
                        try:
                            from config.settings import settings as _dc_settings
                            from voice.wake_detector.data_collection.collector import (
                                wake_data_collection_allowed,
                            )

                            if self._loop_flag(
                                "data_collection",
                                lambda: bool(
                                    _dc_settings.wake_data_collection_enabled and wake_data_collection_allowed()
                                ),
                            ):
                                from voice.wake_detector.data_collection.collector import (
                                    get_clip_collector,
                                )

                                get_clip_collector().save_near_miss_clip(
                                    contiguous(),
                                    last_wake_score,
                                )
                        except Exception:
                            logger.debug("Near-miss clip save failed (data collection optional)")

                    # STAGE2: Score computed - log ALL scores for debugging
                    if last_wake_score is not None:
                        # Log every 25th frame or if score > 0.1 (to capture near-detections)
                        # Near-detections (> 0.5) stay at INFO; the periodic and
                        # low-score lines are DEBUG so an idle mic is not log spam.
                        if frame_count % 25 == 0 or last_wake_score > 0.5 or (last_wake_score > 0.1 and on_legacy_grid):
                            logger.log(
                                logging.INFO if last_wake_score > 0.5 else logging.DEBUG,
                                "[STAGE2] score: score=%.3f threshold=%.3f frame=%d",
                                last_wake_score,
                                effective_threshold,
                                frame_count,
                            )

                    # --- Diagnostic: Record score in history ---
                    if last_wake_score is not None:
                        try:
                            score_history = get_score_history()
                            score_history.record_score(
                                score=last_wake_score,
                                effective_threshold=effective_threshold,
                                playback_active=self._policy.is_playback_active,
                                mic_rms=last_mic_rms,
                                loopback_rms=self._current_loopback_rms,
                                post_aec_rms=self._current_post_aec_rms,
                            )
                        except Exception as diag_err:
                            if frame_count == 5:  # Log once early
                                logger.debug(
                                    "Score history diagnostics unavailable: %s",
                                    diag_err,
                                )

                    # Check for near-misses during contributor mode
                    if self._contributor_manager.is_active and last_wake_score is not None:
                        self._contributor_manager.on_score_update(
                            audio=contiguous().copy(),
                            score=last_wake_score,
                            threshold=effective_threshold,
                            context={
                                "mic_rms": last_mic_rms,
                                "loopback_rms": self._current_loopback_rms,
                                "post_aec_rms": self._current_post_aec_rms,
                                "correlation": self._current_correlation,
                                "playback_active": self._policy.is_playback_active,
                                "frame_count": frame_count,
                            },
                        )

        except Exception as e:
            logger.exception("ViolaWake detection error: %s", e)
        finally:
            self._stop_capture_pump()
            _end_fine_timer(fine_timer)

    # ------------------------------------------------------------------ #
    # Stage 2 wake verification (voice/wake_detector/wake_verifier.py)
    # ------------------------------------------------------------------ #

    # A verified wake's held audio is only for the command that wake starts.
    COMMAND_PREROLL_MAX_AGE_S = 4.0

    # How long the wake loop reuses a settings-backed answer. Each settings
    # read resolves the signed-in account (session database, file stats), and
    # the loop used to ask several times per 80 ms chunk. Every one of those
    # blocking calls gives up the GIL, and on a busy machine getting it back
    # took up to a second, so the loop fell behind the microphone and scored
    # chopped audio (1.0.5 build-7 laptop report, A0.3 run 3).
    LOOP_FLAG_TTL_S = 2.0

    def _loop_flag(self, name: str, read: Callable[[], Any], ttl: float | None = None) -> Any:
        """``read()``'s answer, refreshed at most every ``ttl`` seconds off the wake loop.

        The first call reads synchronously. After that a stale answer is
        returned at once and a background thread re-reads it, so a slow
        settings read never stalls the loop that drains the microphone.
        Errors read as False.
        """
        now = time.monotonic()
        limit = self.LOOP_FLAG_TTL_S if ttl is None else ttl
        cached = self._loop_flags.get(name)
        if cached is None:
            value = self._read_loop_flag(read)
            self._loop_flags[name] = (now, value)
            return value
        if now - cached[0] >= limit and name not in self._loop_flag_refreshing:
            self._loop_flag_refreshing.add(name)
            self._loop_flag_queue.append((name, read))
            self._ensure_flag_refresher()
            self._loop_flag_wake.set()
        return cached[1]

    def _ensure_flag_refresher(self) -> None:
        thread = self._loop_flag_thread
        if thread is not None and thread.is_alive():
            return
        # One long-lived thread: starting a thread per refresh made the wake
        # loop wait for the new thread to start, which is itself slow when busy.
        self._loop_flag_thread = threading.Thread(target=self._flag_refresher, name="wake-flag-refresh", daemon=True)
        self._loop_flag_thread.start()

    def _run_off_loop(self, task: Callable[[], None]) -> None:
        """Run ``task`` on the long-lived refresher thread (no thread start on the wake path)."""
        self._loop_tasks.append(task)
        self._ensure_flag_refresher()
        self._loop_flag_wake.set()

    def _flag_refresher(self) -> None:
        while True:
            self._loop_flag_wake.wait(30.0)
            self._loop_flag_wake.clear()
            while self._loop_tasks:
                task = self._loop_tasks.popleft()
                try:
                    task()
                except Exception:  # noqa: BLE001, RUF100 - background bookkeeping only
                    logger.debug("Wake background task failed", exc_info=True)
            while self._loop_flag_queue:
                name, read = self._loop_flag_queue.popleft()
                try:
                    self._loop_flags[name] = (time.monotonic(), self._read_loop_flag(read))
                finally:
                    self._loop_flag_refreshing.discard(name)

    @staticmethod
    def _read_loop_flag(read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception:  # noqa: BLE001, RUF100 - a diagnostics switch must never stop the loop
            return False

    def _start_capture_pump(self) -> None:
        """Start the capture thread when the stream is a real PortAudio stream."""
        real_stream = GuardedStream is not object and isinstance(self._stream, GuardedStream)
        if not (self._force_capture_pump or real_stream):
            return
        if self._mic_pump is None:
            self._mic_pump = _MicPump(self)
        self._mic_pump.start()
        logger.info("Wake microphone capture thread started")

    def _stop_capture_pump(self) -> None:
        pump = self._mic_pump
        if pump is not None:
            pump.stop()
            pump.flush()

    def _mic_read(self, block: bool = True) -> bytes | None:
        """The next 80 ms of microphone audio, from the capture thread when it runs.

        Raises the stream's read error. None: no audio yet (``block=False``) or
        none within a second (the device is being changed, or stopped).
        """
        pump = self._mic_pump
        if pump is not None and pump.running:
            return pump.read(block=block)
        stream = self._stream
        if stream is None:
            if block:
                raise RuntimeError("audio stream is not open")
            return None
        if not block:
            try:
                if stream.get_read_available() < self.CHUNK_SIZE:
                    return None
            except Exception:  # noqa: BLE001, RUF100 - unknown fill level: a blocking read still drains
                pass
        return stream.read(self.CHUNK_SIZE, exception_on_overflow=False)

    def _warm_wake_verifier(self) -> None:
        """Load the Whisper check in the background at startup, never on a wake."""
        try:
            from voice.wake_detector.wake_verifier import warm_wake_verifier

            warm_wake_verifier()
        except Exception as exc:  # noqa: BLE001, RUF100 - the check is optional; wakes pass without it
            logger.warning("[WAKE_VERIFY] could not start the verifier warm-up: %s", exc)
        threading.Thread(target=self._prewarm_wake_path, name="wake-prewarm", daemon=True).start()

    def _prewarm_wake_path(self) -> None:
        """Pay the wake path's one-time costs at start-up, not on the first "Viola".

        The first wake used to import modules (instrumentation, data
        collection, ducking, the chime), build the settings manager and load
        the policy's Silero VAD inside the detection callback. On a busy
        machine every file read of an import waits for the GIL, and the first
        chime came about 2.7 s after the confirming frame (wake latency
        harness, 2 busy threads) against about 0.2 s for later wakes.
        """
        import importlib

        started = time.perf_counter()
        for name in (
            "admin.instrumentation",
            "config.settings",
            "voice.wake_detector.data_collection.collector",
            "utils.audio_ducking",
            "voice.wake_chime",
            "core.state_hub",
            "core.unified_state",
            "telemetry.accumulator",
        ):
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001, RUF100 - optional pieces; the wake path handles their absence
                logger.debug("Wake pre-warm could not import %s", name, exc_info=True)
        try:
            ensure_vad = getattr(self._policy, "_ensure_vad_model", None)
            if callable(ensure_vad) and ensure_vad():
                # First inference of the ONNX session (one silent window).
                self._policy.compute_vad_confidence(audio_int16=np.zeros(512, dtype=np.int16))
            from voice.wake_detector.wake_decision_policy import _check_state_hub_listening

            _check_state_hub_listening()  # builds the state hub the listening gate reads
            self._loop_flag("wake_audio_logging", _is_wake_audio_logging_enabled)
            get_wake_analytics()
            get_decision_tracer()
        except Exception:  # noqa: BLE001, RUF100 - a warm-up must never break listening
            logger.debug("Wake pre-warm step failed", exc_info=True)
        logger.info("Wake path warmed in %.0f ms", (time.perf_counter() - started) * 1000)

    def _log_wake_rule_change(self, level: float, checked: bool) -> None:
        """Log the base wake rule again when the saved level or the Stage 2 switch changes.

        Only the user's base level is tracked: the temporary boosts during
        playback and quiet hours would otherwise log on every song.
        """
        key = (round(float(level), 4), bool(checked))
        if key == self._logged_wake_rule:
            return
        self._logged_wake_rule = key
        try:
            from violawake.config import stream_operating_point

            k, n, frame_threshold = stream_operating_point(level, checked=checked)
        except (ImportError, TypeError, ValueError):
            return
        logger.info(
            "Wake rule now: wake on %d of %d frames at frame threshold %.2f (sensitivity level %.2f, Stage 2 check %s)",
            k,
            n,
            frame_threshold,
            level,
            "on" if checked else "off",
        )

    def _music_wake_rule(self, level: float, checked: bool) -> tuple[int, int, float] | None:
        """The looser rule for Viola's own music (config.MUSIC_WAKE_RULE), or None.

        Only while Viola's own music plays, the Stage 2 check is on (it is the
        guard: a music-path wake needs a heard "Viola"), and the level is
        Balanced or more sensitive with no quiet-hours boost.
        """
        if not checked or self._force_wake or not self._policy.is_playback_active:
            return None
        try:
            from violawake.config import DEFAULT_THRESHOLD, MUSIC_WAKE_RULE

            if float(level) > DEFAULT_THRESHOLD + 1e-9 or float(self._base_threshold) > DEFAULT_THRESHOLD + 1e-9:
                return None
            return MUSIC_WAKE_RULE
        except (ImportError, TypeError, ValueError):
            return None

    def _stage2_check_on(self) -> bool:
        """The opt-in Stage 2 check is on (and FORCE_WAKE, which skips it, is not)."""
        if self._force_wake:
            return False
        try:
            from voice.wake_detector.wake_verifier import wake_verification_enabled

            return bool(wake_verification_enabled())
        except Exception:  # noqa: BLE001, RUF100 - unreadable switch: the check's own default (off)
            return False

    def _maybe_start_early_check(
        self, engine: Any, score: float, window: np.ndarray | Callable[[], np.ndarray], frame: int
    ) -> None:
        """Start the Stage 2 decode early when the agreement run reaches the launch level.

        Called after every streaming score. Cheap when nothing launches (a
        counter); a launch copies the window and hands it to the verifier's
        worker thread, never blocking this loop.
        """
        if frame == self._wake_fired_frame:
            return  # this chunk already confirmed a wake; its check is done
        tracker = self._early_tracker
        try:
            if tracker is None:
                from voice.wake_detector.wake_verifier import EarlyStartTracker, get_wake_verifier

                tracker = EarlyStartTracker(get_wake_verifier().config.early_start_frames)
                self._early_tracker = tracker
            k, _n = engine.agreement
            if not tracker.update(float(score), float(engine.stream_threshold), int(k)):
                return
            if self._force_wake:
                return  # the check is skipped for these wakes anyway
            from voice.wake_detector.wake_verifier import get_wake_verifier, wake_verification_enabled

            if not wake_verification_enabled():
                return
            if callable(window):
                window = window()
            self._early_decode = get_wake_verifier().start_early(window, frame)
        except Exception as exc:  # noqa: BLE001, RUF100 - the early start is an optimisation; the serial check remains
            logger.debug("[WAKE_VERIFY] early start skipped: %s", exc)

    def _reset_early_check(self) -> None:
        """A gap in the audio: the run count restarts and any early decode is stale."""
        self._early_decode = None
        if self._early_tracker is not None:
            self._early_tracker.reset()

    def _take_early_decode(self, trigger_frame: int) -> Any:
        """The early decode for the wake confirmed on ``trigger_frame``, if it belongs to it."""
        self._wake_fired_frame = trigger_frame
        early = self._early_decode
        self._early_decode = None
        if self._early_tracker is not None:
            self._early_tracker.reset()
        if early is None:
            return None
        try:
            from voice.wake_detector.wake_verifier import EarlyStartTracker

            with self._engine_lock:
                engine = self._engine
                _k, n = engine.agreement if engine is not None else (1, 1)
            if EarlyStartTracker.usable(early.frame, trigger_frame, n):
                return early
        except Exception as exc:  # noqa: BLE001, RUF100 - fall back to the serial check
            logger.debug("[WAKE_VERIFY] early decode unusable: %s", exc)
        return None

    def _verify_wake_candidate(
        self,
        window: np.ndarray,
        score: float,
        early: Any = None,
        *,
        level: float | None = None,
        require_token: bool = False,
    ) -> bool:
        """Second listen before the chime. True lets the wake through.

        Runs on the wake loop thread inside the detection callback, so it may
        read the stream itself; the frames it reads are kept for the command.
        ``early``: the decode this wake's agreement run started (or None).
        ``level``: the sensitivity level (a score below it gets the stricter
        low-score rule). ``require_token``: the looser music rule confirmed the
        wake, so only a heard "Viola" lets it through (and doubt does not).
        It runs while Viola's own music plays too (it used to be skipped).
        """
        try:
            from voice.wake_detector.wake_verifier import get_wake_verifier, wake_verification_enabled

            if not wake_verification_enabled():
                return not require_token
            extra: dict[str, Any] = {"early": early} if early is not None else {}
            if level is not None:
                extra["level"] = level
            if require_token:
                extra["require_token"] = True
            result = get_wake_verifier().verify(
                window,
                self._read_verification_chunk,
                score,
                playback_active=bool(self._policy.is_playback_active),
                **extra,
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - doubt passes (not on the music path)
            logger.warning(
                "[WAKE_VERIFY] %s reason=error score=%.3f (%s)", "REJECT" if require_token else "PASS", score, exc
            )
            return not require_token
        logger.info(result.log_line())
        if result.passed:
            self._start_command_preroll(result.held_frames)
        return result.passed

    def _read_verification_chunk(self, block: bool) -> Any:
        """Read the next 80 ms for the wake check (raw bytes + echo-cancelled audio).

        ``block=False`` returns None while less than a chunk is buffered, so the
        check can stop the moment Whisper answers.
        """
        from voice.wake_detector.wake_verifier import WakeChunk

        if self._stream is None:
            return None
        try:
            audio_data = self._mic_read(block=block)
        except Exception as exc:  # noqa: BLE001, RUF100 - the wake loop's own read handles recovery
            logger.debug("[WAKE_VERIFY] stream read failed: %s", exc)
            return None
        if audio_data is None:
            return None
        self.detection_signal.mark()
        tap = self._frame_tap
        if tap is not None:
            try:
                tap(audio_data)
            except Exception:  # noqa: BLE001, RUF100 - never let a tap error stop the check
                pass
        mic_int16 = np.frombuffer(audio_data, dtype=np.int16)
        self._verify_frame_count += 1
        processed = self._apply_aec_to_chunk(mic_int16, self._verify_frame_count)
        self._emit_processed_frame(processed)
        return WakeChunk(raw=audio_data, audio=processed.astype(np.float32) * (1.0 / AUDIO_INT16_SCALE))

    def _start_command_preroll(self, frames: list[bytes]) -> None:
        with self._command_preroll_lock:
            self._command_preroll = list(frames)
            self._command_preroll_since = time.monotonic()

    def _extend_command_preroll(self, audio_data: bytes) -> None:
        """Keep a verified wake's audio flowing until the recorder takes it."""
        if self._command_preroll_since is None:
            return
        with self._command_preroll_lock:
            since = self._command_preroll_since
            if since is None:
                return
            if time.monotonic() - since > self.COMMAND_PREROLL_MAX_AGE_S:
                # The wake was dropped downstream (rate limit, already
                # listening); its audio belongs to nobody.
                self._command_preroll = []
                self._command_preroll_since = None
                return
            self._command_preroll.append(audio_data)

    def _take_command_preroll(self) -> list[bytes]:
        with self._command_preroll_lock:
            frames = self._command_preroll
            since = self._command_preroll_since
            self._command_preroll = []
            self._command_preroll_since = None
        if since is None or time.monotonic() - since > self.COMMAND_PREROLL_MAX_AGE_S:
            return []
        if frames:
            logger.info("[WAKE_VERIFY] command recording starts with %d frames read during the check", len(frames))
        return list(frames)

    def listen_and_record_command(
        self,
        silence_threshold: int = 500,
        silence_duration: float = 1.0,
        timeout: float = 7.0,
        onset_timeout: float | None = None,
    ) -> Path | None:
        """
        Listen for and record a voice command after wake word detection.

        Two separate windows (1.0.5 build-7 laptop report, A0.6 / session 3):
        a user who waits for the chime used to lose the turn to 1 s of silence
        (the end-of-speech rule ran from the first frame), and after speech the
        recording ran on for about 4 s because the neural end-of-speech
        detector never loaded (``is_available`` is False until first use, and
        nothing used it first), so a 500 RMS threshold decided, which room
        noise or ducked music easily exceeds.

        Args:
            silence_threshold: RMS level used only when the Silero VAD is not
                available (fallback speech/silence test)
            silence_duration: End-of-speech: this much non-speech after speech
                stops the recording
            timeout: Longest command, counted from the speech onset (or from
                the start when ``onset_timeout`` is None)
            onset_timeout: Pre-speech window. If set, wait up to this many
                seconds for speech to start; silence before it never ends the
                recording, and a short lead-in before the onset is kept. If
                None, recording counts as started at once (legacy behaviour).
                Speech already in the audio the wake check read (a command said
                straight after "Viola") counts as the onset.

        Returns:
            Path to recorded audio file, or None if failed / nothing was said
        """
        import wave

        logger.info("[ViolaWake] Recording voice command...")

        if self._stream is None:
            logger.warning("[ViolaWake] Audio stream not initialized")
            return None

        # Set recording flag to pause wake detection loop
        with self._recording_lock:
            self._recording_command = True
        logger.debug("[ViolaWake] Wake detection paused for recording")

        try:
            frames, outcome = self._record_command_frames(
                silence_threshold=silence_threshold,
                silence_duration=silence_duration,
                timeout=timeout,
                onset_timeout=onset_timeout,
            )
            if outcome == "no_speech":
                return None
            if not frames:
                logger.warning("[ViolaWake] No audio recorded")
                return None

            # Save to temp file
            temp_dir = _get_voice_temp_dir()
            output_path = temp_dir / ("command_%s.wav" % secrets.token_hex(8))
            saved_ok = False

            try:
                with wave.open(str(output_path), "wb") as wf:
                    wf.setnchannels(self.CHANNELS)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(self.SAMPLE_RATE)
                    wf.writeframes(b"".join(frames))

                saved_ok = True
                logger.info("[ViolaWake] Recorded %d frames to %s", len(frames), output_path)
                return output_path

            except Exception as e:
                logger.error("[ViolaWake] Failed to save recording: %s", e)
                return None
            finally:
                if not saved_ok and output_path.exists():
                    try:
                        output_path.unlink()
                    except OSError as exc:
                        logger.warning(
                            "[ViolaWake] Failed to clean up temp recording %s: %s",
                            output_path,
                            exc,
                        )

        finally:
            # Always clear recording flag to resume wake detection
            with self._recording_lock:
                self._recording_command = False
            logger.debug("[ViolaWake] Wake detection resumed")

    # Speech kept before a detected onset (the first syllable is often softer
    # than the VAD's speech threshold).
    COMMAND_LEAD_IN_S = 0.4

    def _record_command_frames(
        self,
        *,
        silence_threshold: int,
        silence_duration: float,
        timeout: float,
        onset_timeout: float | None,
    ) -> tuple[list[bytes], str]:
        """Read the command audio. Returns (raw frames, outcome).

        outcome: "end_of_speech", "timeout", "no_speech" (nothing said within
        ``onset_timeout``), "error". Speech/silence is judged on the
        echo-cancelled audio (ducked music or the chime is not the user), with
        the Silero VAD over every 32 ms window, falling back to RMS. The raw
        microphone frames are what is recorded for the transcriber.
        """
        from voice.wake_detector.silero_endpoint import (
            SPEECH_THRESHOLD as _VAD_SPEECH_THRESH,
            get_silero_endpoint_detector,
        )

        vad = get_silero_endpoint_detector()
        vad.reset_state()
        ensure = getattr(vad, "ensure_loaded", None)
        use_vad = bool(ensure()) if callable(ensure) else bool(getattr(vad, "is_available", False))
        logger.debug("[ViolaWake] End-of-speech detection: %s", "Silero VAD" if use_vad else "RMS fallback")

        chunk_s = self.CHUNK_SIZE / float(self.SAMPLE_RATE)
        silence_needed = max(1, round(silence_duration / chunk_s))
        lead_in_frames = max(0, round(self.COMMAND_LEAD_IN_S / chunk_s))

        def is_speech(audio_data: bytes, *, from_stream: bool) -> bool:
            nonlocal use_vad
            mic = np.frombuffer(audio_data, dtype=np.int16)
            judged = mic
            if from_stream:
                # The same echo cancellation the wake check uses; keeps the
                # canceller's reference in step while the wake loop is paused.
                self._verify_frame_count += 1
                try:
                    judged = self._apply_aec_to_chunk(mic, self._verify_frame_count)
                except Exception:  # noqa: BLE001, RUF100 - judge the raw audio instead
                    judged = mic
            rms = float(np.sqrt(np.mean(judged.astype(np.float32) ** 2))) if len(judged) else 0.0
            if use_vad and rms > 1.0:
                full = getattr(vad, "process_chunk_full", None)
                prob = full(judged) if callable(full) else vad.process_chunk(judged)
                if prob >= 0:
                    return prob > _VAD_SPEECH_THRESH
                use_vad = False  # VAD failed mid-recording: RMS from here on
            return rms >= silence_threshold

        frames: list[bytes] = []
        lead_in: deque[bytes] = deque(maxlen=max(1, lead_in_frames))
        speech_started = onset_timeout is None
        speech_frames = 0
        silent_run = 0

        # Audio the wake check already read after the wake word (the start of
        # a "Viola, ..." said in one breath) opens the command recording. If
        # it already holds speech, that is the onset.
        preroll = self._take_command_preroll()
        if preroll:
            heard = [is_speech(f, from_stream=False) for f in preroll]
            frames.extend(preroll)
            if any(heard):
                speech_started = True
                speech_frames = sum(heard)
                last = max(i for i, h in enumerate(heard) if h)
                silent_run = len(heard) - 1 - last

        started = time.monotonic()
        speech_at = started
        outcome = "timeout"
        try:
            while True:
                now = time.monotonic()
                if speech_started:
                    if now - speech_at >= timeout:
                        outcome = "timeout"
                        logger.info("[ViolaWake] Recording timeout reached")
                        break
                elif now - started >= float(onset_timeout or 0.0):
                    outcome = "no_speech"
                    logger.info(
                        "[ViolaWake] No speech within %.1f s after the wake; nothing to transcribe",
                        float(onset_timeout or 0.0),
                    )
                    return [], outcome

                try:
                    audio_data = self._mic_read()
                except Exception as e:
                    logger.error("[ViolaWake] Recording error: %s", e)
                    outcome = "error"
                    break
                if audio_data is None:
                    continue
                speech = is_speech(audio_data, from_stream=True)

                if not speech_started:
                    if speech:
                        speech_started = True
                        speech_at = time.monotonic()
                        frames.extend(lead_in)
                        lead_in.clear()
                        frames.append(audio_data)
                        speech_frames += 1
                        logger.debug("[ViolaWake] Speech onset after %.2f s", speech_at - started)
                    else:
                        lead_in.append(audio_data)
                    continue

                frames.append(audio_data)
                if speech:
                    speech_frames += 1
                    silent_run = 0
                    continue
                silent_run += 1
                if speech_frames > 0 and silent_run >= silence_needed:
                    outcome = "end_of_speech"
                    logger.info("[ViolaWake] Silence detected, stopping recording")
                    break
                if speech_frames == 0 and silent_run >= silence_needed:
                    # Legacy mode (no pre-speech window) and nothing said.
                    outcome = "end_of_speech"
                    logger.info("[ViolaWake] Silence detected, stopping recording")
                    break
        finally:
            logger.info(
                "[ViolaWake] Recording ended: outcome=%s speech_ms=%d trailing_silence_ms=%d total_ms=%d vad=%s",
                outcome,
                round(speech_frames * chunk_s * 1000),
                round(silent_run * chunk_s * 1000),
                round(len(frames) * chunk_s * 1000),
                "silero" if use_vad else "rms",
            )
        return frames, outcome

    def cleanup(self) -> None:
        """Clean up resources."""
        self._stop_capture_pump()
        with self._state_lock:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception as e:
                    logger.debug("Error closing audio stream during cleanup: %s", e)
                self._stream = None

            if self._audio is not None:
                try:
                    terminate_portaudio(self._audio)
                except Exception as e:
                    logger.debug("Error terminating PyAudio during cleanup: %s", e)
                self._audio = None

            with self._engine_lock:
                self._engine = None
                self._engine_callback = None
            self._is_initialized = False

            # Reset AEC state
            if self._aec_processor is not None:
                try:
                    self._aec_processor.reset()
                except Exception as e:
                    logger.debug("Error resetting AEC processor during cleanup: %s", e)
            self._policy.reset()

        logger.info("ViolaWake listener cleaned up")

    def __repr__(self) -> str:
        aec_status = self._aec_processor.name if self._aec_processor else "disabled"
        return f"ViolaWakeListener(threshold={self.threshold}, model={self._model_path.name}, aec={aec_status})"
