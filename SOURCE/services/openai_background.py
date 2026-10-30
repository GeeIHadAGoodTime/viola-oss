"""Shared low-cost Responses helpers for background summarization/extraction.

Every background AI feature (automatic memory, memory selection and clean-up,
compaction summaries, fact extraction, approval-reply classification on
messaging channels, music recommendations, screen analysis) goes through here.
The route follows the user's AI source, never a shared key the user did not
choose:

- **managed** (the default) with a local ``OPENAI_API_KEY`` (a dev ``.env``,
  or the cloud server itself): OpenAI direct with that key, as before.
- **managed on a keyless desktop install** (every real customer: installed
  builds ship no ``.env`` and no key): forwarded to Viola's cloud
  ``/v1/llm/native`` with the signed-in user's GoTrue bearer, the same
  transport the main chat turn uses (``CloudManagedProvider``). The cloud runs
  the call on the server-held key and meters it against that user's managed
  spend cap. Requires a signed-in account and cloud-AI consent; without either,
  nothing is sent and the call raises a typed ``BackgroundLLMUnavailable``.
- **byok**: the user's own key from their settings vault (OpenAI and
  OpenAI-compatible go through Responses; other providers through their own
  provider's ``ask``).
- **local**: the user's local model server (text only).
- **codex**: the Codex sign-in client, as before.

``is_background_ai_available`` is the cheap pre-check callers use instead of
testing ``settings.openai_api_key`` (which is empty on every customer install).
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass
from typing import Any

from config.defaults import (
    DEFAULT_AI_SOURCE,
    DEFAULT_BACKGROUND_TASK_MODEL,
    DEFAULT_CODEX_MODEL,
    DEFAULT_GPT_MODEL,
    DEFAULT_MANAGED_MODEL,
    get_provider_default_model,
)
from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)

_OPENAI_DIRECT_AI_SOURCES = frozenset({"", "openai", "managed", "subscription"})
_CODEX_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_BYOK_OPENAI_PROVIDERS = frozenset({"openai", "openai_compatible"})
_ENCRYPTED_PLACEHOLDER = "***ENCRYPTED***"
_CLOUD_MANAGED_PROVIDER_MODULE = "services.llm.providers.cloud_managed_provider"

ROUTE_OPENAI_DIRECT = "openai_direct"
ROUTE_CLOUD_MANAGED = "cloud_managed"
ROUTE_CODEX = "codex"
ROUTE_BYOK = "byok"
ROUTE_BYOK_PROVIDER = "byok_provider"
ROUTE_LOCAL = "local"
ROUTE_UNAVAILABLE = "unavailable"

# A background request is either plain text or Responses-style content parts
# (``input_text`` / ``input_image``), the latter for screen analysis.
BackgroundContent = str | list[dict[str, Any]]


class BackgroundLLMUnavailable(RuntimeError):
    """Background LLM request cannot run for the selected AI source.

    ``user_message`` is a short plain-English line a caller may show the user.
    """

    user_message = "AI isn't available for this right now."

    def __init__(self, message: str = "", *, user_message: str | None = None) -> None:
        super().__init__(message)
        if user_message:
            self.user_message = user_message


class CodexBackgroundUnavailable(BackgroundLLMUnavailable):
    """Codex background routing was selected but Codex auth is unavailable."""

    user_message = "Sign in to Codex again to use this."


class UnsupportedBackgroundAISource(BackgroundLLMUnavailable):
    """Background routing for the selected AI source has not been wired."""

    user_message = "This isn't available with your current AI source."


class BackgroundLLMSignInRequired(BackgroundLLMUnavailable):
    """Viola-managed AI needs a signed-in Viola account on this desktop."""

    user_message = "Sign in to Viola to use this with Viola-managed AI."


class BackgroundLLMConsentRequired(BackgroundLLMUnavailable):
    """Cloud AI is not consented, so no background cloud call may be made."""

    user_message = "Cloud AI is turned off, so this can't run."


class BackgroundLLMLimitReached(BackgroundLLMUnavailable):
    """The managed spend cap or a cloud safety limit refused the call."""

    user_message = "You've reached your Viola-managed AI limit for now."


@dataclass(frozen=True)
class BackgroundRoute:
    """Where a background request for one user will go, decided up front."""

    kind: str
    ai_source: str
    user_id: str = ""
    reason: str = ""
    error_type: type[BackgroundLLMUnavailable] = BackgroundLLMUnavailable

    @property
    def available(self) -> bool:
        return self.kind != ROUTE_UNAVAILABLE

    def unavailable_error(self) -> BackgroundLLMUnavailable:
        return self.error_type(self.reason or "Background AI is unavailable")


def _is_reasoning_model(model: str) -> bool:
    normalized = model.lower()
    return any(tag in normalized for tag in ("gpt-5", "gpt-6", "o1", "o3", "o4-"))


def _settings_manager_value(key: str, default: Any, *, user_id: str = "") -> Any:
    try:
        from ui.settings_manager import get_settings_manager

        manager = get_settings_manager()
        try:
            return manager.get(key, default, user_id=user_id) if user_id else manager.get(key, default)
        except TypeError:
            return manager.get(key, default)
    except (AttributeError, ImportError, KeyError, RuntimeError, ValueError):
        return default


class _UserScopedSettings:
    """``.get(key, default)`` view of one user's settings for the LLM factory."""

    def __init__(self, user_id: str) -> None:
        self._user_id = user_id

    def get(self, key: str, default: Any = None) -> Any:
        return _settings_manager_value(key, default, user_id=self._user_id)


