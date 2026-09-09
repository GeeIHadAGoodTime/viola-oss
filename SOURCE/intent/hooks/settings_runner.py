"""Claude-compatible settings/session hook configuration runner.

This module is the Python side of Claude Code's ``utils/hooks.ts`` hook
configuration loader + matcher + dispatcher. The in-process
:mod:`intent.hooks.lifecycle` registry stays the compatibility surface for
pre-existing handlers; this runner adds the *config-driven* surface used by
settings files, session-scoped hooks, plugin-supplied hooks, and skill-bundled
hooks.

Key parity points:

* ``HookEvent`` envelope matches Claude's ``createBaseHookInput`` —
  ``hook_event_name``, ``session_id``, ``transcript_path``, ``cwd``,
  ``permission_mode``, ``agent_id``, ``agent_type``.
* :class:`HookMatcher` ``matcher`` follows the ``Write|Edit`` /
  ``^Bash.*`` rule from Claude.
* :class:`HookCommand` ``if`` follows Claude's permission-rule syntax
  ``Bash(git *)`` and is delegated to
  :func:`intent.permissions.policy._parse_rule_tool` /
  :func:`intent.permissions.policy._matches_tool_content`.
* Hook command types: ``command`` (shell), ``prompt`` (LLM), ``http``
  (HTTP POST), ``agent`` (verifier).

The runner produces a normalized :class:`HookResult` (already canonical inside
Viola). Streams of hooks fold left-to-right via :meth:`HookResult.merge` —
the existing dispatcher contract.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from core.logging_config import get_logger
from intent.hooks.schema import (
    HookEvent,
    HookEventName,
    HookResult,
    coerce_hook_event_name,
    hook_result_from_exception,
    hook_result_from_value,
)

logger = get_logger(__name__)

HookKind = Literal["command", "prompt", "http", "agent"]
HookSource = Literal["userSettings", "projectSettings", "policySettings", "plugin", "skill", "session"]


@dataclass(frozen=True)
class HookCommand:
    """A single hook command parsed from settings/session/plugin config.

    Mirrors ``src/schemas/hooks.ts``'s discriminated union. Only ``kind``-
    specific fields are populated.
    """

    kind: HookKind
    source: HookSource = "userSettings"
    plugin_id: str | None = None
    skill_root: str | None = None
    if_condition: str | None = None
    timeout_seconds: float | None = None
    status_message: str | None = None
    once: bool = False
    async_run: bool = False
    async_rewake: bool = False

    # command/prompt/http/agent specific
    command: str | None = None
    shell: str | None = None
    prompt: str | None = None
    model: str | None = None
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    allowed_env_vars: tuple[str, ...] = ()

    def describe(self) -> str:
        if self.kind == "command":
            return "command:%s" % (self.command or "")
        if self.kind == "prompt":
            return "prompt:%s" % (self.prompt or "")[:60]
        if self.kind == "http":
            return "http:%s" % (self.url or "")
        if self.kind == "agent":
            return "agent:%s" % (self.prompt or "")[:60]
        return self.kind


@dataclass(frozen=True)
class HookMatcher:
    """A matcher group from a Claude-shaped hooks config."""

    matcher: str | None
    hooks: tuple[HookCommand, ...]
    source: HookSource = "userSettings"
    plugin_id: str | None = None


@dataclass(frozen=True)
class HookConfig:
    """Resolved hooks-by-event configuration."""

    matchers_by_event: Mapping[HookEventName, tuple[HookMatcher, ...]]

    def matchers_for(self, event: HookEventName | str) -> tuple[HookMatcher, ...]:
        try:
            canonical = coerce_hook_event_name(event)
        except ValueError:
            return ()
        return self.matchers_by_event.get(canonical, ())

    def is_empty(self) -> bool:
        return not any(self.matchers_by_event.values())


# --------------------------------------------------------------------------- #
# Parsing                                                                     #
# --------------------------------------------------------------------------- #


def parse_hook_config(
    raw: Any,
    *,
    source: HookSource = "userSettings",
    plugin_id: str | None = None,
    skill_root: str | None = None,
) -> HookConfig:
    """Parse raw settings-shaped hook config into :class:`HookConfig`.

    Layout follows Claude's ``HooksSettings``::

        {
            "PreToolUse": [
                { "matcher": "Write|Edit", "hooks": [ { ... } ] }
            ]
        }
    """

    matchers_by_event: dict[HookEventName, list[HookMatcher]] = {}
    if not isinstance(raw, Mapping):
        return HookConfig(matchers_by_event={})

    for raw_event, raw_matchers in raw.items():
        try:
            event = coerce_hook_event_name(raw_event)
        except ValueError:
            logger.debug("Skipping unknown hook event in settings: %s", raw_event)
            continue
        if not isinstance(raw_matchers, Sequence) or isinstance(raw_matchers, (str, bytes)):
            continue
        parsed_matchers: list[HookMatcher] = []
        for raw_matcher in raw_matchers:
            matcher = _parse_matcher(
                raw_matcher,
                source=source,
                plugin_id=plugin_id,
                skill_root=skill_root,
            )
            if matcher is not None:
                parsed_matchers.append(matcher)
        if parsed_matchers:
            matchers_by_event.setdefault(event, []).extend(parsed_matchers)

    return HookConfig(matchers_by_event={event: tuple(matchers) for event, matchers in matchers_by_event.items()})


def _parse_matcher(
    raw: Any,
    *,
    source: HookSource,
    plugin_id: str | None,
    skill_root: str | None,
) -> HookMatcher | None:
    if not isinstance(raw, Mapping):
        return None
    raw_hooks = raw.get("hooks")
    if not isinstance(raw_hooks, Sequence) or isinstance(raw_hooks, (str, bytes)):
        return None
    matcher_pattern = raw.get("matcher")
    if matcher_pattern is not None and not isinstance(matcher_pattern, str):
        return None
    commands: list[HookCommand] = []
    for raw_hook in raw_hooks:
        parsed = _parse_command(
            raw_hook,
            source=source,
            plugin_id=plugin_id,
            skill_root=skill_root,
        )
        if parsed is not None:
            commands.append(parsed)
    if not commands:
        return None
    return HookMatcher(
        matcher=matcher_pattern,
        hooks=tuple(commands),
        source=source,
        plugin_id=plugin_id,
    )


def _parse_command(
    raw: Any,
    *,
    source: HookSource,
    plugin_id: str | None,
    skill_root: str | None,
) -> HookCommand | None:
    if not isinstance(raw, Mapping):
        return None
    kind_raw = str(raw.get("type") or "").strip().lower()
    if kind_raw not in {"command", "prompt", "http", "agent"}:
        return None
    timeout_value = raw.get("timeout")
    timeout_seconds: float | None = None
    if isinstance(timeout_value, (int, float)) and timeout_value > 0:
        timeout_seconds = float(timeout_value)
    if_condition = raw.get("if")
    headers = raw.get("headers") or {}
    if not isinstance(headers, Mapping):
        headers = {}
    allowed_env_vars_raw = raw.get("allowedEnvVars") or raw.get("allowed_env_vars") or ()
    allowed_env_vars: tuple[str, ...] = ()
    if isinstance(allowed_env_vars_raw, Sequence) and not isinstance(allowed_env_vars_raw, (str, bytes)):
        allowed_env_vars = tuple(str(item) for item in allowed_env_vars_raw if isinstance(item, str))
    return HookCommand(
        kind=kind_raw,  # type: ignore[arg-type]
        source=source,
        plugin_id=plugin_id,
        skill_root=skill_root,
        if_condition=str(if_condition).strip() if isinstance(if_condition, str) and if_condition.strip() else None,
        timeout_seconds=timeout_seconds,
        status_message=str(raw.get("statusMessage") or raw.get("status_message") or "").strip() or None,
        once=bool(raw.get("once")),
        async_run=bool(raw.get("async")),
        async_rewake=bool(raw.get("asyncRewake") or raw.get("async_rewake")),
        command=str(raw.get("command") or "") or None if kind_raw == "command" else None,
        shell=str(raw.get("shell") or "").strip()
        or None,  # nosec B604 - settings schema field, not subprocess shell=True.
        prompt=str(raw.get("prompt") or "") or None if kind_raw in {"prompt", "agent"} else None,
        model=str(raw.get("model") or "").strip() or None,
        url=str(raw.get("url") or "") or None if kind_raw == "http" else None,
        headers={str(k): str(v) for k, v in headers.items()},
        allowed_env_vars=allowed_env_vars,
    )


# --------------------------------------------------------------------------- #
# Matching                                                                    #
# --------------------------------------------------------------------------- #


_SIMPLE_MATCHER = re.compile(r"^[A-Za-z0-9_|]+$")
_LEGACY_TOOL_NAME_ALIASES = {
    "Task": "Agent",
    "KillShell": "TaskStop",
    "AgentOutputTool": "TaskOutput",
    "BashOutputTool": "TaskOutput",
}
_IF_FILTER_EVENTS = frozenset(
    {
        HookEventName.PRE_TOOL_USE,
        HookEventName.POST_TOOL_USE,
        HookEventName.POST_TOOL_USE_FAILURE,
        HookEventName.PERMISSION_REQUEST,
    }
)


def matcher_matches(matcher: str | None, query: str) -> bool:
    """Mirror Claude's ``matchesPattern`` matcher semantics."""

    if not matcher or matcher == "*":
        return True
    normalized_query = normalize_legacy_tool_name(query)
    if _SIMPLE_MATCHER.match(matcher):
        if "|" in matcher:
            return normalized_query in (
                normalize_legacy_tool_name(part.strip()) for part in matcher.split("|") if part.strip()
            )
        return normalized_query == normalize_legacy_tool_name(matcher)
    try:
        regex = re.compile(matcher)
    except re.error:
        logger.debug("Invalid hook matcher regex: %s", matcher)
        return False
    return any(regex.search(candidate) is not None for candidate in _tool_name_match_candidates(query))


