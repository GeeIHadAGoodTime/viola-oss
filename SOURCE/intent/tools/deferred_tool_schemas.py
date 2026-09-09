"""Deferred MCP tool schema loading and tool_reference resolution.

Large tool catalogs are not sent to the model as full schemas by default.
The initial surface keeps callable discovery tools visible and advertises the
remaining tools as name references.  When ``ToolSearch`` selects a
reference, later provider requests can resolve that reference back to the full
tool schema without bypassing MCP hub routing, approval, or hook dispatch.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from services.conversation.context_frames import (
    Frame,
    FrameKind,
    FrameRole,
    ToolResultBlock,
)
from services.conversation.session_identity import NonEmptyUserId, make_user_id

logger = get_logger(__name__)

ToolDefinition = dict[str, Any]

DEFAULT_DEFER_THRESHOLD = 12
MAX_DEFERRED_TOOL_REGISTRIES = 256
CANONICAL_TOOL_SEARCH_NAME = "ToolSearch"
LEGACY_TOOL_SEARCH_NAME = "tool_search"
VISIBLE_DISCOVERY_TOOLS = frozenset({CANONICAL_TOOL_SEARCH_NAME, LEGACY_TOOL_SEARCH_NAME})
CRITICAL_ALWAYS_LOAD_TOOLS = frozenset(
    {
        "web_search",
        "web_read",
        "browser_navigate",
        "browser_snapshot",
        "browser_interact",
        "browser_fill_form",
        "browser_get_text",
        "browser_get_links",
        "browser_wait",
        "browser_evaluate",
        "browser_run_script",
        "ask_user",
        "desktop_volume",
        "media",
        "memory",
        "payment",
        "phone",
        "signature",
        "resume_signature_gate",
        "cancel_signature_gate",
        "resume_payment_gate",
        "cancel_payment_gate",
    }
)

# S6-014: Claude's defer-loading API uses three signals on each tool:
#   * ``alwaysLoad`` — never defer (set via _meta['anthropic/alwaysLoad']).
#   * ``isMcp`` — always defer (MCP tools are workflow-specific).
#   * ``shouldDefer`` — explicit per-tool opt-in to deferral.
# We accept any of these alongside the existing Viola-flavored markers
# (``_defer_loading``, ``always_load``, ``is_mcp``) so caller code from
# both worlds keeps working.
_ALWAYS_LOAD_KEYS = ("alwaysLoad", "always_load", "_always_load")
_IS_MCP_KEYS = ("isMcp", "is_mcp", "_is_mcp")
_SHOULD_DEFER_KEYS = ("shouldDefer", "should_defer", "_defer_loading", "defer_loading")


def is_deferred_tool(tool: Mapping[str, Any]) -> bool:
    """Return True when ``tool`` should be deferred behind tool_search.

    Mirrors ``src/tools/ToolSearchTool/prompt.ts:isDeferredTool``: alwaysLoad
    wins, then isMcp forces defer, then ToolSearch itself is never deferred,
    then shouldDefer opts in.
    """

    if not isinstance(tool, Mapping):
        return False
    for key in _ALWAYS_LOAD_KEYS:
        if bool(tool.get(key)):
            return False
    name = _tool_name(tool)
    if name in VISIBLE_DISCOVERY_TOOLS:
        return False
    if name in CRITICAL_ALWAYS_LOAD_TOOLS:
        return False
    meta = tool.get("_meta") or tool.get("meta") or {}
    if isinstance(meta, Mapping):
        if meta.get("anthropic/alwaysLoad") is True:
            return False
        if meta.get("anthropic/shouldDefer") is True:
            return True
    for key in _IS_MCP_KEYS:
        if bool(tool.get(key)):
            return True
    for key in _SHOULD_DEFER_KEYS:
        if bool(tool.get(key)):
            return True
    return False


_REGISTRIES: OrderedDict[tuple[str, str], dict[str, ToolDefinition]] = OrderedDict()


@dataclass(frozen=True)
class ToolSchemaRef:
    """A compact reference to a deferred tool schema."""

    name: str
    namespace: str
    description: str
    input_schema_hash: str
    resolver_id: str
    user_id: NonEmptyUserId | str = field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        user_id = _normalize_user_id(self.user_id)
        if user_id:
            object.__setattr__(self, "user_id", make_user_id(user_id))

    def to_block(self) -> dict[str, str]:
        """Return Claude's canonical model-visible ``tool_reference`` block."""
        return {"type": "tool_reference", "tool_name": self.name}


