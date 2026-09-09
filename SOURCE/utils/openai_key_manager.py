from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from config.settings import settings as app_settings
from core.logging_config import get_logger

logger = get_logger(__name__)


class OpenAIKeySource(str, Enum):
    """Enumerates all supported OpenAI API key sources."""

    NONE = "none"
    DIRECT = "direct"
    SETTINGS = "settings"
    CONFIG = "config"
    ENV_VIOLA = "env_viola"
    ENV_OPENAI = "env_openai"


@dataclass(frozen=True)
class OpenAIKeyResolution:
    """Resolved OpenAI API key and relevant metadata."""

    key: str | None
    source: OpenAIKeySource
    masked: str

    def describe(self) -> str:
        """Return a human-friendly description for logging or UI."""
        if not self.key:
            return "OpenAI API key unavailable"
        return f"OpenAI API key from {self.source.value} ({self.masked})"


def mask_secret(secret: str | None, prefix: int = 6, suffix: int = 2) -> str:
    """Return a privacy-preserving mask for secret values."""
    if not secret:
        return ""
    secret = secret.strip()
    if not secret:
        return ""
    if len(secret) <= max(prefix, suffix):
        return "●" * len(secret)
    return f"{secret[:prefix]}…{secret[-suffix:]}"


def resolve_openai_api_key(
    *,
    config: object | None = None,
    direct_key: str | None = None,
    preferred_source: OpenAIKeySource | str | None = None,
    settings_manager: object | None = None,
    extra_precedence: Sequence[OpenAIKeySource] | None = None,
    allow_fallback: bool = True,
) -> OpenAIKeyResolution:
    """
    Resolve the OpenAI API key with explicit precedence.

    Default precedence (highest → lowest):
        1. Explicit/direct key provided to the resolver
        2. Stored user settings (via SettingsManager, decrypted when available)
        3. Config/AppConfig attribute
        4. Environment variable ``VIOLA_OPENAI_API_KEY``
        5. Environment variable ``OPENAI_API_KEY``

    The precedence honours the project vision: local stored settings come before
    environment overrides, while still allowing advanced users/tests to pick a
    specific source through ``preferred_source``.
    """

    def _normalise(value: str | None) -> str | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip()
        if not candidate or candidate == "***ENCRYPTED***":
            return None
        return candidate

    def _get_settings_key() -> str | None:
        mgr = settings_manager
        if mgr is None:
            try:
                from ui.settings_manager import get_settings_manager

                mgr = get_settings_manager()
            except Exception as e:
                logger.debug("Failed to get settings manager: %s", e, exc_info=True)
                return None
        if mgr is None:
            return None
        try:
            getter = getattr(mgr, "get", None)
            if getter is not None:
                return getter("openai_api_key")
            return None
        except Exception as e:
            logger.debug("Failed to get OpenAI key from settings: %s", e, exc_info=True)
            return None

    candidates: list[tuple[OpenAIKeySource, str]] = []

    direct = _normalise(direct_key)
    if direct:
        candidates.append((OpenAIKeySource.DIRECT, direct))

    stored = _normalise(_get_settings_key())
    if stored:
        candidates.append((OpenAIKeySource.SETTINGS, stored))

    if config is not None and hasattr(config, "openai_api_key"):
        cfg_value = _normalise(getattr(config, "openai_api_key", None))
        if cfg_value:
            candidates.append((OpenAIKeySource.CONFIG, cfg_value))

    # settings.openai_api_key loads from VIOLA_OPENAI_API_KEY / OPENAI_API_KEY via config system
    settings_key = _normalise(app_settings.openai_api_key)
    if settings_key:
        candidates.append((OpenAIKeySource.ENV_VIOLA, settings_key))

    # Honour explicit preference when supplied
    preferred: OpenAIKeySource | None = None
    if preferred_source:
        if isinstance(preferred_source, OpenAIKeySource):
            preferred = preferred_source
        else:
            try:
                preferred = OpenAIKeySource(str(preferred_source))
            except ValueError:
                logger.debug(
                    "Unknown preferred OpenAI key source '%s' – ignoring",
                    preferred_source,
                )
    if preferred:
        for source, value in candidates:
            if source is preferred:
                return OpenAIKeyResolution(value, source, mask_secret(value))
        if not allow_fallback:
            return OpenAIKeyResolution(None, OpenAIKeySource.NONE, "")

    # Compose precedence order
    precedence: list[OpenAIKeySource] = [
        OpenAIKeySource.DIRECT,
        OpenAIKeySource.SETTINGS,
        OpenAIKeySource.CONFIG,
        OpenAIKeySource.ENV_VIOLA,
        OpenAIKeySource.ENV_OPENAI,
    ]
    if extra_precedence:
        precedence = list(dict.fromkeys([*extra_precedence, *precedence]))

    for source in precedence:
        for candidate_source, value in candidates:
            if candidate_source is source:
                return OpenAIKeyResolution(value, source, mask_secret(value))

    return OpenAIKeyResolution(None, OpenAIKeySource.NONE, "")
