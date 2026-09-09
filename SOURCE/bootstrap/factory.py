"""
Bootstrap Factory
Modular, testable initialization for Viola subsystems.

Consolidates initialization logic with clear separation of concerns:
- Each component has its own factory method
- Clear error handling and logging
- Easy to test (inject mocks)
- Reusable across entry points (main, desktop, tests)
"""

from __future__ import annotations

import asyncio
import importlib
import platform
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard, cast

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop

    from backend.app_state import AppState
    from backend.intent_bridge import IntentBridge
    from backend.music_adapter import (
        MusicControllerAdapter as MusicControllerAdapterType,
    )
    from config.settings import AppConfig
    from core.runtime_profile import RuntimeProfile as _RuntimeProfileType
    from core.voice_orchestrator import VoiceOrchestrator
    from fastapi import FastAPI
    from music.runtime import PlayerControlSurface as MusicPlayerType
    from voice.pipeline import VoicePipeline
    from voice.synthesis.engine import TTSEngine as TTSEngineType
else:
    # Runtime fallbacks only for types used outside annotations
    _RuntimeProfileType = object
    MusicPlayerType = object
    VoicePipeline = object
    AbstractEventLoop = object
    FastAPI = object  # For cast() at runtime
    TTSEngineType = object  # For cast() at runtime

from config.defaults import DEFAULT_VOICE_MODE
from core.constants import DEFAULT_API_PORT, LOCALHOST
from core.logging_config import get_logger
from diagnostics.startup_telemetry import (
    register_post_bind_initializer,
    subsystem_timer,
)

logger = get_logger(__name__)


def _boot_checkpoint(name: str) -> None:
    """Append a breadcrumb marking a bootstrap() stage reached, independent
    of the logger/stdout state.

    #1500: the Linux AppImage headless smoke's native segfault (rc=139, no
    Python traceback -- a SIGSEGV cannot be caught by any try/except) was
    narrowed to "somewhere between audio_device_validation and the FastAPI
    app existing" by the LAST log line before the crash, but that only
    bounds a ~65-line span with several subsystem constructions in it.
    Mirrors viola_qt.py's own `_boot_checkpoint` (same file, same append-only
    write, same blanket exception swallow) rather than importing it, since
    viola_qt.py imports this module during boot and importing back would be
    circular; duplication of ~6 stdlib-only lines is the right tradeoff over
    a shared-module refactor for a diagnostic breadcrumb.

    Line format matches viola_qt.py's writer exactly -- UTC wall clock + pid +
    name (#4650) -- because both append to the SAME file, and a trail where
    half the lines are dated is barely better than one where none are.
    """
    try:
        import os as _os
        import tempfile as _tf
        import time as _time

        stamp = _time.strftime("%Y-%m-%dT%H:%M:%S", _time.gmtime()) + ".%03dZ" % (int(_time.time() * 1000) % 1000)
        with open(_os.path.join(_tf.gettempdir(), "viola_boot_checkpoints.log"), "a", encoding="utf-8") as _f:
            _f.write("%s pid=%d %s\n" % (stamp, _os.getpid(), name))
    except Exception:  # noqa: BLE001, S110, RUF100 - a breadcrumb must never itself crash the app
        pass


_NO_LOCAL_MUSIC_FOLDER_MESSAGE = "No music folder configured. Set one in Settings → Music."
_SYNCED_MUSIC_FOLDER_MARKERS = (
    ("OneDrive", "OneDrive"),
    ("iCloudDrive", "iCloud"),
    ("Dropbox", "Dropbox"),
)


def _synced_music_folder_name(folder_path: str) -> str | None:
    for marker, display_name in _SYNCED_MUSIC_FOLDER_MARKERS:
        if marker.lower() in folder_path.lower():
            return display_name
    return None


def _broadcast_startup_warning(message: str) -> None:
    try:
        from core.user_context import get_device_user_id
        from ui.websocket.event_hub import get_event_hub

        hub = get_event_hub()
        loop = getattr(hub, "_main_loop", None) if hub is not None else None
        if loop is None or not loop.is_running():
            logger.debug("Startup warning toast skipped; EventHub loop unavailable")
            return
        payload = {"message": message, "level": "warning"}
        user_id = get_device_user_id()
        asyncio.run_coroutine_threadsafe(
            hub.broadcast("error", payload, user_id=user_id, force=True),
            loop,
        )
    except Exception as toast_err:
        logger.debug("Startup warning toast skipped: %s", toast_err)


# The closed set of ways a best-effort startup notice can fail to be raised.
# A blind except here would also swallow real programmer errors (mirrors the
# `_BRIDGE_ERRORS` convention in services/notifications/timer_notifier.py).
_NOTICE_ERRORS = (ImportError, RuntimeError, OSError, TypeError, ValueError, AttributeError, LookupError)


def _queue_audio_device_notice(code: str, state: object) -> None:
    """Tell the user an audio device is unusable, and leave a durable trace.

    Runs during bootstrap, so there is normally no client connected and no
    running loop: ``notify_user`` holds the notice and the EventHub delivers it
    the moment the UI connects. The breadcrumb is the record either way.
    """
    try:
        from core.error_messages import get_user_message
        from core.user_notice import notify_user

        message = get_user_message(code)
        notify_user(code, message, level="warning")
        try:
            state.append_breadcrumb(message)
        except _NOTICE_ERRORS as exc:  # pragma: no cover - defensive
            logger.debug("Failed to record audio device breadcrumb: %s", exc)
    except _NOTICE_ERRORS as exc:  # pragma: no cover - defensive
        logger.debug("Audio device notice skipped: %s", exc)


def _peek_existing_attr(target: object, name: str) -> object | None:
    """Read a direct attribute without materializing lazy or per-user proxies."""
    try:
        return object.__getattribute__(target, name)
    except AttributeError:
        return None


# Type-safe protocols for optional imports
class ConsentServiceProtocol(Protocol):
    """Protocol for consent service."""

    def resolve_access_token(self, provider: str) -> str | None:
        """Resolve access token for provider."""
        ...


# Optional import: runtime profile
_RuntimeProfile: type[_RuntimeProfileType] | None = None
_detect_runtime_profile: Callable[..., _RuntimeProfileType | None] | None = None
_apply_profile_to_settings: Callable[[object, _RuntimeProfileType], None] | None = None

try:
    from core.runtime_profile import (
        RuntimeProfile as _imported_runtime_profile,
        apply_profile_to_settings as _imported_apply_profile_to_settings,
        detect_runtime_profile as _imported_detect_runtime_profile,
    )
except Exception:  # pragma: no cover - runtime profile heuristics optional in tests
    pass
else:
    _RuntimeProfile = _imported_runtime_profile
    _apply_profile_to_settings = _imported_apply_profile_to_settings
    _detect_runtime_profile = _imported_detect_runtime_profile

RuntimeProfile = _RuntimeProfile
detect_runtime_profile = _detect_runtime_profile
apply_profile_to_settings = _apply_profile_to_settings


# Raspberry Pi optimizations

ApplyOptimizationsCallable = Callable[[object], None]
LogResourceProfileCallable = Callable[[str], None]
IsRaspberryPiCallable = Callable[[], bool]

apply_lightweight_optimizations: ApplyOptimizationsCallable
log_resource_profile: LogResourceProfileCallable
is_raspberry_pi: IsRaspberryPiCallable


def _noop_apply_lightweight_optimizations(_config: object) -> None:
    return None


def _noop_log_resource_profile(_context: str = "") -> None:
    return None


def _noop_is_raspberry_pi() -> bool:
    return False


try:
    from performance.pi_optimizations import (
        apply_lightweight_optimizations as _imported_apply_lightweight_optimizations,
        is_raspberry_pi as _imported_is_raspberry_pi,
        log_resource_profile as _imported_log_resource_profile,
    )
except ImportError:
    apply_lightweight_optimizations = _noop_apply_lightweight_optimizations
    log_resource_profile = _noop_log_resource_profile
    is_raspberry_pi = _noop_is_raspberry_pi
else:
    apply_lightweight_optimizations = cast(ApplyOptimizationsCallable, _imported_apply_lightweight_optimizations)
    log_resource_profile = cast(LogResourceProfileCallable, _imported_log_resource_profile)
    is_raspberry_pi = _imported_is_raspberry_pi

from utils.dependency_manager import ensure_voice_dependencies


