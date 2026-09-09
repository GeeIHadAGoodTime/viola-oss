"""Forked-subagent native-message builder for prompt-cache prefix sharing.

Port of Claude Code's ``buildForkedMessages`` /
``filterIncompleteToolCalls`` (``src/tools/AgentTool/forkSubagent.ts`` and
``src/tools/AgentTool/runAgent.ts``).

When Viola spawns a forked child agent, the child must see the SAME native
message prefix the parent had at fork time so the Anthropic / OpenAI prompt
cache can be shared across concurrent fork children. Two byte-identical
prefixes win cache hits; any per-child variation (uuids, timestamps,
diverging tool_result text) breaks them.

The contract this module implements:

1. The parent's last assistant message is kept intact — every ``tool_use``
   block, ``thinking`` block, and ``text`` block flows through.
2. A single synthetic user message follows that assistant. It contains one
   ``tool_result`` block per parent ``tool_use``, each carrying the SAME
   placeholder text (``FORK_PLACEHOLDER_RESULT``). The per-child directive
   is appended as a sibling text block on that same user message.
3. Earlier history is sanitized to drop any *other* assistant messages whose
   tool calls never received a result. The Claude reference filter
   (``filterIncompleteToolCalls``) does exactly this — it stops the child
   from inheriting a second orphan that the placeholder layer would not
   cover. The last assistant message is exempt because the placeholder
   layer pairs it explicitly.

Because the directive is the only per-child variation, all concurrent
children of the same parent emit byte-identical prefixes up to the last
text block. That is the prompt cache key Claude relies on, and Viola needs
to match here to avoid paying full input cost per child.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

# Byte-identical placeholder result text. Do not localize, do not format-string
# in dynamic content, do not add per-child suffixes. Drift here is a silent
# cache miss across every concurrent fork child.
FORK_PLACEHOLDER_RESULT = "Fork started — processing in background"

# XML tag wrapping the child directive. Mirrors Claude Code's
# ``FORK_BOILERPLATE_TAG`` so existing recursion guards keyed on the tag stay
# valid. Also kept identical across children for cache-prefix stability.
FORK_BOILERPLATE_TAG = "fork-boilerplate"

# Prefix appended in front of the directive. Mirrors ``FORK_DIRECTIVE_PREFIX``
# in Claude Code; kept stable across children for the same cache reason.
FORK_DIRECTIVE_PREFIX = "Your directive: "


def build_child_directive_text(directive: str) -> str:
    """Return the boilerplate-wrapped directive injected into the synthetic user message.

    This is the ONLY part of the synthetic user message that legitimately
    varies between concurrent children. Everything else (tool_result blocks,
    placeholder text, ordering) is byte-identical so cache-prefix sharing
    holds up to and including the placeholder blocks.
    """

    return ("""<%s>
Worker instructions take precedence over parent-only delegation guidance.

You are the assigned child worker. Carry out the directive yourself; do not create or delegate to additional agents.
Use the available tools to complete the assigned work. When the directive names a tool, use it before reporting. Once the requested work is already complete and verified, report the result directly without taking additional actions.
Work without conversational replies, clarification questions, suggestions for future work, or commentary between tool calls.
Limit your actions and your report to the assigned scope. Discuss a related system only when necessary, in at most one sentence.
If you change files, commit the changes and include the commit identifier in your report.
After the work is finished, give one factual, structured report, beginning with 'Scope:', in fewer than 500 words.
Keep the report concise and omit opinions or editorial commentary. Stop after that report.
</%s>

%s%s""") % (FORK_BOILERPLATE_TAG, FORK_BOILERPLATE_TAG, FORK_DIRECTIVE_PREFIX, directive)


def _extract_tool_use_blocks(assistant_message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return all ``tool_use`` blocks present in an assistant message."""

    if not isinstance(assistant_message, dict):
        return []
    content = assistant_message.get("content")
    if not isinstance(content, list):
        return []
    tool_uses: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        tool_use_id = block.get("id") or block.get("tool_use_id")
        if not tool_use_id:
            continue
        tool_uses.append(block)
    return tool_uses


def _tool_use_id_for(block: dict[str, Any]) -> str | None:
    raw = block.get("id") or block.get("tool_use_id")
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    return raw or None


