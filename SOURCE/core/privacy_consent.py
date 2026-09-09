"""
Privacy consent checks for cloud data transmission.

All cloud data transmission must be gated behind explicit user consent.
Consent defaults to NOT GIVEN for all services. Users must opt-in via
the settings UI before any data leaves the device.

Usage:
    from core.privacy_consent import is_cloud_stt_consented, is_cloud_llm_consented

    if not is_cloud_stt_consented():
        # Do not send audio to cloud — use local STT only
        ...
"""

from __future__ import annotations

import os
from contextvars import ContextVar

from core.logging_config import get_logger

logger = get_logger(__name__)
_MISSING = object()

# Async-resolved cloud LLM consent for the current request context. The sync
# gate is_cloud_llm_consented() is invoked from inside the cloud async pipeline
# (on the main event loop); its sync->async consent bridge cannot re-enter that
# loop and raises "Cannot synchronously wait on the ... loop from itself",
# which fail-closes even a CONSENTED user to not-consented. Upstream async code
# (the cloud dispatch) resolves consent with `await has_consent(...)` and stashes
# the real boolean here so the sync gate reads it with no bridge. It is set in
# the same task/context that runs the LLM factory, so it propagates directly
# (and through asyncio.to_thread, which copies context). None = not resolved,
# so the gate falls back to its own resolution.
_cloud_llm_consent_override: ContextVar[bool | None] = ContextVar("_cloud_llm_consent_override", default=None)


def set_cloud_llm_consent_override(value: bool | None) -> object:
    """Stash the async-resolved cloud LLM consent for the current context.

    Returns a token; pass it to reset_cloud_llm_consent_override in a finally.
    """
    return _cloud_llm_consent_override.set(value)


def reset_cloud_llm_consent_override(token: object) -> None:
    try:
        _cloud_llm_consent_override.reset(token)  # type: ignore[arg-type]
    except (ValueError, LookupError):
        # Token from a different context; the per-request ContextVar is isolated
        # anyway, so a failed reset is a harmless no-op.
        pass


# Mapping from consent key to environment variable name
_CONSENT_ENV_VARS: dict[str, str] = {
    "consent_cloud_stt": "VIOLA_CONSENT_CLOUD_STT",
    "consent_openai_storage": "VIOLA_CONSENT_OPENAI_STORAGE",
    "consent_cloud_sync": "VIOLA_CONSENT_CLOUD_SYNC",
    "consent_error_reporting": "VIOLA_CONSENT_ERROR_REPORTING",
}


def _cloud_runtime_user_id() -> str | None:
    try:
        from core.request_context import get_request_context

        ctx = get_request_context()
        if ctx is not None and ctx.user_id:
            return ctx.user_id
    except Exception:
        pass

    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except (ImportError, LookupError):
        return None


def _cloud_llm_consent_service_value() -> bool | None:
    surface = "desktop"
    user_id: str | None = None
    try:
        from config.settings import settings as app_settings

        surface = str(
            getattr(app_settings, "app_surface", None) or getattr(app_settings, "deployment_mode", "desktop")
        ).lower()
        if surface != "cloud":
            return None

        user_id = _cloud_runtime_user_id()
        if not user_id:
            return None

        from services.cloud_consent import get_cloud_consent_service

        service = get_cloud_consent_service()
        return service.has_consent_sync(user_id, service.CLOUD_LLM)
    except Exception:
        logger.debug("Cloud consent service read failed")
        if surface == "cloud" and user_id:
            return False
        return None


def _get_consent(key: str) -> bool:
    """Read a consent flag from environment or SettingsManager.

    Checks the environment variable first (e.g. VIOLA_CONSENT_CLOUD_STT),
    then falls back to SettingsManager.
    Returns False (not consented) if neither source is available,
    ensuring fail-closed behavior.
    """
    # Check env var override first (set via .env or system environment)
    env_var = _CONSENT_ENV_VARS.get(key)
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val is not None:
            return str(env_val).strip().lower() in ("1", "true", "yes", "on", "y")

    try:
        from ui.settings_manager import get_settings_manager

        mgr = get_settings_manager()
        value = mgr.get(key, _MISSING)
        if value is _MISSING:
            return False
        return bool(value)
    except Exception:
        # Fail closed: if we can't read settings, assume no consent
        return False


def is_cloud_stt_consented() -> bool:
    """Check if user consented to sending audio to cloud STT providers.

    When False, only local Whisper transcription should be used.
    Cloud providers (Azure Speech, OpenAI Whisper API) must not receive audio.
    """
    return _get_consent("consent_cloud_stt")


def is_cloud_llm_consented() -> bool:
    """Check if the active AI source allows cloud LLM providers.

    The legacy cloud-LLM consent setting was retired; users now choose local-only
    operation by setting ``ai_source="local"``. Cloud SaaS can still enforce per-user consent
    records through CloudConsentService.
    """
    # Async-resolved override set by upstream cloud code (the dispatch), so this
    # sync gate does not have to bridge to async from inside the event loop (that
    # bridge raises on the cloud main loop and fail-closes a consented user).
    override = _cloud_llm_consent_override.get()
    if override is not None:
        return override

    cloud_value = _cloud_llm_consent_service_value()
    if cloud_value is not None:
        return cloud_value

    try:
        from core.product import AiSource, coerce_ai_source
        from ui.settings_manager import get_settings_manager

        mgr = get_settings_manager()
        source = coerce_ai_source(mgr.get("ai_source", None))
        return source is not AiSource.LOCAL
    except Exception:
        return False


def is_cloud_sync_consented() -> bool:
    """Check if user consented to sending player state to cloud relay.

    When False, the cloud relay must not transmit any device state.
    Local P2P sync is unaffected.
    """
    return _get_consent("consent_cloud_sync")


def is_openai_storage_consented() -> bool:
    """Check if user consented to OpenAI server-side response storage."""
    return _get_consent("consent_openai_storage")


def is_error_reporting_consented() -> bool:
    """Check if user consented to sending error reports to Sentry.

    When False, Sentry events must be dropped before transmission.
    """
    return _get_consent("consent_error_reporting")


__all__ = [
    "is_cloud_llm_consented",
    "is_cloud_stt_consented",
    "is_cloud_sync_consented",
    "is_error_reporting_consented",
    "is_openai_storage_consented",
]
