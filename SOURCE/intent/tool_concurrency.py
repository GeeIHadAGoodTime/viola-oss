"""Per-call concurrency-safety classification + batch partitioning.

Claude parity (S6-004): Claude's ``toolOrchestration.partitionToolCalls``
(``services/tools/toolOrchestration.ts:84-116``) splits the assistant's
``tool_use`` batch into consecutive groups, where each group is either:

1. A single non-read-only tool — runs serially.
2. A contiguous run of read-only tools — runs concurrently, bounded by
   ``CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`` (default 10).

Concurrency safety is decided per call. Stateful local surfaces such as
browser, desktop, payment, and signature are never parallelized. For
other MCP tools the source of truth is the server's ``readOnlyHint``
annotation (``services/mcp/client.ts:1795-1799``); for Viola built-ins we
fall back to the legacy allowlist (``_PARALLEL_READ_ONLY_TOOLS`` +
``_PARALLEL_READ_ONLY_ACTIONS``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

# ─── Legacy Viola allowlists (mirror of intent.agent_executor) ───────────────

_PARALLEL_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "web_search",
        "web_read",
        "get_weather",
        "weather",
        "search_tracks",
        "toolsearch",
        "tool_search",
    }
)

_PARALLEL_READ_ONLY_ACTIONS: dict[str, frozenset[str]] = {
    "calendar": frozenset({"list", "today", "tomorrow", "week", "get", "find_free_time"}),
    "google_calendar": frozenset({"list_events", "list_calendars", "get_event", "find_free_time"}),
    "gmail": frozenset({"search", "read", "list_labels"}),
    "google_drive": frozenset({"search", "download", "find_folder", "get_comments"}),
    "google_docs": frozenset({"get_text", "get_suggestions"}),
    "google_sheets": frozenset({"get_text", "get_range", "get_metadata"}),
    "google_slides": frozenset({"get_text", "get_metadata", "get_images", "get_thumbnail"}),
}

_PARALLEL_EXCLUDED_PREFIXES: tuple[str, ...] = (
    "browser_",
    "desktop_",
    "computer",
    "payment",
    "signature",
)

_DEFAULT_MAX_TOOL_USE_CONCURRENCY = 10


def _is_external_namespaced_tool(tool_name: str) -> bool:
    """True for externally-registered MCP tools that carry a server namespace.

    Mirrors ``mcp_hub.approval_bridge._is_namespaced_external_tool_name``.
    Externally-registered tools are NOT trusted to self-declare concurrency
    safety (SEC-010/SEC-033, sweep 2026-06-09): a malicious/registered server
    could claim ``readOnlyHint=True`` to slip a mutating tool into a concurrent
    read batch.
    """
    name = str(tool_name or "").strip()
    if not name:
        return False
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return len(parts) == 3 and bool(parts[1]) and bool(parts[2])
    if "__" in name:
        server, tool = name.split("__", 1)
        return bool(server and tool)
    if "." in name:
        server, tool = name.split(".", 1)
        return bool(server and tool)
    return False


def get_max_tool_use_concurrency() -> int:
    """Return the max concurrent in-flight reads in a safe batch.

    Mirrors ``getMaxToolUseConcurrency``
    (``services/tools/toolOrchestration.ts:8-12``). Respects
    ``CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY`` so Viola can be tuned with
    the same knob as Claude.
    """

    raw = os.environ.get("CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY", "").strip()
    if not raw:
        return _DEFAULT_MAX_TOOL_USE_CONCURRENCY
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOOL_USE_CONCURRENCY
    return value if value > 0 else _DEFAULT_MAX_TOOL_USE_CONCURRENCY


def is_concurrency_safe_tool(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    annotation_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> bool:
    """Decide whether ``tool_name`` is safe to run concurrently.

    Resolution order:

    1. Tools whose name starts with any of
       :data:`_PARALLEL_EXCLUDED_PREFIXES` (browser, payment, etc.) are
       unsafe regardless of args or MCP annotations.
    2. If ``annotation_lookup(tool_name)`` returns a dict carrying
       ``readOnlyHint=True``, the tool is safe.  This honors MCP
       servers' self-declared semantics only after local never-parallel
       stateful surfaces have been excluded.
    3. Tools in :data:`_PARALLEL_READ_ONLY_TOOLS` are safe.
    4. Tools with an ``action`` arg in :data:`_PARALLEL_READ_ONLY_ACTIONS`
       (compound tools like ``calendar``/``gmail``) are safe for that
       specific action.

    Everything else defaults to **unsafe** so writes/mutations never
    race.
    """

    if not tool_name:
        return False

    lower_name = tool_name.lower()
    if lower_name.startswith(_PARALLEL_EXCLUDED_PREFIXES):
        return False

    # Externally-registered (namespaced) MCP tools are never trusted to
    # self-declare concurrency safety. A registered server cannot opt itself
    # into a concurrent read batch by claiming readOnlyHint=True; it falls
    # through to the conservative default (unsafe → serial). SEC-010/SEC-033.
    external = _is_external_namespaced_tool(tool_name)

    # 2. MCP annotation override for non-excluded, non-external tools.
    if annotation_lookup is not None and not external:
        try:
            ann = annotation_lookup(tool_name)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            ann = None
        if isinstance(ann, dict):
            hint = ann.get("readOnlyHint")
            if hint is True:
                return True
            if hint is False:
                return False
    elif external:
        return False

    if lower_name in _PARALLEL_READ_ONLY_TOOLS:
        return True

    args = tool_args if isinstance(tool_args, dict) else {}
    action = str(args.get("action", "")).strip().lower()
    allowed_actions = _PARALLEL_READ_ONLY_ACTIONS.get(lower_name)
    return bool(action and allowed_actions and action in allowed_actions)


@dataclass
class ToolCallBatch:
    """One batch in a partitioned ``tool_use`` sequence.

    Mirrors Claude's ``Batch`` (``services/tools/toolOrchestration.ts:84``).

    Attributes:
        is_concurrency_safe: True for a contiguous run of read-only
            tools (run concurrently); False for a single non-read-only
            tool (run serially).
        tool_calls: The original tool-call dicts in this batch.
    """

    is_concurrency_safe: bool
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def partition_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    is_safe: Callable[[dict[str, Any]], bool],
) -> list[ToolCallBatch]:
    """Partition ``tool_calls`` into consecutive safe/unsafe batches.

    Each batch is either:

    - one unsafe call, or
    - a contiguous run of safe calls.

    Mirrors Claude's ``partitionToolCalls``
    (``services/tools/toolOrchestration.ts:91-116``). The classifier
    ``is_safe`` is injected so the caller can mix annotation lookups
    (MCP) and legacy allowlists (Viola built-ins).
    """

    batches: list[ToolCallBatch] = []
    for call in tool_calls or []:
        try:
            safe = bool(is_safe(call))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            safe = False  # Conservative on classifier failure.
        if safe and batches and batches[-1].is_concurrency_safe:
            batches[-1].tool_calls.append(call)
        else:
            batches.append(ToolCallBatch(is_concurrency_safe=safe, tool_calls=[call]))
    return batches


__all__ = [
    "_PARALLEL_EXCLUDED_PREFIXES",
    "_PARALLEL_READ_ONLY_ACTIONS",
    "_PARALLEL_READ_ONLY_TOOLS",
    "ToolCallBatch",
    "get_max_tool_use_concurrency",
    "is_concurrency_safe_tool",
    "partition_tool_calls",
]
