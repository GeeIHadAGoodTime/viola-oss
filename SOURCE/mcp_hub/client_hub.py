"""MCP Client Hub — single interface for all tool operations.

The hub connects to one or more MCP servers (in-process or external),
discovers their tools, checks approval, executes calls, and converts
results to the legacy format expected by the agent loop.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from typing import Any

from core.logging_config import get_logger
from intent.irreversible_actions import is_irreversible_tool_call
from intent.tool_types import RiskLevel
from intent.tools.deferred_tool_schemas import (
    CANONICAL_TOOL_SEARCH_NAME,
    LEGACY_TOOL_SEARCH_NAME,
    DeferredToolPool,
    format_deferred_tools_block,
    list_tools_deferred as split_tools_deferred,
)

from .approval_bridge import (
    ApprovalBridge,
    recompute_auto_approve,
    set_tool_annotations,
    validate_risk_map,
)
from .inprocess import InProcessConnection
from .tool_surface import ToolSurface
from .types import ServerConfig

try:
    from core.types import UserContext
except ImportError:
    UserContext = Any  # type: ignore[misc,assignment]

logger = get_logger(__name__)


def _confirmation_required_result(deferred: Any) -> dict[str, Any]:
    envelope = deferred.to_envelope() if hasattr(deferred, "to_envelope") else {"confirmation_required": True}
    tool_name = str(envelope.get("tool_name") or getattr(deferred, "tool_name", "") or "")
    return {
        "success": False,
        "data": envelope,
        "display": "",
        "error": "confirmation_required",
        "error_category": "CONFIRMATION_REQUIRED",
        "blocked_tool": tool_name,
        "retryable": True,
    }


def _launch_gated_tool_names() -> set[str]:
    try:
        from services.oauth.google import is_google_restricted_features_enabled

        if is_google_restricted_features_enabled():
            return set()
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.debug("Restricted Google launch-gate lookup failed closed: %s", exc)
    return {"gmail", "google_workspace"}


def _is_exit_stack_cancel_cleanup_error(exc: BaseException) -> bool:
    """Return True for MCP/AnyIO shutdown cancellation noise we intentionally suppress."""
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, RuntimeError) and "cancel scope" in str(exc):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return all(_is_exit_stack_cancel_cleanup_error(child) for child in exc.exceptions)
    return False


def _clear_current_task_cancellation() -> None:
    """Clear cancellation state left behind by suppressed MCP memory-transport cleanup."""
    task = asyncio.current_task()
    if task is None:
        return
    cancelling = getattr(task, "cancelling", None)
    uncancel = getattr(task, "uncancel", None)
    if not callable(cancelling) or not callable(uncancel):
        return
    while cancelling():
        uncancel()


_CODEX_DIRECT_COMMAND_HINTS: frozenset[str] = frozenset(
    {
        "bash",
        "cat",
        "cmd",
        "curl",
        "del",
        "echo",
        "git",
        "ls",
        "node",
        "npm",
        "npx",
        "perl",
        "pip",
        "pip3",
        "powershell",
        "pwd",
        "pwsh",
        "python",
        "python3",
        "rm",
        "rmdir",
        "sh",
        "wget",
        "where",
        "which",
    }
)
_CODEX_SHELL_DIRECTIVE_RE = re.compile(
    r"\b(?:run|execute|invoke)\b.{0,80}\b(?:shell|terminal|command|stdout|stderr)\b"
    r"|\b(?:shell|terminal)\s+command\b"
    r"|\breturn\s+(?:only\s+)?(?:its\s+)?std(?:out|err)\b",
    re.IGNORECASE | re.DOTALL,
)
_CODEX_SHELL_OPERATOR_RE = re.compile(r"&&|\|\||[|;`]|>>?(?:\s|$)|\$\s*\(|[<>]\(|\$\(\(|\$\{")


def _build_user_context_meta(user_context: Any) -> dict[str, Any] | None:
    """Convert a user context object into MCP request metadata."""
    if user_context is None:
        return None

    if isinstance(user_context, dict):
        source = user_context
    else:
        source = {
            "user_id": getattr(user_context, "user_id", None),
            "session_id": getattr(user_context, "session_id", None),
            "device_id": getattr(user_context, "device_id", None),
            "request_id": getattr(user_context, "request_id", None),
            "deployment_mode": getattr(user_context, "deployment_mode", None),
            "task_id": getattr(user_context, "task_id", None),
            "browser_task_id": getattr(user_context, "browser_task_id", None),
            "gate_session_id": getattr(user_context, "gate_session_id", None),
            "payment_gate_active": getattr(user_context, "payment_gate_active", None),
            "payment_gate_override_token": getattr(user_context, "payment_gate_override_token", None),
            "signature_gate_override_token": getattr(user_context, "signature_gate_override_token", None),
            "signature_gate_override_actions": getattr(user_context, "signature_gate_override_actions", None),
            "shell_permission_granted": getattr(user_context, "shell_permission_granted", None),
        }

    user_id = source.get("user_id")
    if not user_id:
        return None

    meta = {
        "viola_user_context": {
            "user_id": user_id,
            "session_id": source.get("session_id"),
            "device_id": source.get("device_id"),
            "request_id": source.get("request_id"),
            "deployment_mode": source.get("deployment_mode"),
            "task_id": source.get("task_id"),
            "browser_task_id": source.get("browser_task_id"),
            "gate_session_id": source.get("gate_session_id"),
            "payment_gate_active": source.get("payment_gate_active"),
            "payment_gate_override_token": source.get("payment_gate_override_token"),
            "signature_gate_override_token": source.get("signature_gate_override_token"),
            "signature_gate_override_actions": source.get("signature_gate_override_actions"),
        }
    }
    if source.get("shell_permission_granted") is True:
        meta["viola_user_context"]["shell_permission_granted"] = True
    payment_confirmation = source.get("payment_confirmation")
    if isinstance(payment_confirmation, dict):
        allowed_payment_keys = {
            "card_label",
            "ceiling_override",
            "confirmation_token",
            "cvc_one_shot",
            "payment_session_id",
        }
        meta["viola_user_context"]["payment_confirmation"] = {
            key: payment_confirmation.get(key)
            for key in allowed_payment_keys
            if payment_confirmation.get(key) is not None
        }
    share_response_context = source.get("share_response_context")
    if isinstance(share_response_context, dict):
        meta["share_response_context"] = share_response_context
    deferred_tool_pool = source.get("deferred_tool_pool")
    if isinstance(deferred_tool_pool, dict):
        meta["viola_deferred_tool_pool"] = deferred_tool_pool
    return meta


def _first_prompt_line(value: str) -> str:
    """Return the first non-empty prompt line for command-shape checks."""
    for line in value.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _first_token(value: str) -> str:
    """Extract and normalize the first token from a shell-like text line."""
    stripped = value.strip()
    if not stripped:
        return ""
    if stripped[0] in {'"', "'"}:
        end = stripped.find(stripped[0], 1)
        token = stripped[1:end] if end > 0 else stripped[1:]
    else:
        token = stripped.split(None, 1)[0]
    token = token.strip("\"'")
    token = token.replace("\\", "/").rsplit("/", 1)[-1]
    if token.lower().endswith(".exe"):
        token = token[:-4]
    return token.lower()


def _looks_like_direct_shell_payload(prompt: str) -> bool:
    """Return True when a Codex prompt is shaped like direct shell delegation."""
    stripped = prompt.strip()
    if not stripped:
        return False
    if _CODEX_SHELL_DIRECTIVE_RE.search(stripped):
        return True

    first_line = _first_prompt_line(stripped)
    token = _first_token(first_line)
    if token in _CODEX_DIRECT_COMMAND_HINTS:
        return True
    if _CODEX_SHELL_OPERATOR_RE.search(first_line):
        return True

    # Paths at the front of the prompt indicate a raw command payload rather
    # than a normal Codex code-review/delegation task.
    return bool(re.match(r"^(?:\.{0,2}[\\/]|[A-Za-z]:[\\/])", first_line))


def _codex_arg_sandbox_refusal(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    """Apply shell command safety checks to direct Codex MCP prompt payloads."""
    if name not in {"codex__codex", "codex.codex"}:
        return None

    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not _looks_like_direct_shell_payload(prompt):
        return None

    from intent.tools.shell import _is_denied

    denied_reason = _is_denied(prompt)
    if denied_reason is None:
        return None

    logger.warning("Denied Codex MCP shell-like prompt for tool '%s'", name)
    return {
        "success": False,
        "data": None,
        "display": "",
        "error": denied_reason,
        "error_category": "SHELL_SANDBOX_BLOCKED",
        "blocked_tool": name,
        "retryable": False,
    }


def _session_call_tool_compat(
    session: Any,
    wire_name: str,
    args: dict[str, Any],
    *,
    read_timeout_seconds: Any,
    progress_callback: Any,
    meta: dict[str, Any] | None,
) -> Any:
    """Call MCP sessions with timeout/progress kwargs, falling back for legacy sessions."""
    try:
        return session.call_tool(
            wire_name,
            args,
            read_timeout_seconds=read_timeout_seconds,
            progress_callback=progress_callback,
            meta=meta,
        )
    except TypeError as exc:
        message = str(exc)
        if "unexpected keyword argument" not in message:
            raise
        if "read_timeout_seconds" not in message and "progress_callback" not in message:
            raise
        logger.debug("MCP session call_tool lacks timeout/progress kwargs; using legacy call shape")
        return session.call_tool(wire_name, args, meta=meta)


def _legacy_computer_alias(tool_name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Translate retired desktop tool names to the unified computer action surface."""
    if tool_name == "analyze_screen":
        return "computer", {
            "action": "analyze_screen",
            "question": args.get("question", ""),
            "capture_mode": args.get("capture_mode", "active_window"),
        }

    if tool_name == "desktop_volume":
        return "computer", {
            "action": "volume",
            "volume_action": args.get("action", "get"),
            "level": args.get("percent"),
            "mute": args.get("mute"),
            "step": args.get("step", 10),
        }

    if tool_name == "desktop_observe":
        action = str(args.get("action") or "list").strip().lower()
        if action == "list":
            return "computer", {"action": "list_windows"}
        if action == "read":
            return "computer", {
                "action": "read_window",
                "title": args.get("title", ""),
            }
        if action == "screenshot":
            return "computer", {
                "action": "screenshot",
                "region": args.get("region", "full"),
            }
        if action == "region":
            return "computer", {
                "action": "observe_region",
                "x": args.get("x"),
                "y": args.get("y"),
                "width": args.get("width"),
                "height": args.get("height"),
            }
        return "computer", {"action": action}

    if tool_name == "desktop_interact":
        action = str(args.get("action") or "focus").strip().lower()
        if action == "focus":
            return "computer", {
                "action": "focus_window",
                "title": args.get("title", ""),
            }
        if action == "click":
            return "computer", {
                "action": "click",
                "name": args.get("name", ""),
                "target_window": args.get("window_title", ""),
            }
        if action == "type":
            return "computer", {
                "action": "type",
                "text": args.get("text", ""),
                "target_window": args.get("window_title", ""),
                "respect_focus": args.get("respect_focus", True),
            }
        if action in {"hotkey", "key"}:
            return "computer", {
                "action": "key",
                "keys": args.get("keys", ""),
                "target_window": args.get("window_title", ""),
            }
        if action == "mouse_click":
            return "computer", {
                "action": "click",
                "x": args.get("x", 0),
                "y": args.get("y", 0),
                "button": args.get("button", "left"),
                "coordinate_mode": "physical",
            }
        if action == "scroll":
            return "computer", {
                "action": "scroll",
                "direction": args.get("direction", "down"),
                "amount": args.get("amount", 5),
                "target_window": args.get("window_title", ""),
            }
        return "computer", {"action": action}

    return None


