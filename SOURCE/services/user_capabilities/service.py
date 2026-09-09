"""Service layer for user-authored routines."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from jsonschema.validators import Draft202012Validator

from core.logging_config import get_logger

from .context import get_current_surface, remember_run_for_completion
from .schema import CallCapabilityAction, CapabilityDraft, Tier, UserCapability, normalize_tier, utc_now_iso
from .storage import CapabilityStore

if TYPE_CHECKING:
    from .tiers import TierComputation

logger = get_logger(__name__)
_mcp_hub: Any | None = None


@dataclass(frozen=True)
class CapabilitySaveResult:
    """Result envelope for create/update operations."""

    capability: UserCapability
    collisions: list[dict[str, Any]]
    instant_collisions: list[dict[str, Any]] = field(default_factory=list)


class CapabilityArgsValidationError(ValueError):
    """Raised when saved call_capability args fail the target tool schema."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        message = "; ".join(str(error.get("message", "")) for error in errors if error.get("message"))
        super().__init__(message or "Routine tool arguments do not match the target tool schema.")


def set_mcp_hub(hub: Any | None) -> None:
    """Publish the live MCP hub for best-effort tool schema validation."""
    global _mcp_hub
    _mcp_hub = hub


def get_current_agent_tier(user_id: str) -> Tier:
    """Read the user's selected autonomy tier from SettingsManager."""
    from ui.settings_manager import get_settings_manager

    sm = get_settings_manager()
    return normalize_tier(sm.get("agent_autonomy", "ensemble", user_id=user_id))


def _required_tier_for_draft(draft: CapabilityDraft) -> tuple[Tier, TierComputation]:
    from .tiers import compute_required_tier, max_tier

    computed = compute_required_tier(draft.actions)
    required_tier = computed.required_tier
    if draft.required_tier is not None:
        required_tier = max_tier(required_tier, draft.required_tier)
    return required_tier, computed