def if_condition_matches(condition: str | None, event: HookEvent) -> bool:
    """Mirror Claude's permission-rule ``if`` filter."""

    if not condition:
        return True
    # Re-use the shared permission-rule parser.
    try:
        from intent.permissions.policy import (
            PermissionContext,
            _matches_tool_content,
            _parse_rule_tool,
        )
    except ImportError:
        return False
    tool_name, content_pattern = _parse_rule_tool(condition)
    if not tool_name:
        return False
    target_tool = normalize_legacy_tool_name((event.tool_name or "").strip())
    condition_tool = normalize_legacy_tool_name(tool_name)
    if condition_tool != "*" and condition_tool != target_tool:
        return False
    if content_pattern is None:
        return True
    context = PermissionContext(
        user_id="hook",
        session_id=event.session_id or "hook",
        tool_name=target_tool,
        tool_input=event.tool_input or {},
    )
    return _matches_tool_content(context, content_pattern)


def normalize_legacy_tool_name(name: str) -> str:
    return _LEGACY_TOOL_NAME_ALIASES.get(str(name), str(name))


def _legacy_tool_names_for(canonical_name: str) -> tuple[str, ...]:
    return tuple(legacy for legacy, canonical in _LEGACY_TOOL_NAME_ALIASES.items() if canonical == canonical_name)