def _resolve_background_ai_source(user_id: str) -> str:
    override = (getattr(settings, "ai_source_override", "") or "").strip().lower()
    if override:
        return override

    raw = _settings_manager_value("ai_source", DEFAULT_AI_SOURCE, user_id=user_id)
    source = raw if isinstance(raw, str) else DEFAULT_AI_SOURCE
    return source.strip().lower() or DEFAULT_AI_SOURCE


def _selected_llm_model(user_id: str) -> str:
    raw = _settings_manager_value("llm_model", "", user_id=user_id)
    return raw.strip() if isinstance(raw, str) else ""


def _is_codex_model_name(model: str) -> bool:
    normalized = (model or "").strip().lower()
    return "codex" in normalized or normalized.startswith("gpt-5.")


def _default_codex_background_model(user_id: str) -> str:
    selected_model = _selected_llm_model(user_id)
    if selected_model and _is_codex_model_name(selected_model):
        return selected_model

    try:
        from services.llm.codex_auth import get_codex_models

        available = [model for model in get_codex_models() if isinstance(model, str) and model.strip()]
    except (ImportError, RuntimeError, ValueError):
        available = []

    preferred = (
        "gpt-5.3-codex-spark",
        DEFAULT_CODEX_MODEL,
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        "gpt-5.2-codex",
    )
    for candidate in preferred:
        if candidate in available:
            return candidate
    for candidate in available:
        if _is_codex_model_name(candidate):
            return candidate
    return DEFAULT_CODEX_MODEL


def _default_byok_background_model(user_id: str) -> str:
    """The user's own selected model, else their provider's default model."""
    selected_model = _selected_llm_model(user_id)
    if selected_model:
        return selected_model
    return get_provider_default_model(_byok_provider_name(user_id)) or DEFAULT_GPT_MODEL