@dataclass(frozen=True)
class DeferredToolPool:
    """Split tool surface for one turn."""

    visible_tools: list[ToolDefinition]
    deferred_refs: list[ToolSchemaRef]
    user_id: NonEmptyUserId | str = ""

    def __post_init__(self) -> None:
        user_id = _normalize_user_id(self.user_id)
        if user_id:
            object.__setattr__(self, "user_id", make_user_id(user_id))


def deferred_tool_pool_to_payload(pool: DeferredToolPool) -> dict[str, Any]:
    """Serialize a request-scoped deferred pool for MCP request metadata."""

    return {
        "visible_tools": copy.deepcopy(list(pool.visible_tools)),
        "deferred_refs": [
            {
                "name": ref.name,
                "namespace": ref.namespace,
                "description": ref.description,
                "input_schema_hash": ref.input_schema_hash,
                "resolver_id": ref.resolver_id,
                "user_id": str(ref.user_id or pool.user_id or ""),
            }
            for ref in pool.deferred_refs
        ],
        "user_id": str(pool.user_id or ""),
    }


def deferred_tool_pool_from_payload(payload: Mapping[str, Any]) -> DeferredToolPool:
    """Rehydrate a request-scoped deferred pool from MCP request metadata."""

    if not isinstance(payload, Mapping):
        raise TypeError("deferred tool pool payload must be a mapping")

    user_id = _normalize_user_id(payload.get("user_id"))
    visible_tools_raw = payload.get("visible_tools")
    deferred_refs_raw = payload.get("deferred_refs")
    visible_tools = [
        _clean_tool(tool)
        for tool in (visible_tools_raw if isinstance(visible_tools_raw, Sequence) else [])
        if isinstance(tool, Mapping) and _tool_name(tool)
    ]

    deferred_refs: list[ToolSchemaRef] = []
    if isinstance(deferred_refs_raw, Sequence):
        for raw_ref in deferred_refs_raw:
            if not isinstance(raw_ref, Mapping):
                continue
            name = str(raw_ref.get("name") or "").strip()
            resolver_id = str(raw_ref.get("resolver_id") or "").strip()
            input_schema_hash = str(raw_ref.get("input_schema_hash") or "").strip()
            if not name or not resolver_id or not input_schema_hash:
                continue
            deferred_refs.append(
                ToolSchemaRef(
                    name=name,
                    namespace=str(raw_ref.get("namespace") or "default"),
                    description=str(raw_ref.get("description") or ""),
                    input_schema_hash=input_schema_hash,
                    resolver_id=resolver_id,
                    user_id=_normalize_user_id(raw_ref.get("user_id")) or user_id,
                )
            )

    return DeferredToolPool(
        visible_tools=visible_tools,
        deferred_refs=deferred_refs,
        user_id=user_id,
    )


def _clean_tool(tool: Mapping[str, Any]) -> ToolDefinition:
    return {str(k): copy.deepcopy(v) for k, v in tool.items() if not str(k).startswith("_")}


