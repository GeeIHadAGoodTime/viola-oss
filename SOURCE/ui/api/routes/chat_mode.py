"""Threaded ChatMode endpoints for the React stage UI."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.json_types import to_json_value
from core.logging_config import get_logger
from fastapi import Depends, HTTPException, Query, Request
from intent.log_redaction import redact_diagnostic_payload
from services.persistence.chat_store import (
    ChatMessageRecord,
    ChatStore,
    ChatThreadRecord,
    get_chat_store,
)
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox
from ui.core.security import is_desktop_surface, is_loopback_request
from ui.security.config import get_security_config

logger = get_logger(__name__)


@dataclass(slots=True)
class _ActiveChatTask:
    user_id: str
    thread_id: str
    task: asyncio.Task[None]


class CreateThreadRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=120)


class UpdateThreadRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=120)


class SendMessageRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=120)


class FeedbackRequest(BaseModel):
    rating: str | None = Field(default=None, max_length=20)


class ForkMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=120)


class RegenerateMessageRequest(BaseModel):
    model: str | None = Field(default=None, max_length=120)


_ACTIVE_CHAT_TASKS: dict[str, _ActiveChatTask] = {}
_CHAT_TOOL_EVENT_SAFE_KEYS = frozenset(
    {
        "tool_name",
        "name",
        "status",
        "progress",
        "message",
        "step_number",
        "step",
        "duration_ms",
        "stream_id",
        "thread_id",
    }
)
_CHAT_TOOL_EVENT_REDACT_KEYS = frozenset({"progress", "message"})


def _thread_payload(thread: ChatThreadRecord) -> dict[str, Any]:
    return thread.to_payload()


def _message_payload(message: ChatMessageRecord) -> dict[str, Any]:
    payload = message.to_payload()
    metadata = payload.get("metadata")
    tools: Any = None
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        tools = _sanitize_chat_tool_events(metadata.get("tools"))
        metadata["tools"] = tools
        payload["metadata"] = metadata
    payload["tools"] = tools if isinstance(tools, list) else []
    return payload


def _sanitize_chat_tool_event(event: object) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        return {}
    sanitized: dict[str, Any] = {}
    for key in _CHAT_TOOL_EVENT_SAFE_KEYS:
        if key not in event:
            continue
        value = event[key]
        if key in _CHAT_TOOL_EVENT_REDACT_KEYS:
            value = redact_diagnostic_payload(value)
        sanitized[key] = to_json_value(value)
    if "tool_name" not in sanitized and isinstance(sanitized.get("name"), str):
        sanitized["tool_name"] = sanitized["name"]
    return sanitized


def _sanitize_chat_tool_events(events: object) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        return []
    sanitized: list[dict[str, Any]] = []
    for event in events[-100:]:
        item = _sanitize_chat_tool_event(event)
        if item:
            sanitized.append(item)
    return sanitized


def _auto_title(text: str) -> str:
    words = [part.strip(" \t\r\n.,;:!?()[]{}") for part in str(text or "").split()]
    clean_words = [word for word in words if word]
    if not clean_words:
        return "New chat"
    title = " ".join(clean_words[:8])
    if len(title) > 72:
        title = "%s..." % title[:69]
    return title[0].upper() + title[1:]


def _chat_stream_id() -> str:
    return "chat-%s" % uuid.uuid4()


def _command_stream_message(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        return ""
    data = envelope.get("data")
    if isinstance(data, dict):
        for key in ("message", "response", "text", "answer"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for key in ("message", "response", "text"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _history_from_messages(
    messages: list[ChatMessageRecord],
) -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for message in messages:
        if message.role not in {"system", "user", "assistant"}:
            continue
        if not message.content.strip():
            continue
        history.append({"role": message.role, "content": message.content})
    return history


def _unique_models(candidates: list[str]) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        model = str(candidate or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def _provider_prefix_valid(provider_id: str, model: str) -> bool:
    normalized_provider = str(provider_id or "").strip().lower()
    normalized_model = str(model or "").strip().lower()
    if not normalized_model:
        return False
    if normalized_provider in {"openai", "managed", "codex"}:
        return normalized_model.startswith(("gpt-", "o1-", "o3-", "chatgpt-", "ft:"))
    if normalized_provider == "anthropic":
        return normalized_model.startswith("claude")
    if normalized_provider == "google":
        return normalized_model.startswith("gemini")
    return True


def _chat_model_catalog(user_id: str) -> dict[str, Any]:
    from config.defaults import (
        DEFAULT_AI_SOURCE,
        DEFAULT_CODEX_MODEL,
        DEFAULT_GPT_MODEL,
        DEFAULT_LOCAL_LLM_MODEL,
        resolve_effective_model,
    )
    from services.llm.providers.base import PROVIDER_INFO
    from ui.settings_manager import get_settings_manager

    settings_mgr = get_settings_manager()
    ai_source = str(settings_mgr.get("ai_source", DEFAULT_AI_SOURCE, user_id=user_id) or DEFAULT_AI_SOURCE)
    provider = str(settings_mgr.get("llm_provider", "openai", user_id=user_id) or "openai")
    configured_model = str(settings_mgr.get("llm_model", "", user_id=user_id) or "")

    provider_id = provider
    provider_name = provider
    models: list[str] = []
    default_model = ""
    native_streaming = True

    if ai_source == "codex":
        provider_id = "codex"
        provider_name = "Codex"
        default_model = DEFAULT_CODEX_MODEL
        try:
            from services.llm.codex_auth import get_codex_models

            models = get_codex_models()
        except Exception:
            logger.debug("Could not read Codex model cache", exc_info=True)
            models = [DEFAULT_CODEX_MODEL, "gpt-5.4-mini"]
    elif ai_source in {"managed", "subscription"}:
        provider_id = "managed"
        provider_name = "Viola Managed AI"
        default_model = DEFAULT_GPT_MODEL
        provider_info = PROVIDER_INFO.get("openai")
        if provider_info is not None:
            models = list(provider_info.popular_models or provider_info.default_models)
    elif ai_source == "local":
        provider_id = "local"
        provider_name = "Local"
        default_model = str(
            settings_mgr.get("llm_model", "", user_id=user_id)
            or settings_mgr.get("local_llm_model", DEFAULT_LOCAL_LLM_MODEL, user_id=user_id)
            or ""
        )
        models = [default_model]
        native_streaming = False
    else:
        provider_info = PROVIDER_INFO.get(provider)
        if provider_info is not None:
            provider_name = provider_info.name
            models = list(provider_info.popular_models or provider_info.default_models)
        default_model = resolve_effective_model(ai_source=ai_source, provider=provider, agent=False)

    models = _unique_models([configured_model, default_model, *models])
    valid_models = [model for model in models if _provider_prefix_valid(provider_id, model)]
    current_model = configured_model if configured_model in valid_models else ""
    if not current_model:
        current_model = default_model if default_model in valid_models else (valid_models[0] if valid_models else "")

    return {
        "ai_source": ai_source,
        "provider": provider_id,
        "provider_name": provider_name,
        "current_model": current_model,
        "models": valid_models,
        "native_streaming": native_streaming,
        "providers": [
            {
                "id": provider_id,
                "name": provider_name,
                "models": valid_models,
                "default_models": [default_model] if default_model else [],
                "native_streaming": native_streaming,
            }
        ],
    }


def _validate_chat_model(user_id: str, model: str | None, *, explicit: bool) -> str | None:
    requested = str(model or "").strip()
    if not requested:
        return None
    catalog = _chat_model_catalog(user_id)
    valid_models = set(catalog.get("models") or [])
    provider = str(catalog.get("provider") or "")
    if requested in valid_models or _provider_prefix_valid(provider, requested):
        return requested
    if explicit:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=failure_response(
                "invalid_chat_model",
                "The selected model is not valid for the active provider.",
            ),
        )
    return None


def _require_request_user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()

    user_context = getattr(request.state, "user_context", None)
    context_user_id = getattr(user_context, "user_id", None)
    if isinstance(context_user_id, str) and context_user_id.strip():
        return context_user_id.strip()

    security = get_security_config()
    if is_desktop_surface() and is_loopback_request(request):
        # #2646 / M-BILL-1 (#337): chat-mode selects the AI provider (managed vs
        # BYOK) from the resolved principal's ai_source setting, so it must prefer
        # the signed-in desktop account over the anonymous device id. Signed-out
        # installs still resolve the device id.
        if not security.auth_enabled:
            from core.user_context import get_current_or_desktop_active_user_id

            return get_current_or_desktop_active_user_id()
        api_key = request.headers.get("X-API-Key")
        if api_key and security.auth_api_key and hmac.compare_digest(api_key, security.auth_api_key):
            from core.user_context import get_current_or_desktop_active_user_id

            return get_current_or_desktop_active_user_id()

    logger.warning(
        "Authenticated ChatMode route missing user principal: method=%s path=%s",
        request.method,
        request.url.path,
    )
    raise HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail=failure_response(
            "authenticated_user_required",
            "Authentication is required for chat.",
        ),
    )


def _get_command_service(context: ApiContext) -> Any:
    if context.command_service is not None:
        return context.command_service
    existing = getattr(context.app.state, "command_service", None)
    if existing is not None:
        return existing

    from services.command import CommandService, CommandServiceContext

    service = CommandService(
        context=CommandServiceContext(
            state=context.bindings.state,
            music=context.bindings.music,
            intent=context.bindings.intent,
            hub=context.hub,
            bindings=context.bindings,
            commands_total=context.commands_total,
            play_events_total=context.play_events_total,
        )
    )
    context.app.state.command_service = service
    return service


async def _store() -> ChatStore:
    store = get_chat_store()
    await store.initialize()
    return store


async def _clear_chat_thread_conversation_mirror(user_id: str, thread_id: str) -> int:
    from services.conversation.state_manager import get_conversation_persistence

    persistence = get_conversation_persistence()
    return await asyncio.to_thread(persistence.clear_history, user_id, thread_id)


def _start_chat_generation(
    *,
    context: ApiContext,
    store: ChatStore,
    user_id: str,
    thread_id: str,
    text: str,
    model: str | None,
    history_messages: list[ChatMessageRecord],
    target_assistant_message_id: str | None = None,
    stream_id: str | None = None,
) -> str:
    from services.llm.stream_bus import bind_stream_producer

    active_stream_id = stream_id or _chat_stream_id()
    bind_stream_producer(active_stream_id, owner_id=user_id)
    task = asyncio.create_task(
        _run_chat_command(
            context=context,
            store=store,
            user_id=user_id,
            thread_id=thread_id,
            stream_id=active_stream_id,
            text=text,
            model=model,
            history_messages=history_messages,
            target_assistant_message_id=target_assistant_message_id,
        )
    )
    _ACTIVE_CHAT_TASKS[active_stream_id] = _ActiveChatTask(user_id=user_id, thread_id=thread_id, task=task)
    return active_stream_id


async def _run_chat_command(
    *,
    context: ApiContext,
    store: ChatStore,
    user_id: str,
    thread_id: str,
    stream_id: str,
    text: str,
    model: str | None,
    history_messages: list[ChatMessageRecord],
    target_assistant_message_id: str | None = None,
) -> None:
    from services.command import CommandRequest
    from services.conversation.state_manager import ConversationStateManager
    from services.llm.stream_bus import (
        command_stream_context,
        finalize_stream,
        get_stream_token_count,
        get_stream_tool_events,
    )

    model_token = None
    try:
        if model:
            from intent.ai_controller import _ctx_agent_model_override

            model_token = _ctx_agent_model_override.set(model)

        command_service = _get_command_service(context)
        request = CommandRequest(
            text=text,
            history=_history_from_messages(history_messages),
            user_id=user_id,
            channel={
                "type": "chat_mode",
                "thread_id": thread_id,
                "stream_id": stream_id,
            },
            trace_id=stream_id,
            origin="typed",
        )
        started = time.time()
        with command_stream_context(stream_id, metadata={"thread_id": thread_id}):
            result = await command_service.execute(request)
        envelope = result.to_envelope()
        assistant_text = _command_stream_message(envelope)
        if not assistant_text:
            assistant_text = str(result.error or "I could not produce a response.")
        token_count = get_stream_token_count(stream_id)
        streaming_mode = "native" if token_count > 0 else "fallback_final"
        metadata = {
            "intent": result.intent,
            "ok": result.ok,
            "policy_flags": result.policy_flags,
            "stream_id": stream_id,
            "elapsed_ms": round((time.time() - started) * 1000),
            "streaming": {
                "mode": streaming_mode,
                "token_count": token_count,
                "native": token_count > 0,
            },
            "regenerating": False,
        }
        metadata["tools"] = _sanitize_chat_tool_events(get_stream_tool_events(stream_id))
        if target_assistant_message_id:
            assistant_message = await store.update_message(
                user_id,
                thread_id,
                target_assistant_message_id,
                content=assistant_text,
                metadata_patch=metadata,
                status="complete",
            )
            if assistant_message is None:
                assistant_message = await store.append_message(
                    user_id,
                    thread_id,
                    role="assistant",
                    content=assistant_text,
                    metadata=metadata,
                )
        else:
            assistant_message = await store.append_message(
                user_id,
                thread_id,
                role="assistant",
                content=assistant_text,
                metadata=metadata,
            )
        try:
            thread_manager = ConversationStateManager(user_id=user_id, session_id=thread_id, restore_turns=80)
            thread_manager.record_exchange(
                text,
                assistant_text,
                intent=result.intent,
                params={
                    "chat_thread_id": thread_id,
                    "assistant_message_id": assistant_message.id,
                },
            )
        except Exception:
            logger.debug("ChatMode conversation state mirror failed", exc_info=True)
        finalize_stream(
            stream_id,
            content=assistant_text,
            streaming_mode=streaming_mode,
            token_count=token_count,
            fallback=token_count == 0,
        )
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            if target_assistant_message_id:
                await store.update_message(
                    user_id,
                    thread_id,
                    target_assistant_message_id,
                    content="Stopped.",
                    metadata_patch={
                        "stream_id": stream_id,
                        "regenerating": False,
                        "tools": [],
                    },
                    status="stopped",
                )
            else:
                await store.append_message(
                    user_id,
                    thread_id,
                    role="assistant",
                    content="Stopped.",
                    metadata={"stream_id": stream_id},
                    status="stopped",
                )
        finalize_stream(stream_id, error=True, message="Stopped.")
        raise
    except Exception:
        logger.exception(
            "ChatMode command execution failed for user=%s thread=%s",
            user_id,
            thread_id,
        )
        with contextlib.suppress(Exception):
            if target_assistant_message_id:
                await store.update_message(
                    user_id,
                    thread_id,
                    target_assistant_message_id,
                    content="Something went wrong while generating the response.",
                    metadata_patch={
                        "stream_id": stream_id,
                        "regenerating": False,
                        "tools": [],
                    },
                    status="error",
                )
            else:
                await store.append_message(
                    user_id,
                    thread_id,
                    role="assistant",
                    content="Something went wrong while generating the response.",
                    metadata={"stream_id": stream_id},
                    status="error",
                )
        finalize_stream(
            stream_id,
            error=True,
            message="Something went wrong while generating the response.",
        )
    finally:
        if model_token is not None:
            from intent.ai_controller import _ctx_agent_model_override

            _ctx_agent_model_override.reset(model_token)
        _ACTIVE_CHAT_TASKS.pop(stream_id, None)


def _not_found(name: str) -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.NOT_FOUND,
        detail=failure_response("%s_not_found" % name, "%s not found." % name.replace("_", " ").title()),
    )


def register_chat_mode_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register ChatMode routes on the feature router."""
    router = context.router

    @router.get("/v1/chat/models", dependencies=[Depends(require_auth)])
    async def chat_models(request: Request) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            return success_response(_chat_model_catalog(user_id))

        return await toolbox.record_and_call(_inner, route="/v1/chat/models", method="GET")

    @router.get("/v1/chat/threads", dependencies=[Depends(require_auth)])
    async def list_threads(
        request: Request,
        search: str = Query(default="", max_length=200),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            threads = await chat_store.list_threads(user_id, search=search, limit=limit)
            return success_response({"threads": [_thread_payload(thread) for thread in threads]})

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads", method="GET")

    @router.post("/v1/chat/threads", dependencies=[Depends(require_auth)])
    async def create_thread(request: Request, body: CreateThreadRequest) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            model = _validate_chat_model(user_id, body.model, explicit=bool(body.model))
            thread = await chat_store.create_thread(
                user_id,
                title=body.title or "New chat",
                model=model,
            )
            return success_response({"thread": _thread_payload(thread)})

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads", method="POST")

    @router.get("/v1/chat/threads/{thread_id}", dependencies=[Depends(require_auth)])
    async def get_thread(request: Request, thread_id: str) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            messages = await chat_store.list_messages(user_id, thread_id)
            return success_response(
                {
                    "thread": _thread_payload(thread),
                    "messages": [_message_payload(message) for message in messages],
                }
            )

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads/{thread_id}", method="GET")

    @router.patch("/v1/chat/threads/{thread_id}", dependencies=[Depends(require_auth)])
    async def update_thread(request: Request, thread_id: str, body: UpdateThreadRequest) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            if body.title is not None:
                thread = await chat_store.rename_thread(user_id, thread_id, body.title)
            if body.model is not None:
                model = _validate_chat_model(user_id, body.model, explicit=bool(body.model))
                thread = await chat_store.set_thread_model(user_id, thread_id, model)
            if thread is None:
                raise _not_found("chat_thread")
            return success_response({"thread": _thread_payload(thread)})

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads/{thread_id}", method="PATCH")

    @router.delete("/v1/chat/threads/{thread_id}", dependencies=[Depends(require_auth)])
    async def delete_thread(request: Request, thread_id: str) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            try:
                await _clear_chat_thread_conversation_mirror(user_id, thread_id)
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                logger.exception(
                    "ChatMode conversation mirror delete failed for user=%s thread=%s",
                    user_id,
                    thread_id,
                )
                raise HTTPException(
                    status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                    detail=failure_response(
                        "chat_delete_incomplete",
                        "Could not delete all local copies of this chat. Please retry.",
                    ),
                ) from None
            deleted = await chat_store.delete_thread(user_id, thread_id)
            if not deleted:
                raise _not_found("chat_thread")
            return success_response({"deleted": True})

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads/{thread_id}", method="DELETE")

    @router.get("/v1/chat/threads/{thread_id}/messages", dependencies=[Depends(require_auth)])
    async def list_messages(request: Request, thread_id: str, limit: int = Query(default=200, ge=1, le=500)) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            messages = await chat_store.list_messages(user_id, thread_id, limit=limit)
            return success_response({"messages": [_message_payload(message) for message in messages]})

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads/{thread_id}/messages", method="GET")

    @router.post("/v1/chat/threads/{thread_id}/send", dependencies=[Depends(require_auth)])
    async def send_message(request: Request, thread_id: str, body: SendMessageRequest) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            existing_messages = await chat_store.list_messages(user_id, thread_id, limit=500)
            selected_model = _validate_chat_model(user_id, body.model or thread.model, explicit=bool(body.model))
            if body.model and selected_model != thread.model:
                updated = await chat_store.set_thread_model(user_id, thread_id, body.model)
                if updated is not None:
                    thread = updated
            if not existing_messages and thread.title.strip().lower() == "new chat":
                titled = await chat_store.rename_thread(user_id, thread_id, _auto_title(body.text))
                if titled is not None:
                    thread = titled
            user_message = await chat_store.append_message(
                user_id,
                thread_id,
                role="user",
                content=body.text,
                metadata={"source": "chat_mode"},
            )
            stream_id = _start_chat_generation(
                context=context,
                store=chat_store,
                user_id=user_id,
                thread_id=thread_id,
                text=body.text,
                model=selected_model,
                history_messages=existing_messages,
            )
            return success_response(
                {
                    "stream_id": stream_id,
                    "thread": _thread_payload(thread),
                    "user_message": _message_payload(user_message),
                }
            )

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads/{thread_id}/send", method="POST")

    @router.post("/v1/chat/streams/{stream_id}/cancel", dependencies=[Depends(require_auth)])
    async def cancel_stream(request: Request, stream_id: str) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            active = _ACTIVE_CHAT_TASKS.get(stream_id)
            if active is None:
                return success_response({"cancelled": False})
            if active.user_id != user_id:
                raise HTTPException(
                    status_code=HTTPStatus.FORBIDDEN,
                    detail=failure_response("stream_access_denied", "Stream access denied."),
                )
            if active.task.done():
                _ACTIVE_CHAT_TASKS.pop(stream_id, None)
                return success_response({"cancelled": False})
            cancel_requested = active.task.cancel()
            if cancel_requested is False:
                _ACTIVE_CHAT_TASKS.pop(stream_id, None)
                return success_response({"cancelled": False})
            from services.llm.stream_bus import finalize_stream

            finalize_stream(stream_id, error=True, message="Stopped.")
            return success_response({"cancelled": True})

        return await toolbox.record_and_call(_inner, route="/v1/chat/streams/{stream_id}/cancel", method="POST")

    @router.post(
        "/v1/chat/threads/{thread_id}/regenerate/{message_id}",
        dependencies=[Depends(require_auth)],
    )
    async def regenerate_message(
        request: Request,
        thread_id: str,
        message_id: str,
        body: RegenerateMessageRequest,
    ) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            messages = await chat_store.list_messages(user_id, thread_id, limit=500)
            target_index = next(
                (index for index, item in enumerate(messages) if item.id == message_id),
                -1,
            )
            if target_index < 0:
                raise _not_found("chat_message")
            target_message = messages[target_index]
            if target_message.role != "assistant":
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST,
                    detail=failure_response(
                        "regenerate_requires_assistant_message",
                        "Only assistant messages can be regenerated.",
                    ),
                )
            prior_user_index = next(
                (
                    index
                    for index in range(target_index - 1, -1, -1)
                    if messages[index].role == "user" and messages[index].content.strip()
                ),
                -1,
            )
            if prior_user_index < 0:
                raise HTTPException(
                    status_code=HTTPStatus.BAD_REQUEST,
                    detail=failure_response(
                        "regenerate_missing_user_message",
                        "No prior user message was found for regeneration.",
                    ),
                )
            selected_model = _validate_chat_model(user_id, body.model or thread.model, explicit=bool(body.model))
            if body.model and selected_model != thread.model:
                updated_thread = await chat_store.set_thread_model(user_id, thread_id, selected_model)
                if updated_thread is not None:
                    thread = updated_thread
            await chat_store.delete_messages_after(user_id, thread_id, target_message.id)
            stream_id = _chat_stream_id()
            from services.llm.stream_bus import register_stream

            register_stream(stream_id, owner_id=user_id)
            streaming_message = await chat_store.update_message(
                user_id,
                thread_id,
                target_message.id,
                content="",
                metadata_patch={"stream_id": stream_id, "regenerating": True},
                status="streaming",
            )
            if streaming_message is None:
                raise _not_found("chat_message")
            _start_chat_generation(
                context=context,
                store=chat_store,
                user_id=user_id,
                thread_id=thread_id,
                text=messages[prior_user_index].content,
                model=selected_model,
                history_messages=messages[:prior_user_index],
                target_assistant_message_id=target_message.id,
                stream_id=stream_id,
            )
            refreshed_messages = await chat_store.list_messages(user_id, thread_id, limit=500)
            return success_response(
                {
                    "stream_id": stream_id,
                    "thread": _thread_payload(thread),
                    "message": _message_payload(streaming_message),
                    "messages": [_message_payload(message) for message in refreshed_messages],
                }
            )

        return await toolbox.record_and_call(
            _inner,
            route="/v1/chat/threads/{thread_id}/regenerate/{message_id}",
            method="POST",
        )

    @post_message_feedback(router, toolbox)
    async def _feedback(request: Request, thread_id: str, message_id: str, body: FeedbackRequest) -> dict[str, Any]:
        user_id = _require_request_user_id(request)
        rating = body.rating if body.rating in {"up", "down", None} else None
        chat_store = await _store()
        updated = await chat_store.update_message(
            user_id,
            thread_id,
            message_id,
            metadata_patch={"rating": rating},
        )
        if updated is None:
            raise _not_found("chat_message")
        return success_response({"message": _message_payload(updated)})

    @router.post(
        "/v1/chat/threads/{thread_id}/messages/{message_id}/fork",
        dependencies=[Depends(require_auth)],
    )
    async def fork_message(request: Request, thread_id: str, message_id: str, body: ForkMessageRequest) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            model = _validate_chat_model(user_id, body.model, explicit=bool(body.model))
            try:
                thread, messages = await chat_store.fork_thread(
                    user_id,
                    thread_id,
                    message_id,
                    content=body.content,
                    model=model,
                )
            except KeyError:
                raise _not_found("chat_message") from None
            selected_model = _validate_chat_model(user_id, model or thread.model, explicit=False)
            stream_id = _start_chat_generation(
                context=context,
                store=chat_store,
                user_id=user_id,
                thread_id=thread.id,
                text=messages[-1].content,
                model=selected_model,
                history_messages=messages[:-1],
            )
            return success_response(
                {
                    "stream_id": stream_id,
                    "thread": _thread_payload(thread),
                    "messages": [_message_payload(message) for message in messages],
                }
            )

        return await toolbox.record_and_call(
            _inner,
            route="/v1/chat/threads/{thread_id}/messages/{message_id}/fork",
            method="POST",
        )

    @router.get("/v1/chat/threads/{thread_id}/export", dependencies=[Depends(require_auth)])
    async def export_thread(request: Request, thread_id: str) -> Any:
        async def _inner() -> dict[str, Any]:
            user_id = _require_request_user_id(request)
            chat_store = await _store()
            thread = await chat_store.get_thread(user_id, thread_id)
            if thread is None:
                raise _not_found("chat_thread")
            messages = await chat_store.list_messages(user_id, thread_id, limit=500)
            lines = ["# %s" % thread.title, ""]
            for message in messages:
                role_label = message.role.capitalize()
                lines.extend(["## %s" % role_label, "", message.content, ""])
            filename = "%s.md" % "".join(part if part.isalnum() else "-" for part in thread.title.lower()).strip("-")
            return success_response(
                {
                    "filename": filename or "chat.md",
                    "markdown": "\n".join(lines).strip(),
                }
            )

        return await toolbox.record_and_call(_inner, route="/v1/chat/threads/{thread_id}/export", method="GET")

    logger.info("ChatMode routes registered")


def post_message_feedback(router: Any, toolbox: RouteToolbox) -> Any:
    def _decorator(handler: Any) -> Any:
        @router.post(
            "/v1/chat/threads/{thread_id}/messages/{message_id}/feedback",
            dependencies=[Depends(require_auth)],
        )
        async def feedback_route(request: Request, thread_id: str, message_id: str, body: FeedbackRequest) -> Any:
            async def _inner() -> dict[str, Any]:
                return await handler(request, thread_id, message_id, body)

            return await toolbox.record_and_call(
                _inner,
                route="/v1/chat/threads/{thread_id}/messages/{message_id}/feedback",
                method="POST",
            )

        return feedback_route

    return _decorator


__all__ = ["register_chat_mode_routes"]