def _resolve_background_model(*, ai_source: str, requested_model: str | None, user_id: str) -> str:
    """Pick the model a background request runs on for this AI source.

    - managed (Viola's key): always ``DEFAULT_MANAGED_MODEL``, whatever the
      caller asked for. The cloud route enforces the same rule server-side.
    - byok: the user's own selected model (their key, their choice); a
      caller's generic background request never forces a model onto it.
    - codex: the user's Codex subscription model, as before.
    """
    requested = requested_model.strip() if isinstance(requested_model, str) else ""
    if ai_source in _OPENAI_DIRECT_AI_SOURCES:
        return DEFAULT_MANAGED_MODEL
    if ai_source == "codex" and requested in {"", DEFAULT_BACKGROUND_TASK_MODEL}:
        return _default_codex_background_model(user_id)
    if ai_source == "byok":
        return _default_byok_background_model(user_id)
    return requested or DEFAULT_BACKGROUND_TASK_MODEL


def _codex_reasoning_effort(effort: str) -> str:
    normalized = (effort or "").strip().lower()
    if normalized in _CODEX_REASONING_EFFORTS:
        return normalized
    if normalized in {"", "none", "minimal"}:
        return "low"
    return "low"


def _reasoning_payload(*, ai_source: str, model: str) -> dict[str, str]:
    from config.defaults import get_configured_reasoning_effort

    effort = get_configured_reasoning_effort("routing", model)
    if ai_source == "codex":
        effort = _codex_reasoning_effort(effort)
    return {
        "effort": effort,
        "summary": "auto",
    }


def _create_openai_direct_client(*, api_key: str, timeout_s: float, base_url: str = "") -> Any:
    from openai import AsyncOpenAI

    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout_s}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def _byok_provider_name(user_id: str) -> str:
    provider_raw = _settings_manager_value("llm_provider", "openai", user_id=user_id)
    return provider_raw.strip().lower() if isinstance(provider_raw, str) and provider_raw.strip() else "openai"


def _byok_api_key(user_id: str) -> str:
    api_key_raw = _settings_manager_value("llm_api_key", "", user_id=user_id)
    api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""
    if not api_key or api_key == _ENCRYPTED_PLACEHOLDER:
        legacy_raw = _settings_manager_value("openai_api_key", "", user_id=user_id)
        api_key = legacy_raw.strip() if isinstance(legacy_raw, str) else ""
    if not api_key or api_key == _ENCRYPTED_PLACEHOLDER:
        return ""
    return api_key


def _byok_client_config(user_id: str) -> tuple[str, str]:
    provider = _byok_provider_name(user_id)
    if provider not in _BYOK_OPENAI_PROVIDERS:
        raise UnsupportedBackgroundAISource(
            "Background Responses requests are unavailable for ai_source=byok "
            "with llm_provider='%s'; refusing to use the shared OpenAI key." % provider
        )

    api_key = _byok_api_key(user_id)
    if not api_key:
        raise BackgroundLLMUnavailable(
            "No BYOK API key for background Responses request",
            user_message="Add your API key in Settings to use this.",
        )

    base_url = ""
    if provider == "openai_compatible":
        base_raw = _settings_manager_value("llm_base_url", "", user_id=user_id)
        base_url = base_raw.strip() if isinstance(base_raw, str) else ""
        if not base_url:
            raise UnsupportedBackgroundAISource(
                "Background Responses requests are unavailable for ai_source=byok "
                "with llm_provider='openai_compatible' and no llm_base_url."
            )
    return api_key, base_url


def _create_background_client(*, ai_source: str, timeout_s: float, user_id: str) -> Any:
    if ai_source == "codex":
        try:
            from services.llm.codex_auth import (
                create_codex_openai_client,
                is_codex_available,
            )
        except ImportError as exc:
            raise CodexBackgroundUnavailable(
                "Codex background LLM unavailable: Codex auth support is not importable; "
                "refusing to fall through to api.openai.com."
            ) from exc

        if not is_codex_available():
            raise CodexBackgroundUnavailable(
                "Codex background LLM unavailable: ai_source=codex requires a valid Codex CLI sign-in; "
                "refusing to fall through to api.openai.com."
            )
        try:
            return create_codex_openai_client()
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            raise CodexBackgroundUnavailable(
                "Codex background LLM unavailable: Codex client creation failed; "
                "refusing to fall through to api.openai.com."
            ) from exc

    if ai_source == "byok":
        api_key, base_url = _byok_client_config(user_id)
        return _create_openai_direct_client(api_key=api_key, timeout_s=timeout_s, base_url=base_url)

    if ai_source in _OPENAI_DIRECT_AI_SOURCES:
        api_key = settings.openai_api_key
        if not api_key:
            raise BackgroundLLMUnavailable("No OpenAI API key for background OpenAI request")
        return _create_openai_direct_client(api_key=api_key, timeout_s=timeout_s)

    raise UnsupportedBackgroundAISource(
        "Background Responses requests are unavailable for ai_source='%s'; "
        "refusing to use an OpenAI-direct fallback." % ai_source
    )