def _tool_name_match_candidates(tool_name: str) -> tuple[str, ...]:
    normalized = normalize_legacy_tool_name(tool_name)
    candidates = [tool_name, normalized, *_legacy_tool_names_for(normalized)]
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def select_hooks(
    config: HookConfig,
    event: HookEvent,
) -> tuple[HookCommand, ...]:
    """Resolve the ordered list of hooks fired for ``event``."""

    event_name = coerce_hook_event_name(event.name)
    matchers = config.matchers_for(event_name)
    if not matchers:
        return ()
    query = _match_query(event_name, event)
    match_all = event_name is HookEventName.USER_PROMPT_SUBMIT
    selected: list[HookCommand] = []
    for matcher_group in matchers:
        if not match_all and not matcher_matches(matcher_group.matcher, query):
            continue
        for hook in matcher_group.hooks:
            if event_name in _IF_FILTER_EVENTS and not if_condition_matches(hook.if_condition, event):
                continue
            selected.append(hook)
    return tuple(selected)


def _match_query(event: HookEventName, hook_event: HookEvent) -> str:
    """Mirror Claude's per-event match query selection.

    See ``utils/hooks.ts`` ~:1616 — the matcher applies to ``tool_name`` for
    tool events, ``agent_type`` for subagent events, and an empty string for
    most session events.
    """

    if event in {
        HookEventName.PRE_TOOL_USE,
        HookEventName.POST_TOOL_USE,
        HookEventName.POST_TOOL_USE_FAILURE,
        HookEventName.PERMISSION_REQUEST,
        HookEventName.PERMISSION_DENIED,
    }:
        return str(hook_event.tool_name or "")
    if event in {HookEventName.SESSION_START, HookEventName.CONFIG_CHANGE}:
        return str(hook_event.payload.get("source") or "")
    if event in {HookEventName.SETUP, HookEventName.PRE_COMPACT, HookEventName.POST_COMPACT}:
        return str(hook_event.payload.get("trigger") or "")
    if event in {HookEventName.SUBAGENT_START, HookEventName.SUBAGENT_STOP}:
        return str(hook_event.payload.get("agent_type") or "")
    if event is HookEventName.NOTIFICATION:
        return str(hook_event.payload.get("notification_type") or hook_event.payload.get("type") or "")
    if event is HookEventName.SESSION_END:
        return str(hook_event.payload.get("reason") or "")
    if event is HookEventName.STOP_FAILURE:
        return str(hook_event.payload.get("error") or hook_event.payload.get("reason") or "")
    if event in {HookEventName.ELICITATION, HookEventName.ELICITATION_RESULT}:
        return str(hook_event.payload.get("mcp_server_name") or hook_event.payload.get("server_name") or "")
    if event is HookEventName.INSTRUCTIONS_LOADED:
        return str(hook_event.payload.get("load_reason") or "")
    if event is HookEventName.FILE_CHANGED:
        # F-005: Claude matches FileChanged hooks on the basename, not the
        # full path (utils/hooks/fileChangedWatcher.ts:28-77). Honor an
        # explicit ``file_name`` payload first; fall back to deriving the
        # basename from ``file_path``/``path`` for callers that haven't
        # populated it yet.
        explicit_name = hook_event.payload.get("file_name")
        if isinstance(explicit_name, str) and explicit_name:
            return explicit_name
        from pathlib import Path as _Path

        full_path = hook_event.payload.get("file_path") or hook_event.payload.get("path") or ""
        return _Path(str(full_path)).name if full_path else ""
    if event is HookEventName.CWD_CHANGED:
        # Match on the new cwd (callers can switch to old_cwd via if-condition).
        return str(
            hook_event.payload.get("new_cwd") or hook_event.payload.get("cwd") or hook_event.payload.get("path") or ""
        )
    return ""