def _normalize_phrase_key(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _phrase_lookup(phrases: list[str]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for phrase in phrases:
        key = _normalize_phrase_key(phrase)
        if key:
            lookup.setdefault(key, phrase)
    return lookup


def _effective_required_tier(capability: UserCapability) -> tuple[Tier, TierComputation]:
    from .tiers import compute_required_tier, max_tier

    computation = compute_required_tier(capability.actions)
    return max_tier(capability.required_tier, computation.required_tier), computation


def _is_runnable(capability: UserCapability, user_tier: object) -> bool:
    from .tiers import CapabilityTierError, ensure_tier_allowed

    if capability.disabled:
        return False
    required_tier, computation = _effective_required_tier(capability)
    try:
        ensure_tier_allowed(
            required_tier=required_tier,
            user_tier=user_tier,
            requirements=computation.tool_requirements,
        )
    except (CapabilityTierError, ValueError):
        return False
    return True


def _surface_blocked_tools(capability: UserCapability, surface: object) -> list[str]:
    from .tiers import blocked_tools_for_surface

    return blocked_tools_for_surface(capability.actions, surface)


def _resolve_tool_schemas_from_hub() -> dict[str, dict[str, Any]] | None:
    hub = _mcp_hub
    if hub is None:
        return None

    tools: object
    try:
        try:
            tools = hub.list_tools(tier="symphony", interactive=True)
        except TypeError:
            tools = hub.list_tools()
    except Exception as exc:
        logger.debug("Skipping capability arg validation; MCP tool schema lookup failed: %s", exc)
        return None

    if not isinstance(tools, list):
        return None

    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = str(tool.get("name") or "").strip()
        input_schema = tool.get("inputSchema")
        if name and isinstance(input_schema, dict):
            schemas[name] = input_schema
    return schemas


def _schema_field(tool_name: str, path: object) -> str:
    parts = [str(part) for part in path]
    suffix = ".".join(parts)
    return "%s.args.%s" % (tool_name, suffix) if suffix else "%s.args" % tool_name


def _enum_values(values: object) -> str:
    if isinstance(values, list):
        return "{%s}" % ", ".join(str(value) for value in values)
    return str(values)


def _format_schema_error(tool_name: str, error: JsonSchemaValidationError) -> dict[str, Any]:
    field = _schema_field(tool_name, error.path)
    if error.validator == "required":
        missing = ""
        if isinstance(error.message, str) and "'" in error.message:
            parts = error.message.split("'")
            if len(parts) >= 2:
                missing = parts[1]
        if missing:
            field = "%s.args.%s" % (tool_name, missing)
            message = "%s is required" % field
        else:
            message = "%s %s" % (field, error.message)
    elif error.validator == "enum":
        message = "%s value %r is not in %s" % (field, error.instance, _enum_values(error.validator_value))
    elif error.validator == "type":
        message = "%s expected type %s" % (field, error.validator_value)
    else:
        message = "%s %s" % (field, error.message)
    return {"field": field, "message": message}


def _validate_args_against_tool_schemas(draft: CapabilityDraft) -> None:
    schemas = _resolve_tool_schemas_from_hub()
    if not schemas:
        return

    validation_errors: list[dict[str, Any]] = []
    for action in draft.actions:
        if not isinstance(action, CallCapabilityAction):
            continue
        schema = schemas.get(action.name)
        if schema is None:
            continue
        try:
            Draft202012Validator.check_schema(schema)
            validator = Draft202012Validator(schema)
        except SchemaError as exc:
            logger.debug("Skipping capability arg validation for %s; invalid MCP schema: %s", action.name, exc)
            continue
        validation_errors.extend(
            _format_schema_error(action.name, error)
            for error in sorted(validator.iter_errors(action.args), key=lambda item: list(item.path))
        )

    if validation_errors:
        raise CapabilityArgsValidationError(validation_errors)


def detect_phrase_collisions(
    spec: dict[str, Any] | CapabilityDraft,
    *,
    user_id: str,
    root: Path | None = None,
    exclude_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return saved shortcuts that share normalized trigger phrases with a draft."""
    draft = spec if isinstance(spec, CapabilityDraft) else CapabilityDraft.model_validate(spec)
    draft_phrases = _phrase_lookup(draft.trigger.phrases)
    if not draft_phrases:
        return []

    normalized_exclude = str(exclude_id or "").strip().lower()
    collisions: list[dict[str, Any]] = []
    store = CapabilityStore(user_id, root=root, create=False)
    for capability in store.list():
        if normalized_exclude and capability.id == normalized_exclude:
            continue
        existing_phrases = _phrase_lookup(capability.trigger.phrases)
        shared_keys = sorted(set(draft_phrases).intersection(existing_phrases))
        if not shared_keys:
            continue
        collisions.append(
            {
                "capability_id": capability.id,
                "capability_name": capability.name,
                "conflicting_phrases": [existing_phrases[key] for key in shared_keys],
            }
        )
    return collisions


def detect_instant_collisions(
    spec: dict[str, Any] | CapabilityDraft,
    *,
    exclude_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return trigger phrases intercepted by deterministic instant commands."""
    del exclude_id
    try:
        from intent.instant_commands_patterns import INSTANT_PATTERNS
    except Exception:
        return []

    draft = spec if isinstance(spec, CapabilityDraft) else CapabilityDraft.model_validate(spec)
    collisions: list[dict[str, Any]] = []
    for phrase in draft.trigger.phrases:
        normalized = phrase.strip().lower()
        if not normalized:
            continue
        for pattern, command_name, _params, _description in INSTANT_PATTERNS:
            if pattern.match(normalized):
                collisions.append(
                    {
                        "phrase": phrase,
                        "instant_command_name": command_name,
                        "matched_pattern": pattern.pattern,
                    }
                )
                break
    return collisions


def create_for_user(
    spec: dict[str, Any],
    *,
    user_id: str,
    root: Path | None = None,
    user_tier: object | None = None,
) -> CapabilitySaveResult:
    """Validate, tier-gate, and save a routine for one user."""
    from .tiers import ensure_tier_allowed

    draft = CapabilityDraft.model_validate(spec)
    _validate_args_against_tool_schemas(draft)
    required_tier, computation = _required_tier_for_draft(draft)
    selected_tier = user_tier if user_tier is not None else get_current_agent_tier(user_id)
    ensure_tier_allowed(
        required_tier=required_tier,
        user_tier=selected_tier,
        requirements=computation.tool_requirements,
    )

    store = CapabilityStore(user_id, root=root, create=True)
    capability = UserCapability(
        schema_version=draft.schema_version,
        id=store.next_available_id(draft.name),
        name=draft.name,
        description=draft.description,
        trigger=draft.trigger,
        actions=draft.actions,
        required_tier=required_tier,
        disabled=draft.disabled,
        created_by=draft.created_by,
        created_at=draft.created_at or utc_now_iso(),
    )
    collisions = detect_phrase_collisions(draft, user_id=user_id, root=root)
    instant_collisions = detect_instant_collisions(draft)
    store.write(capability)
    return CapabilitySaveResult(
        capability=capability,
        collisions=collisions,
        instant_collisions=instant_collisions,
    )


def list_for_user(
    user_id: str,
    *,
    root: Path | None = None,
    user_tier: object | None = None,
    surface: object | None = None,
) -> list[dict[str, Any]]:
    """List one user's routines newest-first."""
    selected_tier = user_tier if user_tier is not None else get_current_agent_tier(user_id)
    current_surface = surface if surface is not None else get_current_surface()
    capabilities = CapabilityStore(user_id, root=root, create=False).list()
    items: list[dict[str, Any]] = []
    for capability in capabilities:
        payload = capability.model_dump(mode="json")
        tier_runnable = _is_runnable(capability, selected_tier)
        blocked_tools = _surface_blocked_tools(capability, current_surface)
        payload["runnable"] = tier_runnable
        payload["blocked_tools"] = blocked_tools
        payload["runnable_on_current_surface"] = tier_runnable and not blocked_tools
        items.append(payload)
    return items


def _compact_inline(value: object, *, max_len: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max(0, max_len - 3)].rstrip() + "..."


def _format_trigger_phrases(value: object, *, max_count: int = 4) -> str:
    if not isinstance(value, dict):
        return ""
    raw_phrases = value.get("phrases")
    if not isinstance(raw_phrases, list):
        return ""
    phrases = [_compact_inline(phrase, max_len=80) for phrase in raw_phrases if str(phrase or "").strip()]
    if not phrases:
        return ""
    shown = phrases[:max_count]
    suffix = " (+%d more)" % (len(phrases) - max_count) if len(phrases) > max_count else ""
    return ", ".join('"%s"' % phrase for phrase in shown) + suffix


def _format_action_tools(value: object, *, max_count: int = 6) -> str:
    if not isinstance(value, list):
        return ""
    tools: list[str] = []
    for action in value:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or "").strip()
        if action_type == "call_capability":
            tool_name = _compact_inline(action.get("name"), max_len=80)
            if tool_name:
                tools.append(tool_name)
        elif action_type == "summarize":
            tools.append("summarize")
    if not tools:
        return ""
    shown = tools[:max_count]
    suffix = " (+%d more)" % (len(tools) - max_count) if len(tools) > max_count else ""
    return ", ".join(shown) + suffix


def build_context_for_user(
    user_id: str,
    *,
    root: Path | None = None,
    user_tier: object | None = None,
    surface: object | None = None,
) -> str:
    """Build read-only model context listing the user's saved routines.

    Routines are listed as neutral data: every visible routine, no behavioral
    directive, no first-N cap. The context intentionally omits tool args so
    saved routines cannot leak credentials or long user-provided payloads into
    every model turn.
    """

    account_id = str(user_id or "").strip()
    if not account_id:
        return ""

    raw_capabilities = CapabilityStore(account_id, root=root, create=False).list()
    if not raw_capabilities:
        return ""

    selected_tier = user_tier if user_tier is not None else get_current_agent_tier(account_id)
    current_surface = surface if surface is not None else get_current_surface()
    capabilities: list[dict[str, Any]] = []
    for capability in raw_capabilities:
        payload = capability.model_dump(mode="json")
        tier_runnable = _is_runnable(capability, selected_tier)
        blocked_tools = _surface_blocked_tools(capability, current_surface)
        payload["runnable"] = tier_runnable
        payload["blocked_tools"] = blocked_tools
        payload["runnable_on_current_surface"] = tier_runnable and not blocked_tools
        capabilities.append(payload)

    visible = [item for item in capabilities if not bool(item.get("disabled"))]
    if not visible:
        return ""

    lines = ["USER ROUTINES (saved user-authored capabilities):"]
    for capability in visible:
        capability_id = _compact_inline(capability.get("id"), max_len=80)
        name = _compact_inline(capability.get("name"), max_len=120)
        if not capability_id or not name:
            continue
        runnable = bool(capability.get("runnable"))
        runnable_on_surface = bool(capability.get("runnable_on_current_surface"))
        status = "runnable" if runnable and runnable_on_surface else "not runnable"
        blocked_tools = capability.get("blocked_tools")
        if isinstance(blocked_tools, list) and blocked_tools:
            status = "not runnable on this surface: %s" % ", ".join(
                _compact_inline(tool, max_len=80) for tool in blocked_tools
            )
        elif not runnable:
            status = "not runnable for current tier"

        line_parts = ["- id=%s" % capability_id, "name=%s" % name, "status=%s" % status]
        description = _compact_inline(capability.get("description"), max_len=180)
        if description:
            line_parts.append("description=%s" % description)
        phrases = _format_trigger_phrases(capability.get("trigger"))
        if phrases:
            line_parts.append("trigger_phrases=%s" % phrases)
        tools = _format_action_tools(capability.get("actions"))
        if tools:
            line_parts.append("tools=%s" % tools)
        lines.append(" | ".join(line_parts))

    return "\n".join(lines)


def read_for_user(capability_id: str, *, user_id: str, root: Path | None = None) -> UserCapability | None:
    """Read one user's routine by id."""
    return CapabilityStore(user_id, root=root, create=False).read(capability_id)


def delete_for_user(capability_id: str, *, user_id: str, root: Path | None = None) -> dict[str, Any]:
    """Delete one user's routine by id."""
    return CapabilityStore(user_id, root=root, create=False).delete(capability_id)


def update_for_user(
    capability_id: str,
    spec: dict[str, Any],
    *,
    user_id: str,
    root: Path | None = None,
    user_tier: object | None = None,
) -> CapabilitySaveResult:
    """Validate and atomically replace an existing routine while preserving authorship."""
    from .tiers import ensure_tier_allowed

    store = CapabilityStore(user_id, root=root, create=False)
    existing = store.read(capability_id)
    if existing is None:
        raise FileNotFoundError("Routine '%s' was not found." % capability_id)

    draft = CapabilityDraft.model_validate(spec)
    _validate_args_against_tool_schemas(draft)
    required_tier, computation = _required_tier_for_draft(draft)
    selected_tier = user_tier if user_tier is not None else get_current_agent_tier(user_id)
    ensure_tier_allowed(
        required_tier=required_tier,
        user_tier=selected_tier,
        requirements=computation.tool_requirements,
    )

    disabled = draft.disabled if "disabled" in spec else existing.disabled
    updated = UserCapability(
        schema_version=draft.schema_version,
        id=existing.id,
        name=draft.name,
        description=draft.description,
        trigger=draft.trigger,
        actions=draft.actions,
        required_tier=required_tier,
        disabled=disabled,
        created_by=existing.created_by,
        created_at=existing.created_at,
    )
    collisions = detect_phrase_collisions(draft, user_id=user_id, root=root, exclude_id=existing.id)
    instant_collisions = detect_instant_collisions(draft, exclude_id=existing.id)
    store.write(
        updated,
        audit_action="update",
        detail="updated %s.json" % existing.id,
        before=existing.model_dump(mode="json"),
        after=updated.model_dump(mode="json"),
    )
    return CapabilitySaveResult(
        capability=updated,
        collisions=collisions,
        instant_collisions=instant_collisions,
    )


def set_disabled_for_user(
    capability_id: str,
    disabled: bool,
    *,
    user_id: str,
    root: Path | None = None,
) -> UserCapability:
    """Enable or disable an existing routine."""
    store = CapabilityStore(user_id, root=root, create=False)
    existing = store.read(capability_id)
    if existing is None:
        raise FileNotFoundError("Routine '%s' was not found." % capability_id)

    updated = existing.model_copy(update={"disabled": bool(disabled)})
    store.write(
        updated,
        audit_action="toggle",
        detail="disabled=%s" % str(updated.disabled).lower(),
        before=existing.disabled,
        after=updated.disabled,
    )
    return updated


def build_run_plan(
    capability_id: str,
    *,
    user_id: str,
    root: Path | None = None,
    user_tier: object | None = None,
    surface: object | None = None,
) -> dict[str, Any]:
    """Build a structured action plan for the LLM to execute next."""
    from .tiers import compute_required_tier, ensure_surface_allowed, ensure_tier_allowed, max_tier

    capability = read_for_user(capability_id, user_id=user_id, root=root)
    if capability is None:
        raise FileNotFoundError("Routine '%s' was not found." % capability_id)
    if capability.disabled:
        raise ValueError("Routine '%s' is disabled." % capability_id)

    computation = compute_required_tier(capability.actions)
    required_tier = max_tier(capability.required_tier, computation.required_tier)
    selected_tier = user_tier if user_tier is not None else get_current_agent_tier(user_id)
    ensure_tier_allowed(
        required_tier=required_tier,
        user_tier=selected_tier,
        requirements=computation.tool_requirements,
    )
    current_surface = surface if surface is not None else get_current_surface()
    ensure_surface_allowed(capability.actions, current_surface)

    steps: list[dict[str, Any]] = []
    for index, action in enumerate(capability.actions, start=1):
        if action.type == "call_capability":
            steps.append(
                {
                    "step": index,
                    "type": "call_capability",
                    "tool": action.name,
                    "args": action.args,
                }
            )
            continue
        steps.append(
            {
                "step": index,
                "type": "summarize",
                "style": action.style,
                "source": "prior_step_outputs",
            }
        )

    started_at = utc_now_iso()
    store = CapabilityStore(user_id, root=root, create=True)
    store.audit_run_start(
        capability.id,
        plan_steps_count=len(steps),
        started_at=started_at,
    )
    remember_run_for_completion(
        user_id=user_id,
        root=root,
        capability_id=capability.id,
        started_at=started_at,
        plan_steps_count=len(steps),
    )

    return {
        "id": capability.id,
        "name": capability.name,
        "description": capability.description,
        "required_tier": required_tier,
        "trigger": capability.trigger.model_dump(mode="json"),
        "plan": steps,
        "instructions": (
            "Execute the plan in order. For call_capability steps, call the named MCP tool with args. "
            "For summarize steps, summarize the prior step outputs in the requested style."
        ),
    }