def _last_assistant_index(messages: Sequence[dict[str, Any]]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return index
    return -1


def filter_incomplete_tool_calls(
    messages: Sequence[dict[str, Any]],
    *,
    keep_last_assistant: bool = True,
) -> list[dict[str, Any]]:
    """Drop assistant messages whose tool_use blocks have no matching tool_result.

    Port of Claude Code's ``filterIncompleteToolCalls`` (``runAgent.ts:866``).
    Walks the message list once to collect every ``tool_use_id`` that has a
    paired ``tool_result``, then drops any assistant whose tool_use blocks are
    all unanswered.

    The Claude variant drops these BEFORE the placeholder turn is appended,
    so it cannot accidentally drop the most recent assistant (which is the
    one the placeholder turn pairs). When ``keep_last_assistant`` is True the
    final assistant message is preserved regardless — the placeholder layer
    in :func:`build_forked_messages` is responsible for it.
    """

    result_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            if isinstance(tool_use_id, str) and tool_use_id.strip():
                result_ids.add(tool_use_id.strip())

    last_assistant = _last_assistant_index(messages) if keep_last_assistant else -1

    filtered: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant" and index != last_assistant:
            content = message.get("content")
            if isinstance(content, list):
                has_incomplete = any(
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and isinstance(_tool_use_id_for(block), str)
                    and _tool_use_id_for(block) not in result_ids
                    for block in content
                )
                if has_incomplete:
                    continue
        filtered.append(message)
    return filtered


def _build_placeholder_tool_result_blocks(
    tool_use_blocks: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one placeholder ``tool_result`` block per tool_use, in order.

    Block content uses the structured ``[{"type": "text", "text": ...}]``
    shape to match Claude's serializer output. ``is_error`` is omitted (left
    falsy) because the parent's tool call is not failing — it is owned by a
    different worker.
    """

    blocks: list[dict[str, Any]] = []
    for tool_use in tool_use_blocks:
        tool_use_id = _tool_use_id_for(tool_use)
        if tool_use_id is None:
            continue
        blocks.append(
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [
                    {
                        "type": "text",
                        "text": FORK_PLACEHOLDER_RESULT,
                    }
                ],
            }
        )
    return blocks


def build_forked_messages(
    parent_messages: Sequence[dict[str, Any]],
    directive: str,
) -> list[dict[str, Any]]:
    """Build the native message prefix a forked child should start with.

    The returned list is independent of ``parent_messages`` (deep-copied) and
    has no per-child uuids or timestamps, so two concurrent children given
    the same ``parent_messages`` and ``directive`` produce two byte-identical
    lists. That is the property the prompt cache keys on.

    Layout::

        [
            *filter_incomplete_tool_calls(parent history),
            assistant_message_intact_with_all_tool_uses,
            {
                role: "user",
                content: [
                    {tool_result placeholder for parent tool_use #1},
                    {tool_result placeholder for parent tool_use #2},
                    ...
                    {text: child directive},
                ],
            },
        ]

    If the parent's last assistant message has no ``tool_use`` blocks (the
    parent is calling the agent tool via text? unusual but possible), the
    synthetic user message degrades to a single text block carrying just the
    directive. The parent assistant is still preserved.
    """

    safe_directive = directive if isinstance(directive, str) else ""
    cleaned = filter_incomplete_tool_calls(parent_messages, keep_last_assistant=True)
    if not cleaned:
        return [
            {
                "role": "user",
                "content": build_child_directive_text(safe_directive),
            }
        ]

    last_assistant = _last_assistant_index(cleaned)
    if last_assistant < 0:
        # No assistant turn at all — degrade to fresh-mode-style single user.
        return [
            {
                "role": "user",
                "content": build_child_directive_text(safe_directive),
            },
        ]

    prefix = copy.deepcopy(cleaned[: last_assistant + 1])
    assistant_message = prefix[-1]
    tool_use_blocks = _extract_tool_use_blocks(assistant_message)
    placeholder_blocks = _build_placeholder_tool_result_blocks(tool_use_blocks)

    directive_block = {
        "type": "text",
        "text": build_child_directive_text(safe_directive),
    }

    if placeholder_blocks:
        synthetic_user: dict[str, Any] = {
            "role": "user",
            "content": [*placeholder_blocks, directive_block],
        }
    else:
        # Last assistant has no tool calls — there is nothing to placeholder.
        # Preserve the parent prefix and append a directive-only user turn.
        synthetic_user = {
            "role": "user",
            "content": [directive_block],
        }

    return [*prefix, synthetic_user]


__all__ = [
    "FORK_BOILERPLATE_TAG",
    "FORK_DIRECTIVE_PREFIX",
    "FORK_PLACEHOLDER_RESULT",
    "build_child_directive_text",
    "build_forked_messages",
    "filter_incomplete_tool_calls",
]
