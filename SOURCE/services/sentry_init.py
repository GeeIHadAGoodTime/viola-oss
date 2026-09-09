"""Process entry-point wrapper for Sentry initialization."""

from __future__ import annotations

from typing import Any

from config.settings import settings
from core.logging_config import get_logger
from core.sentry_integration import SentryInitResult, configure_sentry
from core.user_context import is_desktop_local_principal, user_id_or_none

logger = get_logger(__name__)


def init_sentry(entry_point: str, *, require_config: bool | None = None) -> SentryInitResult:
    """Initialize Sentry for a Python process entry point.

    ``require_config=None`` means required for cloud/prod surfaces and optional
    elsewhere. Entry points call this directly so ratchet gates can find the
    consistent helper signature.
    """
    required = _sentry_required_for_surface() if require_config is None else require_config
    result = configure_sentry(entry_point=entry_point, require_config=required)
    if result.initialized:
        logger.info(
            "sentry_init: ok release=%s environment=%s entry_point=%s",
            result.release,
            result.environment,
            result.entry_point,
        )
    else:
        logger.info(
            "sentry_init: skipped release=%s environment=%s entry_point=%s reason=%s",
            result.release,
            result.environment,
            result.entry_point,
            result.reason,
        )
    return result


def _sentry_required_for_surface() -> bool:
    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", surface) or surface).strip().lower()
    env_name = str(getattr(settings, "env", "dev") or "dev").strip().lower()
    return surface == "cloud" or deployment == "cloud" or env_name == "prod"


class SentryUserContextMiddleware:
    """Attach authenticated request user_id to the current Sentry scope."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        user_id = _gotrue_user_id_from_scope(scope)
        sentry_sdk = None
        if user_id is not None:
            try:
                import sentry_sdk as _sentry_sdk

                sentry_sdk = _sentry_sdk
                sentry_sdk.set_user({"id": user_id})
                sentry_sdk.set_tag("auth_user_source", "auth.middleware")
            except ImportError:
                sentry_sdk = None
            except Exception:
                logger.debug("Could not attach Sentry user context", exc_info=True)
                sentry_sdk = None

        try:
            await self.app(scope, receive, send)
        finally:
            if sentry_sdk is not None:
                try:
                    sentry_sdk.set_user(None)
                except Exception:
                    logger.debug("Could not clear Sentry user context", exc_info=True)


def _gotrue_user_id_from_scope(scope: dict[str, Any]) -> str | None:
    state = scope.get("state")
    user = _state_value(state, "user")
    if type(user).__name__ == "_LocalUser":
        return None

    user_id = user_id_or_none(getattr(user, "id", None))
    if user_id is None:
        user_id = user_id_or_none(_state_value(state, "user_id"))
    if user_id is None:
        request_context = _state_value(state, "request_context")
        user_id = user_id_or_none(getattr(request_context, "user_id", None))
    if user_id is None or is_desktop_local_principal(user_id):
        return None
    return user_id


def _state_value(state: object, key: str) -> object:
    if isinstance(state, dict):
        return state.get(key)
    return getattr(state, key, None)


__all__ = [
    "SentryUserContextMiddleware",
    "init_sentry",
]