def _preserve_defer_signals(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a sanitized tool dict that keeps the defer-signal markers.

    ``_clean_tool`` strips every underscore-prefixed key, but the
    ``alwaysLoad`` / ``isMcp`` / ``shouldDefer`` signals live precisely
    on those keys (``_meta``, ``_always_load``, ``_is_mcp``, ...). We
    need them visible to ``is_deferred_tool`` so the defer split can
    honor MCP-registered meta. F-014 (R3-C): without this carry, an
    ``anthropic/alwaysLoad`` meta marker registered on the MCP server
    side never reaches the defer split — and ``start_agent`` silently
    ends up behind ToolSearch under bulk-defer.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = dict(_clean_tool(raw))
    for key in (*_ALWAYS_LOAD_KEYS, *_IS_MCP_KEYS, *_SHOULD_DEFER_KEYS):
        if key in raw:
            out[key] = copy.deepcopy(raw[key])
    meta = raw.get("_meta") or raw.get("meta")
    if isinstance(meta, Mapping):
        out["_meta"] = copy.deepcopy(dict(meta))
    return out


def _tool_name(tool: Mapping[str, Any]) -> str:
    name = tool.get("name")
    if isinstance(name, str):
        return name.strip()
    if isinstance(tool.get("function"), Mapping):
        fn_name = tool["function"].get("name")  # type: ignore[index]
        if isinstance(fn_name, str):
            return fn_name.strip()
    return ""


def _input_schema(tool: Mapping[str, Any]) -> Any:
    if isinstance(tool.get("function"), Mapping):
        function = tool["function"]  # type: ignore[index]
        return function.get("parameters") or function.get("input_schema") or function.get("inputSchema") or {}
    return tool.get("inputSchema") or tool.get("input_schema") or tool.get("parameters") or {}


def _schema_hash(tool: Mapping[str, Any]) -> str:
    payload = _input_schema(tool)
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    except Exception:
        encoded = str(payload).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _namespace_for(context: Any, name: str) -> str:
    routes = getattr(context, "_tool_routes", None)
    if isinstance(routes, Mapping):
        server = routes.get(name)
        if isinstance(server, str) and server:
            return server
    if "__" in name:
        return name.split("__", 1)[0]
    return "default"


def _normalize_user_id(user_id: object) -> str:
    return str(user_id or "").strip()


def _optional_user_id(value: object) -> NonEmptyUserId | None:
    cleaned = _normalize_user_id(value)
    if not cleaned:
        return None
    return make_user_id(cleaned)


def _resolve_user_id(context: Any, user_id: object) -> NonEmptyUserId | None:
    """Resolve a deferred-tool registry user id from explicit + context-owned sources.

    Per CLAUDE.md ("functions touching user data must not read 'current user'
    from fallback instead of explicit parameters") this resolver does NOT fall
    back to `core.user_context.get_current_user_id()`. Callers must pass a
    concrete `user_id` or supply a context object that carries one on itself.
    Ambient/context-var fallback would silently scope a deferred-tool registry
    lookup to whatever happens to be set globally — exactly the cross-tenant
    confusion class S7-09 flagged.
    """

    explicit = _normalize_user_id(user_id)
    if explicit:
        return make_user_id(explicit)

    if isinstance(context, Mapping):
        for key in ("user_id", "session_user_id"):
            mapped = _normalize_user_id(context.get(key))
            if mapped:
                return make_user_id(mapped)

    for attr in ("user_id", "_user_id", "current_user_id"):
        candidate = _normalize_user_id(getattr(context, attr, None))
        if candidate:
            return make_user_id(candidate)

    return None


def _registry_key(user_id: NonEmptyUserId | str, resolver_id: str) -> tuple[str, str]:
    return str(make_user_id(user_id)), resolver_id


def _get_registry(user_id: NonEmptyUserId | str, resolver_id: str) -> dict[str, ToolDefinition]:
    key = _registry_key(user_id, resolver_id)
    registry = _REGISTRIES.get(key)
    if registry is None:
        return {}
    _REGISTRIES.move_to_end(key)
    return registry


def _find_ref_by_name(user_id: NonEmptyUserId | str, name: str) -> ToolSchemaRef | None:
    scoped_user_id = make_user_id(user_id)
    for registry_user_id, resolver_id in reversed(list(_REGISTRIES.keys())):
        if registry_user_id != str(scoped_user_id):
            continue
        registry = _get_registry(scoped_user_id, resolver_id)
        tool = registry.get(name)
        if tool is None:
            continue
        return ToolSchemaRef(
            name=name,
            namespace="default",
            description=str(tool.get("description") or ""),
            input_schema_hash=_schema_hash(tool),
            resolver_id=resolver_id,
            user_id=scoped_user_id,
        )
    return None


def _store_registry(
    user_id: NonEmptyUserId | str,
    resolver_id: str,
    registry: dict[str, ToolDefinition],
) -> None:
    key = _registry_key(user_id, resolver_id)
    _REGISTRIES[key] = registry
    _REGISTRIES.move_to_end(key)
    while len(_REGISTRIES) > MAX_DEFERRED_TOOL_REGISTRIES:
        _REGISTRIES.popitem(last=False)


def _resolver_id_for(
    tools: Sequence[Mapping[str, Any]],
    *,
    tier: str | None,
    interactive: bool,
    user_id: NonEmptyUserId | str,
) -> str:
    parts = ["%s:%s" % (_tool_name(tool), _schema_hash(tool)) for tool in tools if _tool_name(tool)]
    user_hash = hashlib.sha256(str(make_user_id(user_id)).encode("utf-8", errors="replace")).hexdigest()[:16]
    seed = json.dumps(
        {
            "tier": tier or "",
            "interactive": bool(interactive),
            "user": user_hash,
            "tools": sorted(parts),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "mcp:%s" % hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _list_visible_tools(context: Any, *, tier: str | None, interactive: bool) -> list[ToolDefinition]:
    # F-014 (R3-C): use ``_preserve_defer_signals`` instead of ``_clean_tool``
    # so MCP-registered ``alwaysLoad`` / ``isMcp`` / ``shouldDefer`` markers
    # survive into the defer split. ``_clean_tool`` strips every underscore-
    # prefixed key including ``_meta``, where ``anthropic/alwaysLoad`` lives.
    if isinstance(context, Mapping) and "tools" in context:
        tools = context.get("tools")
        if isinstance(tools, Sequence):
            return [_preserve_defer_signals(tool) for tool in tools if isinstance(tool, Mapping) and _tool_name(tool)]

    # issue #2094: prefer a defer-signal-preserving lister when the context
    # provides one. Plain ``list_tools`` (still used as a fallback for any
    # caller/mock that doesn't implement the newer method) strips every
    # underscore-prefixed key -- including ``_meta``, where
    # ``anthropic/alwaysLoad`` lives -- before this function's own
    # ``_preserve_defer_signals`` call ever runs, so a tool whose alwaysLoad/
    # isMcp/shouldDefer marker lives only in ``_meta`` (not the hardcoded
    # CRITICAL_ALWAYS_LOAD_TOOLS name set) was silently deferred regardless of
    # the marker. See ``MCPClientHub.list_tools_with_defer_signals``.
    list_tools_with_defer_signals = getattr(context, "list_tools_with_defer_signals", None)
    list_tools = (
        list_tools_with_defer_signals
        if callable(list_tools_with_defer_signals)
        else getattr(context, "list_tools", None)
    )
    if not callable(list_tools):
        return []

    try:
        tools = list_tools(tier=tier, interactive=interactive)
    except TypeError:
        try:
            tools = list_tools(tier=tier)
        except TypeError:
            tools = list_tools()

    if not isinstance(tools, Sequence):
        return []
    return [_preserve_defer_signals(tool) for tool in tools if isinstance(tool, Mapping) and _tool_name(tool)]


def list_tools_deferred(
    context: Any,
    *,
    tier: str | None = None,
    interactive: bool = True,
    defer_threshold: int = DEFAULT_DEFER_THRESHOLD,
    user_id: NonEmptyUserId | str | None = None,
) -> DeferredToolPool:
    """Return the visible/deferred split for the current tool context.

    S6-014: per-tool ``alwaysLoad`` / ``isMcp`` / ``shouldDefer`` signals
    take precedence over the threshold heuristic. The threshold is only
    consulted when the per-tool flags don't already force a split.
    """
    all_tools = _list_visible_tools(context, tier=tier, interactive=interactive)
    scoped_user_id = _resolve_user_id(context, user_id)
    if not all_tools:
        return DeferredToolPool(visible_tools=[], deferred_refs=[], user_id=scoped_user_id or "")

    # Per-tool defer signals: short-circuit when any tool is explicitly
    # marked for defer or always-load. This is the Claude path.
    has_explicit_defer_flags = any(is_deferred_tool(tool) for tool in all_tools)

    if not has_explicit_defer_flags and len(all_tools) <= max(0, defer_threshold):
        return DeferredToolPool(visible_tools=all_tools, deferred_refs=[], user_id=scoped_user_id or "")

    if not scoped_user_id:
        raise ValueError("user_id is required for deferred tool registry scope")

    resolver_id = _resolver_id_for(
        all_tools,
        tier=tier,
        interactive=interactive,
        user_id=scoped_user_id,
    )
    registry: dict[str, ToolDefinition] = {}
    visible_tools: list[ToolDefinition] = []
    deferred_refs: list[ToolSchemaRef] = []

    use_explicit = has_explicit_defer_flags
    for tool in all_tools:
        name = _tool_name(tool)
        if not name:
            continue
        registry[name] = _clean_tool(tool)
        if name in VISIBLE_DISCOVERY_TOOLS:
            visible_tools.append(_clean_tool(tool))
            continue
        # F-014 (R3-C): alwaysLoad is a hard "never defer" signal even in
        # bulk-defer mode — Claude's fork parity contract
        # (``tools/ToolSearchTool/prompt.ts:73-81``) keeps Agent visible on
        # turn one. Carve it out before the bulk-defer fallback.
        if _has_always_load(tool):
            visible_tools.append(_clean_tool(tool))
            continue
        # When explicit flags are present, only defer tools that opt in;
        # otherwise fall back to the threshold-based bulk defer.
        should_defer = is_deferred_tool(tool) if use_explicit else True
        if not should_defer:
            visible_tools.append(_clean_tool(tool))
            continue
        deferred_refs.append(
            ToolSchemaRef(
                name=name,
                namespace=_namespace_for(context, name),
                description=str(tool.get("description") or ""),
                input_schema_hash=_schema_hash(tool),
                resolver_id=resolver_id,
                user_id=scoped_user_id,
            )
        )

    _store_registry(scoped_user_id, resolver_id, registry)
    return DeferredToolPool(
        visible_tools=sorted(visible_tools, key=lambda item: _tool_name(item)),
        deferred_refs=sorted(deferred_refs, key=lambda ref: ref.name),
        user_id=scoped_user_id,
    )


def _has_always_load(tool: Mapping[str, Any]) -> bool:
    if not isinstance(tool, Mapping):
        return False
    if _tool_name(tool) in CRITICAL_ALWAYS_LOAD_TOOLS:
        return True
    for key in _ALWAYS_LOAD_KEYS:
        if bool(tool.get(key)):
            return True
    meta = tool.get("_meta") or tool.get("meta") or {}
    if isinstance(meta, Mapping) and meta.get("anthropic/alwaysLoad") is True:
        return True
    return False


def _ref_from_block(block: Any, *, user_id: NonEmptyUserId | str | None = None) -> ToolSchemaRef | None:
    try:
        scoped_user_id = _optional_user_id(user_id)
    except ValueError:
        return None
    if isinstance(block, ToolSchemaRef):
        try:
            block_user_id = _optional_user_id(block.user_id)
        except ValueError:
            return None
        if scoped_user_id and block_user_id and scoped_user_id != block_user_id:
            return None
        effective_user_id = block_user_id or scoped_user_id
        if not effective_user_id:
            return None
        if block_user_id:
            return block
        return ToolSchemaRef(
            name=block.name,
            namespace=block.namespace,
            description=block.description,
            input_schema_hash=block.input_schema_hash,
            resolver_id=block.resolver_id,
            user_id=effective_user_id,
        )
    if not isinstance(block, Mapping):
        return None

    block_type = block.get("type")
    if block_type != "tool_reference":
        return None
    name = block.get("tool_name") or block.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    resolver_id = block.get("resolver_id")
    schema_hash = block.get("input_schema_hash")
    namespace = block.get("namespace")
    description = block.get("description")

    if not scoped_user_id:
        return None
    if not isinstance(resolver_id, str) or not resolver_id:
        return _find_ref_by_name(scoped_user_id, name.strip())

    if not isinstance(schema_hash, str) or not schema_hash:
        tool = _get_registry(scoped_user_id, resolver_id).get(name.strip())
        schema_hash = _schema_hash(tool) if tool else ""
    return ToolSchemaRef(
        name=name.strip(),
        namespace=str(namespace or "default"),
        description=str(description or ""),
        input_schema_hash=schema_hash,
        resolver_id=resolver_id,
        user_id=scoped_user_id,
    )


# Why a reference stops short of becoming a callable schema. ToolSearch tells
# the model a tool is now loadable; resolution happens a turn later and used to
# drop a reference with a bare ``continue``, so nothing -- not the model, not a
# log line -- recorded that the promised schema never arrived. The model then
# tried to call a tool it had been told was ready, or searched for it again.
RESOLUTION_RESOLVABLE = "resolvable"
UNRESOLVED_UNKNOWN_REFERENCE = "unknown_reference"
UNRESOLVED_MISSING_USER_SCOPE = "missing_user_scope"
UNRESOLVED_REGISTRY_MISSING = "registry_missing"
UNRESOLVED_SCHEMA_CHANGED = "schema_changed"


@dataclass(frozen=True)
class UnresolvedToolReference:
    """A deferred reference that will not expand into a callable schema."""

    name: str
    reason: str


def _reference_name(raw_ref: Any) -> str:
    """Best-effort name for a reference that failed to parse."""
    if isinstance(raw_ref, ToolSchemaRef):
        return raw_ref.name
    if isinstance(raw_ref, Mapping):
        for key in ("tool_name", "name"):
            value = raw_ref.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def resolve_tool_references_detailed(
    refs: Iterable[ToolSchemaRef | Mapping[str, Any]],
    *,
    user_id: NonEmptyUserId | str | None = None,
) -> tuple[list[ToolDefinition], list[UnresolvedToolReference]]:
    """Resolve references, and say which ones did not resolve and why."""
    resolved: list[ToolDefinition] = []
    unresolved: list[UnresolvedToolReference] = []
    seen: set[str] = set()
    scoped_user_id = _optional_user_id(user_id)
    for raw_ref in refs:
        ref = _ref_from_block(raw_ref, user_id=scoped_user_id)
        if ref is None:
            name = _reference_name(raw_ref)
            logger.warning(
                "Deferred tool reference %r did not match any registered tool for this user; "
                "its schema will not be loaded",
                name,
            )
            unresolved.append(UnresolvedToolReference(name=name, reason=UNRESOLVED_UNKNOWN_REFERENCE))
            continue
        if ref.name in seen:
            continue
        ref_user_id = _optional_user_id(ref.user_id)
        if not ref_user_id:
            logger.warning(
                "Deferred tool reference %r carries no user scope; its schema will not be loaded",
                ref.name,
            )
            unresolved.append(UnresolvedToolReference(name=ref.name, reason=UNRESOLVED_MISSING_USER_SCOPE))
            continue
        registry = _get_registry(ref_user_id, ref.resolver_id)
        tool = registry.get(ref.name)
        if tool is None:
            # The registry is an LRU capped at MAX_DEFERRED_TOOL_REGISTRIES, so
            # a long-lived process serving many tenants can evict the entry
            # between the search and the turn that would use it.
            logger.warning(
                "Deferred tool reference %r has no registry entry under resolver %s "
                "(evicted or never stored); its schema will not be loaded",
                ref.name,
                ref.resolver_id,
            )
            unresolved.append(UnresolvedToolReference(name=ref.name, reason=UNRESOLVED_REGISTRY_MISSING))
            continue
        if ref.input_schema_hash and _schema_hash(tool) != ref.input_schema_hash:
            logger.warning(
                "Deferred tool reference %r no longer matches the registered schema hash; "
                "its schema will not be loaded",
                ref.name,
            )
            unresolved.append(UnresolvedToolReference(name=ref.name, reason=UNRESOLVED_SCHEMA_CHANGED))
            continue
        resolved.append(_clean_tool(tool))
        seen.add(ref.name)
    return resolved, unresolved


def resolve_tool_references(
    refs: Iterable[ToolSchemaRef | Mapping[str, Any]],
    *,
    user_id: NonEmptyUserId | str | None = None,
) -> list[ToolDefinition]:
    """Resolve deferred tool references to full tool schemas."""
    resolved, _unresolved = resolve_tool_references_detailed(refs, user_id=user_id)
    return resolved


def tool_reference_resolution_status(
    names: Iterable[str],
    *,
    user_id: NonEmptyUserId | str | None = None,
) -> dict[str, str]:
    """Per name, whether a ``tool_reference`` block for it would resolve.

    ToolSearch's answer is read by the model as "this tool is loadable now", so
    it needs to be checked against the same path that will actually run: a bare
    ``{"type": "tool_reference", "tool_name": name}`` block, resolved a turn
    later. Anything else would be checking a different question.
    """
    try:
        scoped_user_id = _optional_user_id(user_id)
    except ValueError:
        scoped_user_id = None

    status: dict[str, str] = {}
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name or name in status:
            continue
        if scoped_user_id is None:
            status[name] = UNRESOLVED_MISSING_USER_SCOPE
            continue
        _resolved, unresolved = resolve_tool_references_detailed(
            [{"type": "tool_reference", "tool_name": name}],
            user_id=scoped_user_id,
        )
        status[name] = unresolved[0].reason if unresolved else RESOLUTION_RESOLVABLE
    return status


def _extract_blocks(value: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if isinstance(value, Mapping) and value.get("type") == "tool_result":
        content = value.get("content")
        if isinstance(content, Mapping) and content.get("type") == "tool_reference":
            blocks.append(dict(content))
        elif isinstance(content, ToolSchemaRef):
            blocks.append(content.to_block())
        elif isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
            for item in content:
                if isinstance(item, Mapping) and item.get("type") == "tool_reference":
                    blocks.append(dict(item))
                elif isinstance(item, ToolSchemaRef):
                    blocks.append(item.to_block())
        return blocks
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, Mapping) and item.get("type") == "tool_result":
                blocks.extend(_extract_blocks(item))
    return blocks


def extract_tool_references_from_messages(
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    user_id: NonEmptyUserId | str | None = None,
) -> list[ToolSchemaRef]:
    """Extract ``tool_reference`` blocks from provider message history."""
    refs: list[ToolSchemaRef] = []
    seen: set[tuple[str, str, str]] = set()
    scoped_user_id = _optional_user_id(user_id)
    for message in messages or []:
        for block in _extract_blocks(message.get("content")):
            ref = _ref_from_block(block, user_id=scoped_user_id)
            if ref is None:
                continue
            key = (ref.resolver_id, ref.name, ref.input_schema_hash)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs


@dataclass(frozen=True, slots=True, init=False)
class DeferredToolResolver:
    """User-scoped resolver for expanding deferred tool references."""

    user_id: NonEmptyUserId
    messages: Sequence[Mapping[str, Any]] | None

    def __init__(
        self,
        *,
        user_id: NonEmptyUserId | str,
        messages: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        object.__setattr__(self, "user_id", make_user_id(user_id))
        object.__setattr__(self, "messages", messages)

    def extract(self, messages: Sequence[Mapping[str, Any]] | None = None) -> list[ToolSchemaRef]:
        """Extract references using this resolver's tenant scope."""

        return extract_tool_references_from_messages(
            self.messages if messages is None else messages,
            user_id=self.user_id,
        )

    def resolve(self, refs: Iterable[ToolSchemaRef | Mapping[str, Any]]) -> list[ToolDefinition]:
        """Resolve references using this resolver's tenant scope."""

        return resolve_tool_references(refs, user_id=self.user_id)

    def expand(
        self,
        base_tools: Sequence[Mapping[str, Any]] | None,
        *,
        messages: Sequence[Mapping[str, Any]] | None = None,
        allowed_names: set[str] | None = None,
    ) -> list[ToolDefinition] | None:
        """Merge allowed referenced schemas into a provider tool list."""

        if base_tools is None:
            return None

        merged = [_clean_tool(tool) for tool in base_tools if isinstance(tool, Mapping) and _tool_name(tool)]
        seen = {_tool_name(tool) for tool in merged}
        allowed = {str(name).strip() for name in (allowed_names or set()) if str(name).strip()}
        if not allowed:
            return merged
        refs = self.extract(self.messages if messages is None else messages)
        for tool in self.resolve(refs):
            name = _tool_name(tool)
            if not name or name in seen:
                continue
            if name not in allowed:
                continue
            merged.append(tool)
            seen.add(name)
        return merged


def expand_tools_with_deferred_references(
    base_tools: Sequence[Mapping[str, Any]] | None,
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    allowed_names: set[str] | None = None,
    user_id: NonEmptyUserId | str | None = None,
) -> list[ToolDefinition] | None:
    """Merge resolved deferred schemas into a provider tool list."""
    if base_tools is None:
        return None

    merged = [_clean_tool(tool) for tool in base_tools if isinstance(tool, Mapping) and _tool_name(tool)]
    allowed = {str(name).strip() for name in (allowed_names or set()) if str(name).strip()}
    if not allowed:
        return merged
    scoped_user_id = _optional_user_id(user_id)
    if not scoped_user_id:
        return merged
    resolver = DeferredToolResolver(user_id=scoped_user_id, messages=messages)
    return resolver.expand(merged, allowed_names=allowed)


def build_tool_reference_frame(ref: ToolSchemaRef) -> Frame:
    """Build a canonical frame carrying a tool_reference result block."""
    content = json.dumps(ref.to_block(), sort_keys=True)
    return Frame(
        kind=FrameKind.TOOL_RESULT,
        role=FrameRole.TOOL,
        blocks=(
            ToolResultBlock(
                tool_use_id="tool_reference:%s" % ref.name,
                tool_name=CANONICAL_TOOL_SEARCH_NAME,
                content=content,
                is_error=False,
            ),
        ),
        is_meta=False,
        origin="tool_reference",
        tool_use_id="tool_reference:%s" % ref.name,
        extra={"tool_reference": ref.to_block()},
    )


# issue #2094: the names alone are not self-describing. Claude Code ships the
# same list inside a system-reminder that states what the list MEANS (schemas
# not loaded, therefore not directly callable, load with ToolSearch) and, by
# saying so, makes the list's exhaustiveness the discriminator: a tool absent
# from it is already loaded. Without that framing a model reading a bare tag
# cannot tell "already visible" from "needs a search" and defensively searches
# for tools it can already call, burning a full LLM turn (measured live twice
# on always-loaded `weather`, plus a `timer` control, 2026-07-29).
_DEFERRED_TOOLS_PREAMBLE = (
    "The tools named below are NOT loaded: only the name is known, with no parameter schema, "
    "so they cannot be called yet. Load one with ToolSearch (for example, the query "
    '"select:<name>") and it becomes directly callable on the next turn. '
    "This list is complete. Every other tool you can see a schema for is already loaded and "
    "is directly callable now, with no ToolSearch first."
)


def format_deferred_tools_block(refs: Sequence[ToolSchemaRef], *, limit: int | None = None) -> str:
    """Render the model-visible deferred tool catalog."""
    if not refs:
        return ""
    selected = list(refs[:limit]) if limit is not None else list(refs)
    lines = ["<available-deferred-tools>", _DEFERRED_TOOLS_PREAMBLE]
    for ref in selected:
        lines.append(ref.name)
    lines.append("</available-deferred-tools>")
    return "\n".join(lines)


def clear_deferred_tool_registry(user_id: NonEmptyUserId | str | None = None) -> None:
    """Clear resolver state for isolated tests or a specific user scope."""
    scoped_user_id = _normalize_user_id(user_id)
    if not scoped_user_id:
        _REGISTRIES.clear()
        return
    for key in list(_REGISTRIES.keys()):
        if key[0] == scoped_user_id:
            _REGISTRIES.pop(key, None)


__all__ = [
    "CANONICAL_TOOL_SEARCH_NAME",
    "CRITICAL_ALWAYS_LOAD_TOOLS",
    "DEFAULT_DEFER_THRESHOLD",
    "LEGACY_TOOL_SEARCH_NAME",
    "MAX_DEFERRED_TOOL_REGISTRIES",
    "RESOLUTION_RESOLVABLE",
    "UNRESOLVED_MISSING_USER_SCOPE",
    "UNRESOLVED_REGISTRY_MISSING",
    "UNRESOLVED_SCHEMA_CHANGED",
    "UNRESOLVED_UNKNOWN_REFERENCE",
    "DeferredToolPool",
    "DeferredToolResolver",
    "ToolDefinition",
    "ToolSchemaRef",
    "UnresolvedToolReference",
    "build_tool_reference_frame",
    "clear_deferred_tool_registry",
    "deferred_tool_pool_from_payload",
    "deferred_tool_pool_to_payload",
    "expand_tools_with_deferred_references",
    "extract_tool_references_from_messages",
    "format_deferred_tools_block",
    "is_deferred_tool",
    "list_tools_deferred",
    "resolve_tool_references",
    "resolve_tool_references_detailed",
    "tool_reference_resolution_status",
]