def static_file_changed_watch_paths(config: HookConfig, *, cwd: str | Path) -> tuple[str, ...]:
    """Derive initial watch paths from FileChanged matcher literals."""

    base = Path(cwd)
    paths: list[str] = []
    for matcher_group in config.matchers_for(HookEventName.FILE_CHANGED):
        matcher = str(matcher_group.matcher or "").strip()
        if not matcher or matcher == "*":
            continue
        for raw_part in matcher.split("|"):
            part = raw_part.strip()
            if not part or _looks_like_regex_file_matcher(part):
                continue
            path = Path(part).expanduser()
            if not path.is_absolute():
                path = base / path
            paths.append(str(path))
    return _unique_strings(tuple(paths))


def _looks_like_regex_file_matcher(value: str) -> bool:
    return any(char in value for char in "^$[](){}+?\\")


def _unique_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return tuple(result)


# --------------------------------------------------------------------------- #
# Envelope                                                                    #
# --------------------------------------------------------------------------- #


def build_hook_envelope(
    event: HookEvent,
    *,
    cwd: str | None = None,
    permission_mode: str | None = None,
    transcript_path: str | None = None,
    agent_id: str | None = None,
    agent_type: str | None = None,
) -> dict[str, Any]:
    """Build the Claude-shaped hook input JSON sent over stdin/HTTP/prompt.

    Output matches ``src/utils/hooks.ts:createBaseHookInput`` keys:
    ``hook_event_name``, ``session_id``, ``transcript_path``, ``cwd``,
    ``permission_mode``, ``agent_id``, ``agent_type``. Tool events add
    ``tool_name`` and ``tool_input``.
    """

    event_name = coerce_hook_event_name(event.name)
    envelope: dict[str, Any] = {
        "hook_event_name": event_name.value,
        "session_id": event.session_id or "",
        "transcript_path": transcript_path or "",
        "cwd": cwd or "",
    }
    if permission_mode:
        envelope["permission_mode"] = permission_mode
    if agent_id:
        envelope["agent_id"] = agent_id
    if agent_type:
        envelope["agent_type"] = agent_type
    if event.tool_name is not None:
        envelope["tool_name"] = event.tool_name
    if event.tool_input is not None:
        envelope["tool_input"] = event.tool_input
    # Carry every payload field that is not already populated. Hook commands
    # see the raw event payload (transcript, message, prompt, etc.).
    for key, value in event.payload.items():
        if key not in envelope and value is not None:
            envelope[key] = value
    return envelope


