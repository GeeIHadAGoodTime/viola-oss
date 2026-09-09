"""``/model`` — show or switch the active LLM model.

Parity reference: ``src/commands/model/``. Claude's ``/model`` reads/writes
the session model setting. Viola routes through SettingsManager (the
runtime source of truth) so the change persists across turns.

The command is intentionally informational by default — switching models
on the desktop requires the auto-switch path in
``services.llm.factory`` which is plumbed through the existing
``ai_source`` setting. The handler exposes both reading the current
selection and queuing a switch via SettingsManager.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger
from intent.commands.registry import CommandInvocation, CommandSpec

logger = get_logger(__name__)


def command_spec(*, pipeline: Any) -> CommandSpec:
    """Build the ``/model`` spec bound to ``pipeline``."""

    def _handler(invocation: CommandInvocation) -> dict[str, object]:
        return _handle(invocation, pipeline)

    return CommandSpec(
        name="model",
        aliases=("/model",),
        source="builtin",
        handler=_handler,
        remote_safe=False,
        description="Show or set the active LLM model.",
        command_type="local-jsx",
        argument_hint="[model]",
        is_session_control=True,
        extra={"view": "model-picker"},
    )


def _handle(invocation: CommandInvocation, pipeline: Any) -> dict[str, object]:
    target = _extract_target(invocation)
    settings = _resolve_settings_manager(pipeline)
    if target:
        model_value = None if target.lower() == "default" else target
        if model_value is not None and not _model_is_available(model_value, pipeline, settings):
            return {
                "message": "Model unavailable: %s." % model_value,
                "data": {"command": "model", "requested": model_value, "applied": False, "reason": "unavailable"},
            }
        if settings is None:
            return {
                "message": "Cannot set model — settings manager unavailable.",
                "data": {"command": "model", "requested": target, "applied": False},
            }
        setter = getattr(pipeline, "set_session_model", None)
        if callable(setter):
            try:
                setter(model_value)
                _record_session_model_change(model_value)
                return {
                    "message": _model_applied_message(model_value),
                    "data": {"command": "model", "requested": target, "applied": True, "scope": "session"},
                }
            except (RuntimeError, ValueError, TypeError) as exc:
                return {
                    "message": "Model change failed: %s" % exc,
                    "data": {"command": "model", "requested": target, "applied": False},
                }
        try:
            settings.set("session_model_override", model_value)
            _record_session_model_change(model_value)
            return {
                "message": _model_applied_message(model_value),
                "data": {"command": "model", "requested": target, "applied": True, "scope": "session"},
            }
        except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
            return {
                "message": "Model change failed: %s" % exc,
                "data": {"command": "model", "requested": target, "applied": False},
            }
    current = _current_model(settings)
    if current is None:
        return {
            "message": "Model is unset.",
            "data": {"command": "model", "current": None},
        }
    return {
        "message": "Current model: %s" % current,
        "data": {"command": "model", "current": current},
    }


def _extract_target(invocation: CommandInvocation) -> str | None:
    raw = invocation.args.get("raw_args") if invocation.args else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _resolve_settings_manager(pipeline: Any) -> Any:
    manager = getattr(pipeline, "settings_manager", None)
    if manager is not None:
        return manager
    try:
        from ui.settings_manager import get_settings_manager
    except ImportError:
        return None
    try:
        return get_settings_manager()
    except Exception:
        return None


def _current_model(settings: Any) -> str | None:
    if settings is None:
        return None
    for key in ("session_model_override", "model", "managed_model", "openai_model"):
        try:
            value = settings.get(key, None)
        except Exception:
            value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _model_is_available(model: str, pipeline: Any, settings: Any) -> bool:
    available = _available_models(pipeline, settings)
    return not available or model in available


def _available_models(pipeline: Any, settings: Any) -> set[str]:
    values: list[Any] = []
    for owner in (pipeline, getattr(pipeline, "ai_controller", None), settings):
        if owner is None:
            continue
        direct = getattr(owner, "available_models", None)
        if isinstance(direct, (list, tuple, set)):
            values.extend(direct)
        getter = getattr(owner, "get_available_models", None)
        if callable(getter):
            try:
                result = getter()
            except (RuntimeError, ValueError, TypeError, AttributeError):
                result = None
            if isinstance(result, (list, tuple, set)):
                values.extend(result)
    if settings is not None:
        for key in ("available_models", "models"):
            try:
                value = settings.get(key, None)
            except (RuntimeError, ValueError, TypeError, AttributeError):
                value = None
            if isinstance(value, (list, tuple, set)):
                values.extend(value)
    return {str(value).strip() for value in values if str(value).strip()}


def _record_session_model_change(model: str | None) -> None:
    try:
        from bootstrap.session_state import get_session_state

        state = get_session_state()
        if model is None:
            state.clear_model_override(source="command", reason="/model default")
            return
        state.record_model_change(model, source="command")
    except (ImportError, RuntimeError, AttributeError, ValueError) as exc:
        logger.debug("SessionState model update failed: %s", exc)


def _model_applied_message(model: str | None) -> str:
    if model is None:
        return "Session model reset to default."
    return "Session model set to %s." % model


__all__ = ["command_spec"]