# ---------------------------------------------------------------------------
# Route resolution ("is background AI available for this user")
# ---------------------------------------------------------------------------


def _app_surface() -> str:
    return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()


def _local_openai_key() -> str:
    value = getattr(settings, "openai_api_key", "") or ""
    return value.strip() if isinstance(value, str) else ""


def _cloud_url() -> str:
    value = getattr(settings, "cloud_url", "") or getattr(settings, "api_base_url", "") or ""
    return value.strip() if isinstance(value, str) else ""


def _cloud_llm_consented() -> bool:
    """Cloud-AI consent, failing closed when it cannot be read."""
    try:
        from core.privacy_consent import is_cloud_llm_consented

        return bool(is_cloud_llm_consented())
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Cloud LLM consent unreadable; treating as not consented", exc_info=True)
        return False


def _resolve_account_user_id(user_id: str) -> str:
    """Return the signed-in Viola account a cloud call would run as, or "".

    An explicit ``user_id`` wins; with none, the request principal or the
    desktop's signed-in account is used. Device/local pseudo-identities and
    placeholders are not accounts, so they resolve to "".
    """
    candidate = (user_id or "").strip()
    if not candidate:
        try:
            from core.user_context import get_current_or_desktop_active_user_id

            resolved = get_current_or_desktop_active_user_id()
        except (LookupError, ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return ""
        candidate = resolved.strip() if isinstance(resolved, str) else ""
    if not candidate:
        return ""
    try:
        from core.account_gate import user_has_viola_account
        from core.user_context import is_placeholder_user_id

        if is_placeholder_user_id(candidate) or not user_has_viola_account(candidate):
            return ""
    except ImportError:
        return ""
    return candidate


def _cloud_forward_importable() -> bool:
    try:
        return importlib.util.find_spec(_CLOUD_MANAGED_PROVIDER_MODULE) is not None
    except (ImportError, ValueError):
        return False


def _unavailable(
    ai_source: str,
    reason: str,
    error_type: type[BackgroundLLMUnavailable] = BackgroundLLMUnavailable,
    *,
    user_id: str = "",
) -> BackgroundRoute:
    return BackgroundRoute(
        kind=ROUTE_UNAVAILABLE,
        ai_source=ai_source,
        user_id=user_id,
        reason=reason,
        error_type=error_type,
    )


def _resolve_managed_route(ai_source: str, user_id: str) -> BackgroundRoute:
    if _local_openai_key():
        # Dev .env, or the cloud server's own key: direct, as before.
        return BackgroundRoute(kind=ROUTE_OPENAI_DIRECT, ai_source=ai_source, user_id=user_id)
    if _app_surface() == "cloud":
        return _unavailable(ai_source, "No OpenAI API key for background OpenAI request", user_id=user_id)
    if not _cloud_llm_consented():
        return _unavailable(
            ai_source,
            "Cloud AI consent not granted; background cloud AI disabled.",
            BackgroundLLMConsentRequired,
            user_id=user_id,
        )
    if not _cloud_url():
        return _unavailable(ai_source, "No cloud_url configured; background cloud AI disabled.", user_id=user_id)
    account_user_id = _resolve_account_user_id(user_id)
    if not account_user_id:
        return _unavailable(
            ai_source,
            "Viola-managed background AI needs a signed-in Viola account.",
            BackgroundLLMSignInRequired,
            user_id=user_id,
        )
    if not _cloud_forward_importable():
        return _unavailable(
            ai_source,
            "Managed cloud forward is not part of this build.",
            UnsupportedBackgroundAISource,
            user_id=account_user_id,
        )
    return BackgroundRoute(kind=ROUTE_CLOUD_MANAGED, ai_source=ai_source, user_id=account_user_id)


# (kind, ai_source, error type) combinations already logged in this process.
# Holds no user data: it only stops a per-turn caller (memory selection) from
# logging the same route on every turn.
_LOGGED_ROUTES: set[tuple[str, str, str]] = set()


def _log_route_once(route: BackgroundRoute) -> None:
    key = (route.kind, route.ai_source, route.error_type.__name__ if not route.available else "")
    if key in _LOGGED_ROUTES:
        return
    _LOGGED_ROUTES.add(key)
    if route.available:
        logger.info("Background AI route: %s (ai_source=%s)", route.kind, route.ai_source or "managed")
    else:
        logger.info(
            "Background AI unavailable (ai_source=%s, %s): %s",
            route.ai_source or "managed",
            route.error_type.__name__,
            route.reason,
        )


def resolve_background_route(user_id: str = "") -> BackgroundRoute:
    """Decide where a background AI request for ``user_id`` would go.

    Cheap and side-effect free apart from a once-per-process log line: reads
    settings, consent and the desktop account; it never makes a network call.
    """
    route = _resolve_background_route(user_id)
    _log_route_once(route)
    return route


def _resolve_background_route(user_id: str) -> BackgroundRoute:
    ai_source = _resolve_background_ai_source(user_id)

    if ai_source == "codex":
        try:
            from services.llm.codex_auth import is_codex_available
        except ImportError:
            return _unavailable(
                ai_source,
                "Codex background LLM unavailable: Codex auth support is not importable.",
                CodexBackgroundUnavailable,
                user_id=user_id,
            )
        try:
            codex_ready = bool(is_codex_available())
        except (OSError, RuntimeError, ValueError):
            codex_ready = False
        if not codex_ready:
            return _unavailable(
                ai_source,
                "Codex background LLM unavailable: ai_source=codex requires a valid Codex CLI sign-in; "
                "refusing to fall through to api.openai.com.",
                CodexBackgroundUnavailable,
                user_id=user_id,
            )
        return BackgroundRoute(kind=ROUTE_CODEX, ai_source=ai_source, user_id=user_id)

    if ai_source == "byok":
        if _byok_provider_name(user_id) in _BYOK_OPENAI_PROVIDERS:
            try:
                _byok_client_config(user_id)
            except BackgroundLLMUnavailable as exc:
                return _unavailable(ai_source, str(exc), type(exc), user_id=user_id)
            return BackgroundRoute(kind=ROUTE_BYOK, ai_source=ai_source, user_id=user_id)
        if not _byok_api_key(user_id):
            return _unavailable(ai_source, "No BYOK API key for background request", user_id=user_id)
        return BackgroundRoute(kind=ROUTE_BYOK_PROVIDER, ai_source=ai_source, user_id=user_id)

    if ai_source == "local":
        return BackgroundRoute(kind=ROUTE_LOCAL, ai_source=ai_source, user_id=user_id)

    if ai_source in _OPENAI_DIRECT_AI_SOURCES:
        return _resolve_managed_route(ai_source, user_id)

    return _unavailable(
        ai_source,
        "Background Responses requests are unavailable for ai_source='%s'; "
        "refusing to use an OpenAI-direct fallback." % ai_source,
        UnsupportedBackgroundAISource,
        user_id=user_id,
    )


def is_background_ai_available(user_id: str = "") -> bool:
    """True when a background AI call for ``user_id`` has somewhere to go.

    That is: a local OpenAI key, OR Viola-managed AI with a signed-in account
    and cloud-AI consent, OR BYOK with a key, OR a local model, OR a working
    Codex sign-in.
    """
    try:
        return resolve_background_route(user_id).available
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Background AI availability check failed; treating as unavailable", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text

    # Import lazily to avoid pulling services.llm into module import paths.
    from services.llm.openai_utils import extract_message_text_from_response

    return extract_message_text_from_response(response)


def _content_has_images(content: BackgroundContent) -> bool:
    if isinstance(content, str):
        return False
    return any(isinstance(part, dict) and part.get("type") in {"input_image", "image", "image_url"} for part in content)


def _text_only_content(content: BackgroundContent) -> str:
    if isinstance(content, str):
        return content
    if _content_has_images(content):
        raise UnsupportedBackgroundAISource(
            "Image input is not available for this AI source.",
            user_message="Screen analysis isn't available with your current AI source.",
        )
    return "\n".join(
        str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("text") is not None
    )


def _record_background_usage(
    *,
    usage: Any,
    model: str,
    latency_ms: int,
    request_type: str,
    user_id: str,
    session_id: str,
) -> None:
    # F-054: bridge background compaction/extraction usage into the
    # session cost tracker so the dollars the reservation enforces
    # are visible to ``/cost`` and the exit-summary hook. Without this
    # the spend was enforceable but invisible.
    try:
        from admin.instrumentation import record_llm_call

        record_llm_call(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(usage, "cached_tokens", 0) or 0),
            cache_write_tokens=int(getattr(usage, "cache_write_tokens", 0) or 0),
            model=model,
            latency_ms=latency_ms,
            request_type=request_type,
            user_id=user_id,
            session_id=session_id,
        )
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Background OpenAI usage instrumentation skipped", exc_info=True)


