"""Provider-boundary validation for canonical conversation chains."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

ProviderName = Literal["anthropic", "openai_responses"]

_FORBIDDEN_PROSE_MARKERS = (
    "[Prior conversation context]",
    "[Current request]",
    "<conversation-history>",
    "</conversation-history>",
    "<recent-turns>",
    "</recent-turns>",
    "<conversation-repair>",
    "</conversation-repair>",
)


class CanonicalChainValidationError(ValueError):
    """Raised when provider-bound messages violate the canonical chain contract."""


@dataclass(frozen=True)
class _WireEntry:
    index: int
    role: str | None
    kind: str
    payload: Mapping[str, Any]


class CanonicalChainValidator:
    """Validate rendered provider payloads before they leave frame rendering."""

    def validate(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        provider: ProviderName | None = None,
    ) -> None:
        provider_name = provider or _infer_provider(messages)
        if provider_name == "anthropic":
            entries = list(_anthropic_entries(messages))
        elif provider_name == "openai_responses":
            entries = list(_openai_entries(messages))
        else:
            raise CanonicalChainValidationError("Unsupported provider for canonical chain validation")

        self._reject_forbidden_prose(messages)
        self._reject_system_history(entries)
        self._validate_start_role(entries)
        self._validate_role_alternation(entries, provider=provider_name)
        self._validate_tool_pairs(entries, provider=provider_name)

    def _reject_forbidden_prose(self, messages: Sequence[Mapping[str, Any]]) -> None:
        text = _stringify_text_payload(messages)
        for marker in _FORBIDDEN_PROSE_MARKERS:
            if marker in text:
                raise CanonicalChainValidationError("Forbidden prose-history marker in provider chain: %s" % marker)

    def _reject_system_history(self, entries: Sequence[_WireEntry]) -> None:
        for entry in entries:
            if entry.role == "system":
                raise CanonicalChainValidationError("role='system' is not allowed in provider message history")
            if entry.role == "tool":
                raise CanonicalChainValidationError("role='tool' is not canonical provider history")

    def _validate_start_role(self, entries: Sequence[_WireEntry]) -> None:
        first = next((entry for entry in entries if entry.role is not None), None)
        if first is None:
            return
        if first.role != "user":
            raise CanonicalChainValidationError("Canonical provider chain must start with a user/meta-user message")
        if first.kind in {"tool_result", "function_call_output"}:
            raise CanonicalChainValidationError("Provider chain cannot start with an orphan tool result")

    def _validate_role_alternation(self, entries: Sequence[_WireEntry], *, provider: ProviderName) -> None:
        previous: _WireEntry | None = None
        for entry in entries:
            if entry.role is None:
                continue
            if previous is not None and previous.role == entry.role:
                if provider == "openai_responses" and _openai_same_role_batch_allowed(previous, entry):
                    previous = entry
                    continue
                raise CanonicalChainValidationError(
                    "Consecutive provider wire entries share role %r at indexes %d and %d"
                    % (entry.role, previous.index, entry.index)
                )
            previous = entry

    def _validate_tool_pairs(self, entries: Sequence[_WireEntry], *, provider: ProviderName) -> None:
        pending: set[str] = set()
        seen_uses: set[str] = set()
        seen_results: set[str] = set()

        for entry in entries:
            for tool_use_id in _tool_use_ids(entry, provider=provider):
                if not tool_use_id:
                    raise CanonicalChainValidationError("tool_use_id/call_id cannot be empty")
                if tool_use_id in seen_uses:
                    raise CanonicalChainValidationError("Duplicate tool use id in provider chain: %s" % tool_use_id)
                seen_uses.add(tool_use_id)
                pending.add(tool_use_id)

            for tool_result_id in _tool_result_ids(entry, provider=provider):
                if not tool_result_id:
                    raise CanonicalChainValidationError("tool_result id/call_id cannot be empty")
                if tool_result_id not in seen_uses:
                    raise CanonicalChainValidationError(
                        "Orphan tool result appears before matching tool use: %s" % tool_result_id
                    )
                if tool_result_id in seen_results:
                    raise CanonicalChainValidationError("Duplicate tool result in provider chain: %s" % tool_result_id)
                seen_results.add(tool_result_id)
                pending.discard(tool_result_id)

        if pending:
            missing = ", ".join(sorted(pending))
            raise CanonicalChainValidationError("Missing tool result for tool use id(s): %s" % missing)


def validate_canonical_chain(
    messages: Sequence[Mapping[str, Any]],
    *,
    provider: ProviderName | None = None,
) -> None:
    """Validate a rendered provider message chain."""

    CanonicalChainValidator().validate(messages, provider=provider)


def _infer_provider(messages: Sequence[Mapping[str, Any]]) -> ProviderName:
    response_item_types = {"message", "function_call", "function_call_output"}
    if any(str(message.get("type") or "") in response_item_types for message in messages):
        return "openai_responses"
    return "anthropic"


def _anthropic_entries(messages: Sequence[Mapping[str, Any]]) -> Iterable[_WireEntry]:
    for index, message in enumerate(messages):
        role = str(message.get("role") or "").strip() or None
        yield _WireEntry(index=index, role=role, kind="message", payload=message)


def _openai_entries(items: Sequence[Mapping[str, Any]]) -> Iterable[_WireEntry]:
    for index, item in enumerate(items):
        item_type = str(item.get("type") or "").strip()
        if item_type == "message":
            role = str(item.get("role") or "").strip() or None
            yield _WireEntry(index=index, role=role, kind="message", payload=item)
        elif item_type == "function_call":
            yield _WireEntry(index=index, role="assistant", kind="function_call", payload=item)
        elif item_type == "function_call_output":
            yield _WireEntry(index=index, role="user", kind="function_call_output", payload=item)
        else:
            yield _WireEntry(index=index, role=None, kind=item_type, payload=item)


def _openai_same_role_batch_allowed(previous: _WireEntry, current: _WireEntry) -> bool:
    if previous.kind == current.kind == "function_call":
        return True
    if previous.kind == current.kind == "function_call_output":
        return True
    if {previous.kind, current.kind} == {"message", "function_call"} and current.role == "assistant":
        return True
    return False


def _tool_use_ids(entry: _WireEntry, *, provider: ProviderName) -> list[str]:
    if provider == "openai_responses":
        if entry.kind == "function_call":
            return [str(entry.payload.get("call_id") or "")]
        return []

    ids: list[str] = []
    content = entry.payload.get("content")
    if entry.role == "assistant" and isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                ids.append(str(block.get("id") or ""))
    return ids


def _tool_result_ids(entry: _WireEntry, *, provider: ProviderName) -> list[str]:
    if provider == "openai_responses":
        if entry.kind == "function_call_output":
            return [str(entry.payload.get("call_id") or "")]
        return []

    ids: list[str] = []
    content = entry.payload.get("content")
    if entry.role == "user" and isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                ids.append(str(block.get("tool_use_id") or ""))
    return ids


def _stringify_text_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_stringify_text_payload(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_stringify_text_payload(item) for item in value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


__all__ = [
    "CanonicalChainValidationError",
    "CanonicalChainValidator",
    "validate_canonical_chain",
]