# --------------------------------------------------------------------------- #
# Runner                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookExecutionContext:
    """Per-event execution context for the settings runner."""

    cwd: str
    permission_mode: str | None = None
    transcript_path: str | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    allowed_env_vars: tuple[str, ...] = ()


class HookSettingsRunner:
    """Resolve and execute settings/session/plugin/skill hooks for an event.

    The runner doesn't *replace* the in-process lifecycle registry; it stacks
    on top. Order: settings/session config hooks first (matching Claude
    Code's execution model), then in-process handlers. The aggregated
    :class:`HookResult` is merged with whatever lifecycle handlers produce
    upstream so callers can keep the existing dispatch_lifecycle contract.
    """

    def __init__(
        self,
        config: HookConfig | None = None,
        *,
        exec_command: ExecCommandHook | None = None,
        exec_prompt: ExecPromptHook | None = None,
        exec_http: ExecHttpHook | None = None,
        exec_agent: ExecAgentHook | None = None,
    ) -> None:
        # Imports kept local to avoid a hard import cycle on module load.
        from intent.hooks.exec_command import ExecCommandHook
        from intent.hooks.exec_http import ExecHttpHook
        from intent.hooks.exec_prompt import ExecPromptHook

        self._config = config or HookConfig(matchers_by_event={})
        from intent.hooks.exec_agent import default_exec_agent
        from intent.hooks.exec_command import default_exec_command
        from intent.hooks.exec_http import default_exec_http
        from intent.hooks.exec_prompt import default_exec_prompt

        self._exec_command = exec_command or default_exec_command
        self._exec_prompt = exec_prompt or default_exec_prompt
        self._exec_http = exec_http or default_exec_http
        self._exec_agent = exec_agent or default_exec_agent
        self._fired_once: set[int] = set()

    def configure(self, config: HookConfig) -> None:
        self._config = config
        self._fired_once.clear()

    def merge(self, config: HookConfig) -> None:
        """Layer additional matchers (e.g. plugin/skill hooks) onto the runner."""

        if config.is_empty():
            return
        combined: dict[HookEventName, list[HookMatcher]] = {
            event: list(matchers) for event, matchers in self._config.matchers_by_event.items()
        }
        for event, matchers in config.matchers_by_event.items():
            combined.setdefault(event, []).extend(matchers)
        self._config = HookConfig(matchers_by_event={event: tuple(matchers) for event, matchers in combined.items()})

    async def dispatch_async(
        self,
        event: HookEvent,
        context: HookExecutionContext,
    ) -> HookResult:
        """Run all matching hooks concurrently and aggregate results.

        F-035: Claude's hook runner (``utils/hooks.ts:2743-2744``) kicks
        off every matching hook concurrently (``Promise.all`` over the
        executor list) and aggregates after all settle. The previous
        Viola implementation awaited each hook serially, so a slow
        ``http`` hook stalled every following ``command`` hook.

        Aggregation order follows the Claude kind enum (the ``kind``
        ordering in ``HookResult.merge``) which gives deterministic
        last-wins behavior for fields like ``updated_input`` regardless
        of which task completed first.
        """

        hooks = select_hooks(self._config, event)
        if not hooks:
            return HookResult()
        hooks = _dedupe_hooks(hooks)
        envelope = build_hook_envelope(
            event,
            cwd=context.cwd,
            permission_mode=context.permission_mode,
            transcript_path=context.transcript_path,
            agent_id=context.agent_id,
            agent_type=context.agent_type,
        )

        # Filter / once-bookkeep BEFORE kicking off concurrent execution.
        runnable: list[HookCommand] = []
        for hook in hooks:
            if _skip_hook_for_event(hook, event.name):
                continue
            if hook.once and id(hook) in self._fired_once:
                continue
            runnable.append(hook)
        if not runnable:
            return HookResult()

        async def _run(hook: HookCommand) -> tuple[HookResult, bool]:
            try:
                outcome = await self._execute_hook(hook, event, envelope, context)
                return outcome, True
            except Exception as exc:
                logger.exception("Hook %s (%s) failed", hook.kind, hook.describe())
                return hook_result_from_exception(event.name, hook.describe(), exc), False

        gathered = await asyncio.gather(*[_run(hook) for hook in runnable])

        # Mark once-hooks fired only after a successful execution, mirroring
        # the previous serial behavior (a hook that raised should re-run
        # on the next dispatch — test_once_hook_marker_is_consumed_only_
        # after_success).
        aggregate = HookResult()
        for hook, (hook_result, succeeded) in zip(runnable, gathered, strict=True):
            if hook.once and succeeded:
                self._fired_once.add(id(hook))
            aggregate = aggregate.merge(hook_result)
        return aggregate

    def dispatch(
        self,
        event: HookEvent,
        context: HookExecutionContext,
    ) -> HookResult:
        """Synchronous convenience wrapper around :meth:`dispatch_async`."""

        if self._config.is_empty():
            return HookResult()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.dispatch_async(event, context))
        # We were called from inside a running loop — schedule and wait.
        return _run_coroutine_in_thread(self.dispatch_async(event, context))

    async def _execute_hook(
        self,
        hook: HookCommand,
        event: HookEvent,
        envelope: Mapping[str, Any],
        context: HookExecutionContext,
    ) -> HookResult:
        envelope_json = json.dumps(envelope, sort_keys=True, default=str)
        if hook.kind == "command":
            raw = await self._exec_command(hook, envelope_json, context)
        elif hook.kind == "prompt":
            raw = await self._exec_prompt(hook, envelope, context)
        elif hook.kind == "http":
            raw = await self._exec_http(hook, envelope, context)
        elif hook.kind == "agent":
            raw = await self._exec_agent(hook, envelope, context)
        else:  # pragma: no cover - defensive
            raise ValueError("Unknown hook kind: %s" % hook.kind)
        # F-037: settings/plugin/skill hooks claim Claude parity, so we
        # parse their output in strict mode. ``session``-source hooks are
        # parity-claimers too. Non-canonical snake_case keys are rejected
        # so the parity contract isn't silently relaxed; the in-process
        # lifecycle registry continues to accept legacy aliases.
        strict = hook.source in {"userSettings", "projectSettings", "policySettings", "plugin", "skill", "session"}
        return hook_result_from_value(raw, expected_event=event.name, strict=strict)


