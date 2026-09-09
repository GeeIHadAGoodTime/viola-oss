"""``/permissions`` — inspect and adjust active permission rules.

Parity reference: ``src/commands/permissions/``. Claude's ``/permissions``
shows the active permission mode (``default`` / ``acceptEdits`` /
``bypassPermissions`` / ``dontAsk`` / ``plan``) and lists active allow/ask/
deny rules. Viola surfaces the same view via :mod:`intent.permissions.policy`.

Setting the mode goes through the SettingsManager-backed permission state
implementation. When called with no
arguments, returns the current snapshot.

R9-B F-042 decision: ``auto`` and ``bubble`` are Viola-specific *public*
extensions, not Claude parity modes. They're labeled accordingly in
``_VIOLA_EXTENSION_MODES`` so callers / telemetry / docs can distinguish
"Claude-parity mode" from "Viola-extension mode" without rejecting the
extensions outright. Hiding them would force the runtime to silently
ignore configured policy on tenants that depend on them.
"""

from __future__ import annotations

from typing import Any

from intent.commands.registry import CommandInvocation, CommandSpec

# Claude-parity modes (entrypoints/sdk/coreSchemas.ts:337-346).
_CLAUDE_PARITY_MODES = {"default", "acceptEdits", "bypassPermissions", "dontAsk", "plan"}
# Viola-specific public extensions surfaced through /permissions.
_VIOLA_EXTENSION_MODES = {"auto", "bubble"}
_VALID_MODES = _CLAUDE_PARITY_MODES | _VIOLA_EXTENSION_MODES


def command_spec(*, pipeline: Any) -> CommandSpec:
    """Build the ``/permissions`` spec bound to ``pipeline``."""

    def _handler(invocation: CommandInvocation) -> dict[str, object]:
        return _handle(invocation, pipeline)

    return CommandSpec(
        name="permissions",
        aliases=("/permissions",),
        source="builtin",
        handler=_handler,
        requires_permission=True,
        remote_safe=False,
        description="Show or change the active permission mode.",
        command_type="local-jsx",
        argument_hint="[mode]",
        is_session_control=True,
        extra={
            "view": "permission-editor",
            # F-042: label modes by Claude-parity vs Viola-extension so
            # UI/telemetry/docs can present them honestly.
            "claude_parity_modes": tuple(sorted(_CLAUDE_PARITY_MODES)),
            "viola_extension_modes": tuple(sorted(_VIOLA_EXTENSION_MODES)),
            "internal_modes": tuple(sorted(_VIOLA_EXTENSION_MODES)),  # legacy alias
        },
    )


def _handle(invocation: CommandInvocation, pipeline: Any) -> dict[str, object]:
    target = _extract_target(invocation)
    policy = _resolve_policy(pipeline)
    if target:
        if target not in _VALID_MODES:
            return {
                "message": "Unknown permission mode: %s" % target,
                "data": {
                    "command": "permissions",
                    "requested": target,
                    "applied": False,
                    "valid_modes": sorted(_VALID_MODES),
                },
            }
        applied = _apply_mode(policy, target, pipeline)
        return {
            "message": "Permission mode set to %s." % target if applied else "Mode change failed.",
            "data": {
                "command": "permissions",
                "requested": target,
                "applied": applied,
            },
        }
    snapshot = _snapshot(policy)
    mode = str(snapshot.get("mode", "unknown"))
    snapshot["mode_classification"] = (
        "claude-parity"
        if mode in _CLAUDE_PARITY_MODES
        else "viola-extension" if mode in _VIOLA_EXTENSION_MODES else "unknown"
    )
    snapshot["claude_parity_modes"] = tuple(sorted(_CLAUDE_PARITY_MODES))
    snapshot["viola_extension_modes"] = tuple(sorted(_VIOLA_EXTENSION_MODES))
    return {
        "message": "Permission mode: %s" % mode,
        "data": {"command": "permissions", **snapshot},
    }


def _extract_target(invocation: CommandInvocation) -> str | None:
    raw = invocation.args.get("raw_args") if invocation.args else None
    if isinstance(raw, str) and raw.strip():
        # Allow either "/permissions acceptEdits" or "/permissions mode=acceptEdits".
        token = raw.strip().split()[0]
        if "=" in token:
            token = token.split("=", 1)[1]
        return token
    return None


def _resolve_policy(pipeline: Any) -> Any:
    for attr in ("permission_policy", "permissions"):
        candidate = getattr(pipeline, attr, None)
        if candidate is not None:
            return candidate
    return None


def _apply_mode(policy: Any, mode: str, pipeline: Any) -> bool:
    if policy is not None and hasattr(policy, "set_mode"):
        try:
            policy.set_mode(mode)
            return True
        except (RuntimeError, ValueError, AttributeError, TypeError):
            return False
    if policy is not None and hasattr(policy, "set_permission_mode"):
        try:
            policy.set_permission_mode(mode)
            return True
        except (RuntimeError, ValueError, AttributeError, TypeError):
            return False
    # Fall back to settings persistence so the next pipeline boot
    # picks the mode back up.
    settings = getattr(pipeline, "settings_manager", None)
    if settings is None:
        try:
            from ui.settings_manager import get_settings_manager

            settings = get_settings_manager()
        except ImportError:
            settings = None
    if settings is None:
        return False
    try:
        settings.set("permission_mode", mode)
        return True
    except Exception:
        return False


def _snapshot(policy: Any) -> dict[str, object]:
    if policy is None:
        return {"mode": "unknown", "rules": []}
    snapshot: dict[str, object] = {"mode": "default"}
    for method in ("snapshot", "describe", "as_dict", "to_dict"):
        getter = getattr(policy, method, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                continue
            if isinstance(value, dict):
                snapshot.update(value)
                break
    mode = getattr(policy, "mode", None)
    if mode and "mode" not in snapshot:
        snapshot["mode"] = str(mode)
    rules = getattr(policy, "rules", None)
    if rules is not None and "rules" not in snapshot:
        snapshot["rules"] = [_format_rule(rule) for rule in rules]
    return snapshot


def _format_rule(rule: Any) -> dict[str, object]:
    return {
        "tool_name": getattr(rule, "tool_name", None),
        "behavior": getattr(rule, "behavior", None),
        "source": getattr(rule, "source", None),
        "reason": getattr(rule, "reason", None),
    }


__all__ = ["command_spec"]