class MCPClientHub:
    """Central hub that connects to MCP servers and routes tool calls.

    Usage::

        hub = MCPClientHub(approval_bridge=bridge)
        await hub.initialize(configs)
        result = await hub.call_tool("file_read", {"path": "."})
        await hub.shutdown()
    """

    def __init__(self, approval_bridge: ApprovalBridge) -> None:
        self._bridge = approval_bridge
        # server_name -> ClientSession
        self._sessions: dict[str, Any] = {}
        # server_name -> ServerConfig
        self._configs: dict[str, ServerConfig] = {}
        # tool_name -> server_name (routing table)
        self._tool_routes: dict[str, str] = {}
        # tool_name -> tool schema dict (from list_tools)
        self._tool_schemas: dict[str, dict[str, Any]] = {}
        # Tools hidden from LLM but still callable via call_tool()
        self._hidden_tools: set[str] = set()
        # tool_name -> list of reasons explaining why the tool is hidden
        self._hidden_tool_reasons: dict[str, set[str]] = {}
        # compound tool name -> granular child names that were collapsed
        self._compound_children: dict[str, list[str]] = {}
        # Each memory transport owns its task-affine contexts independently of
        # whichever request lazily connects or later disconnects the server.
        self._inprocess_connections: dict[str, InProcessConnection] = {}
        # stdio transport: server_name -> streams context manager
        self._streams_contexts: dict[str, Any] = {}
        # stdio transport: server_name -> MCPServerLauncher instance
        self._launchers: dict[str, Any] = {}
        self._initialized = False
        # Optional async callback invoked when a browser tool is requested
        # but the browser server is not connected.  Set by the controller
        # to trigger on-demand reconnection.  Signature: async () -> bool
        self._browser_reconnect_cb: Any = None
        self.last_approval_path: str | None = None
        # Lazy browser server initialization (2026-04-08):
        # Deferred server configs are not connected at startup — they are
        # connected on first access (when a tool from that server is requested).
        # This eliminates parsing duplicate tool schemas at startup when
        # both Playwright and CDP browser servers are registered but only
        # one is configured for use.
        self._deferred_server_configs: dict[str, ServerConfig] = {}
        # Lock to prevent concurrent lazy-init of the same server
        self._lazy_init_lock: asyncio.Lock = asyncio.Lock()
        self._warned_phantom_tool_maps: set[tuple[str, tuple[str, ...]]] = set()
        self._warned_schema_issues: set[tuple[str, str, str]] = set()

    async def initialize(self, configs: list[ServerConfig] | None = None) -> None:
        """Connect to all configured MCP servers.

        Args:
            configs: List of server configurations. If None, no servers
                     are connected (useful for testing).
        """
        if self._initialized:
            logger.warning("MCPClientHub already initialized")
            return

        if configs:
            for config in configs:
                if not config.enabled:
                    logger.debug("Skipping disabled server: %s", config.name)
                    continue
                try:
                    await self.connect_server(config.name, config)
                except Exception as _conn_err:
                    logger.exception("Failed to connect to server '%s'", config.name)
                except BaseException as _conn_err:
                    # py3.11: CancelledError / cancel-noise BaseExceptionGroups
                    # are BaseException and would escape the guard above,
                    # killing boot for every other server. A cancellation that
                    # nobody requested on THIS task (task.cancelling() == 0)
                    # is leaked transport-cleanup noise — contain it and keep
                    # the hub alive. A genuine cancellation propagates.
                    if not self._contain_leaked_connect_cancellation(config.name, _conn_err):
                        raise

        self._initialized = True

        # Populate annotation cache for annotation-based risk derivation.
        # Extract _annotations from stored schemas and pass to the bridge.
        _annotations = {}
        for tool_name, schema in self._tool_schemas.items():
            ann = schema.get("_annotations")
            if ann is not None:
                _annotations[tool_name] = ann
        set_tool_annotations(_annotations)
        recompute_auto_approve()

        # INT-07: safety assertion — recompute_auto_approve MUST have run
        # before the hub declares itself ready.  If this count is zero the
        # module state is broken (frozen-too-early bug).
        from mcp_hub import approval_bridge as _approval_bridge

        assert _approval_bridge._recompute_count >= 1, "AGENT_AUTO_APPROVE recompute did not run after annotation load"

        # Validate RISK_MAP coverage against all registered tools.
        # Logs a WARNING for tools with neither manual entry nor annotations
        # (prevents silent defaults that caused the March 23 collapse).
        validate_risk_map(set(self._tool_schemas.keys()))

        logger.info(
            "MCPClientHub initialized: %d servers, %d tools",
            len(self._sessions),
            len(self._tool_routes),
        )

    def register_deferred_server(self, name: str, config: ServerConfig) -> None:
        """Register a server config for lazy initialization.

        The server is NOT connected immediately.  Instead, it is connected
        on the first ``call_tool()`` request that targets it (detected via
        the ``_browser_reconnect_cb`` path or the explicit deferred check
        in ``call_tool``).

        Use this for the non-configured browser backend so it doesn't
        parse 16 duplicate tool schemas at startup.

        Args:
            name: Logical server name (e.g. "browser-cdp", "browser-pw").
            config: Server connection configuration.
        """
        self._deferred_server_configs[name] = config
        logger.info(
            "Registered deferred server '%s' (will connect on first access)",
            name,
        )

    async def _lazy_connect_deferred(self, server_name: str) -> bool:
        """Connect a deferred server on first access.

        Returns True if the server was successfully connected, False otherwise.
        Thread-safe via ``_lazy_init_lock``.
        """
        async with self._lazy_init_lock:
            # Double-check — another coroutine may have connected it
            if server_name in self._sessions:
                return True
            config = self._deferred_server_configs.pop(server_name, None)
            if config is None:
                return False
            try:
                await self.connect_server(server_name, config)
                logger.info(
                    "Lazy-connected deferred server '%s'",
                    server_name,
                )
                return True
            except Exception:
                logger.exception(
                    "Failed to lazy-connect deferred server '%s'",
                    server_name,
                )
                return False
            except BaseException as exc:
                # See initialize(): contain leaked cancellation noise from a
                # failed connect; propagate genuine cancellation.
                if not self._contain_leaked_connect_cancellation(server_name, exc):
                    raise
                return False

    def _contain_leaked_connect_cancellation(self, server_name: str, exc: BaseException) -> bool:
        """Contain cancellation noise leaked by a failed server connect.

        Returns True when the exception is cancellation state that no caller
        requested for the current task (``task.cancelling() == 0``) — i.e.
        leaked MCP/anyio transport-cleanup noise, which must not take down
        the hub or poison the connecting task. Returns False for genuine
        cancellation (or non-cancel BaseExceptions), which the caller must
        re-raise.
        """
        if not _is_exit_stack_cancel_cleanup_error(exc):
            return False
        task = asyncio.current_task()
        if task is not None and task.cancelling() > 0:
            # Someone actually cancelled this task — honor it.
            return False
        _clear_current_task_cancellation()
        logger.error(
            "Contained leaked cancellation from failed connect to server '%s': %r",
            server_name,
            exc,
        )
        return True

    async def shutdown(self) -> None:
        """Clean disconnect from all servers."""
        if not self._initialized:
            return

        server_names = list(self._sessions.keys())
        for name in server_names:
            try:
                await self.disconnect_server(name)
            except Exception:
                logger.exception("Error disconnecting server '%s'", name)

        try:
            from core.db_backend import close_pg_pool

            await close_pg_pool()
        except Exception:
            logger.exception("Error closing shared PostgreSQL pool")

        self._initialized = False
        logger.info("MCPClientHub shut down")

    async def connect_server(self, name: str, config: ServerConfig) -> None:
        """Connect to a single MCP server.

        Args:
            name: Logical server name.
            config: Server connection configuration.

        Raises:
            ValueError: If transport type is unsupported.
        """
        if name == "google-workspace" and "google_workspace" in _launch_gated_tool_names():
            logger.info("Skipping google-workspace MCP connection because restricted Google features are disabled")
            return

        if name in self._sessions:
            logger.warning("Server '%s' already connected, disconnecting first", name)
            await self.disconnect_server(name)

        if config.transport == "inprocess":
            session = await self._connect_inprocess(name, config)
        elif config.transport == "stdio":
            session = await self._connect_stdio(name, config)
        elif config.transport == "sse":
            msg = "SSE transport not yet implemented"
            raise ValueError(msg)
        else:
            msg = "Unsupported transport: %s" % config.transport
            raise ValueError(msg)

        self._sessions[name] = session
        self._configs[name] = config

        # Discover tools from this server
        try:
            await self._discover_tools(name, session)
        except BaseException:
            await self.disconnect_server(name)
            raise

        # Google Workspace compound tool consolidation:
        # After discovering granular tools, register compound schemas and
        # hide the granular originals from the LLM surface.
        if name == "google-workspace":
            self._register_compound_tools()

        # After each server connects, validate tool maps against registered
        # tools.  Phantom names (typos, unimplemented tools) are logged as
        # warnings so they're caught in development rather than silently
        # excluded at runtime — the root cause of the browser_fill_ref bug.
        self._validate_tool_maps()
        self._validate_tool_schemas()
        self.hide_runtime_unavailable_tools()

    def _validate_tool_maps(self) -> None:
        """Warn about tool names in maps that don't match any registered tool.

        _TIER_TOOL_MAP is the user-facing risk-control feature: Solo, Ensemble,
        and Symphony each define a real tool surface so users pick how much
        autonomy Viola has. _TASK_TOOL_MAP and _FOCUSED_TOOL_MAP were removed
        as overengineering — only the simple 3-tier allow-list remains. Spend
        counters and approval policy are orthogonal safety layers.
        """
        registered = set(self._tool_schemas.keys())
        if not registered:
            return
        allowed_absent: set[str] = set()
        if not any(name.startswith("browser_") for name in registered):
            allowed_absent.update(
                tool_name
                for tool_set in self._TIER_TOOL_MAP.values()
                if tool_set is not None
                for tool_name in tool_set
                if tool_name.startswith("browser_")
            )
        for tier_name, tool_set in self._TIER_TOOL_MAP.items():
            if tool_set is None:
                continue
            phantom = tool_set - registered - _launch_gated_tool_names() - allowed_absent
            if phantom:
                warning_key = (tier_name, tuple(sorted(phantom)))
                if warning_key in self._warned_phantom_tool_maps:
                    continue
                self._warned_phantom_tool_maps.add(warning_key)
                logger.warning(
                    "Phantom tools in _TIER_TOOL_MAP[%s]: %s — these tools "
                    "are not registered by any MCP server and will be "
                    "silently excluded from the agent's toolbox",
                    tier_name,
                    sorted(phantom),
                )

    def _validate_tool_schemas(self) -> None:
        """Warn about tool schemas that are too vague for small models.

        Catches the class of bug where a tool parameter is typed as bare
        ``dict`` / ``object`` with no properties — the LLM has no schema
        guidance and silently falls back to a simpler tool.
        """
        for name, schema in self._tool_schemas.items():
            input_schema = schema.get("inputSchema", {})
            props = input_schema.get("properties", {})
            for param_name, param_schema in props.items():
                ptype = param_schema.get("type", "")
                # Bare object with neither fields nor an explicit map policy.
                if (
                    ptype == "object"
                    and not param_schema.get("properties")
                    and "additionalProperties" not in param_schema
                ):
                    warning_key = (name, param_name, "bare_object")
                    if warning_key in self._warned_schema_issues:
                        continue
                    self._warned_schema_issues.add(warning_key)
                    logger.warning(
                        "Tool %s param '%s' is bare object with no properties — "
                        "small models cannot construct this. Add a typed schema.",
                        name,
                        param_name,
                    )
                # Array with bare object items
                if ptype == "array":
                    items = param_schema.get("items", {})
                    if (
                        items.get("type") == "object"
                        and not items.get("properties")
                        and "additionalProperties" not in items
                        and not items.get("$ref")
                    ):
                        warning_key = (name, param_name, "bare_object_array")
                        if warning_key in self._warned_schema_issues:
                            continue
                        self._warned_schema_issues.add(warning_key)
                        logger.warning(
                            "Tool %s param '%s' is array of bare objects — "
                            "small models cannot construct this. Add item schema.",
                            name,
                            param_name,
                        )

    async def _connect_inprocess(self, name: str, config: ServerConfig) -> Any:
        """Connect to an in-process MCP server.

        Imports the module specified in config.module and calls the first
        supported server factory it exposes to get the FastMCP server
        instance, then creates a connected client session via memory
        transport.
        """
        # Import the server module and get the FastMCP instance
        if config.module:
            import importlib

            module = importlib.import_module(config.module)
            factory_names = (
                "create_core_tools_server",
                "create_computer_use_server",
                "create_browser_cdp_server",
                "create_browser_server",
            )
            factory = None
            for factory_name in factory_names:
                factory = getattr(module, factory_name, None)
                if factory is not None:
                    break
            if factory is None:
                msg = "Module '%s' has no supported factory (%s)" % (
                    config.module,
                    ", ".join(factory_names),
                )
                raise ValueError(msg)
            server = factory()
        else:
            msg = "In-process server '%s' requires 'module' in config" % name
            raise ValueError(msg)

        connection = InProcessConnection(name)
        session = await connection.start(server)
        self._inprocess_connections[name] = connection

        logger.info("Connected to in-process server '%s'", name)
        return session

    async def _connect_stdio(self, name: str, config: ServerConfig) -> Any:
        """Connect to an external MCP server via stdio subprocess.

        Launches the server as a child process and connects over stdin/stdout
        using the MCP SDK's stdio_client transport.

        Args:
            name: Logical server name.
            config: Server configuration with command and args.

        Returns:
            Connected ClientSession for the external server.
        """
        from . import launcher as _launcher_mod

        launcher = _launcher_mod.MCPServerLauncher()
        session, streams_ctx = await launcher.launch(config)

        # Store the owner-task handle and launcher for cleanup. The transport
        # contexts live inside the launcher's owner task (task-affine anyio
        # scopes); disconnect_server passes this handle back to launcher.stop.
        self._streams_contexts[name] = streams_ctx
        self._launchers[name] = launcher

        logger.info("Connected to external server '%s' via stdio", name)
        return session

    async def _discover_tools(self, server_name: str, session: Any) -> None:
        """Discover and register tools from a connected server.

        For external (non-inprocess) servers, tool names are registered
        under Claude's canonical ``mcp__<server>__<tool>`` form (matches
        ``services/mcp/client.ts:1768-1775`` and
        ``services/mcp/mcpStringUtils.ts:48-66``). Viola's legacy
        ``<server>__<tool>`` alias is also registered as a routing-only
        compatibility name so existing permission rules, prompts, and
        deferred-tool references keep working until they migrate.

        Built-in (``namespace=False``) servers register tools under their
        natural names — built-ins do not collide with Claude's canonical
        ``mcp__`` namespace, and renaming them would break every prompt
        that mentions ``browser_navigate`` / ``calendar`` / etc.
        """
        from .mcp_string_utils import build_mcp_tool_name, viola_legacy_alias

        result = await session.list_tools()

        # Prefix tool names only when the server config has namespace=True.
        # Built-in servers (core-tools, browser) use namespace=False
        # so their tools are accessible by their natural names (e.g. browser_navigate).
        # User-configured external servers use namespace=True to avoid conflicts.
        config = self._configs.get(server_name)
        is_external = config is not None and config.namespace

        new_count = 0
        for tool in result.tools:
            if is_external:
                # Claude canonical name is the model-visible primary; the
                # legacy ``<server>__<tool>`` alias is registered for
                # backward-compatible permission/dispatch lookups only
                # (no separate schema entry — the model never sees it).
                tool_name = build_mcp_tool_name(server_name, tool.name)
                legacy_alias = viola_legacy_alias(server_name, tool.name)
            else:
                tool_name = tool.name
                legacy_alias = None

            # Conflict handling: first server wins for unprefixed names
            if tool_name in self._tool_routes:
                existing_server = self._tool_routes[tool_name]
                if existing_server != server_name:
                    logger.warning(
                        "Tool '%s' from server '%s' conflicts with '%s', " "skipping (first wins)",
                        tool_name,
                        server_name,
                        existing_server,
                    )
                    continue

            self._tool_routes[tool_name] = server_name
            if legacy_alias and legacy_alias != tool_name and legacy_alias not in self._tool_routes:
                self._tool_routes[legacy_alias] = server_name
            schema_entry: dict[str, Any] = {
                "name": tool_name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema if tool.inputSchema else {},
                # Store the original (unprefixed) name for call_tool routing
                "_original_name": tool.name,
            }
            if legacy_alias and legacy_alias != tool_name:
                schema_entry["_legacy_alias"] = legacy_alias
            if is_external:
                schema_entry["_is_mcp_external"] = True
                schema_entry["_mcp_server"] = server_name
                schema_entry["_mcp_tool"] = tool.name
            # Preserve MCP tool annotations for annotation-based risk derivation.
            if tool.annotations is not None:
                schema_entry["_annotations"] = {
                    "readOnlyHint": tool.annotations.readOnlyHint,
                    "destructiveHint": tool.annotations.destructiveHint,
                }
            if tool.meta is not None:
                schema_entry["_meta"] = dict(tool.meta)
            self._tool_schemas[tool_name] = schema_entry
            new_count += 1
            if not is_external and tool_name == CANONICAL_TOOL_SEARCH_NAME:
                self._tool_routes[LEGACY_TOOL_SEARCH_NAME] = server_name
                legacy_schema_entry = dict(schema_entry)
                legacy_schema_entry["name"] = LEGACY_TOOL_SEARCH_NAME
                legacy_schema_entry["_original_name"] = CANONICAL_TOOL_SEARCH_NAME
                legacy_schema_entry["_canonical_name"] = CANONICAL_TOOL_SEARCH_NAME
                self._tool_schemas[LEGACY_TOOL_SEARCH_NAME] = legacy_schema_entry
                self.hide_tools({LEGACY_TOOL_SEARCH_NAME}, reason="claude_toolsearch_alias")

        logger.info(
            "Discovered %d tools from server '%s'",
            new_count,
            server_name,
        )

    def _schema_for_route_name(self, name: str) -> dict[str, Any]:
        """Return the schema for a routed name, including route-only aliases."""
        schema = self._tool_schemas.get(name)
        if schema is not None:
            return schema
        for candidate in self._tool_schemas.values():
            if candidate.get("_legacy_alias") == name:
                return candidate
        return {}

    def _forget_tool_name(self, tool_name: str) -> None:
        """Remove a tool from all hub-owned lookup/visibility indexes."""
        self._tool_routes.pop(tool_name, None)
        self._tool_schemas.pop(tool_name, None)
        self._hidden_tools.discard(tool_name)
        self._hidden_tool_reasons.pop(tool_name, None)
        self._compound_children.pop(tool_name, None)

    def _register_compound_tools(self) -> None:
        """Register compound tool schemas and hide granular originals.

        Called after Google Workspace MCP tool discovery to consolidate
        56 granular tools into 9 compound tools (gmail, google_calendar, etc.).
        Granular tools remain callable via ``call_tool()`` but are hidden
        from the LLM-facing tool surface.
        """
        from .compound_tools import (
            COMPOUND_REGISTRY,
            HIDDEN_GRANULAR_TOOLS,
            build_compound_schemas,
        )

        compound_schemas = build_compound_schemas(self._tool_schemas)
        for schema in compound_schemas:
            name = schema["name"]
            self._tool_schemas[name] = schema
            # Route compound tools to a virtual server name so call_tool()
            # can detect them.  The actual dispatch is handled by
            # resolve_compound_call() which recurses into call_tool().
            self._tool_routes[name] = "_compound"
            registry_actions = COMPOUND_REGISTRY.get(name, [])
            self._compound_children[name] = sorted(
                {
                    original_tool
                    for _action_name, original_tool, _desc in registry_actions
                    if original_tool in self._tool_schemas
                }
            )

        # Hide granular tools that are now covered by compound tools.
        # They remain callable (call_tool resolves them) but are not
        # shown to the LLM.
        self.hide_tools(HIDDEN_GRANULAR_TOOLS & set(self._tool_schemas), reason="compound_hidden")
        if "calendar" in self._tool_schemas and "google_calendar" in self._tool_schemas:
            # The generic calendar tool already covers provider selection and
            # CRUD actions. Keep the Google-specific compound available for
            # direct/internal calls, but hide it from the default LLM surface
            # so the model takes the provider-agnostic path first.
            self.hide_tools({"google_calendar"}, reason="compound_hidden")

    async def disconnect_server(self, name: str) -> None:
        """Disconnect from a server and remove its tools."""
        if name not in self._sessions:
            logger.warning("Server '%s' not connected", name)
            return

        connection = self._inprocess_connections.pop(name, None)
        if connection is not None:
            await connection.close()

        # Clean up stdio contexts if present
        if name in self._streams_contexts:
            launcher = self._launchers.get(name)
            if launcher:
                await launcher.stop(self._streams_contexts[name], self._sessions[name])
            del self._streams_contexts[name]
            self._launchers.pop(name, None)

        # Remove tool routes for this server
        tools_to_remove = [tool_name for tool_name, server_name in self._tool_routes.items() if server_name == name]
        for tool_name in tools_to_remove:
            self._forget_tool_name(tool_name)

        # Also remove compound tools if this was the google-workspace server
        if name == "google-workspace":
            compound_to_remove = [
                tool_name for tool_name, server_name in self._tool_routes.items() if server_name == "_compound"
            ]
            for tool_name in compound_to_remove:
                self._forget_tool_name(tool_name)
            tools_to_remove.extend(compound_to_remove)

        del self._sessions[name]
        self._configs.pop(name, None)

        logger.info(
            "Disconnected server '%s', removed %d tools",
            name,
            len(tools_to_remove),
        )

    def hide_tools(self, names: set[str], *, reason: str = "manual_hidden") -> None:
        """Hide tools from LLM-facing lists without removing them.

        Hidden tools are excluded from ``list_tools()`` and
        ``get_tool_schemas()`` but remain callable via ``call_tool()``.

        Args:
            names: Set of tool names to hide.
        """
        self._hidden_tools |= names
        for name in names:
            self._hidden_tool_reasons.setdefault(name, set()).add(reason)
        actually_hidden = names & set(self._tool_schemas)
        logger.info(
            "Hidden %d tools from LLM: %s",
            len(actually_hidden),
            sorted(actually_hidden),
        )

    def _runtime_unavailable_tools(self) -> set[str]:
        """Return tools that should be hidden because this runtime cannot use them."""
        hidden: set[str] = set()

        try:
            from config.settings import settings

            telnyx_api_key = getattr(settings, "telnyx_api_key", "") or ""
            telnyx_phone_number = getattr(settings, "telnyx_phone_number", "") or ""
            telnyx_sip_connection_id = getattr(settings, "telnyx_sip_connection_id", "") or ""
            if not all((telnyx_api_key, telnyx_phone_number, telnyx_sip_connection_id)):
                hidden.add("phone")
        except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
            logger.debug("Could not read settings for runtime tool visibility")

        return hidden

    def hide_runtime_unavailable_tools(self) -> set[str]:
        """Hide tools whose hard prerequisites are missing in this runtime."""
        hidden = self._runtime_unavailable_tools()
        if hidden:
            self.hide_tools(hidden, reason="runtime_unavailable")
        return hidden

    def remove_tool_schema(self, name: str) -> None:
        """Remove a single tool from LLM-facing lists.

        Convenience wrapper around ``hide_tools`` for backward compat.
        """
        self.hide_tools({name}, reason="manual_hidden")

    def build_tool_surface(
        self,
        tier: str | None = None,
        interactive: bool = True,
        provider_native: list[dict[str, Any]] | None = None,
        step_log_visible: list[dict[str, Any]] | None = None,
    ) -> ToolSurface:
        """Return the canonical runtime tool surface for the current turn."""
        baseline_visible = self.list_tools(
            tier=None,
            interactive=True,
        )
        hub_visible = self.list_tools(
            tier=tier,
            interactive=interactive,
        )
        hidden_reasons = {name: sorted(reasons) for name, reasons in self._hidden_tool_reasons.items()}
        baseline_names = {tool.get("name", "") for tool in baseline_visible}
        visible_names = {tool.get("name", "") for tool in hub_visible}
        filtered_out = {str(name) for name in baseline_names - visible_names if str(name)}
        if not interactive:
            for name in filtered_out:
                hidden_reasons.setdefault(name, [])
                if "interactive_excluded" not in hidden_reasons[name]:
                    hidden_reasons[name].append("interactive_excluded")
                    hidden_reasons[name].sort()
        if tier is not None:
            for name in filtered_out:
                hidden_reasons.setdefault(name, [])
                if "tier_hidden" not in hidden_reasons[name]:
                    hidden_reasons[name].append("tier_hidden")
                    hidden_reasons[name].sort()
        return ToolSurface.build(
            hub_visible=hub_visible,
            hidden_reasons=hidden_reasons,
            collapsed_from=self._compound_children,
            provider_native=provider_native,
            step_log_visible=step_log_visible,
        )

    # Tools that need a longer per-call timeout (120s vs default 60s).
    # Browser operations and web searches may involve page loads, JS execution, etc.
    _SLOW_TOOLS: frozenset[str] = frozenset(
        {
            "browser_navigate",
            "browser_interact",
            "browser_fill_form",
            "browser_screenshot",
            "browser_scroll",
            "browser_evaluate",
            "browser_run_script",
            "web_search",
            "web_read",
            "file_write",  # MCP server self-authoring may be slow
            # ALL Google Workspace tools need full timeout — API calls
            # include OAuth token refresh, cold start, and retry logic.
            # Listed individually (not wildcard) so each is explicit.
            "gmail_search",
            "gmail_get",
            "gmail_send",
            "gmail_createDraft",
            "gmail_sendDraft",
            "gmail_modify",
            "gmail_batchModify",
            "gmail_modifyThread",
            "gmail_downloadAttachment",
            "gmail_listLabels",
            "gmail_createLabel",
            "calendar_list",
            "calendar_listEvents",
            "calendar_getEvent",
            "calendar_createEvent",
            "calendar_updateEvent",
            "calendar_deleteEvent",
            "calendar_findFreeTime",
            "calendar_respondToEvent",
            "chat_listSpaces",
            "chat_findSpaceByName",
            "chat_sendMessage",
            "chat_getMessages",
            "chat_sendDm",
            "chat_findDmByEmail",
            "chat_listThreads",
            "chat_setUpSpace",
            "docs_getText",
            "docs_create",
            "docs_writeText",
            "docs_replaceText",
            "docs_formatText",
            "docs_getSuggestions",
            "drive_search",
            "drive_downloadFile",
            "drive_moveFile",
            "drive_trashFile",
            "drive_renameFile",
            "drive_findFolder",
            "drive_createFolder",
            "drive_getComments",
            "sheets_getText",
            "sheets_getRange",
            "sheets_getMetadata",
            "slides_getText",
            "slides_getMetadata",
            "slides_getImages",
            "slides_getSlideThumbnail",
            "people_getUserProfile",
            "people_getMe",
            "people_getUserRelations",
            "auth_clear",
            "auth_refreshToken",
        }
    )

    # Phone call tools need much longer timeouts — calls can take 5-10 minutes.
    # max_call_duration in TelnyxConfig is 600s; we add 60s grace for dial + summary.
    _PHONE_TOOLS: frozenset[str] = frozenset(
        {
            "phone",
            "transmit_payment_to_call",
        }
    )

    # Gate-resume tools spawn a fresh agent loop that drives the resumed task to
    # completion (or the next handoff). For long browser-driven filing or
    # checkout flows with several post-review pages,
    # this can legitimately take many minutes. The default 120s "everything else"
    # bucket cuts the resumed loop mid-flow, returns ok=False to the calling
    # agent, and the cancellation clobbers the override/checkpoint state — making
    # retry impossible. Match the phone-tool 660s budget: the resumed task is
    # the same class of long-running work.
    _GATE_RESUME_TOOLS: frozenset[str] = frozenset(
        {
            "resume_signature_gate",
            "resume_payment_gate",
            "cancel_signature_gate",
            "cancel_payment_gate",
        }
    )

    # Tier-based tool visibility (Solo/Ensemble/Symphony permission tiers).
    # Tiers are a real risk-control feature: users pick how much autonomy Viola
    # has via Settings → Capability Tier. Allow-lists below match the UI
    # capability table in ui/react-app/src/components/SettingsModal.jsx
    # (TIER_CAPABILITIES). New tools default to Symphony-only unless explicitly
    # added here. Keep it simple: three allow-lists, no per-action filtering.
    _SOLO_TOOLS: frozenset[str] = frozenset(
        {
            # --- Plumbing (always available) ---
            "think",
            "ToolSearch",
            "tool_search",
            "ask_user",
            # --- Memory & knowledge (UI: Memory) ---
            "memory",
            "workbench",
            # --- Web read (UI: Web search) ---
            "web_search",
            "web_read",
            "weather",
            # --- Music & playback (UI: Music & playback) ---
            "media",
            "playback",
            "view_queue",
            "playlist",
            "get_liked_songs",
            "rate_track",
            "check_music_provider_status",
            "connect_music_provider",
            # --- Basic features (timers, reminders, notifications) ---
            "timer",
            "alarm",
            "notify",
            # --- Personal local calendar (UI: Scheduling). The always-on LOCAL
            # calendar is the user's own local data, same risk class as memory /
            # alarms / notify already granted at Solo; excluding it left the
            # default (Solo) install unable to add or read events, so the agent
            # fell back to open_app_panel or a reminder (#1015). Remote sync
            # (Google/Microsoft/CalDAV) still requires a separate OAuth consent;
            # irreversible calendar writes (e.g. delete) stay confirmation-gated
            # by the tier-independent unified confirmation gate. ---
            "calendar",
            # --- Self-reporting & introspection ---
            "file_bug_report",
            "user_capabilities",
            "run_user_capability",
            # --- Settings (user can change their own tier) ---
            "user_settings",
            # --- Viola UI navigation (own panels only, not third-party apps) ---
            "open_app_panel",
            "pair_speaker_setup",
            # --- Pending-gate plumbing (works at every tier) ---
            "resume_signature_gate",
            "cancel_signature_gate",
            "resume_payment_gate",
            "cancel_payment_gate",
            # --- Smart home (founder-locked 2026-07-04): available at every
            # tier — users who don't want it simply don't connect Home
            # Assistant. Control actions still route through confirm-tier
            # approval via the tool's own _CONFIRM annotation
            # (mcp_servers/core_tools/server.py), so the tier no longer
            # pre-hides the tool but per-action approval still applies. ---
            "smart_home",
        }
    )
    _ENSEMBLE_TOOLS: frozenset[str] = _SOLO_TOOLS | frozenset(
        {
            # --- Filesystem (UI: File access — read-only at Ensemble) ---
            "file_read",
            # --- Email & workspace (UI: Email). Compound tools — send-side
            # actions are approval-gated by mcp_hub/approval_bridge.py. ---
            "gmail",
            "google_workspace",
            # --- Calendar & scheduling (UI: Scheduling) ---
            "calendar",
            "schedule",
            "check_pending_tasks",
            # --- System introspection ---
            "system_info",
            "check_api_registry",
            "api_credential",
            "mcp_servers",
            # --- Browser (UI: Browser — sandboxed, read-only at Ensemble) ---
            "browser_navigate",
            "browser_back",
            "browser_forward",
            "browser_refresh",
            "browser_snapshot",
            "browser_get_text",
            "browser_screenshot",
            "browser_scroll",
            "browser_wait",
            "browser_close",
            "browser_status",
            "browser_get_api_log",
        }
    )
    _TIER_TOOL_MAP: dict[str, set[str] | None] = {
        "solo": set(_SOLO_TOOLS),
        "ensemble": set(_ENSEMBLE_TOOLS),
        "symphony": None,  # Full access — everything visible
    }
    # Defense-in-depth: tools that must never appear under solo/ensemble even
    # if accidentally added to the allow-lists. Keep this list minimal.
    _TIER_HIDDEN_TOOL_MAP: dict[str, frozenset[str]] = {
        "solo": frozenset({"computer", "desktop_volume"}),
        "ensemble": frozenset({"computer", "desktop_volume"}),
        "symphony": frozenset(),
    }

    # ------------------------------------------------------------------
    # Shared tool visibility helpers (CONTESTED-9: single source of truth)
    # ------------------------------------------------------------------

    def _resolve_filters(
        self,
        tier: str | None,
        interactive: bool = True,
    ) -> set[str] | None:
        """Compute the legacy tier permission filter for a query.

        Returns:
            ``None`` means all tools are allowed by the legacy allowlist.
            Deny-only tier gates are applied separately.
        """
        allowed_by_tier = self._TIER_TOOL_MAP.get(tier or "", None)
        if not interactive:
            if allowed_by_tier is not None:
                allowed_by_tier = set(allowed_by_tier)
                allowed_by_tier.discard("ask_user")
        return allowed_by_tier

    def _is_tool_visible(
        self,
        name: str,
        allowed_by_tier: set[str] | None,
        *,
        tier: str | None = None,
    ) -> bool:
        """Check whether a tool should be visible given current filters."""
        if name in self._hidden_tools:
            return False
        if name in _launch_gated_tool_names():
            return False
        if allowed_by_tier is not None and name not in allowed_by_tier:
            return False
        normalized_tier = str(tier or "").strip().lower()
        if normalized_tier and name in self._TIER_HIDDEN_TOOL_MAP.get(normalized_tier, frozenset()):
            return False
        return True

    def list_tools(
        self,
        tier: str | None = None,
        interactive: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the visible tools from all connected servers.

        Args:
            tier: Capability label (solo, ensemble, symphony). Solo and
                Ensemble hide desktop computer use; Symphony receives the full surface.
            interactive: When False, ``ask_user`` is excluded.

        Each tool is a dict with name, description, inputSchema (JSON Schema).
        Internal keys (prefixed with ``_``) are excluded from the output.
        Hidden tools (via ``hide_tools``) are excluded.
        Dynamic API tools (from the credential vault) are included for
        symphony-tier users when credentials are available.
        """
        allowed_by_tier = self._resolve_filters(
            tier,
            interactive=interactive,
        )

        cleaned: list[dict[str, Any]] = []
        for schema in self._tool_schemas.values():
            if not self._is_tool_visible(schema["name"], allowed_by_tier, tier=tier):
                continue
            if not interactive and schema["name"] == "ask_user":
                continue
            cleaned.append({k: v for k, v in schema.items() if not k.startswith("_")})

        # Include dynamic API tools for the all-tools surface.
        if allowed_by_tier is None:
            for tool_def in self._get_dynamic_tools():
                if not interactive and tool_def.get("name") == "ask_user":
                    continue
                cleaned.append({k: v for k, v in tool_def.items() if not k.startswith("_")})

        return sorted(cleaned, key=lambda t: t.get("name", ""))

    def list_tools_with_defer_signals(
        self,
        tier: str | None = None,
        interactive: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the same visible tools as ``list_tools`` but keep defer-signal keys.

        Issue #2094: ``list_tools`` strips every underscore-prefixed key
        (including ``_meta``, where ``anthropic/alwaysLoad`` lives) before the
        defer split in ``intent.tools.deferred_tool_schemas`` ever sees a
        tool. That module's ``_list_visible_tools`` already carries a
        ``_preserve_defer_signals`` step meant to keep alwaysLoad/isMcp/
        shouldDefer markers alive into the split (F-014), but when it calls
        ``context.list_tools(...)`` here, the hub had already performed the
        identical strip internally -- so ``_preserve_defer_signals`` received
        dicts with nothing left to preserve. Any tool that relies on the
        ``_meta`` channel alone (not the hardcoded
        ``CRITICAL_ALWAYS_LOAD_TOOLS`` name set -- ``weather`` and
        ``start_agent`` are the two real cases) was silently deferred behind
        ToolSearch regardless of its marker.

        This method applies the exact same tier/interactive filtering as
        ``list_tools`` but returns deep copies of the raw schema entries
        (underscore keys intact), so ``_list_visible_tools`` has something to
        preserve. ``deferred_tool_schemas._list_visible_tools`` prefers this
        method over ``list_tools`` when the context provides it; callers/mocks
        that only implement ``list_tools`` keep the prior (pre-#2094) behavior
        unchanged.
        """
        allowed_by_tier = self._resolve_filters(
            tier,
            interactive=interactive,
        )

        preserved: list[dict[str, Any]] = []
        for schema in self._tool_schemas.values():
            if not self._is_tool_visible(schema["name"], allowed_by_tier, tier=tier):
                continue
            if not interactive and schema["name"] == "ask_user":
                continue
            preserved.append(copy.deepcopy(schema))

        # Include dynamic API tools for the all-tools surface, matching
        # ``list_tools``'s own handling.
        if allowed_by_tier is None:
            for tool_def in self._get_dynamic_tools():
                if not interactive and tool_def.get("name") == "ask_user":
                    continue
                preserved.append(copy.deepcopy(tool_def))

        return sorted(preserved, key=lambda t: t.get("name", ""))

    def list_tools_deferred(
        self,
        tier: str | None = None,
        interactive: bool = True,
        user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        """Return the visible/deferred tool split for one user-scoped turn.

        Args:
            tier: Capability tier.
            interactive: When False, ``ask_user`` is excluded.
            user_id: Concrete user id used to scope deferred schema resolution.

        Returns:
            (visible_tools, deferred_refs) for the current tier/runtime.
        """
        pool = self.get_deferred_tool_pool(
            tier=tier,
            interactive=interactive,
            user_id=user_id,
        )
        return pool.visible_tools, pool.deferred_refs

    def get_deferred_tool_pool(
        self,
        tier: str | None = None,
        interactive: bool = True,
        user_id: str | None = None,
    ) -> DeferredToolPool:
        """Build the user-scoped deferred tool pool shared by native and text paths."""

        return split_tools_deferred(
            self,
            tier=tier,
            interactive=interactive,
            user_id=user_id,
        )

    def get_tool_schemas(
        self,
        tier: str | None = None,
        interactive: bool = True,
        user_id: str | None = None,
    ) -> str:
        """Return tool schemas in the legacy format.

        Args:
            tier: Capability label (solo, ensemble, symphony). Solo and
                Ensemble hide desktop computer use; Symphony includes it.
            user_id: Accepted for compatibility with text-tool callers; tool
                visibility is tier-based in this hub.

        Produces the same human-readable text as
        ToolRegistry.generate_schema_text() so the agent loop
        doesn't need changes during the bridge period.
        """
        _ = user_id
        if not self._tool_schemas:
            return ""

        if user_id is not None:
            pool = self.get_deferred_tool_pool(
                tier=tier,
                interactive=interactive,
                user_id=user_id,
            )
            sections = self._format_schema_sections(pool.visible_tools)
            deferred_tools_text = format_deferred_tools_block(pool.deferred_refs)
            if deferred_tools_text:
                sections.append(deferred_tools_text)
            return "\n\n".join(section for section in sections if section)

        allowed_by_tier = self._resolve_filters(
            tier,
            interactive=interactive,
        )

        # Group tools by category (inferred from tool name prefix or generic)
        by_category: dict[str, list[dict[str, Any]]] = {}
        for tool in self._tool_schemas.values():
            if not self._is_tool_visible(tool["name"], allowed_by_tier, tier=tier):
                continue
            if not interactive and tool["name"] == "ask_user":
                continue
            category = self._infer_category(tool["name"])
            by_category.setdefault(category, []).append(tool)

        # Include dynamic API tools for the all-tools tier only.
        if allowed_by_tier is None:
            for tool_def in self._get_dynamic_tools():
                if not interactive and tool_def.get("name") == "ask_user":
                    continue
                by_category.setdefault("dynamic_api", []).append(tool_def)

        return "\n\n".join(self._format_schema_sections([tool for tools in by_category.values() for tool in tools]))

    def _format_schema_sections(self, tools: list[dict[str, Any]]) -> list[str]:
        """Format tool schemas grouped by category."""

        by_category: dict[str, list[dict[str, Any]]] = {}
        for tool in tools:
            name = str(tool.get("name") or "")
            if not name:
                continue
            category = self._infer_category(name)
            by_category.setdefault(category, []).append(tool)

        sections: list[str] = []
        for category in sorted(by_category.keys()):
            tools = sorted(by_category[category], key=lambda t: t.get("name", ""))
            section_lines = ["### %s tools" % category.title()]
            for tool in tools:
                section_lines.append(self._tool_to_schema_text(tool))
            sections.append("\n".join(section_lines))

        return sections

    def _infer_category(self, tool_name: str) -> str:
        """Infer a tool's category from its name prefix."""
        prefixes = {
            "browser_": "browser",
        }
        for prefix, category in prefixes.items():
            if tool_name.startswith(prefix):
                return category

        # Map specific names to categories
        name_map = {
            "file_read": "filesystem",
            "file_write": "filesystem",
            "run_command": "shell",
            "web_search": "web",
            "web_read": "web",
            "system_info": "system",
            "timer": "general",
            "media": "general",
            "connect_music_provider": "general",
            "check_music_provider_status": "general",
            "delegate_to_provider": "delegation",
            "file_bug_report": "general",
            "calendar": "calendar",
        }
        return name_map.get(tool_name, "general")

    def _tool_to_schema_text(self, tool: dict[str, Any]) -> str:
        """Convert an MCP tool dict to legacy schema text format."""
        name = tool.get("name", "unknown_tool")
        description = tool.get("description", "")

        parts = ["- %s: %s" % (name, description)]

        # Extract parameters from inputSchema
        input_schema = tool.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        if properties:
            parts.append("  Parameters:")
            for param_name, param_schema in sorted(properties.items()):
                param_desc = param_schema.get("description", "")
                parts.append("    - %s: %s" % (param_name, param_desc))

        return "\n".join(parts)

    def _attach_inprocess_cloud_bearer(self, call_meta: dict[str, Any], server_name: str | None) -> None:
        """Add the calling request's cloud bearer to ``_meta`` for in-process servers.

        See the call site in :meth:`call_tool` for why this is needed (GitHub
        #584: the memory transport pins the tool handler to the hub-build
        context, so a cloud tool can never read the current request's token).

        Two guards keep this from widening the credential's blast radius:

        * The target server must be ``transport == "inprocess"`` — first-party
          code already running in this process. A stdio/SSE server, including
          any user-configured external MCP server, never receives it.
        * The bearer rides INSIDE ``viola_user_context``, so it is only attached
          when an authenticated principal was already resolved for this call.
          The credential therefore always travels with the user it belongs to.
        """

        user_context_meta = call_meta.get("viola_user_context")
        if not isinstance(user_context_meta, dict) or not user_context_meta.get("user_id"):
            return
        if not server_name:
            return
        config = self._configs.get(server_name) or self._deferred_server_configs.get(server_name)
        if getattr(config, "transport", "") != "inprocess":
            return
        try:
            from core.user_context import get_current_cloud_access_token

            access_token = get_current_cloud_access_token()
        except (ImportError, LookupError, RuntimeError):
            logger.debug("Caller cloud bearer lookup failed for in-process tool call", exc_info=True)
            return
        if isinstance(access_token, str) and access_token.strip():
            user_context_meta["cloud_access_token"] = access_token.strip()

    async def call_tool(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        channel: Any | None = None,
        allowed_tools: set[str] | None = None,
        user_context: UserContext | None = None,
        approval_already_granted: bool = False,
        *,
        tool_use_id: str | None = None,
        on_progress: Any = None,
        abort_signal: Any = None,
    ) -> dict[str, Any]:
        """Execute a tool call with approval checking.

        Args:
            name: Tool name.
            args: Tool arguments (defaults to empty dict).
            channel: Optional MessageChannel for interactive approval.
            allowed_tools: Historical compatibility parameter. Production
                routing passes None. Plan pricing does not restrict the tool
                surface; approval policy and spend counters are separate gates.
            tool_use_id: Optional Claude-style ``tool_use_id`` to forward as
                MCP ``_meta`` (``claudecode/toolUseId`` — see
                ``services/mcp/client.ts:1840-1843``).
            on_progress: Optional callable invoked with MCP progress events
                (started / progress / completed / failed). Matches
                Claude's ``onProgress`` plumbing
                (``services/mcp/client.ts:1845-1908``).
            abort_signal: Optional ``asyncio.Event`` or object exposing
                ``is_set()``/``cancel()``; bridged into ``client.callTool``
                via the SDK's ``signal`` so the server can release
                long-running work when the user interrupts (S6-015).

        Returns:
            Legacy-format result dict:
            {"success": bool, "data": Any, "display": str, "error": str}
        """
        if args is None:
            args = {}
        alias = None if name in self._tool_routes else _legacy_computer_alias(name, args)
        if alias is not None:
            legacy_name = name
            name, args = alias
            logger.warning(
                "Deprecated desktop tool '%s' forwarded to unified computer action '%s'",
                legacy_name,
                args.get("action", ""),
            )

        _call_start = time.monotonic()
        self.last_approval_path = "no_approval_needed"
        server_name = self._tool_routes.get(name)

        # Resolve compound tool calls (e.g. gmail(action=search) -> gmail_search)
        # before the main dispatch.  The resolved call recurses into call_tool()
        # so approval, routing, and result formatting all apply normally.
        if server_name == "_compound":
            from .compound_tools import resolve_compound_call

            _compound = resolve_compound_call(name, args)
            if _compound is not None:
                real_name, real_args = _compound
                return await self.call_tool(
                    real_name,
                    real_args,
                    channel=channel,
                    allowed_tools=allowed_tools,
                    user_context=user_context,
                    approval_already_granted=approval_already_granted,
                )

        # Route only unregistered dynamic API tools through the tool factory.
        # Registered core tools such as api_credential own their route and must
        # not be shadowed by the dynamic "api_*" convenience dispatcher.
        if name.startswith("api_") and server_name is None:
            risk = self._bridge.get_call_risk(name, args)
            approval_mgr = getattr(self._bridge, "_approval", None)
            pre_approved_tools = getattr(approval_mgr, "_pre_approved_tools", set())
            if risk == RiskLevel.SAFE:
                self.last_approval_path = "risk_safe"
            elif risk == RiskLevel.CONFIRM:
                self.last_approval_path = "pre_approved" if name in pre_approved_tools else "risk_confirm_auto"

            try:
                from intent.approval import ConfirmationDeferred

                approved = await self._bridge.check_approval(name, args, channel=channel)
            except ConfirmationDeferred as deferred:
                self.last_approval_path = "confirmation_required"
                return _confirmation_required_result(deferred)
            if risk == RiskLevel.DANGEROUS:
                self.last_approval_path = "user_confirmed" if approved else "approval_denied"
            elif not approved:
                self.last_approval_path = "approval_denied"

            if not approved:
                return {
                    "success": False,
                    "data": None,
                    "display": "",
                    "error": "Tool '%s' was not approved by the user." % name,
                    "error_category": "APPROVAL_BLOCKED",
                    "blocked_tool": name,
                    "retryable": False,
                }
            return await self._call_dynamic_tool(name, args, user_context=user_context)

        # Find which server owns this tool
        # Lazy-connect deferred servers on first access (2026-04-08).
        # If the tool isn't in the routing table but belongs to a deferred
        # server, connect that server now and retry the lookup.
        if server_name is None and self._deferred_server_configs:
            for _def_name, _def_cfg in list(self._deferred_server_configs.items()):
                # Heuristic: browser tools start with "browser_"
                if name.startswith("browser_") and "browser" in _def_name:
                    connected = await self._lazy_connect_deferred(_def_name)
                    if connected:
                        server_name = self._tool_routes.get(name)
                    break

        # If a browser tool is missing and we have a reconnection callback,
        # attempt on-demand reconnection before giving up.  This handles two
        # cases: (1) initial background init failed after all retries, and
        # (2) browser subprocess crashed mid-session.
        if server_name is None and name.startswith("browser_") and self._browser_reconnect_cb is not None:
            logger.info(
                "Browser tool '%s' not found — attempting on-demand reconnection",
                name,
            )
            try:
                reconnected = await self._browser_reconnect_cb()
                if reconnected:
                    server_name = self._tool_routes.get(name)
            except (OSError, RuntimeError, TypeError, ValueError) as _recon_err:
                logger.warning("Browser reconnection failed: %s", _recon_err)

        if server_name is None:
            return {
                "success": False,
                "data": None,
                "display": "",
                "error": "Unknown tool: %s. Available: %s" % (name, ", ".join(sorted(self._tool_routes.keys()))),
            }

        session = self._sessions.get(server_name)
        if session is None:
            # If browser server was connected but session is gone (subprocess
            # crashed), try reconnection before returning an error.
            if name.startswith("browser_") and self._browser_reconnect_cb is not None:
                logger.info(
                    "Browser server session lost for tool '%s' — attempting reconnection",
                    name,
                )
                try:
                    reconnected = await self._browser_reconnect_cb()
                    if reconnected:
                        session = self._sessions.get(server_name)
                except (OSError, RuntimeError, TypeError, ValueError) as _recon_err:
                    logger.warning("Browser reconnection failed: %s", _recon_err)

            if session is None:
                return {
                    "success": False,
                    "data": None,
                    "display": "",
                    "error": "Server '%s' not connected" % server_name,
                }

        codex_refusal = _codex_arg_sandbox_refusal(name, args)
        if codex_refusal is not None:
            return codex_refusal

        # Check approval. ``approval_already_granted`` is reserved for narrow
        # code-owned continuations after the hosted confirmation page or the
        # irreversible-action runtime gate has already collected explicit user
        # approval.
        risk = self._bridge.get_call_risk(name, args)
        schema = self._schema_for_route_name(name)
        approval_grants_this_call = approval_already_granted and (
            name == "fill_payment_details" or is_irreversible_tool_call(name, args, schema)
        )
        if approval_grants_this_call:
            approved = True
            self.last_approval_path = "approval_already_granted"
        else:
            approval_mgr = getattr(self._bridge, "_approval", None)
            pre_approved_tools = getattr(approval_mgr, "_pre_approved_tools", set())
            if risk == RiskLevel.SAFE:
                self.last_approval_path = "risk_safe"
            elif risk == RiskLevel.CONFIRM:
                self.last_approval_path = "pre_approved" if name in pre_approved_tools else "risk_confirm_auto"

            try:
                from intent.approval import ConfirmationDeferred

                approved = await self._bridge.check_approval(name, args, channel=channel)
            except ConfirmationDeferred as deferred:
                self.last_approval_path = "confirmation_required"
                return _confirmation_required_result(deferred)
            if risk == RiskLevel.DANGEROUS:
                self.last_approval_path = "user_confirmed" if approved else "approval_denied"
            elif not approved:
                self.last_approval_path = "approval_denied"
        if not approved:
            return {
                "success": False,
                "data": None,
                "display": "",
                "error": "Tool '%s' was not approved by the user." % name,
                "error_category": "APPROVAL_BLOCKED",
                "blocked_tool": name,
                "retryable": False,
            }

        # Use the original (unprefixed) name for external servers
        schema = self._schema_for_route_name(name)
        wire_name = schema.get("_original_name", name)

        # Coerce dict/list args to JSON strings when schema expects string.
        # LLMs commonly send structured objects (e.g. delivery_address: {street:...})
        # where the schema expects a JSON-encoded string.
        input_schema = schema.get("inputSchema", {})
        props = input_schema.get("properties", {})
        for key, val in list(args.items()):
            if isinstance(val, (dict, list)) and props.get(key, {}).get("type") == "string":
                args[key] = json.dumps(val)

        # Per-tool timeout — phone calls get 660s, slow tools get full base, others get 67%
        from config.settings import settings as _settings

        _base = getattr(_settings, "agent_tool_timeout_seconds", 180.0)
        call_meta = _build_user_context_meta(user_context) or {}
        # GitHub #584 — carry the CALLING request's live cloud bearer.
        #
        # An in-process MCP server is connected over the SDK memory transport,
        # which runs the server in its own anyio task created once when the hub
        # is built. That task captured ``contextvars`` at creation time, so a
        # tool handler permanently observes the hub-build request's context, not
        # the context of the request currently calling it. On the cloud surface
        # the phone tool's only bearer source IS such a contextvar, so it dialled
        # ``/api/phone/call`` with a stale-or-empty credential and got 401
        # ("Cloud authentication failed") even after the user confirmed the call.
        # Attaching the bearer per call is what makes it current.
        #
        # Restricted to ``inprocess`` servers ON PURPOSE: those are first-party
        # modules in this very process, so this hands the credential nowhere it
        # could not already reach. It is never attached for stdio/SSE servers,
        # so a third-party MCP server can never observe a user's GoTrue bearer.
        self._attach_inprocess_cloud_bearer(call_meta, server_name)
        # Claude parity (S6-007): forward ``claudecode/toolUseId`` on _meta so
        # MCP servers can correlate progress/audit log entries to the host
        # turn (``services/mcp/client.ts:1840-1843``).
        if tool_use_id:
            call_meta["claudecode/toolUseId"] = tool_use_id
        if not call_meta:
            call_meta = None
        if name in self._PHONE_TOOLS:
            timeout = 660.0  # 11 min — covers max_call_duration (600s) + dial/summary
        elif name in self._GATE_RESUME_TOOLS:
            timeout = 660.0  # 11 min — gate resume drives a fresh agent loop to completion
        elif name in self._SLOW_TOOLS:
            timeout = _base
        else:
            timeout = max(60.0, _base * 0.67)

        # Claude parity (S6-007): emit MCP "started" progress and prepare a
        # ProgressFnT adapter so the SDK's per-progress-notification
        # callback bridges into our agent-loop progress channel.
        _server_for_progress = server_name or ""
        if on_progress is not None and tool_use_id:
            try:
                on_progress(
                    {
                        "tool_use_id": tool_use_id,
                        "type": "mcp_progress",
                        "status": "started",
                        "server_name": _server_for_progress,
                        "tool_name": name,
                    }
                )
            except Exception as exc:
                logger.debug("on_progress(started) raised for %s: %s", name, exc)

        progress_callback = None
        if on_progress is not None and tool_use_id:

            async def progress_callback(progress: float, total: float | None, message: str | None) -> None:
                try:
                    on_progress(
                        {
                            "tool_use_id": tool_use_id,
                            "type": "mcp_progress",
                            "status": "progress",
                            "server_name": _server_for_progress,
                            "tool_name": name,
                            "progress": progress,
                            "total": total,
                            "progress_message": message,
                        }
                    )
                except Exception as exc:
                    logger.debug("on_progress(progress) raised for %s: %s", name, exc)

        # Build a read_timeout that mirrors our outer asyncio.wait_for. Claude
        # passes the SDK ``timeout`` so the inner SSE/stream layer can also
        # release the request promptly when the server hangs
        # (``services/mcp/client.ts:3091-3115``). We keep the outer
        # asyncio.wait_for as a secondary guard.
        import datetime as _dt

        read_timeout_seconds = _dt.timedelta(seconds=timeout)

        # Bridge an abort signal into the request: if the caller's signal
        # fires we cancel the inner call_tool task so the SDK's stream can
        # tear down. Match Claude's ``signal`` semantics
        # (``services/mcp/client.ts:3091-3115``).
        _call_task: asyncio.Task[Any] | None = None
        _abort_waiter: asyncio.Task[Any] | None = None

        async def _run_with_abort() -> Any:
            nonlocal _call_task, _abort_waiter
            inner = _session_call_tool_compat(
                session,
                wire_name,
                args,
                read_timeout_seconds=read_timeout_seconds,
                progress_callback=progress_callback,
                meta=call_meta,
            )
            if abort_signal is None:
                return await inner
            _call_task = asyncio.ensure_future(inner)
            wait_for_abort = getattr(abort_signal, "wait", None)
            if callable(wait_for_abort):
                _abort_waiter = asyncio.ensure_future(wait_for_abort())
            else:

                async def _poll_abort() -> None:
                    while True:
                        is_set = getattr(abort_signal, "is_set", None)
                        if callable(is_set) and is_set():
                            return
                        await asyncio.sleep(0.1)

                _abort_waiter = asyncio.ensure_future(_poll_abort())
            done, _pending = await asyncio.wait(
                {_call_task, _abort_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if _abort_waiter in done and _call_task not in done:
                _call_task.cancel()
                with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                    await _call_task
                raise asyncio.CancelledError("MCP tool call aborted by signal")
            if _abort_waiter is not None and not _abort_waiter.done():
                _abort_waiter.cancel()
                with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                    await _abort_waiter
            return _call_task.result()

        # Call the tool via MCP session with timeout enforcement
        try:
            result = await asyncio.wait_for(_run_with_abort(), timeout=timeout)
        except TimeoutError:
            logger.warning("MCP tool '%s' timed out after %.0fs", name, timeout)
            error_msg = "Tool '%s' timed out after %.0f seconds" % (name, timeout)
            phone_retry_guard = name in self._PHONE_TOOLS
            if phone_retry_guard:
                error_msg += ". The phone call was in progress and was interrupted before completion."
            self._emit_mcp_terminal_progress(
                on_progress=on_progress,
                tool_use_id=tool_use_id,
                server=server_name,
                tool=name,
                status="failed",
                elapsed_ms=int((time.monotonic() - _call_start) * 1000),
            )
            timeout_payload = {
                "success": False,
                "data": None,
                "display": "",
                "error": error_msg,
            }
            if phone_retry_guard:
                timeout_payload["retryable"] = False
                timeout_payload["requires_user_confirmation_before_retry"] = True
            return timeout_payload
        except Exception as exc:
            from .errors import McpAuthError, is_mcp_unauthorized

            # Claude parity (S6-009): convert 401/Unauthorized into a typed
            # auth error and mark the server as needs-auth so the UI/hub
            # can prompt the user to re-authenticate
            # (``services/mcp/client.ts:3194-3208`` +
            # ``services/tools/toolExecution.ts:1599-1622``).
            if is_mcp_unauthorized(exc) and server_name:
                logger.warning(
                    "MCP tool '%s' on server '%s' returned 401 Unauthorized — token may have expired",
                    name,
                    server_name,
                )
                self._mark_server_needs_auth(server_name)
                self._emit_mcp_terminal_progress(
                    on_progress=on_progress,
                    tool_use_id=tool_use_id,
                    server=server_name,
                    tool=name,
                    status="failed",
                    elapsed_ms=int((time.monotonic() - _call_start) * 1000),
                )
                raise McpAuthError(
                    server_name,
                    'MCP server "%s" requires re-authorization (token expired)' % server_name,
                ) from exc

            # Auto-reconnect on ClosedResourceError: the MCP server's
            # communication channel died.  Reconnect and retry once.
            from anyio import ClosedResourceError as _ClosedErr

            if isinstance(exc, _ClosedErr) and name in self._tool_routes:
                server_name = self._tool_routes[name]
                config = self._configs.get(server_name)
                if config is not None:
                    logger.warning(
                        "MCP server '%s' connection lost (ClosedResourceError) " "while calling '%s' — reconnecting",
                        server_name,
                        name,
                    )
                    try:
                        await self.connect_server(server_name, config)
                        new_session = self._sessions.get(server_name)
                        if new_session is not None:
                            result = await asyncio.wait_for(
                                _session_call_tool_compat(
                                    new_session,
                                    wire_name,
                                    args,
                                    read_timeout_seconds=read_timeout_seconds,
                                    progress_callback=progress_callback,
                                    meta=call_meta,
                                ),
                                timeout=timeout,
                            )
                            # Skip to result conversion below
                            converted = self._convert_result(result)
                            elapsed_ms = int((time.monotonic() - _call_start) * 1000)
                            self._emit_mcp_terminal_progress(
                                on_progress=on_progress,
                                tool_use_id=tool_use_id,
                                server=server_name,
                                tool=name,
                                status=("completed" if converted.get("success", True) else "failed"),
                                elapsed_ms=elapsed_ms,
                            )
                            logger.info(
                                "MCP tool %s completed (after reconnect) in %dms (ok=%s)",
                                name,
                                elapsed_ms,
                                converted.get("success"),
                            )
                            return converted
                    except Exception as reconnect_exc:
                        logger.exception(
                            "MCP reconnect+retry failed for '%s': %s",
                            name,
                            reconnect_exc,
                        )
                    except BaseException as reconnect_exc:
                        # This reconnect runs inside a live request task —
                        # leaked cancellation noise from a failed connect must
                        # not cancel the user's in-flight command. Genuine
                        # cancellation propagates.
                        if not self._contain_leaked_connect_cancellation(server_name, reconnect_exc):
                            raise

            logger.exception("MCP call_tool failed for '%s'", name)
            error_result: dict[str, Any] = {
                "success": False,
                "data": None,
                "display": "",
                "error": "Tool execution error: %s" % exc,
            }
            # GAP-1 fix: extract structured context from ViolaError hierarchy
            self._enrich_error_result(error_result, exc)
            self._emit_mcp_terminal_progress(
                on_progress=on_progress,
                tool_use_id=tool_use_id,
                server=server_name,
                tool=name,
                status="failed",
                elapsed_ms=int((time.monotonic() - _call_start) * 1000),
            )
            return error_result

        # Convert MCP CallToolResult to legacy format
        converted = self._convert_result(result)
        elapsed_ms = int((time.monotonic() - _call_start) * 1000)
        self._emit_mcp_terminal_progress(
            on_progress=on_progress,
            tool_use_id=tool_use_id,
            server=server_name,
            tool=name,
            status="completed" if converted.get("success", True) else "failed",
            elapsed_ms=elapsed_ms,
        )
        logger.info(
            "MCP tool %s completed in %dms (ok=%s)",
            name,
            elapsed_ms,
            converted.get("success", True),
        )
        return converted

    @staticmethod
    def _emit_mcp_terminal_progress(
        *,
        on_progress: Any,
        tool_use_id: str | None,
        server: str | None,
        tool: str,
        status: str,
        elapsed_ms: int,
    ) -> None:
        """Emit an MCP completed/failed progress event (Claude parity)."""

        if on_progress is None or not tool_use_id:
            return
        try:
            on_progress(
                {
                    "tool_use_id": tool_use_id,
                    "type": "mcp_progress",
                    "status": status,
                    "server_name": server or "",
                    "tool_name": tool,
                    "elapsed_time_ms": elapsed_ms,
                }
            )
        except Exception as exc:
            logger.debug("on_progress(%s) raised for %s: %s", status, tool, exc)

    def _mark_server_needs_auth(self, server_name: str) -> None:
        """Mark an MCP server as needing re-authorization.

        Persisted in ``self._server_states`` so the hub / approval bridge
        can surface a re-auth prompt. Matches Claude's
        ``setAppState`` ``mcp.clients[…].type = 'needs-auth'``.
        """

        states = getattr(self, "_server_states", None)
        if states is None:
            states = {}
            self._server_states = states  # type: ignore[attr-defined]
        states[server_name] = "needs-auth"
        logger.warning("MCP server '%s' marked needs-auth", server_name)

    async def _call_dynamic_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        user_context: UserContext | None = None,
    ) -> dict[str, Any]:
        """Execute a dynamic API tool via the tool factory.

        Dynamic tools have names prefixed with ``api_`` and are backed
        by user-provided credentials in the vault.
        """
        service_name = name[4:]  # Strip "api_" prefix
        try:
            from services.api_vault.tool_factory import get_tool_factory

            user_id = self._require_user_id_from_context(user_context, tool_name=name)
            factory = get_tool_factory()
            result = await factory.execute_api_call(
                service_name=service_name,
                endpoint=args.get("endpoint", "/"),
                method=args.get("method", "GET"),
                params=args.get("params"),
                headers=args.get("headers"),
                user_id=user_id,
            )

            if result.get("ok"):
                data = result.get("data", "")
                display = json.dumps(data, default=str) if isinstance(data, (dict, list)) else str(data)
                return {
                    "success": True,
                    "data": data,
                    "display": display,
                    "error": "",
                }
            return {
                "success": False,
                "data": None,
                "display": "",
                "error": result.get("error", "Dynamic API call failed"),
                "error_category": result.get("error_category"),
                "retryable": result.get("retryable", False),
            }
        except Exception as exc:
            logger.exception("Dynamic tool '%s' failed", name)
            return {
                "success": False,
                "data": None,
                "display": "",
                "error": "Dynamic API tool requires authenticated request context.",
                "error_category": "EXPECTED_AUTH",
                "retryable": False,
            }

    @staticmethod
    def _require_user_id_from_context(user_context: UserContext | None, *, tool_name: str) -> str:
        """Resolve user_id from the current request context for Tier-3 credentials."""
        if isinstance(user_context, dict):
            raw_user_id = user_context.get("user_id")
        else:
            raw_user_id = getattr(user_context, "user_id", None)
        user_id = str(raw_user_id or "").strip()
        if not user_id:
            raise ValueError("%s requires authenticated request user_id" % tool_name)
        return user_id

    def _convert_result(self, mcp_result: Any) -> dict[str, Any]:
        """Convert MCP CallToolResult to legacy result dict.

        MCP returns CallToolResult with content list and isError boolean.
        Legacy format is {"success", "data", "display", "error"}.

        Claude parity (S6-008): preserve ``_meta`` and ``structuredContent``
        plus non-text content blocks (image, audio, resource, resource_link)
        on the result dict. The agent loop's per-tool result mapper
        decides whether to surface them to the model — losing them at
        conversion time would erase any chance of doing so downstream
        (``services/mcp/client.ts:2478-2684`` /
        ``services/mcp/client.ts:1897-1908``).
        """
        # Extract all content blocks, not just text. Claude's
        # ``transformResultContent`` distinguishes text / audio / image /
        # resource / resource_link blocks and persists binary payloads to
        # disk; we keep the structured form here so per-tool result
        # mappers can render the appropriate ``ContentBlockParam`` shape.
        content_blocks: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for block in getattr(mcp_result, "content", None) or []:
            converted_block = self._convert_content_block(block)
            if converted_block is None:
                continue
            content_blocks.append(converted_block)
            if converted_block.get("type") == "text":
                text_parts.append(str(converted_block.get("text") or ""))

        joined_text = "\n".join(text_parts)
        raw_meta = getattr(mcp_result, "_meta", None) or getattr(mcp_result, "meta", None)
        structured_content = getattr(mcp_result, "structuredContent", None)

        mcp_meta: dict[str, Any] = {}
        if isinstance(raw_meta, dict) and raw_meta:
            mcp_meta["_meta"] = dict(raw_meta)
        if structured_content is not None:
            try:
                mcp_meta["structuredContent"] = (
                    dict(structured_content) if isinstance(structured_content, dict) else structured_content
                )
            except Exception:
                mcp_meta["structuredContent"] = structured_content

        if mcp_result.isError:
            error_payload: dict[str, Any] = {
                "success": False,
                "data": None,
                "display": "",
                "error": joined_text or "Tool returned an error",
            }
            if content_blocks:
                error_payload["mcp_content"] = content_blocks
            if mcp_meta:
                error_payload["mcp_meta"] = mcp_meta
            return error_payload

        # Try to parse text as JSON for the data field. Image/audio/resource
        # blocks are preserved separately on ``mcp_content`` so the model
        # surface can still see them when the per-tool mapper opts in.
        data: Any = joined_text
        try:
            data = json.loads(joined_text)
        except (json.JSONDecodeError, ValueError):
            pass  # Keep as string

        # Detect error payloads returned as successful MCP responses.
        # Browser tools (fill_ref, click_ref) catch Playwright exceptions
        # and return {"error": "..."} without setting isError.  Without
        # this check, the spin detector never sees these failures and the
        # agent loops 20+ times on the same failing action.
        # Success indicators: "filled", "clicked", "selected", "title" (navigate).
        # If "error" is present without any success indicator, it's a failure.
        # Also: if the tool explicitly returns ok=false, respect that signal
        # even when success keys are present (e.g. fill_form partial failures
        # return "filled" AND "errors" with ok=false).
        _success_keys = {"filled", "clicked", "selected", "title", "output"}
        if isinstance(data, dict) and (
            ("error" in data and not any(k in data for k in _success_keys)) or data.get("ok") is False
        ):
            result = {
                "success": False,
                "data": data,
                "display": joined_text,
                "error": data.get("error") or data.get("warning") or "Tool reported failure",
            }
            for key in (
                "error_category",
                "retryable",
                "required_tier",
            ):
                if key in data:
                    result[key] = data[key]
            if content_blocks:
                result["mcp_content"] = content_blocks
            if mcp_meta:
                result["mcp_meta"] = mcp_meta
            return result

        result: dict[str, Any] = {
            "success": True,
            "data": data,
            "display": joined_text,
            "error": "",
        }
        if content_blocks:
            result["mcp_content"] = content_blocks
        if mcp_meta:
            result["mcp_meta"] = mcp_meta
        return result

    def _convert_content_block(self, block: Any) -> dict[str, Any] | None:
        """Convert one MCP content block into a Claude-compatible dict.

        Mirrors ``transformResultContent`` (``services/mcp/client.ts:2478-
        2591``): text → ``{type: text, text}``; image → ``{type: image,
        source: {type: base64, media_type, data}}``; audio → opaque
        text marker (Viola does not yet persist audio to disk); resource
        (text) → ``{type: text, text: "[Resource from ...] ..."}``;
        resource (blob) → image when MIME is image/*, else text marker
        with size; resource_link → text marker.

        Returns ``None`` for unsupported block types so they don't
        silently take a slot in the converted content list.
        """

        if block is None:
            return None
        block_type = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if block_type == "text":
            text_value = getattr(block, "text", None) if not isinstance(block, dict) else block.get("text")
            return {"type": "text", "text": str(text_value or "")}
        if block_type == "image":
            data_value = getattr(block, "data", None) if not isinstance(block, dict) else block.get("data")
            mime = getattr(block, "mimeType", None) if not isinstance(block, dict) else block.get("mimeType")
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": str(mime or "image/png"),
                    "data": str(data_value or ""),
                },
            }
        if block_type == "audio":
            mime = getattr(block, "mimeType", None) if not isinstance(block, dict) else block.get("mimeType")
            return {
                "type": "text",
                "text": "[MCP audio content (%s) — not yet surfaced]" % (mime or "audio/*"),
            }
        if block_type == "resource":
            resource = getattr(block, "resource", None) if not isinstance(block, dict) else block.get("resource")
            if resource is None:
                return None
            uri = getattr(resource, "uri", None) if not isinstance(resource, dict) else resource.get("uri")
            text_value = getattr(resource, "text", None) if not isinstance(resource, dict) else resource.get("text")
            blob_value = getattr(resource, "blob", None) if not isinstance(resource, dict) else resource.get("blob")
            mime = getattr(resource, "mimeType", None) if not isinstance(resource, dict) else resource.get("mimeType")
            prefix = "[Resource from MCP at %s] " % (uri or "<unknown>")
            if text_value is not None:
                return {"type": "text", "text": "%s%s" % (prefix, text_value)}
            if blob_value is not None and isinstance(mime, str) and mime.startswith("image/"):
                return {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": str(blob_value),
                    },
                }
            if blob_value is not None:
                # Best-effort marker: we don't persist arbitrary binary
                # blobs to disk yet (Claude's ``persistBinaryContent`` is
                # parity-deferred). Surface a useful text token instead.
                try:
                    import base64 as _b64

                    size_bytes = len(_b64.b64decode(str(blob_value), validate=False))
                except Exception:
                    size_bytes = -1
                return {
                    "type": "text",
                    "text": "%sBinary content (%s, %d bytes) preserved by MCP but not surfaced inline"
                    % (prefix, mime or "unknown", size_bytes),
                }
            return None
        if block_type == "resource_link":
            name_value = getattr(block, "name", None) if not isinstance(block, dict) else block.get("name")
            uri = getattr(block, "uri", None) if not isinstance(block, dict) else block.get("uri")
            description = (
                getattr(block, "description", None) if not isinstance(block, dict) else block.get("description")
            )
            text = "[Resource link: %s] %s" % (name_value or "<unnamed>", uri or "")
            if description:
                text = "%s (%s)" % (text, description)
            return {"type": "text", "text": text}
        return None

    @staticmethod
    def _enrich_error_result(
        result: dict[str, Any],
        exc: BaseException,
    ) -> None:
        """Extract structured error context from exceptions.

        When the exception is a ViolaError with an ErrorContext, the
        user_message and retryable flag are propagated into the result
        dict so the agent loop can pass the safe-for-display message to
        the LLM. The internal operator-diagnostic hint on the
        ErrorContext is NOT lifted into the model-visible result —
        R5-P0-A (2026-05-30) deleted the prose channel into the model.
        The model decides recovery from the structured ``error_category``
        (set below) and the unified system prompt.

        For all exceptions, the error_classification system is consulted
        to determine the error category.
        """
        from core.exceptions import ViolaError

        # Extract ViolaError-specific context
        if isinstance(exc, ViolaError):
            ctx = getattr(exc, "context", None)
            if ctx is not None and ctx.user_message:
                result["error"] = ctx.user_message
            result["retryable"] = getattr(exc, "retryable", False)

        # Classify the error category using the diagnostics system
        try:
            from diagnostics.error_classification import categorize_exception

            category = categorize_exception(exc)
            result["error_category"] = category.name
            # Infer retryable from category if not already set by ViolaError
            if not result.get("retryable") and category.name.startswith("EXPECTED_"):
                result["retryable"] = True
        except (
            AttributeError,
            ImportError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            logger.debug("Tool error classification unavailable: %s", exc)

    def find_required_tier(self, tool_name: str) -> str | None:
        """Return the minimum tier required to access a tool.

        Desktop computer use is Symphony-only. Other registered tools return
        None unless a temporary allowlist map is installed by old tests.
        """
        # If the tool isn't registered at all, we can't determine a tier
        if tool_name not in self._tool_routes and tool_name not in self._tool_schemas:
            return None

        for hidden in self._TIER_HIDDEN_TOOL_MAP.values():
            if tool_name in hidden:
                return "symphony"

        if self._TIER_TOOL_MAP.get("solo") is None:
            return None

        # Check each tier from lowest to highest
        for tier in ("solo", "ensemble", "symphony"):
            allowed = self._TIER_TOOL_MAP.get(tier)
            if allowed is None:
                # symphony = None means all tools visible
                return tier
            if tool_name in allowed:
                return tier
        return None

    def _get_dynamic_tools(self) -> list[dict[str, Any]]:
        """Get tool definitions from the API credential vault.

        Returns dynamic API tools for services that have stored credentials.
        These appear alongside static MCP tools — transparent to the LLM.
        """
        try:
            from core.user_context import get_current_user_id
            from services.api_vault.tool_factory import get_tool_factory

            user_id = get_current_user_id()
            factory = get_tool_factory()
            return factory.get_available_dynamic_tools(user_id=user_id)
        except (ImportError, LookupError, OSError, RuntimeError, ValueError):
            logger.debug("Dynamic tool factory not available")
            return []

    def get_dynamic_catalog_summary(self) -> str:
        """Get a compact summary of available dynamic APIs for LLM context."""
        try:
            from services.api_vault.catalog import get_api_catalog

            return get_api_catalog().get_catalog_summary()
        except (ImportError, OSError, RuntimeError, ValueError):
            return ""

    def get_server_status(self) -> dict[str, str]:
        """Return status of all servers."""
        status: dict[str, str] = {}
        for name in self._configs:
            if name in self._sessions:
                tool_count = sum(1 for sn in self._tool_routes.values() if sn == name)
                status[name] = "connected (%d tools)" % tool_count
            else:
                status[name] = "disconnected"
        return status

    def get_server_health_flags(self) -> dict[str, bool]:
        """Return the compact MCP readiness map used by native-agent logs."""
        browser_config = self._configs.get("browser") or self._deferred_server_configs.get("browser")
        browser_connected = "browser" in self._sessions
        return {
            "core": "core-tools" in self._sessions,
            "browser": browser_connected,
            "browser_local": browser_connected and getattr(browser_config, "transport", "") == "inprocess",
        }
