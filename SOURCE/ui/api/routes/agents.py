"""Agent runtime REST routes."""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

from fastapi.responses import JSONResponse

from contracts.api_response import ResponseEnvelope, failure_response, success_response
from core.json_types import to_json_value
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from intent.log_redaction import redact_diagnostic_payload
from ui.api.routes.auth_dependencies import require_auth
from ui.core.security import is_desktop_surface, is_loopback_request
from ui.security.config import get_security_config
from ui.security.rate_limit_utils import configure_router_rate_limits

logger = get_logger(__name__)


def _authenticated_user_id_for_request(request: Request) -> str:
    """Resolve the same stable principal used by /v1/command."""

    user = getattr(request.state, "user", None)
    user_id = getattr(user, "id", None)
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()

    user_context = getattr(request.state, "user_context", None)
    context_user_id = getattr(user_context, "user_id", None)
    if isinstance(context_user_id, str) and context_user_id.strip():
        return context_user_id.strip()

    security = get_security_config()
    desktop_loopback = is_desktop_surface() and is_loopback_request(request)
    if desktop_loopback:
        # #2646 / M-BILL-1 (#337): prefer the signed-in desktop account over the
        # anonymous device principal on the loopback surface, mirroring
        # /v1/command. Signed-out installs still resolve the device id and gate
        # as before.
        if not security.auth_enabled:
            from core.user_context import get_current_or_desktop_active_user_id

            return get_current_or_desktop_active_user_id()
        api_key = request.headers.get("X-API-Key")
        if api_key and security.auth_api_key:
            import hmac as _hmac

            if _hmac.compare_digest(api_key, security.auth_api_key):
                from core.user_context import get_current_or_desktop_active_user_id

                return get_current_or_desktop_active_user_id()

    logger.warning(
        "Authenticated route missing user principal: method=%s path=%s",
        request.method,
        request.url.path,
    )
    raise HTTPException(
        status_code=HTTPStatus.UNAUTHORIZED,
        detail=failure_response(
            "authenticated_user_required",
            "Authentication is required for this command.",
        ),
    )


def _route_exists(app: FastAPI, path: str, methods: set[str]) -> bool:
    for route in getattr(app.router, "routes", []):
        if getattr(route, "path", None) != path:
            continue
        route_methods = getattr(route, "methods", set()) or set()
        if methods.issubset(set(route_methods)):
            return True
    return False


def _agent_registry_from_controller(controller: Any) -> Any | None:
    required_methods = ("list_active", "list_recent_completed", "cancel", "get")
    if all(callable(getattr(controller, name, None)) for name in required_methods):
        return controller
    registry = getattr(controller, "_agent_registry", None) or getattr(controller, "agent_registry", None)
    if registry is None:
        return None
    if all(callable(getattr(registry, name, None)) for name in required_methods):
        return registry
    return None


def _candidate_controllers(app: FastAPI, intent: Any | None) -> Iterable[Any]:
    yield getattr(app.state, "ai_controller", None)

    pipeline = getattr(app.state, "intent_pipeline", None)
    yield getattr(pipeline, "ai_controller", None)

    api_context = getattr(app.state, "api_context", None)
    bindings = getattr(api_context, "bindings", None)
    bound_intent = getattr(bindings, "intent", None)
    bound_pipeline = getattr(bound_intent, "_pipeline", None)
    yield getattr(bound_pipeline, "ai_controller", None)

    yield intent
    yield getattr(intent, "ai_controller", None)
    intent_pipeline = getattr(intent, "_pipeline", None)
    yield getattr(intent_pipeline, "ai_controller", None)

    try:
        ai_controller_module = importlib.import_module("intent.ai_controller")
    except ImportError:
        return
    getter = getattr(ai_controller_module, "get_ai_controller", None)
    if callable(getter):
        yield getter()


def _resolve_agent_registry(app: FastAPI, intent: Any | None) -> Any | None:
    for controller in _candidate_controllers(app, intent):
        if controller is None:
            continue
        registry = _agent_registry_from_controller(controller)
        if registry is not None:
            return registry
    try:
        registry_module = importlib.import_module("services.agent_runtime.registry")
    except ImportError:
        return None
    return _agent_registry_from_controller(registry_module)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _call_registry(method: Any, *args: Any) -> Any:
    return await _maybe_await(method(*args))


def _timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.timestamp()
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = "%s+00:00" % raw[:-1]
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def _json_time(value: Any) -> Any:
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.isoformat()
    return to_json_value(value)


def _seconds_between(start: Any, end: Any) -> float | None:
    start_ts = _timestamp(start)
    end_ts = _timestamp(end)
    if start_ts is None or end_ts is None:
        return None
    return round(max(0.0, end_ts - start_ts), 3)


