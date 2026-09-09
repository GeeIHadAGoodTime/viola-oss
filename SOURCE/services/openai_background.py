"""Shared low-cost Responses helpers for background summarization/extraction."""

from __future__ import annotations

from typing import Any

from config.defaults import (
    DEFAULT_AI_SOURCE,
    DEFAULT_BACKGROUND_TASK_MODEL,
    DEFAULT_CODEX_MODEL,
)
from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)

_OPENAI_DIRECT_AI_SOURCES = frozenset({"", "openai", "managed", "subscription"})
_CODEX_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_BYOK_OPENAI_PROVIDERS = frozenset({"openai", "openai_compatible"})


class BackgroundLLMUnavailable(RuntimeError):
    """Background LLM request cannot run for the selected AI source."""


class CodexBackgroundUnavailable(BackgroundLLMUnavailable):
    """Codex background routing was selected but Codex auth is unavailable."""


class UnsupportedBackgroundAISource(BackgroundLLMUnavailable):
    """Background routing for the selected AI source has not been wired."""


def _is_reasoning_model(model: str) -> bool:
    normalized = model.lower()
    return any(tag in normalized for tag in ("gpt-5", "o1", "o3", "o4-"))


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


def _resolve_background_model(*, ai_source: str, requested_model: str | None, user_id: str) -> str:
    requested = requested_model.strip() if isinstance(requested_model, str) else ""
    if ai_source == "codex" and requested in {"", DEFAULT_BACKGROUND_TASK_MODEL}:
        return _default_codex_background_model(user_id)
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


def _byok_client_config(user_id: str) -> tuple[str, str]:
    provider_raw = _settings_manager_value("llm_provider", "openai", user_id=user_id)
    provider = provider_raw.strip().lower() if isinstance(provider_raw, str) else "openai"
    if provider not in _BYOK_OPENAI_PROVIDERS:
        raise UnsupportedBackgroundAISource(
            "Background Responses requests are unavailable for ai_source=byok "
            "with llm_provider='%s'; refusing to use the shared OpenAI key." % provider
        )

    api_key_raw = _settings_manager_value("llm_api_key", "", user_id=user_id)
    api_key = api_key_raw.strip() if isinstance(api_key_raw, str) else ""
    if not api_key or api_key == "***ENCRYPTED***":
        legacy_raw = _settings_manager_value("openai_api_key", "", user_id=user_id)
        api_key = legacy_raw.strip() if isinstance(legacy_raw, str) else ""
    if not api_key or api_key == "***ENCRYPTED***":
        raise BackgroundLLMUnavailable("No BYOK API key for background Responses request")

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


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return output_text

    # Import lazily to avoid pulling services.llm into module import paths.
    from services.llm.openai_utils import extract_message_text_from_response

    return extract_message_text_from_response(response)


async def run_background_openai_response(
    *,
    system_prompt: str,
    user_content: str,
    max_output_tokens: int,
    user_id: str = "",
    session_id: str = "",
    timeout_s: float = 30.0,
    model: str | None = None,
) -> str:
    """Run a small background Responses request with GPT-5-safe params.

    Compaction and fact-extraction callers share this helper so request-shape
    changes for GPT-5-family models are handled in one place.
    """
    from services.llm.openai_consent import enforce_storage_consent

    ai_source = _resolve_background_ai_source(user_id)
    model = _resolve_background_model(ai_source=ai_source, requested_model=model, user_id=user_id)
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
    enforce_storage_consent(payload)
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
        operation="background_openai_response",
    )
    await reservation.reserve()

    import time as _time

    start_time = _time.time()
    try:
        response = await client.responses.create(**payload)
    except Exception:
        await reservation.settle(failed=True)
        raise

    final_usage = usage_from_openai_response(response, estimated_usage)
    await reservation.settle(final_usage)
    latency_ms = round((_time.time() - start_time) * 1000)

    # F-054: bridge background compaction/extraction usage into the
    # session cost tracker so the dollars the reservation enforces
    # are visible to ``/cost`` and the exit-summary hook. Without this
    # the spend was enforceable but invisible.
    try:
        from admin.instrumentation import record_llm_call

        record_llm_call(
            input_tokens=int(getattr(final_usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(final_usage, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(final_usage, "cached_tokens", 0) or 0),
            cache_write_tokens=int(getattr(final_usage, "cache_write_tokens", 0) or 0),
            model=model,
            latency_ms=latency_ms,
            request_type="background_compaction",
            user_id=user_id,
            session_id=session_id,
        )
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Background OpenAI usage instrumentation skipped", exc_info=True)

    content = _response_text(response)
    if not content:
        raise RuntimeError("Empty text from background OpenAI Responses request")
    return content


__all__ = [
    "BackgroundLLMUnavailable",
    "CodexBackgroundUnavailable",
    "UnsupportedBackgroundAISource",
    "run_background_openai_response",
]