def _skip_hook_for_event(hook: HookCommand, event_name: HookEventName | str) -> bool:
    return hook.kind == "http" and coerce_hook_event_name(event_name) in {
        HookEventName.SESSION_START,
        HookEventName.SETUP,
    }


def _dedupe_hooks(hooks: tuple[HookCommand, ...]) -> tuple[HookCommand, ...]:
    """De-duplicate hooks by identity key, keeping the LAST occurrence.

    F-035: Claude's ``utils/hooks.ts:1712-1806`` builds a Map keyed by
    the hook command identity. ``Map.set`` overwrites earlier entries,
    so a project-settings hook with the same identity as a user-settings
    hook *replaces* the earlier one. Iteration order is insertion order
    of the LATEST write. Pre-fix Viola used ``set`` semantics: first-
    write wins, which silenced any per-project override layered on top
    of a user-level identical-identity hook.
    """

    seen: dict[tuple[Any, ...], HookCommand] = {}
    for hook in hooks:
        key = (
            _dedupe_namespace(hook),
            hook.kind,
            hook.if_condition,
            hook.command,
            hook.shell,
            hook.prompt,
            hook.model,
            hook.url,
            tuple(sorted(hook.headers.items())),
            hook.allowed_env_vars,
        )
        seen[key] = hook  # last-wins
    return tuple(seen.values())