class _LegacySymbolPlaceholder:
    """Callable placeholder used when legacy exports are unavailable."""

    def __init__(self, module_path: str, symbol: str, error: Exception) -> None:
        self._module_path = module_path
        self._symbol = symbol
        self._error = error

    def __call__(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError(f"{self._symbol} from {self._module_path} is unavailable: {self._error}")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Missing legacy export {self._module_path}.{self._symbol}: {self._error}>"


LegacyCallable = Callable[..., object]


def _import_legacy_symbol(module_path: str, symbol: str) -> LegacyCallable | _LegacySymbolPlaceholder:
    """Safely import a symbol required by legacy tests."""
    try:
        module = importlib.import_module(module_path)
        exported = getattr(module, symbol)
        if callable(exported):
            return cast(LegacyCallable, exported)
        raise TypeError(f"{module_path}.{symbol} is not callable")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Legacy export unavailable: %s.%s (%s)", module_path, symbol, exc)
        return _LegacySymbolPlaceholder(module_path, symbol, exc)


def _is_placeholder(obj: object) -> TypeGuard[_LegacySymbolPlaceholder]:
    return isinstance(obj, _LegacySymbolPlaceholder)


def _is_mock_symbol(obj: object) -> bool:
    """Detect MagicMock replacements so tests can observe call sites."""
    return getattr(obj, "__module__", "").startswith("unittest.mock")


# Legacy exports required by historical tests (see docs/legacy_surfaces.md)
# Deferred to module __getattr__ to avoid ~2.2s of imports at module load time.
# IntentInterpreter import deferred — backend.intent_bridge pulls in the entire
# LLM SDK chain (~2-3s).  The placeholder check in create_intent_interpreter()
# is replaced by try/except around the actual import.
IntentInterpreter: LegacyCallable | _LegacySymbolPlaceholder | None = None

_LAZY_LEGACY_EXPORTS: dict[str, tuple[str, str]] = {
    "TTSEngine": ("voice.synthesis.engine", "TTSEngine"),
    "MusicPlayer": ("music.player", "MusicPlayer"),
    "MusicControllerAdapter": ("backend.music_adapter", "MusicControllerAdapter"),
    "create_app": ("backend.fastapi_app", "build_fastapi_app"),
}


def __getattr__(name: str) -> object:
    if name in _LAZY_LEGACY_EXPORTS:
        module_path, symbol = _LAZY_LEGACY_EXPORTS[name]
        result = _import_legacy_symbol(module_path, symbol)
        globals()[name] = result  # Cache for subsequent access
        return result
    # Consent service: deferred to avoid 1.2s cold-start import
    if name == "get_consent_service":
        try:
            from music.consent import get_consent_service as _gcs

            globals()["get_consent_service"] = _gcs
            return _gcs
        except Exception:
            return None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class BootstrapConfig:
    """
    Configuration for bootstrap process.

    Attributes:
        host: API server host
        port: API server port
        voice: Enable voice features
        wake: Enable wake word
        desktop_mode: Running in desktop (PyQt6) mode
    """

    host: str = LOCALHOST
    port: int = DEFAULT_API_PORT
    voice: bool = False
    wake: bool = False
    desktop_mode: bool = False
    voice_disabled_reason: str | None = None
    voice_missing_dependencies: tuple[str, ...] = ()


@dataclass
class BootstrapResult:
    """
    Result of bootstrap process.

    Contains all initialized components.
    """

    state: AppState
    music: MusicControllerAdapterType | None
    tts: TTSEngineType | None
    intent: IntentBridge
    app: FastAPI
    voice: VoiceOrchestrator | None = None
    uvicorn_server: object | None = None  # uvicorn.Server - avoid heavy import
    adaptive_manager: object | None = None  # IMPROVEMENT #4: Adaptive resource manager
    runtime_profile: _RuntimeProfileType | None = None
    state_snapshotter: object | None = None  # diagnostics.state_snapshot.PersistentSnapshotter
    telemetry_scheduler: object | None = None  # telemetry.scheduler.TelemetryScheduler
    health_checker: object | None = None  # admin.health_checker.HealthChecker
    wake_data_services: tuple[object, ...] = ()  # (StorageManager, UploadQueue, TriggerClassifier)
    voice_disabled_reason: str | None = None
    voice_missing_dependencies: tuple[str, ...] = ()
    degraded_components: tuple[str, ...] = ()


class BootstrapFactory:
    """
    Factory for creating and initializing Viola components.

    Provides modular initialization with clear error handling.
    Each method can be overridden or mocked for testing.
    """

    @staticmethod
    def _ensure_voice_dependencies(config: BootstrapConfig) -> None:
        """Ensure wake/voice dependencies are installed before boot."""
        if not (config.voice or config.wake):
            config.voice_disabled_reason = None
            config.voice_missing_dependencies = ()
            return
        try:
            missing = ensure_voice_dependencies(auto_install=False, strict=False)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Voice dependency check failed: %s", exc)
            missing = []
        if missing:
            missing_names = tuple(spec.import_name for spec in missing)
            logger.warning(
                "Voice dependencies unavailable (%s); disabling voice features for this session.",
                ", ".join(missing_names),
            )
            if config.voice:
                config.voice = False
            if config.wake:
                config.wake = False
            config.voice_disabled_reason = "missing_dependencies"
            config.voice_missing_dependencies = missing_names
            return
        config.voice_disabled_reason = None
        config.voice_missing_dependencies = ()

    @staticmethod
    def _wake_unavailable_reason(*, model_exists: bool, pyaudio_ok: bool) -> str:
        if not model_exists:
            return "wake_model_missing"
        if not pyaudio_ok:
            return "wake_audio_unavailable"
        return "wake_unavailable"

    @staticmethod
    def _resolve_wake_configuration(
        *,
        config: BootstrapConfig,
        settings_obj: object,
        settings_mgr: object,
        model_path: Path | None,
        model_exists: bool,
        pyaudio_ok: bool,
    ) -> None:
        """Resolve wake runtime state and publish structured voice status."""
        from diagnostics.voice_status import make_voice_status, set_voice_status

        voice_mode = str(settings_mgr.get("voice_mode", DEFAULT_VOICE_MODE) or DEFAULT_VOICE_MODE)
        explicit_wake = settings_mgr.get("wake_enabled", None)
        wake_available = model_exists and pyaudio_ok
        missing_dependencies: list[str] = []
        if not model_exists:
            missing_dependencies.append("wake_model")
        if not pyaudio_ok:
            missing_dependencies.append("pyaudio")

        def publish_status(
            *,
            active_voice_mode: str,
            degraded: bool = False,
            reason: str | None = None,
        ) -> None:
            set_voice_status(
                make_voice_status(
                    requested_voice_mode=voice_mode,
                    active_voice_mode=active_voice_mode,
                    wake_enabled=bool(getattr(settings_obj, "wake_enabled", False)),
                    wake_engine=str(getattr(settings_obj, "wake_engine", "none")),
                    degraded=degraded,
                    reason=reason,
                    missing_dependencies=missing_dependencies if degraded else [],
                    details={
                        "model_path": str(model_path) if model_path else None,
                        "model_exists": model_exists,
                        "pyaudio_available": pyaudio_ok,
                    },
                )
            )

        if voice_mode == "wake_word":
            if wake_available:
                settings_obj.wake_enabled = True
                settings_obj.wake_engine = "violawake"
                config.wake = True
                publish_status(active_voice_mode="wake_word")
            else:
                reason = BootstrapFactory._wake_unavailable_reason(
                    model_exists=model_exists,
                    pyaudio_ok=pyaudio_ok,
                )
                settings_obj.wake_enabled = False
                settings_obj.wake_engine = "none"
                config.wake = False
                publish_status(
                    active_voice_mode="push_to_talk",
                    degraded=True,
                    reason=reason,
                )
        elif voice_mode == "disabled":
            settings_obj.wake_enabled = False
            settings_obj.wake_engine = "none"
            config.wake = False
            publish_status(active_voice_mode="disabled")
        else:
            if explicit_wake is not None:
                wake_on = bool(explicit_wake) and wake_available
            else:
                wake_on = False
            settings_obj.wake_enabled = wake_on
            settings_obj.wake_engine = "violawake" if wake_on else "none"
            config.wake = wake_on
            publish_status(
                active_voice_mode="push_to_talk",
                degraded=bool(explicit_wake) and not wake_available,
                reason=(
                    BootstrapFactory._wake_unavailable_reason(
                        model_exists=model_exists,
                        pyaudio_ok=pyaudio_ok,
                    )
                    if bool(explicit_wake) and not wake_available
                    else None
                ),
            )

    @staticmethod
    def _apply_audio_settings_to_app_config(
        *,
        settings_obj: object,
        settings_mgr: object,
    ) -> None:
        """Sync persisted audio/STT settings into AppConfig before voice init."""

        stt_backend = settings_mgr.get("stt_engine", getattr(settings_obj, "stt_backend", "whisper_local"))
        settings_obj.stt_backend = str(stt_backend or "whisper_local")

        for key in ("whisper_model", "whisper_device", "whisper_language"):
            value = settings_mgr.get(key, getattr(settings_obj, key, ""))
            if value not in (None, ""):
                setattr(settings_obj, key, value)

        input_device = settings_mgr.get("input_device", getattr(settings_obj, "input_device", None))
        output_device = settings_mgr.get("output_device", getattr(settings_obj, "output_device", None))
        settings_obj.input_device = str(input_device).strip() or None
        settings_obj.output_device = str(output_device).strip() or None

        # wake_sensitivity and tts_rate are user settings, but the subsystems
        # that consume them read AppConfig, not SettingsManager: the wake
        # policy via WakePolicyConfig.from_app_config (voice/wake_detector/
        # wake/config.py) and the speech engine at construction (voice/
        # synthesis/kokoro_engine.py, voice/synthesis/engine.py). AppConfig is
        # loaded from .env and never refreshed from settings.json, so without
        # this sync the value the user saved — from the Settings UI or by
        # asking Viola — was stored and then ignored for the life of the
        # process. Syncing here, before voice init, is what makes the saved
        # value the one the subsystem is actually built with.
        for numeric_key in ("wake_sensitivity", "tts_rate"):
            value = settings_mgr.get(numeric_key, None)
            if value is None:
                continue
            try:
                current = getattr(settings_obj, numeric_key)
                setattr(settings_obj, numeric_key, type(current)(value))
            except (AttributeError, TypeError, ValueError):
                logger.warning("Ignoring unusable %s setting: %r", numeric_key, value)

    @staticmethod
    def create_state(runtime_profile: _RuntimeProfileType | None = None) -> AppState:
        """
        Create application state.

        Returns:
            AppState instance
        """
        try:
            from backend.app_state import AppState

            profile_name = runtime_profile.name.value if runtime_profile else "desktop_full"
            profile_caps = runtime_profile.capabilities.as_dict() if runtime_profile else {}

            state = AppState(
                start_time=time.time(),
                version="0.1",
                platform=platform.platform(),
                runtime_profile=profile_name,
                runtime_capabilities=profile_caps,
            )
            logger.info("✅ State initialized (platform: %s)", state.platform)
            return state

        except Exception as e:
            logger.error("❌ Failed to create state: %s", e)
            raise

    @staticmethod
    def create_tts(config: object | None = None) -> TTSEngineType | None:
        """Create TTS engine based on configured backend.

        Delegates to ``TTSFactory`` when an ``AppConfig`` is available,
        which provides the canonical backend selection and fallback chain.
        Falls back to direct Kokoro/pyttsx3 creation when no AppConfig
        is present (e.g. in minimal test harnesses).

        Args:
            config: Configuration object (optional, ideally an ``AppConfig``)

        Returns:
            TTS engine instance or None if unavailable
        """
        # Canonical path: delegate to TTSFactory when we have an AppConfig
        try:
            from config.settings import AppConfig
            from voice.synthesis.factory import TTSFactory

            if isinstance(config, AppConfig):
                factory = TTSFactory(config)
                engine = factory.create_primary()
                logger.info("TTS Engine initialized via TTSFactory: %s", type(engine).__name__)
                return cast(TTSEngineType, engine)
        except Exception as e:
            logger.warning("TTSFactory creation failed: %s, trying direct creation", e)

        # Fallback: direct creation (original logic for non-AppConfig callers)
        tts_backend = getattr(config, "tts_backend", "kokoro") if config else "kokoro"

        # Try Kokoro first (default). Shares the ONE process-wide engine so this
        # fallback path cannot reintroduce the second 325 MB model load + second
        # opener-cache build that the TTSFactory path above avoids
        # (voice.synthesis.factory.get_shared_kokoro).
        if tts_backend == "kokoro":
            try:
                from voice.synthesis.factory import get_shared_kokoro

                kokoro = get_shared_kokoro(cast(Any, config))
                if kokoro is not None and kokoro.is_available():
                    logger.info("TTS Engine initialized: Kokoro-82M (shared, lazy-loaded)")
                    return cast(TTSEngineType, kokoro)
                logger.warning("Kokoro model files not found, falling back to pyttsx3")
            except Exception as e:
                logger.warning("Kokoro TTS init failed: %s, falling back to pyttsx3", e)

        # Fallback to pyttsx3 via lazy loader
        try:
            from voice.synthesis.engine import TTSEngine as TTSEngineImpl

            LazyTTSEngineImpl = None
            try:
                from voice.synthesis.lazy_engine import (
                    LazyTTSEngine as _LazyTTSEngineImpl,
                )
            except ImportError:
                LazyTTSEngineImpl = None
            else:
                LazyTTSEngineImpl = _LazyTTSEngineImpl

            if LazyTTSEngineImpl is not None and not _is_mock_symbol(TTSEngineImpl):
                lazy_tts = LazyTTSEngineImpl(config=config)
                logger.info("TTS Engine initialized: pyttsx3 (lazy-loaded)")
                return cast(TTSEngineType, lazy_tts)

            tts = TTSEngineImpl(config=config) if config else TTSEngineImpl()
            logger.info("TTS Engine initialized: pyttsx3 (immediate)")
            return tts

        except Exception as e:
            logger.error("Failed to initialize TTS: %s", e)
            return None

    @staticmethod
    def create_music_player(
        config: object | None = None,
        state: AppState | None = None,
        tts_engine: TTSEngineType | None = None,
        settings: object | None = None,
    ) -> MusicPlayerType | None:
        """
        Create music player.

        Args:
            config: Configuration object
            state: Application state
            tts_engine: TTS engine for announcements
            settings: Settings manager

        Returns:
            Music player instance or None if unavailable
        """
        try:
            from music.player import MusicPlayer

            provider_token_resolver = None
            try:
                from music.consent import get_consent_service

                consent_service = get_consent_service()
                provider_token_resolver = consent_service.resolve_access_token
            except Exception as exc:
                logger.debug("Consent service unavailable: %s", exc)

            player = MusicPlayer(
                config=config,
                state=state,
                tts_engine=tts_engine,
                gpt_handler=None,
                settings=settings,
                provider_token_resolver=provider_token_resolver,
            )

            backend_name = _peek_existing_attr(player, "_backend_name") or "unknown"
            logger.info("✅ Music Player initialized (backend: %s)", backend_name)

            # YouTube anonymous search no longer needs warm-start: BrowserSearchEngine
            # is pure HTTP (no Qt webview to initialize, no in-process Chromium target
            # to spin up). First call is ~one HTTP round trip.

            # Return MusicPlayer directly - no adapter wrapping needed
            return player

        except Exception as e:
            logger.exception("❌ Failed to initialize Music Player: %s", e)
            return None

    @staticmethod
    def create_music_adapter(
        player: MusicPlayerType | None,
    ) -> MusicControllerAdapterType | None:
        """
        Create music controller adapter.

        Args:
            player: Raw music player instance

        Returns:
            Music controller adapter or None
        """
        if not player:
            return None

        try:
            from backend.music_adapter import (
                MusicControllerAdapter as MusicControllerAdapterImpl,
            )

            return MusicControllerAdapterImpl(player)
        except Exception as e:
            logger.error("❌ Failed to create music adapter: %s", e)
            return None

    @staticmethod
    def create_intent_interpreter(
        music: MusicControllerAdapterType | None,
        tts: TTSEngineType | None,
        state: AppState,
    ) -> IntentBridge:
        """
        Create intent interpreter.

        Args:
            music: Music controller
            tts: TTS engine
            state: Application state

        Returns:
            Intent interpreter instance
        """
        try:
            from backend.intent_bridge.bridge import IntentBridge as IntentBridgeImpl
            from core.user_context import get_device_user_id

            intent = IntentBridgeImpl(music=music, tts=tts, state=state, user_id=get_device_user_id())
            logger.info("✅ Intent interpreter initialized")
            return intent

        except Exception as e:
            logger.error("❌ Failed to create intent interpreter: %s", e)
            raise

    @staticmethod
    def setup_audio_ducking(player: MusicPlayerType | None) -> bool:
        """
        Setup audio ducking for voice interaction.

        Args:
            player: Music player instance

        Returns:
            True if successful, False otherwise
        """
        if not player:
            return False

        try:
            from utils.audio_ducking import AudioDucker, set_global_ducker

            ducker = AudioDucker(
                music_player=player,
                duck_level=20,  # Duck to 20% volume during voice interaction
                fade_duration=0.3,  # 300ms smooth fade
            )
            set_global_ducker(ducker)
            logger.info("✅ Audio ducking initialized (music will lower during voice interaction)")
            return True

        except Exception as e:
            logger.warning("⚠️ Audio ducking unavailable: %s", e)
            return False

    @staticmethod
    def create_fastapi_app(
        state: AppState,
        music: MusicControllerAdapterType | None,
        intent: IntentBridge,
    ) -> FastAPI:
        """
        Create FastAPI application.

        Args:
            state: Application state
            music: Music controller
            intent: Intent interpreter

        Returns:
            FastAPI app instance
        """
        try:
            from backend.fastapi_app import build_fastapi_app

            app = cast(FastAPI, build_fastapi_app(state, music, intent))
            logger.info("✅ FastAPI app created")
            return app

        except Exception as e:
            logger.exception("❌ Failed to create FastAPI app: %s", e)
            raise

    @staticmethod
    def create_voice_orchestrator(
        state: AppState,
        intent: IntentBridge,
        player: MusicPlayerType | None,
        tts: TTSEngineType | None,
        wake_enabled: bool = False,
        event_loop: AbstractEventLoop | None = None,
    ) -> VoiceOrchestrator | None:
        """
        Create voice orchestrator (optional).

        Args:
            state: Application state
            intent: Intent interpreter
            music: Music controller
            tts: TTS engine
            wake_enabled: Enable wake word detection
            event_loop: Event loop for async callback handling (optional)

        Returns:
            Voice orchestrator or None if disabled/failed
        """
        try:
            from core.voice_orchestrator import VoiceOrchestrator
            from models.state_manager import ConsolidatedState
            from utils.async_helper import get_or_create_event_loop

            # Get or create event loop if not provided
            if event_loop is None:
                event_loop = get_or_create_event_loop()
                logger.debug("Event loop obtained/created for voice orchestrator")

            voice_orch = VoiceOrchestrator(
                cast(ConsolidatedState, state),
                intent,
                cast(MusicPlayerType, player) if player is not None else None,
                tts,
                wake_enabled=wake_enabled,
                event_loop=event_loop,
            )
            logger.info("✅ Voice orchestrator initialized (wake=%s)", wake_enabled)
            return voice_orch

        except Exception as e:
            logger.exception("❌ Failed to initialize voice: %s", e)
            logger.warning("⚠️ Voice features disabled due to initialization error")
            return None

    @staticmethod
    def create_spoke_voice_handler(
        config: AppConfig,
        voice_pipeline: VoicePipeline,
        on_voice_command: Callable[[object], None] | None = None,
    ) -> object | None:
        """
        Create Spoke voice handler with wake-word integration and cancellation protocol.

        This integrates the wake-word pipeline into Spokes with proper cancellation
        when wake word fires. Uses existing VoicePipeline - no new magic.

        Args:
            config: Application configuration
            voice_pipeline: Existing VoicePipeline instance
            on_voice_command: Optional callback when voice command audio is captured
                             (for sending to Hub or local processing)

        Returns:
            SpokeVoiceHandler instance or None if failed
        """
        try:
            from voice.spoke_handler import SpokeVoiceHandler

            handler: object = SpokeVoiceHandler(
                config=config,
                voice_pipeline=voice_pipeline,
                on_voice_command=on_voice_command,
            )
            logger.info("✅ Spoke voice handler initialized with wake-word pipeline")
            return handler
        except ImportError:
            logger.debug("SpokeVoiceHandler not available")
            return None
        except Exception as e:
            logger.exception("❌ Failed to create Spoke voice handler: %s", e)
            return None

    @classmethod
    def bootstrap(cls, config: BootstrapConfig) -> BootstrapResult:
        """
        Full bootstrap process.

        Args:
            config: Bootstrap configuration

        Returns:
            BootstrapResult with all initialized components

        Raises:
            Exception if critical components fail
        """
        try:
            logger.info("🔧 Bootstrapping Viola subsystems...")

            # Get settings
            try:
                from config import settings
            except ImportError:
                settings = None

            runtime_profile: _RuntimeProfileType | None = None
            if detect_runtime_profile:
                try:
                    runtime_profile = detect_runtime_profile()
                except Exception as profile_err:  # pragma: no cover - defensive logging
                    logger.debug("Runtime profile detection failed: %s", profile_err)
                    runtime_profile = None

            if settings and runtime_profile:
                apply_profile_to_settings(settings, runtime_profile)
                logger.debug(
                    "Runtime profile %s metadata: %s",
                    runtime_profile.name.value,
                    runtime_profile.metadata,
                )

            # Respect runtime profile capability hints for voice/wake
            if runtime_profile:
                capabilities = runtime_profile.capabilities
                if not capabilities.enable_voice and config.voice:
                    logger.warning(
                        "Runtime profile %s disables voice, overriding CLI flag",
                        runtime_profile.name.value,
                    )
                    config.voice = False
                if not capabilities.enable_wake_word and config.wake:
                    logger.info(
                        "Runtime profile %s disables wake word; forcing wake flag off",
                        runtime_profile.name.value,
                    )
                    config.wake = False

            with subsystem_timer("voice_dependencies"):
                cls._ensure_voice_dependencies(config)

            # =================================================================
            # HARD-ENFORCED WAKE CONFIG - SINGLE SOURCE OF TRUTH
            # =================================================================
            # This is the ONLY place wake config is resolved.
            # All other code reads from settings.wake_enabled / settings.wake_engine.
            # NO resolver, NO cache, NO fallbacks elsewhere.
            # =================================================================
            from config.wake_config import (
                check_pyaudio_available,
                get_violawake_model_path,
            )
            from ui.settings_manager import get_settings_manager

            if not settings:
                logger.error("❌ AppConfig settings import failed; cannot enforce wake invariant")
                # Fall back to conservative defaults
                config.wake = False
            else:
                settings_mgr = get_settings_manager()
                cls._apply_audio_settings_to_app_config(
                    settings_obj=settings,
                    settings_mgr=settings_mgr,
                )
                voice_mode = settings_mgr.get("voice_mode", DEFAULT_VOICE_MODE)
                # Prefer the persisted active custom model when one is enabled.
                active_model_path = str(
                    settings_mgr.get("wake_word_active_model", "") or settings_mgr.get("wake_word_model", "") or ""
                ).strip()
                active_candidate = Path(active_model_path).expanduser() if active_model_path else None
                if active_candidate and active_candidate.is_file():
                    model_path = active_candidate
                else:
                    model_path = get_violawake_model_path()
                model_exists = bool(model_path) and model_path.exists()
                pyaudio_ok = check_pyaudio_available()
                wake_available = model_exists and pyaudio_ok

                if not model_exists:
                    logger.warning("⚠️ ViolaWake model not found at %s", model_path)
                if not pyaudio_ok:
                    logger.warning("⚠️ PyAudio not available")

                # HARD ENFORCEMENT: voice_mode governs wake_enabled and wake_engine
                # Note: wake_engine is Literal["violawake", "none"] - these string assignments are safe
                if voice_mode == "wake_word":
                    if wake_available:
                        settings.wake_enabled = True
                        settings.wake_engine = "violawake"
                        config.wake = True
                    else:
                        # Can't fulfill wake_word mode - log error but downgrade
                        logger.error(
                            "❌ voice_mode=wake_word but wake unavailable! "
                            "Model exists=%s, PyAudio=%s. Downgrading to push_to_talk.",
                            model_exists,
                            pyaudio_ok,
                        )
                        settings.wake_enabled = False
                        settings.wake_engine = "none"
                        config.wake = False
                cls._resolve_wake_configuration(
                    config=config,
                    settings_obj=settings,
                    settings_mgr=settings_mgr,
                    model_path=model_path,
                    model_exists=model_exists,
                    pyaudio_ok=pyaudio_ok,
                )

                # Set model path if available
                if model_exists:
                    settings.wake_keyword_path = str(model_path)

                logger.info(
                    "WAKE RESOLVED FINAL: voice_mode=%s wake_enabled=%s wake_engine=%s",
                    voice_mode,
                    settings.wake_enabled,
                    settings.wake_engine,
                )

            # IMPROVEMENT #1: Auto-detect Raspberry Pi and enable lightweight mode
            if settings and is_raspberry_pi() and not getattr(settings, "lightweight_mode", False):
                logger.info("🍓 Raspberry Pi auto-detected - enabling lightweight mode")
                settings.lightweight_mode = True

            # Apply Raspberry Pi optimizations if lightweight mode enabled
            if settings and getattr(settings, "lightweight_mode", False):
                logger.info("🍓 Lightweight mode detected - applying Pi optimizations...")
                apply_lightweight_optimizations(settings)

                # Auto-detect Raspberry Pi and log resource profile
                if is_raspberry_pi():
                    logger.info("🍓 Raspberry Pi detected!")
                    log_resource_profile("on Raspberry Pi")

            # Initialize components in order
            if settings and config.voice_disabled_reason:
                try:
                    settings.wake_enabled = False
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Unable to disable wake flag in settings: %s", exc)

            with subsystem_timer("app_state"):
                state = cls.create_state(runtime_profile)
            _boot_checkpoint("bootstrap-0-app-state-created")

            # Validate audio devices early (logs details, never crashes).
            #
            # #1500: "never crashes" was violated on the self-hosted Linux
            # AppImage build runner -- validate_audio_devices() itself has a
            # correct `except Exception: logger.exception(...)` guard, but
            # that is powerless against a native SIGSEGV (rc=139, zero
            # traceback even from PYTHONFAULTHANDLER) inside PortAudio's own
            # C-level device enumeration on a host with literally no audio
            # backend (no real hardware, no dummy ALSA device, no PipeWire --
            # confirmed via the surrounding ALSA/JACK connection failures in
            # boot.log). A dummy `~/.asoundrc` null pcm/ctl default (landed
            # one layer earlier) did not fully prevent it: PortAudio's
            # enumeration walks the SYSTEM alsa.conf's card-alias table
            # (cards.pcm.front/rear/hdmi/surround*/etc), not just the
            # "default" device a per-user override controls. A SIGSEGV
            # cannot be caught by ANY Python try/except, so the only durable
            # fix is not calling into PortAudio at all in an environment that
            # has proven itself unable to survive it -- exactly the
            # VIOLA_DISABLE_UPDATE_CHECK pattern already used by this same
            # bootstrap sequence for the equivalent "known-inapplicable in
            # this environment" case.
            #
            # UPDATE: after landing this skip, the same rc=139 segfault
            # signature persisted on the next run even with the skip
            # confirmed active (boot.log showed "Audio device validation
            # skipped" immediately followed by "Segmentation fault", nothing
            # in between) -- so PortAudio device enumeration was not the
            # only native crash site, or was never it at all (a
            # viola_boot_crash.log staleness bug on this persistent runner,
            # fixed alongside this comment, had been showing an unrelated
            # already-fixed SystemExit crash from a PRIOR run and briefly
            # masked that this run's real failure was purely the segfault).
            # The _boot_checkpoint calls below narrow the ~65-line span
            # between here and the FastAPI app existing to pinpoint exactly
            # which subsystem construction is the actual native crash site.
            import os as _os

            from audio_core.device_validation import AudioDeviceStatus

            if _os.environ.get("VIOLA_SKIP_AUDIO_DEVICE_VALIDATION") == "1":
                logger.info("Audio device validation skipped (VIOLA_SKIP_AUDIO_DEVICE_VALIDATION=1)")
                _audio_status = AudioDeviceStatus(
                    input_ok=True,
                    output_ok=True,
                    input_name="(validation skipped - VIOLA_SKIP_AUDIO_DEVICE_VALIDATION=1)",
                    output_name="(validation skipped - VIOLA_SKIP_AUDIO_DEVICE_VALIDATION=1)",
                    input_fallback=False,
                    output_fallback=False,
                )
            else:
                with subsystem_timer("audio_device_validation"):
                    from audio_core.device_validation import validate_audio_devices

                    _audio_status = validate_audio_devices()
            _boot_checkpoint("bootstrap-A-audio-device-status-resolved")
            if _audio_status.input_fallback or _audio_status.output_fallback:
                logger.warning("Audio device fallback occurred during startup validation")
            if not _audio_status.input_ok or not _audio_status.output_ok:
                logger.error(
                    "Audio device problem: input_ok=%s output_ok=%s — non-voice features will still work",
                    _audio_status.input_ok,
                    _audio_status.output_ok,
                )
                # That log line used to be the entire reaction. A clean-VM run
                # hit this three times and the user was told nothing: Viola
                # came up looking healthy while voice was, in fact, deaf or
                # mute. Record it where a client can read it once the UI is up
                # (nothing is connected yet at boot, so a broadcast now would
                # reach nobody), and queue the on-screen notice for delivery.
                try:
                    state.runtime_capabilities["audio_input_ok"] = bool(_audio_status.input_ok)
                    state.runtime_capabilities["audio_output_ok"] = bool(_audio_status.output_ok)
                except _NOTICE_ERRORS as exc:  # pragma: no cover - defensive
                    logger.debug("Failed to record audio device status in runtime_capabilities: %s", exc)
                if not _audio_status.output_ok:
                    _queue_audio_device_notice("audio_output_unavailable", state)
                if not _audio_status.input_ok:
                    _queue_audio_device_notice("audio_input_unavailable", state)

            if config.voice_disabled_reason:
                dependencies_note = ", ".join(config.voice_missing_dependencies) or "unknown"
                try:
                    state.append_breadcrumb(f"Voice input disabled: missing dependencies ({dependencies_note}).")
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Failed to record voice-disabled breadcrumb: %s", exc)
                try:
                    state.runtime_capabilities["voice_enabled"] = False
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "Failed to set voice_enabled=False in runtime_capabilities: %s",
                        exc,
                    )
            else:
                try:
                    state.runtime_capabilities["voice_enabled"] = True
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "Failed to set voice_enabled=True in runtime_capabilities: %s",
                        exc,
                    )
            _boot_checkpoint("bootstrap-B-voice-capability-flag-set")
            # Defer TTS creation — imports TTS SDK chain (~2.5s).
            # Nobody speaks before the UI loads. LazyTTSEngine materializes
            # the real engine on first attribute access (speak, is_available).
            from bootstrap.lazy_tts import LazyTTSEngine

            tts = LazyTTSEngine(lambda: cls.create_tts(config=settings))
            _boot_checkpoint("bootstrap-C-lazy-tts-wrapped")
            from bootstrap.lazy_music import LazyMusicControllerAdapter

            def _create_music_after_bind() -> MusicControllerAdapterType | None:
                player = cls.create_music_player(
                    config=settings,
                    state=state,
                    tts_engine=tts,
                    settings=settings,
                )
                adapter = cls.create_music_adapter(player)
                if player:
                    cls.setup_audio_ducking(player)
                return adapter

            music = LazyMusicControllerAdapter(_create_music_after_bind)
            _boot_checkpoint("bootstrap-D-lazy-music-wrapped")
            # Defer IntentBridge creation — imports LLM SDK chain (~2-3s)
            # and constructor does provider init (~1.6s). Nobody issues a
            # voice command before the UI loads. LazyIntentBridge materializes
            # the real IntentBridge on first attribute access.
            from bootstrap.lazy_intent import LazyIntentBridge

            intent = LazyIntentBridge(lambda: cls.create_intent_interpreter(music, tts, state))
            _boot_checkpoint("bootstrap-E-lazy-intent-wrapped")

            # Create FastAPI app (voice orchestrator deferred to post-startup)
            with subsystem_timer("fastapi_app"):
                app = cls.create_fastapi_app(state, music, intent)
            _boot_checkpoint("bootstrap-F-fastapi-app-created")
            voice_orch = None  # Created post-startup in startup_controller
            register_post_bind_initializer(app, "music", music.materialize)
            register_post_bind_initializer(app, "llm", intent.materialize)

            # ----------------------------------------------------------
            # Startup health check: verify critical subsystems
            # ----------------------------------------------------------
            degraded: list[str] = []
            if tts is None:
                degraded.append("tts")
                logger.warning("⚠️ TTS engine unavailable — voice responses disabled")
            # Voice orchestrator is now deferred to post-startup — not a degradation
            # if config.voice and voice_orch is None:
            #     degraded.append("voice")

            if degraded:
                logger.warning(
                    "Startup health: DEGRADED — unavailable subsystems: %s",
                    ", ".join(degraded),
                )
            else:
                logger.info("Startup health: ALL CRITICAL SUBSYSTEMS OK")

            logger.info("🎉 Bootstrap complete!")

            # Wire timer expiration notifications — speak via TTS and
            # broadcast via messaging hub when timers complete.
            try:
                from services.timer_service import TimerService

                _tts_ref = tts  # capture for closure

                def _on_timer_completed(user_id: str, timer_id: str, label: str) -> None:
                    """Notify the user when a timer expires."""
                    notification = f"Your {label} is done!" if label else "Your timer is done!"
                    logger.info("Timer notification: %s (user=%s)", notification, user_id)

                    # 1) Speak via TTS (synchronous — called from timer check thread)
                    try:
                        from core.user_context import user_scope

                        engine = _tts_ref
                        with user_scope(user_id):
                            # LazyTTSEngine wraps the real engine; try speak_sync first
                            speak_sync = getattr(engine, "speak_sync", None)
                            if callable(speak_sync):
                                speak_sync(notification)
                            else:
                                # Fall back to async speak via event loop
                                import asyncio as _aio

                                speak = getattr(engine, "speak", None)
                                if callable(speak):
                                    try:
                                        loop = _aio.get_running_loop()
                                    except RuntimeError:
                                        loop = None
                                    if loop and loop.is_running():
                                        _aio.run_coroutine_threadsafe(speak(notification), loop)
                                    else:
                                        _aio.run(speak(notification))
                    except Exception as tts_err:
                        logger.warning("Timer TTS notification failed: %s", tts_err)

                    # 2) Send through the user-scoped notification service if available.
                    #    user_requested: the user set this timer for this moment, so it
                    #    is delivered now rather than deferred (#4790). Before, quiet
                    #    hours parked it in a queue nothing drained, which is one of the
                    #    three silent-expiry paths #1404 documented; with the queue now
                    #    actually draining, deferring it instead would surface "your
                    #    timer is done" as a stale toast whenever quiet hours ended.
                    try:
                        import asyncio as _aio

                        from services.notifications.push_service import get_push_service

                        push_service = get_push_service()
                        if push_service is not None:
                            try:
                                loop = _aio.get_running_loop()
                            except RuntimeError:
                                loop = None
                            if loop and loop.is_running():
                                _aio.run_coroutine_threadsafe(
                                    push_service.send(
                                        notification,
                                        user_id=user_id,
                                        metadata={"timer_id": timer_id},
                                        user_requested=True,
                                    ),
                                    loop,
                                )
                            else:
                                _aio.run(
                                    push_service.send(
                                        notification,
                                        user_id=user_id,
                                        metadata={"timer_id": timer_id},
                                        user_requested=True,
                                    )
                                )
                    except Exception as msg_err:
                        logger.debug("Timer messaging notification skipped: %s", msg_err)

                timer_service = TimerService.get_instance()
                timer_service.on_completion(_on_timer_completed)

                # In-app surface (#1404): push timer lifecycle to the desktop
                # webview over the EventHub — a live countdown while timers run
                # and an on-screen notification at expiry. Without this the only
                # signal was TTS + a user-scoped push that the desktop path
                # suppresses (Windows-toast skip / no web-push subscription /
                # quiet-hours queue), so a timer could expire with nothing shown.
                from services.notifications.timer_notifier import TimerNotifier

                timer_service.add_listener(TimerNotifier())
                logger.info("Timer expiration notifications wired (TTS + messaging + in-app EventHub)")
            except Exception as timer_wire_err:
                logger.warning("Could not wire timer notifications: %s", timer_wire_err)

            adaptive_manager = None
            snapshotter = None
            tel_scheduler = None
            health_checker = None
            wake_data_services = ()

            def _init_local_library() -> None:
                local_folder = None
                try:
                    from ui.settings_manager import get_settings_manager

                    configured_folder = get_settings_manager().get("local_music_folder")
                    local_folder = str(configured_folder).strip() if configured_folder else None
                except Exception as sm_err:
                    logger.debug(
                        "Could not read local_music_folder from SettingsManager: %s",
                        sm_err,
                    )
                if not local_folder:
                    logger.warning("%s", _NO_LOCAL_MUSIC_FOLDER_MESSAGE)
                    _broadcast_startup_warning(_NO_LOCAL_MUSIC_FOLDER_MESSAGE)
                    return
                synced_provider = _synced_music_folder_name(local_folder)
                if synced_provider:
                    warning = (
                        "Music folder is in %s. Files may auto-download to your system drive on access. "
                        "Consider moving to a non-synced folder for stable playback."
                    ) % synced_provider
                    logger.warning("%s", warning)
                    _broadcast_startup_warning(warning)
                if not Path(local_folder).is_dir():
                    logger.debug("Local music folder not found: %s", local_folder)
                    return
                from music.providers.local.provider import LocalMusicProvider

                local_provider = LocalMusicProvider()
                local_provider.initialize(local_folder)
                logger.info("Local library scan started: %s", local_folder)

            def _init_adaptive_manager() -> None:
                if not settings or not getattr(settings, "lightweight_mode", False):
                    return
                from performance.adaptive_resource_manager import (
                    AdaptiveResourceManager,
                )

                manager = AdaptiveResourceManager(config=settings)
                app.state.adaptive_manager = manager
                try:
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(manager.start_monitoring())
                    finally:
                        loop.close()
                except Exception as exc:
                    logger.debug("Could not start adaptive monitoring: %s", exc)

            def _init_state_snapshotter() -> None:
                from diagnostics.state_snapshot import PersistentSnapshotter

                created = PersistentSnapshotter()
                created.start(music=music, voice=voice_orch)
                app.state.state_snapshotter = created

            def _init_telemetry_scheduler() -> None:
                app_surface = getattr(settings, "app_surface", "desktop") if settings else "desktop"
                if app_surface != "desktop":
                    return
                from telemetry.scheduler import TelemetryScheduler

                from telemetry import get_accumulator

                interval = getattr(settings, "telemetry_send_interval_hours", 4) if settings else 4
                scheduler = TelemetryScheduler(get_accumulator(), interval_hours=interval)
                scheduler.start()
                app.state.telemetry_scheduler = scheduler

            def _init_admin_health_checker() -> None:
                from admin.health_checker import HealthChecker as AdminHealthChecker

                checker = AdminHealthChecker()
                checker.start()
                app.state.health_checker = checker

            def _init_wake_data_services() -> None:
                if not settings or not getattr(settings, "wake_data_collection_enabled", False):
                    return
                from voice.wake_detector.data_collection.classifier import (
                    TriggerClassifier,
                )
                from voice.wake_detector.data_collection.storage_manager import (
                    StorageManager,
                )
                from voice.wake_detector.data_collection.uploader import UploadQueue

                storage_mgr = StorageManager()
                upload_queue = UploadQueue()
                trigger_classifier = TriggerClassifier()
                storage_mgr.start()
                upload_queue.start()
                trigger_classifier.start()
                app.state.wake_data_services = (
                    storage_mgr,
                    upload_queue,
                    trigger_classifier,
                )
                logger.info("Wake data collection services started")

            register_post_bind_initializer(app, "local_library", _init_local_library)
            register_post_bind_initializer(app, "adaptive_manager", _init_adaptive_manager)
            register_post_bind_initializer(app, "state_snapshotter", _init_state_snapshotter)
            register_post_bind_initializer(app, "telemetry_scheduler", _init_telemetry_scheduler)
            register_post_bind_initializer(app, "admin_health_checker", _init_admin_health_checker)
            register_post_bind_initializer(app, "wake_data_services", _init_wake_data_services)

            # F-009: initialize Claude-compatible session/control-plane state
            # separate from Viola service bootstrap. This gives hook/slash/
            # bootstrap callers a stable in-memory analogue of Claude's
            # ``bootstrap/state.ts`` SessionState — model lineage, bypass-
            # permission scope, scheduled-task records, plugin-hook clearing,
            # session-only flags. The service bootstrap above remains the
            # source of truth for persistent stores.
            try:
                from bootstrap.session_state import reset_session_state

                materialized_intent = _peek_existing_attr(intent, "_real")
                reset_session_state(
                    user_id=(getattr(materialized_intent, "user_id", None) if materialized_intent is not None else None)
                )
            except (
                ImportError,
                RuntimeError,
            ) as session_state_err:  # pragma: no cover - defensive
                logger.debug("session state init failed: %s", session_state_err)

            result = BootstrapResult(
                state=state,
                music=music,
                tts=tts,
                intent=intent,
                app=app,
                voice=voice_orch,
                adaptive_manager=adaptive_manager,
                runtime_profile=runtime_profile,
                state_snapshotter=snapshotter,
                telemetry_scheduler=tel_scheduler,
                health_checker=health_checker,
                wake_data_services=wake_data_services,
                voice_disabled_reason=config.voice_disabled_reason,
                voice_missing_dependencies=config.voice_missing_dependencies,
                degraded_components=tuple(degraded),
            )
            app.state.bootstrap_result = result
            return result

        except Exception as e:
            logger.exception("❌ Bootstrap failed: %s", e)
            raise


