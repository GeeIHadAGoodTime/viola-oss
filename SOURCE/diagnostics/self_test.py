"""
Lightweight self-test harness for critical Viola surfaces.

This module provides a self-test that exercises:
- Consent / multi-provider session wiring
- YouTube Music linking surface (config state, not real network)
- Voice pipeline wiring (without real microphone audio)

The self-test is designed to be run in development/test environments only
and does not perform real OAuth or use real audio devices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from config.settings import get_runtime_base_url
from core.logging_config import get_logger

if TYPE_CHECKING:
    from backend.app_state import AppState
    from services.supervisor import HeartbeatSupervisor
    from voice.pipeline import VoicePipeline


# Lazy import helpers for runtime use (avoids import-time circular dependencies)
def _get_app_state_class() -> type[AppState]:
    """Lazy import AppState to avoid circular dependency at import time."""
    from backend.app_state import AppState

    return AppState


def _get_consent_service():
    """Lazy import and call get_consent_service."""
    from music.consent import get_consent_service

    return get_consent_service()


def _get_heartbeat_supervisor_class() -> type[HeartbeatSupervisor]:
    """Lazy import HeartbeatSupervisor to avoid circular dependency at import time."""
    from services.supervisor import HeartbeatSupervisor

    return HeartbeatSupervisor


def _get_voice_pipeline_class() -> type[VoicePipeline]:
    """Lazy import VoicePipeline to avoid circular dependency at import time."""
    from voice.pipeline import VoicePipeline

    return VoicePipeline


logger = get_logger(__name__)


# Feature flag check - only allow in dev/test
def _is_dev_mode() -> bool:
    """Check if running in dev/test mode."""
    from config.settings import settings

    # Use centralized settings
    return settings.dev_mode or settings.test_mode


class SelfTestResult:
    """Result of a self-test run."""

    def __init__(self):
        self.ok: bool = True
        self.consent_routes_ok: bool = False
        self.youtube_status_ok: bool = False
        self.voice_pipeline_init_ok: bool = False
        self.messages: list[str] = []
        self.failing_components: list[str] = []
        self.details: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "ok": self.ok,
            "consent_routes_ok": self.consent_routes_ok,
            "youtube_status_ok": self.youtube_status_ok,
            "voice_pipeline_init_ok": self.voice_pipeline_init_ok,
            "message": ("; ".join(self.messages) if self.messages else "All checks passed"),
            "failing_components": self.failing_components,
            "details": self.details,
        }


def check_consent_routes(app: Any) -> tuple[bool, str, dict[str, Any]]:
    """
    Check that consent routes are registered and respond with sane shapes.

    Args:
        app: FastAPI application instance

    Returns:
        (success, message, details)
    """
    try:
        # Check if consent router is included
        routes = getattr(app.router, "routes", [])
        consent_paths = [
            "/v1/consent/providers",
            "/v1/consent/session",
            "/v1/consent/capabilities",
        ]

        found_paths = []
        for route in routes:
            route_path = getattr(route, "path", None)
            if route_path and any(consent_path in route_path for consent_path in consent_paths):
                found_paths.append(route_path)

        if not found_paths:
            return False, "No consent routes found", {"found_paths": []}

        # Try to get consent service to verify it's accessible
        try:
            service = _get_consent_service()
            if service is None:
                return (
                    False,
                    "Consent service unavailable",
                    {"found_paths": found_paths},
                )
        except Exception as exc:
            return (
                False,
                f"Consent service error: {exc}",
                {"found_paths": found_paths, "error": str(exc)},
            )

        return (
            True,
            f"Consent routes registered: {len(found_paths)}",
            {
                "found_paths": found_paths,
                "service_available": True,
            },
        )
    except Exception as exc:
        logger.exception("Error checking consent routes")
        return False, f"Error checking consent routes: {exc}", {"error": str(exc)}


def check_youtube_status() -> tuple[bool, str, dict[str, Any]]:
    """
    Check YouTube Music provider status without performing real OAuth.

    Returns:
        (success, message, details)
    """
    try:
        service = _get_consent_service()
        if service is None:
            return False, "Consent service unavailable", {}

        # Get provider statuses
        statuses = service.list_statuses(
            user_id="default"
        )  # mt-ok: self-test only, checks provider registration not user data

        # Find YouTube Music provider
        youtube_status = None
        for status in statuses:
            if status.provider_id == "youtube_music":
                youtube_status = status
                break

        if youtube_status is None:
            return (
                False,
                "YouTube Music provider not found in status list",
                {
                    "available_providers": [s.provider_id for s in statuses],
                },
            )

        # Check that status has expected fields
        status_dict = youtube_status.to_dict()
        required_fields = ["provider_id", "state", "display_name"]
        missing_fields = [f for f in required_fields if f not in status_dict]

        if missing_fields:
            return (
                False,
                f"YouTube status missing fields: {missing_fields}",
                {
                    "status": status_dict,
                    "missing_fields": missing_fields,
                },
            )

        # Check if credentials are configured (by checking if authorization_url can be generated)
        # This doesn't perform real OAuth, just checks if the adapter can generate a URL
        try:
            from music.consent import providers

            adapter = providers.get_provider("youtube_music")
            if adapter is None:
                return (
                    False,
                    "YouTube Music adapter not registered",
                    {
                        "status": status_dict,
                    },
                )

            # Try to generate authorization URL (this checks config, not network)
            # Use a fake redirect_uri for testing
            auth_url_error: str | None = None
            try:
                auth_url = adapter.authorization_url(
                    redirect_uri=f"{get_runtime_base_url()}/v1/consent/callback",
                    state="test_state",
                )
                has_credentials = auth_url is not None and len(auth_url) > 0
            except Exception as url_exc:
                # If we can't generate URL, credentials are likely missing
                has_credentials = False
                auth_url_error = str(url_exc)

            details = {
                "status": status_dict,
                "has_credentials": has_credentials,
                "state": status_dict.get("state", "unknown"),
            }

            if not has_credentials:
                details["auth_url_error"] = auth_url_error if auth_url_error is not None else "unknown"
                return (
                    True,
                    "YouTube provider found but credentials not configured",
                    details,
                )

            return True, "YouTube provider status OK", details

        except Exception as exc:
            logger.warning("Error checking YouTube adapter: %s", exc)
            return (
                True,
                "YouTube provider found (adapter check failed)",
                {
                    "status": status_dict,
                    "adapter_error": str(exc),
                },
            )

    except Exception as exc:
        logger.exception("Error checking YouTube status")
        return False, f"Error checking YouTube status: {exc}", {"error": str(exc)}


def check_voice_pipeline_init() -> tuple[bool, str, dict[str, Any]]:
    """
    Check that voice pipeline can be initialized with wake enabled without throwing.

    This uses mocks to avoid real audio device access.

    Returns:
        (success, message, details)
    """
    try:
        from config import AppConfig

        # Create a test config with wake enabled but test_mode to avoid real audio
        test_config = AppConfig(
            wake_enabled=True,
            wake_engine="violawake",  # Use violawake engine for testing
            test_mode=True,  # This should prevent real audio device access
        )

        # Try to create voice pipeline with minimal dependencies
        # We'll use mocks for audio components
        try:
            # Create minimal state
            AppState = _get_app_state_class()
            state = AppState()

            # Create supervisor
            HeartbeatSupervisor = _get_heartbeat_supervisor_class()
            supervisor = HeartbeatSupervisor(state)

            # Lightweight dummy components to avoid heavy dependencies (TTS/STT backends)
            class _DummyTranscriber:
                def prewarm(self) -> None:
                    return

            class _DummySynthesizer:
                pass

            # Create pipeline with injected dummies; avoids loading real backends
            VoicePipeline = _get_voice_pipeline_class()
            pipeline = VoicePipeline(
                config=test_config,
                on_wake_word_detected=lambda: None,
                supervisor=supervisor,
                transcriber=cast(Any, _DummyTranscriber()),
                synthesizer=cast(Any, _DummySynthesizer()),
            )

            if pipeline is None:
                return False, "Voice pipeline creation returned None", {}

            # Check that key components exist
            has_wake = hasattr(pipeline, "wake_detector") and pipeline.wake_detector is not None
            has_transcriber = hasattr(pipeline, "transcriber") and pipeline.transcriber is not None
            has_synthesizer = hasattr(pipeline, "synthesizer") and pipeline.synthesizer is not None

            details = {
                "pipeline_created": True,
                "has_wake_detector": has_wake,
                "has_transcriber": has_transcriber,
                "has_synthesizer": has_synthesizer,
            }

            if not (has_wake and has_transcriber and has_synthesizer):
                return False, "Voice pipeline missing components", details

            return True, "Voice pipeline initialized successfully", details

        except ImportError as import_exc:
            # Some dependencies might not be available in test environments
            return (
                False,
                f"Voice pipeline dependencies unavailable: {import_exc}",
                {
                    "error": str(import_exc),
                },
            )
        except Exception as exc:
            logger.exception("Error initializing voice pipeline")
            return (
                False,
                f"Voice pipeline initialization failed: {exc}",
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )

    except Exception as exc:
        logger.exception("Error checking voice pipeline")
        return False, f"Error checking voice pipeline: {exc}", {"error": str(exc)}


def run_self_test(app: Any | None = None) -> SelfTestResult:
    """
    Run the complete self-test suite.

    Args:
        app: FastAPI application instance (optional, will try to get from context if not provided)

    Returns:
        SelfTestResult with all check results
    """
    result = SelfTestResult()

    # Check dev mode
    if not _is_dev_mode():
        result.ok = False
        result.messages.append("Self-test only available in dev/test mode")
        result.failing_components.append("dev_mode_check")
        return result

    # Check consent routes
    if app is None:
        result.ok = False
        result.messages.append("FastAPI app not provided")
        result.failing_components.append("app_context")
        return result

    consent_ok, consent_msg, consent_details = check_consent_routes(app)
    result.consent_routes_ok = consent_ok
    result.messages.append(f"Consent routes: {consent_msg}")
    result.details["consent"] = consent_details
    if not consent_ok:
        result.ok = False
        result.failing_components.append("consent_routes")

    # Check YouTube status
    youtube_ok, youtube_msg, youtube_details = check_youtube_status()
    result.youtube_status_ok = youtube_ok
    result.messages.append(f"YouTube status: {youtube_msg}")
    result.details["youtube"] = youtube_details
    if not youtube_ok:
        result.ok = False
        result.failing_components.append("youtube_status")

    # Check voice pipeline (robust to unexpected exceptions)
    try:
        voice_ok, voice_msg, voice_details = check_voice_pipeline_init()
    except Exception as exc:  # pragma: no cover - defensive
        voice_ok, voice_msg, voice_details = (
            False,
            f"Voice pipeline check failed: {exc}",
            {"error": str(exc), "error_type": type(exc).__name__},
        )
    result.voice_pipeline_init_ok = voice_ok
    result.messages.append(f"Voice pipeline: {voice_msg}")
    result.details["voice_pipeline"] = voice_details
    if not voice_ok:
        result.ok = False
        result.failing_components.append("voice_pipeline")

    return result