def _task_user_id(task: Any) -> str | None:
    value = getattr(task, "user_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(task, dict):
        dict_value = task.get("user_id")
        if isinstance(dict_value, str) and dict_value.strip():
            return dict_value.strip()
    return None


_FIELD_ALIASES = {
    "id": ("id", "agent_id"),
    "final_result": ("final_result", "result"),
}


def _field(task: Any, name: str) -> Any:
    names = _FIELD_ALIASES.get(name, (name,))
    if isinstance(task, dict):
        for candidate in names:
            if candidate in task:
                return task.get(candidate)
        return None
    for candidate in names:
        if hasattr(task, candidate):
            return getattr(task, candidate)
    return None


def _owned_tasks(tasks: Iterable[Any] | None, user_id: str) -> list[Any]:
    if tasks is None:
        return []
    return [task for task in tasks if _task_user_id(task) == user_id]


def _serialize_task(task: Any, *, now: datetime) -> dict[str, Any]:
    started_at = _field(task, "started_at")
    completed_at = _field(task, "completed_at")
    end = completed_at if completed_at is not None else now
    completed_at_seconds_ago = None
    if completed_at is not None:
        completed_at_seconds_ago = _seconds_between(completed_at, now)

    agent_id = to_json_value(_field(task, "id"))
    return {
        "id": agent_id,
        "agent_id": agent_id,
        "task": to_json_value(_field(task, "task")),
        "reason": to_json_value(_field(task, "reason")),
        "status": to_json_value(_field(task, "status")),
        "started_at": _json_time(started_at),
        "completed_at": _json_time(completed_at),
        "completed_at_seconds_ago": completed_at_seconds_ago,
        "stream_id": to_json_value(_field(task, "stream_id")),
        "elapsed_seconds": _seconds_between(started_at, end),
        "error_detail": to_json_value(redact_diagnostic_payload(_field(task, "error_detail"))),
    }


def _failure_json(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=failure_response(code, message),
    )


def register_agent_routes(app: FastAPI, *, intent: Any | None = None) -> None:
    """Register authenticated agent runtime routes."""

    router = APIRouter()
    configure_router_rate_limits(router, default_limit=None, limiter_enabled=True)

    if not _route_exists(app, "/v1/agents", {"GET"}):

        async def _list_agents(
            request: Request,
            limit: int = Query(default=20, ge=0, le=100),
        ) -> ResponseEnvelope:
            user_id = _authenticated_user_id_for_request(request)
            registry = _resolve_agent_registry(app, intent)
            if registry is None:
                return success_response({"active": [], "recent_completed": []})

            active = _owned_tasks(await _call_registry(registry.list_active, user_id), user_id)
            recent_completed = _owned_tasks(
                await _call_registry(registry.list_recent_completed, user_id, limit),
                user_id,
            )
            now = datetime.now(UTC)
            return success_response(
                {
                    "active": [_serialize_task(task, now=now) for task in active],
                    "recent_completed": [_serialize_task(task, now=now) for task in recent_completed],
                }
            )

        router.add_api_route(
            "/v1/agents",
            _list_agents,
            methods=["GET"],
            dependencies=[Depends(require_auth)],
            rate_limit="10/minute",
        )

    if not _route_exists(app, "/v1/agents/{agent_id}/cancel", {"POST"}):

        async def _cancel_agent(
            request: Request,
            agent_id: str = Path(..., min_length=1),
        ) -> Any:
            user_id = _authenticated_user_id_for_request(request)
            registry = _resolve_agent_registry(app, intent)
            if registry is None:
                return _failure_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "agent_registry_unavailable",
                    "Agent registry is not available.",
                )

            task = await _call_registry(registry.get, user_id, agent_id)
            if task is None or _task_user_id(task) != user_id:
                return _failure_json(
                    HTTPStatus.NOT_FOUND,
                    "agent_not_found",
                    "Agent not found.",
                )

            if not await _call_registry(registry.cancel, user_id, agent_id):
                return _failure_json(
                    HTTPStatus.NOT_FOUND,
                    "agent_not_found",
                    "Agent not found.",
                )

            final_task = await _call_registry(registry.get, user_id, agent_id) or task
            final_status = _field(final_task, "status") or "cancelled"
            return success_response(
                {
                    "agent_id": agent_id,
                    "final_status": to_json_value(final_status),
                }
            )

        router.add_api_route(
            "/v1/agents/{agent_id}/cancel",
            _cancel_agent,
            methods=["POST"],
            dependencies=[Depends(require_auth)],
            rate_limit="10/minute",
        )

    if router.routes:
        app.include_router(router)