def _dedupe_namespace(hook: HookCommand) -> str:
    if hook.source == "plugin":
        return hook.plugin_id or ""
    if hook.source == "skill":
        return hook.skill_root or ""
    return ""


def _run_coroutine_in_thread(coro: Any) -> HookResult:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


# --------------------------------------------------------------------------- #
# Loaders                                                                     #
# --------------------------------------------------------------------------- #


def merge_configs(configs: Iterable[HookConfig]) -> HookConfig:
    """Merge multiple configs into one. Order is preserved."""

    combined: dict[HookEventName, list[HookMatcher]] = {}
    for config in configs:
        for event, matchers in config.matchers_by_event.items():
            combined.setdefault(event, []).extend(matchers)
    return HookConfig(matchers_by_event={event: tuple(matchers) for event, matchers in combined.items()})


def load_settings_hook_config(
    settings_payload: Any,
    *,
    source: HookSource = "userSettings",
) -> HookConfig:
    """Parse ``settings.hooks`` from a settings file payload."""

    if not isinstance(settings_payload, Mapping):
        return HookConfig(matchers_by_event={})
    return parse_hook_config(settings_payload.get("hooks"), source=source)


def load_plugin_hook_configs(plugin_roots: Iterable[str | Path] | None = None) -> list[HookConfig]:
    """Walk plugin roots and return one HookConfig per plugin that ships hooks.

    Plugins follow the same disk layout as ``load_plugin_output_styles``:
    ``<root>/<plugin_id>/hooks.json``. Plugins without that file are
    silently skipped — hooks are optional.

    The matchers are tagged with ``source="plugin"`` and the plugin
    directory name as ``plugin_id`` so downstream telemetry can identify
    where a hook originated.
    """

    roots = [Path(root) for root in (plugin_roots or _default_plugin_hook_roots())]
    configs: list[HookConfig] = []
    for root in roots:
        if not root.exists():
            continue
        for plugin_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            hook_file = plugin_dir / "hooks.json"
            if not hook_file.exists():
                continue
            try:
                payload = json.loads(hook_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to parse plugin hook config %s: %s", hook_file, exc)
                continue
            hook_block = payload
            if isinstance(payload, Mapping) and "hooks" in payload:
                hook_block = payload.get("hooks")
            config = parse_hook_config(
                hook_block,
                source="plugin",
                plugin_id=plugin_dir.name,
            )
            if not config.is_empty():
                configs.append(config)
    return configs


def load_skill_hook_configs(skill_roots: Iterable[str | Path] | None = None) -> list[HookConfig]:
    """Walk skill roots and return one HookConfig per skill that ships hooks.

    Mirrors :func:`load_plugin_hook_configs` for SkillMd skills. Each
    skill directory may carry a ``hooks.json`` block; missing files are
    silently skipped.
    """

    roots = [Path(root) for root in (skill_roots or _default_skill_hook_roots())]
    configs: list[HookConfig] = []
    for root in roots:
        if not root.exists():
            continue
        for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            hook_file = skill_dir / "hooks.json"
            if not hook_file.exists():
                continue
            try:
                payload = json.loads(hook_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to parse skill hook config %s: %s", hook_file, exc)
                continue
            hook_block = payload
            if isinstance(payload, Mapping) and "hooks" in payload:
                hook_block = payload.get("hooks")
            config = parse_hook_config(
                hook_block,
                source="skill",
                skill_root=str(skill_dir),
            )
            if not config.is_empty():
                configs.append(config)
    return configs


def load_production_hook_config(
    *,
    user_settings: Any = None,
    project_settings: Any = None,
    policy_settings: Any = None,
    plugin_roots: Iterable[str | Path] | None = None,
    skill_roots: Iterable[str | Path] | None = None,
    session_hooks: HookConfig | None = None,
) -> HookConfig:
    """Merge every hook source the production runner should respect.

    Order (matches Claude's ``utils/hooks.ts:1492-1565`` precedence):
    user settings → project settings → policy settings → plugins →
    skills → session. Each layer's matchers are concatenated; selection
    inside ``HookSettingsRunner.dispatch_async`` already de-dupes.
    """

    layers: list[HookConfig] = []
    if user_settings is not None:
        layers.append(load_settings_hook_config(user_settings, source="userSettings"))
    if project_settings is not None:
        layers.append(load_settings_hook_config(project_settings, source="projectSettings"))
    if policy_settings is not None:
        layers.append(load_settings_hook_config(policy_settings, source="policySettings"))
    layers.extend(load_plugin_hook_configs(plugin_roots))
    layers.extend(load_skill_hook_configs(skill_roots))
    if session_hooks is not None and not session_hooks.is_empty():
        layers.append(session_hooks)
    return merge_configs(layers)


def _default_plugin_hook_roots() -> tuple[Path, ...]:
    try:
        from core.constants import PLUGIN_BUILTIN_DIR, PLUGIN_USER_DIR
    except ImportError:
        return ()
    return (Path(PLUGIN_BUILTIN_DIR), Path(PLUGIN_USER_DIR))


def _default_skill_hook_roots() -> tuple[Path, ...]:
    return (Path("skills/builtin"), Path("skills/user"))


# Type hints used as forward references in HookSettingsRunner.__init__.
ExecCommandHook = Any
ExecPromptHook = Any
ExecHttpHook = Any
ExecAgentHook = Any


__all__ = [
    "HookCommand",
    "HookConfig",
    "HookExecutionContext",
    "HookKind",
    "HookMatcher",
    "HookSettingsRunner",
    "HookSource",
    "build_hook_envelope",
    "if_condition_matches",
    "load_plugin_hook_configs",
    "load_production_hook_config",
    "load_settings_hook_config",
    "load_skill_hook_configs",
    "matcher_matches",
    "merge_configs",
    "parse_hook_config",
    "select_hooks",
    "static_file_changed_watch_paths",
]