def _cloud_result_text(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    if result.get("type") not in {None, "answer"}:
        return ""
    for key in ("answer", "content"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


async def _run_cloud_managed(
    *,
    route: BackgroundRoute,
    system_prompt: str,
    content: BackgroundContent,
    max_output_tokens: int,
    model: str,
    timeout_s: float,
) -> tuple[str, Any]:
    """Forward one background request through Viola's cloud as ``route.user_id``.

    The cloud (``/v1/llm/native``) authenticates the GoTrue bearer, reserves
    and settles the request against that user's managed spend cap, and runs it
    on the server-held key. Nothing is reserved locally (the cloud owns the
    meter), and no key is ever written to the desktop.
    """
    from core.exceptions import LLMQuotaExceededError, ProviderUnavailableError
    from services.llm.spend_accounting import (
        LlmTokenUsage,
        estimate_openai_payload_usage,
        usage_from_openai_usage,
    )

    try:
        from services.llm.providers.base import LLMConfig, LLMProviderType
        from services.llm.providers.cloud_managed_provider import (
            CloudManagedProvider,
            CloudManagedSignInRequired,
        )
    except ImportError as exc:
        raise UnsupportedBackgroundAISource("Managed cloud forward is not part of this build.") from exc

    provider = CloudManagedProvider(
        LLMConfig(provider=LLMProviderType.OPENAI.value, api_key=None, model=model, base_url=None),
        cloud_url=_cloud_url(),
    )
    messages = [{"role": "user", "content": content}]
    estimated: LlmTokenUsage = estimate_openai_payload_usage(
        {"instructions": system_prompt, "input": messages, "max_output_tokens": max_output_tokens},
        default_output_tokens=max_output_tokens,
    )

    try:
        result = await provider.forward_background_request(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_output_tokens,
            model=model,
            expected_user_id=route.user_id,
            timeout_s=timeout_s,
        )
    except CloudManagedSignInRequired as exc:
        raise BackgroundLLMSignInRequired(str(exc)) from exc
    except ProviderUnavailableError as exc:
        raise BackgroundLLMUnavailable(
            str(exc),
            user_message="Viola's cloud AI couldn't be reached. Please try again shortly.",
        ) from exc
    except LLMQuotaExceededError as exc:
        # The managed spend cap refused it (ManagedLlmSpendCapError).
        public = str(getattr(exc, "public_message", "") or "")
        raise BackgroundLLMLimitReached(str(exc), user_message=public or None) from exc

    if isinstance(result, dict) and result.get("_policy_denial"):
        message = str(result.get("answer") or "")
        raise BackgroundLLMLimitReached(
            "Managed cloud AI refused the background request: %s" % (result.get("error") or "policy_denial"),
            user_message=message or None,
        )
    text = _cloud_result_text(result)
    usage = usage_from_openai_usage(result.get("_usage") if isinstance(result, dict) else None, estimated)
    return text, usage


def _create_settings_provider(kind: str, user_id: str) -> Any:
    """Build the user's own LLM provider (BYOK non-OpenAI, or local model)."""
    from services.llm.factory import LLMProviderFactory

    scoped = _UserScopedSettings(user_id)
    if kind == ROUTE_LOCAL:
        return LLMProviderFactory._create_local_provider(scoped)
    return LLMProviderFactory._create_byok_provider(scoped)


async def _run_provider_ask(
    *,
    route: BackgroundRoute,
    system_prompt: str,
    content: BackgroundContent,
    max_output_tokens: int,
    timeout_s: float,
) -> tuple[str, Any, str]:
    """Run a text-only background request on the user's own provider."""
    from services.llm.spend_accounting import (
        LlmSpendReservation,
        estimate_openai_payload_usage,
    )

    text_content = _text_only_content(content)
    try:
        provider = await asyncio.to_thread(_create_settings_provider, route.kind, route.user_id)
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise BackgroundLLMUnavailable("Could not create the %s provider: %s" % (route.kind, exc)) from exc
    if provider is None:
        raise BackgroundLLMUnavailable(
            "No usable %s provider for the background request" % route.kind,
            user_message=(
                "Your local AI server isn't reachable."
                if route.kind == ROUTE_LOCAL
                else "Check your API key in Settings."
            ),
        )
    model = str(getattr(getattr(provider, "config", None), "model", "") or route.kind)
    estimated = estimate_openai_payload_usage(
        {"instructions": system_prompt, "input": text_content, "max_output_tokens": max_output_tokens},
        default_output_tokens=max_output_tokens,
    )
    reservation = LlmSpendReservation(
        user_id=route.user_id,
        model=model,
        estimated_usage=estimated,
        operation="background_provider_ask",
        provider=provider,
    )
    await reservation.reserve()
    try:
        result = await asyncio.wait_for(
            provider.ask(
                text_content,
                system_prompt=system_prompt,
                include_history=False,
                max_tokens=max_output_tokens,
                temperature=0.0,
            ),
            timeout=timeout_s,
        )
    except BaseException:
        await reservation.settle(failed=True)
        raise
    if isinstance(result, dict):
        error = result.get("error")
        text = result.get("content") or result.get("answer") or ""
    else:
        error = None
        text = result if isinstance(result, str) else ""
    usage = estimated
    if error:
        await reservation.settle(failed=True)
        raise BackgroundLLMUnavailable("Background %s request failed: %s" % (route.kind, error))
    await reservation.settle(usage)
    return str(text or ""), usage, model


async def run_background_openai_response(
    *,
    system_prompt: str,
    user_content: BackgroundContent,
    max_output_tokens: int,
    user_id: str = "",
    session_id: str = "",
    timeout_s: float = 30.0,
    model: str | None = None,
    operation: str = "background_openai_response",
    request_type: str = "background_compaction",
) -> str:
    """Run a small background Responses request with GPT-5-safe params.

    Compaction and fact-extraction callers share this helper so request-shape
    changes for GPT-5-family models are handled in one place. The route is
    chosen by ``resolve_background_route``; an unavailable route raises a typed
    ``BackgroundLLMUnavailable`` before anything is sent.
    """
    import time as _time

    route = resolve_background_route(user_id)
    if not route.available:
        raise route.unavailable_error()

    ai_source = route.ai_source
    model = _resolve_background_model(ai_source=ai_source, requested_model=model, user_id=user_id)

    if route.kind == ROUTE_CLOUD_MANAGED:
        start_time = _time.time()
        content, usage = await _run_cloud_managed(
            route=route,
            system_prompt=system_prompt,
            content=user_content,
            max_output_tokens=max_output_tokens,
            model=model,
            timeout_s=timeout_s,
        )
        _record_background_usage(
            usage=usage,
            model=model,
            latency_ms=round((_time.time() - start_time) * 1000),
            request_type=request_type,
            user_id=route.user_id,
            session_id=session_id,
        )
        if not content:
            raise RuntimeError("Empty text from background cloud managed request")
        return content

    if route.kind in {ROUTE_LOCAL, ROUTE_BYOK_PROVIDER}:
        start_time = _time.time()
        content, usage, provider_model = await _run_provider_ask(
            route=route,
            system_prompt=system_prompt,
            content=user_content,
            max_output_tokens=max_output_tokens,
            timeout_s=timeout_s,
        )
        _record_background_usage(
            usage=usage,
            model=provider_model,
            latency_ms=round((_time.time() - start_time) * 1000),
            request_type=request_type,
            user_id=user_id,
            session_id=session_id,
        )
        if not content:
            raise RuntimeError("Empty text from background %s request" % route.kind)
        return content

    from services.llm.openai_consent import enforce_storage_consent

    payload: dict[str, Any] = {
        "model": model,
        "instructions": system_prompt,
        "input": [{"role": "user", "content": user_content}],
        "max_output_tokens": max_output_tokens,
    }
    if _is_reasoning_model(model):
        payload["reasoning"] = _reasoning_payload(ai_source=ai_source, model=model)
    else:
        payload["temperature"] = 0.0
    payload.setdefault("store", True)
    enforce_storage_consent(payload, user_id=user_id)
    client = _create_background_client(ai_source=ai_source, timeout_s=timeout_s, user_id=user_id)

    from services.llm.spend_accounting import (
        LlmSpendReservation,
        estimate_openai_payload_usage,
        usage_from_openai_response,
    )

    estimated_usage = estimate_openai_payload_usage(payload, default_output_tokens=max_output_tokens)
    reservation = LlmSpendReservation(
        user_id=user_id,
        model=model,
        estimated_usage=estimated_usage,
        operation=operation,
    )
    await reservation.reserve()

    start_time = _time.time()
    try:
        response = await client.responses.create(**payload)
    except Exception:
        await reservation.settle(failed=True)
        raise

    final_usage = usage_from_openai_response(response, estimated_usage)
    await reservation.settle(final_usage)
    latency_ms = round((_time.time() - start_time) * 1000)

    _record_background_usage(
        usage=final_usage,
        model=model,
        latency_ms=latency_ms,
        request_type=request_type,
        user_id=user_id,
        session_id=session_id,
    )

    content = _response_text(response)
    if not content:
        raise RuntimeError("Empty text from background OpenAI Responses request")
    return content


__all__ = [
    "ROUTE_BYOK",
    "ROUTE_BYOK_PROVIDER",
    "ROUTE_CLOUD_MANAGED",
    "ROUTE_CODEX",
    "ROUTE_LOCAL",
    "ROUTE_OPENAI_DIRECT",
    "ROUTE_UNAVAILABLE",
    "BackgroundContent",
    "BackgroundLLMConsentRequired",
    "BackgroundLLMLimitReached",
    "BackgroundLLMSignInRequired",
    "BackgroundLLMUnavailable",
    "BackgroundRoute",
    "CodexBackgroundUnavailable",
    "UnsupportedBackgroundAISource",
    "is_background_ai_available",
    "resolve_background_route",
    "run_background_openai_response",
]