# Convenience function
def bootstrap_viola(
    host: str = LOCALHOST,
    port: int = DEFAULT_API_PORT,
    voice: bool = False,
    wake: bool = False,
    desktop_mode: bool = False,
) -> BootstrapResult:
    """
    Convenience function for bootstrapping Viola.

    Args:
        host: API server host
        port: API server port
        voice: Enable voice features
        wake: Enable wake word
        desktop_mode: Running in desktop mode

    Returns:
        BootstrapResult with all components
    """
    config = BootstrapConfig(
        host=host,
        port=port,
        voice=voice,
        wake=wake,
        desktop_mode=desktop_mode,
    )
    return BootstrapFactory.bootstrap(config)


# =============================================================================
# Cleanup Helpers (consolidated from core/bootstrap_factory.py)
# =============================================================================


class _VoiceController(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class _MusicBackendCleanup(Protocol):
    def cleanup(self) -> None: ...


class _MusicBackendStop(Protocol):
    def stop(self) -> None: ...


class _HasStopWorker(Protocol):
    _stop_worker: bool


class _HasCondition(Protocol):
    _cv: threading.Condition


class _HasWorkerThreads(Protocol):
    _worker: threading.Thread | None
    _autoplay_monitor: threading.Thread | None


class _HasBackend(Protocol):
    _backend: _MusicBackendCleanup | _MusicBackendStop | None


class _MusicService(Protocol):
    player: MusicPlayerType | None


class _AppStateController(Protocol):
    def mark_ready(self) -> None: ...


class _DeviceDiscoveryService(Protocol):
    stop: Callable[..., object] | None
    shutdown: Callable[..., object] | None


class _HasAppState(Protocol):
    state: object


class _HasDeviceDiscovery(Protocol):
    device_discovery: _DeviceDiscoveryService | None


class _HasDeviceDiscoveryStopEvent(Protocol):
    device_discovery_stop_event: threading.Event | None


def _await_coroutine_safe(
    coro: object | None,
    description: str,
    timeout: float,
    target_loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """
    Safely await a coroutine during cleanup, handling various event loop states.

    This helper handles:
    - Running event loops (schedule task or run_coroutine_threadsafe)
    - No event loop (create temporary one)
    - Closed event loops (create new one)
    """
    import concurrent.futures

    if coro is None:
        return
    if not asyncio.iscoroutine(coro):
        return

    running_loop = None
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if target_loop is None:
        target_loop = running_loop
        if target_loop is None:
            try:
                target_loop = asyncio.get_event_loop()
            except RuntimeError:
                target_loop = None

    # Case 1: Target loop is running
    if target_loop and target_loop.is_running():
        if running_loop and running_loop is target_loop:
            logger.debug("%s coroutine scheduled on running event loop", description)
            target_loop.create_task(asyncio.wait_for(coro, timeout=timeout))
            return

        future = asyncio.run_coroutine_threadsafe(asyncio.wait_for(coro, timeout=timeout), target_loop)
        try:
            future.result(timeout)
            logger.debug("%s coroutine completed on running loop", description)
        except concurrent.futures.TimeoutError:
            logger.warning("%s coroutine timed out", description)
        except (RuntimeError, asyncio.CancelledError) as exc:
            logger.debug("Coroutine %s cancelled: %s", description, exc)
        except Exception as exc:
            logger.warning("Error awaiting %s: %s", description, exc)
        return

    # Case 2: No target loop - create temporary one
    if not target_loop:
        target_loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(target_loop)
            target_loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
            logger.debug("%s coroutine completed on new event loop", description)
        except (TimeoutError, asyncio.CancelledError, RuntimeError) as exc:
            logger.debug("Coroutine %s timed out: %s", description, exc)
        except Exception as exc:
            logger.warning("Error awaiting %s: %s", description, exc)
        finally:
            asyncio.set_event_loop(None)
            target_loop.close()
        return

    # Case 3: Target loop exists but not running
    try:
        target_loop.run_until_complete(asyncio.wait_for(coro, timeout=timeout))
        logger.debug("%s coroutine completed", description)
    except (TimeoutError, asyncio.CancelledError, RuntimeError) as exc:
        logger.debug("Coroutine %s timed out: %s", description, exc)
    except Exception as exc:
        logger.warning("Error awaiting %s: %s", description, exc)


def _cleanup_voice(voice: _VoiceController | None, timeout: float) -> None:
    """Clean up voice orchestrator."""
    if voice is None:
        return
    try:
        logger.info("Stopping voice orchestrator...")
        voice.stop()
        logger.info("Voice orchestrator stopped")
    except (RuntimeError, OSError) as e:
        logger.warning("Error stopping voice: %s", e)
    except Exception as e:
        logger.warning("Unexpected error stopping voice: %s", e)


def _cleanup_music_player(music: _MusicService | None, timeout: float) -> None:
    """Clean up music player and its threads."""
    if music is None or music.player is None:
        return

    try:
        logger.info("Stopping music player...")
        player = music.player

        # Stop playback
        try:
            if hasattr(player, "stop"):
                player.stop()
        except (RuntimeError, OSError) as e:
            logger.debug("Error stopping playback: %s", e)
        except Exception as e:
            logger.warning("Unexpected error stopping playback: %s", e)

        # Set worker stop flag
        if hasattr(player, "_stop_worker"):
            cast(_HasStopWorker, player)._stop_worker = True

        # Notify condition variable
        if hasattr(player, "_cv"):
            try:
                cv = cast(_HasCondition, player)._cv
                with cv:
                    cv.notify_all()
            except (RuntimeError, threading.ThreadError) as e:
                logger.debug("Error notifying threads: %s", e)

        # Wait for worker threads
        start_time = time.time()
        worker_threads = cast(_HasWorkerThreads, player) if hasattr(player, "_worker") else None
        worker = worker_threads._worker if worker_threads is not None else None
        autoplay = worker_threads._autoplay_monitor if worker_threads is not None else None
        _wait_for_thread(worker, name="_worker", timeout=timeout, start_time=start_time)
        _wait_for_thread(autoplay, name="_autoplay_monitor", timeout=timeout, start_time=start_time)

        # Cleanup backend
        if hasattr(player, "_backend"):
            _cleanup_player_backend(cast(_HasBackend, player))

        logger.info("Music player stopped")
    except (RuntimeError, OSError, AttributeError) as e:
        logger.debug("Error stopping music player: %s", e)
    except Exception as e:
        logger.warning("Unexpected error stopping music player: %s", e)


def _wait_for_thread(thread: threading.Thread | None, *, name: str, timeout: float, start_time: float) -> None:
    """Wait for a thread to stop with remaining timeout."""
    if thread is None:
        return

    remaining = timeout - (time.time() - start_time)
    if remaining <= 0:
        return
    if not thread.is_alive():
        return

    logger.debug("Waiting for %s thread (timeout: %.1fs)...", name, remaining)
    thread.join(timeout=remaining)
    if thread.is_alive():
        logger.warning("%s thread did not stop in time", name)
    else:
        logger.debug("%s thread stopped", name)


def _cleanup_player_backend(player: _HasBackend) -> None:
    """Cleanup the player backend (VLC/ffplay processes)."""
    import subprocess

    backend = player._backend
    if backend is None:
        return

    try:
        if hasattr(backend, "cleanup"):
            cast(_MusicBackendCleanup, backend).cleanup()
        elif hasattr(backend, "stop"):
            cast(_MusicBackendStop, backend).stop()
        logger.debug("Backend cleaned up")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        logger.debug("Error cleaning up backend: %s", e)
    except Exception as e:
        logger.warning("Unexpected error cleaning up backend: %s", e)


def _cleanup_uvicorn_server(uvicorn_server: object | None, timeout: float) -> None:
    """Clean up web server wrapper."""
    if uvicorn_server is None:
        return

    try:
        logger.info("Stopping Uvicorn server...")
        if hasattr(uvicorn_server, "stop"):
            uvicorn_server.stop(timeout=timeout)
        logger.info("Uvicorn server stopped")
    except (RuntimeError, OSError, AttributeError, asyncio.CancelledError) as e:
        logger.debug("Error stopping Uvicorn: %s", e)
    except Exception as e:
        logger.warning("Unexpected error stopping Uvicorn: %s", e)


def _cleanup_device_discovery(app: _HasAppState | None, timeout: float) -> None:
    """Clean up device discovery service."""
    if app is None:
        return
    app_state = getattr(app, "state", None)
    if app_state is None:
        return

    if not hasattr(app_state, "device_discovery"):
        return

    discovery_state = cast(_HasDeviceDiscovery, app_state)
    discovery = discovery_state.device_discovery
    if discovery is not None:
        try:
            logger.info("Stopping device discovery service...")
            stop_result: object | None = None

            if discovery.stop is not None:
                stop_result = discovery.stop()
            elif discovery.shutdown is not None:
                stop_result = discovery.shutdown()

            if stop_result is not None and asyncio.iscoroutine(stop_result):
                _await_coroutine_safe(stop_result, "Device discovery shutdown", timeout)

            logger.info("Device discovery stopped")
        except (RuntimeError, OSError, AttributeError, asyncio.CancelledError) as e:
            logger.debug("Error stopping device discovery: %s", e)
        except Exception as e:
            logger.warning("Unexpected error stopping device discovery: %s", e)
        finally:
            discovery_state.device_discovery = None

    if not hasattr(app_state, "device_discovery_stop_event"):
        return
    stop_state = cast(_HasDeviceDiscoveryStopEvent, app_state)
    stop_event = stop_state.device_discovery_stop_event
    if stop_event is not None:
        try:
            stop_event.wait(timeout=timeout)
        except (RuntimeError, threading.ThreadError):
            logger.debug("Device discovery stop_event.wait() failed")
        finally:
            stop_state.device_discovery_stop_event = None


def _cleanup_asyncio_tasks(timeout: float) -> None:
    """Cancel pending asyncio tasks and stop event loop."""
    loop: asyncio.AbstractEventLoop | None = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return

    if loop is None or loop.is_closed():
        return

    try:
        pending_tasks = [task for task in asyncio.all_tasks(loop) if not task.done()]
    except RuntimeError:
        pending_tasks = []

    if pending_tasks:
        logger.info("Cancelling %d pending asyncio task(s)...", len(pending_tasks))
        for task in pending_tasks:
            task.cancel()

        async def _wait_for_pending() -> None:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        _await_coroutine_safe(_wait_for_pending(), "Asyncio tasks cleanup", timeout, target_loop=loop)
        logger.info("All asyncio tasks cancelled.")

    if loop.is_running():
        logger.info("Requesting asyncio event loop stop...")
        loop.call_soon_threadsafe(loop.stop)
    else:
        try:
            loop.stop()
        except Exception as e:
            logger.debug("Loop stop failed: %s", e)


def _cleanup_tts(tts: object | None) -> None:
    """Stop TTS engine and release model resources."""
    if tts is None:
        return
    stop_fn = getattr(tts, "stop", None)
    if stop_fn is None or not callable(stop_fn):
        return
    try:
        logger.info("Stopping TTS engine...")
        stop_fn()
        logger.info("TTS engine stopped")
    except Exception as e:
        logger.warning("Error stopping TTS engine: %s", e)


def _cleanup_telemetry_scheduler(scheduler: object | None) -> None:
    """Stop the telemetry scheduler with a final send attempt."""
    if scheduler is None:
        return
    stop_fn = getattr(scheduler, "stop", None)
    if stop_fn is None or not callable(stop_fn):
        return
    try:
        logger.info("Stopping telemetry scheduler...")
        stop_fn(final_send=True)
        logger.info("Telemetry scheduler stopped")
    except Exception as e:
        logger.warning("Error stopping telemetry scheduler: %s", e)


def _cleanup_health_checker(checker: object | None) -> None:
    """Stop the admin health checker daemon thread."""
    if checker is None:
        return
    stop_fn = getattr(checker, "stop", None)
    if stop_fn is None or not callable(stop_fn):
        return
    try:
        logger.info("Stopping admin health checker...")
        stop_fn()
        logger.info("Admin health checker stopped")
    except Exception as e:
        logger.warning("Error stopping admin health checker: %s", e)


def _cleanup_wake_data_services(services: tuple[object, ...]) -> None:
    """Stop all wake data collection background services."""
    if not services:
        return
    for svc in services:
        stop_fn = getattr(svc, "stop", None)
        if stop_fn is None or not callable(stop_fn):
            continue
        try:
            stop_fn()
        except Exception as e:
            logger.warning("Error stopping wake data service %s: %s", type(svc).__name__, e)
    logger.info("Wake data collection services stopped")


def _cleanup_background_resolver() -> None:
    """Shutdown background resolver thread pool."""
    try:
        from utils.background_resolver import shutdown_background_resolver
    except ImportError:
        logger.debug("Background resolver module not available (non-critical)")
        return
    except Exception as e:
        logger.debug("Failed to import background resolver (non-critical): %s", e)
        return

    try:
        shutdown_background_resolver()
        logger.info("Background resolver stopped")
    except Exception as exc:
        logger.warning("Error stopping background resolver: %s", exc)


# =============================================================================
# Bootstrap Dataclass (legacy-compatible interface)
# =============================================================================

from core.constants import TIMEOUT_MEDIUM


@dataclass
class Bootstrap:
    """
    Bootstrap result container with cleanup support.

    This provides a legacy-compatible interface that wraps BootstrapResult
    with cleanup helpers for graceful shutdown.
    """

    state: _AppStateController | None
    music: _MusicService | None
    tts: object | None
    intent: object | None
    app: _HasAppState | None
    voice: _VoiceController | None
    uvicorn_server: object | None = None
    runtime_profile: object | None = None
    telemetry_scheduler: object | None = None  # telemetry.scheduler.TelemetryScheduler
    health_checker: object | None = None  # admin.health_checker.HealthChecker
    wake_data_services: tuple[object, ...] = ()  # (StorageManager, UploadQueue, TriggerClassifier)
    voice_disabled_reason: str | None = None
    voice_missing_dependencies: tuple[str, ...] = ()
    degraded_components: tuple[str, ...] = ()

    def cleanup(self, timeout: float = 5.0) -> None:
        """
        Clean up all resources gracefully.

        Uses extracted helper functions to reduce complexity.
        Each cleanup phase is handled by a dedicated function.
        """
        logger.info("Starting graceful shutdown...")

        # 1. Stop voice orchestrator
        _cleanup_voice(self.voice, timeout)

        # 2. Stop TTS engine (releases ONNX model memory)
        _cleanup_tts(self.tts)

        # 3. Stop music player and threads
        _cleanup_music_player(self.music, timeout)

        # 4. Stop Uvicorn server
        _cleanup_uvicorn_server(self.uvicorn_server, timeout)

        # 5. Stop device discovery
        _cleanup_device_discovery(self.app, timeout)

        # 6. Telemetry scheduler (final send + stop)
        _cleanup_telemetry_scheduler(self.telemetry_scheduler)

        # 6b. Stop admin health checker
        _cleanup_health_checker(self.health_checker)

        # 6c. Stop wake data collection services
        _cleanup_wake_data_services(self.wake_data_services)

        # 7. Settle delay
        _await_coroutine_safe(asyncio.sleep(TIMEOUT_MEDIUM), "Cleanup settle delay", timeout)

        # 8. Cancel asyncio tasks
        _cleanup_asyncio_tasks(timeout)

        # 9. Stop background resolver
        _cleanup_background_resolver()

        logger.info("Graceful shutdown complete")
