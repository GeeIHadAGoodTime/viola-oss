"""Agent message manager -- Anthropic native message history manipulation."""

from __future__ import annotations

from typing import Any

from intent.context_compaction import (
    filter_compact_boundaries_for_provider,
    messages_after_compact_boundary,
    project_native_messages_for_provider,
)
from services.conversation.message_invariants import (
    repair_delta_messages,
    repair_full_transcript,
)


def tool_result_replacement_session_id(executor: Any) -> str:
    """Return the stable conversation/session id for tool-result records."""

    for value in (
        getattr(executor, "_session_id", None),
        getattr(getattr(executor, "_conversation_state_manager", None), "_session_id", None),
        getattr(executor, "task_id", None),
    ):
        if value:
            return str(value)
    return "default"


def _replacement_record_key(record: Any) -> tuple[str, str, str]:
    if isinstance(record, dict):
        kind = str(record.get("kind") or "tool-result")
        tool_use_id = str(record.get("tool_use_id") or "")
        replacement = str(record.get("replacement") or "")
    else:
        kind = str(getattr(record, "kind", "tool-result") or "tool-result")
        tool_use_id = str(getattr(record, "tool_use_id", "") or "")
        replacement = str(getattr(record, "replacement", "") or "")
    return kind, tool_use_id, replacement


def _snapshot_entry_from_record(record: Any) -> dict[str, Any] | None:
    if isinstance(record, dict):
        tool_use_id = str(record.get("tool_use_id") or "")
        replacement = str(record.get("replacement") or record.get("preview") or "")
        original_chars = record.get("original_chars", record.get("bytes"))
        session_id = record.get("session_id")
        message_index = record.get("message_index")
        path = record.get("path")
    else:
        tool_use_id = str(getattr(record, "tool_use_id", "") or "")
        replacement = str(getattr(record, "replacement", "") or "")
        original_chars = getattr(record, "original_chars", None)
        session_id = getattr(record, "session_id", None)
        message_index = getattr(record, "message_index", None)
        path = getattr(record, "path", None)
    if not tool_use_id or not replacement:
        return None
    entry: dict[str, Any] = {
        "kind": "tool-result",
        "tool_use_id": tool_use_id,
        "replacement": replacement,
        "preview": replacement,
    }
    if original_chars is not None:
        try:
            entry["bytes"] = int(original_chars)
        except (TypeError, ValueError):
            entry["bytes"] = len(replacement)
    if session_id:
        entry["session_id"] = str(session_id)
    if message_index is not None:
        entry["message_index"] = message_index
    if path:
        entry["path"] = str(path)
    return entry


def record_tool_result_replacements(executor: Any, new_records: list[Any]) -> None:
    """Record replacement metadata on the executor and session snapshot."""

    if not new_records:
        return
    records = getattr(executor, "_tool_result_replacement_records", None)
    if records is None:
        records = []
        executor._tool_result_replacement_records = records
    existing_keys = {_replacement_record_key(record) for record in records}
    for record in new_records:
        key = _replacement_record_key(record)
        if not key[1] or key in existing_keys:
            continue
        records.append(record)
        existing_keys.add(key)

    manager = getattr(executor, "_conversation_state_manager", None)
    snapshot = getattr(manager, "_content_replacement_state", None)
    if not isinstance(snapshot, dict):
        return
    for record in new_records:
        entry = _snapshot_entry_from_record(record)
        if entry is not None:
            entry.setdefault("session_id", tool_result_replacement_session_id(executor))
            snapshot[entry["tool_use_id"]] = entry


def reconstruct_executor_content_replacement_state(executor: Any, messages: list[dict[str, Any]]) -> None:
    """Rebuild frozen replacement decisions from provider-bound startup state."""

    if getattr(executor, "_content_replacement_state", None) is None:
        return
    from services.conversation.tool_result_storage import provision_content_replacement_state

    records = list(getattr(executor, "_tool_result_replacement_records", None) or [])
    manager_snapshot = getattr(
        getattr(executor, "_conversation_state_manager", None), "_content_replacement_state", None
    )
    if isinstance(manager_snapshot, dict):
        for tool_use_id, value in manager_snapshot.items():
            if not isinstance(value, dict):
                continue
            replacement = value.get("replacement") or value.get("preview")
            if replacement:
                records.append(
                    {
                        "kind": "tool-result",
                        "tool_use_id": str(value.get("tool_use_id") or tool_use_id),
                        "replacement": str(replacement),
                    }
                )
    initial_messages = filter_compact_boundaries_for_provider(messages_after_compact_boundary(messages))
    executor._tool_result_replacement_records = records
    executor._content_replacement_state = provision_content_replacement_state(
        initial_messages=initial_messages,
        initial_records=records,
        enabled=True,
    )


class AgentMessageManager:
    """Message history management for native tool calling."""

    _MAX_SCREENSHOT_IMAGES = 2  # max screenshot images retained in context
    _KEEP_FULL_EXCHANGES = 3

    def __init__(self, executor: Any) -> None:
        self._exec = executor

    def trim(self) -> None:
        """Prepare native history for a provider call without owning compaction."""

        state = getattr(self._exec, "_content_replacement_state", None)
        if state is None and not hasattr(self._exec, "_content_replacement_state"):
            from services.conversation.tool_result_storage import provision_content_replacement_state

            state = provision_content_replacement_state(enabled=True)
            self._exec._content_replacement_state = state
        projected = project_native_messages_for_provider(
            self._exec._native_messages,
            content_replacement_state=state,
            replacement_writer=lambda records: record_tool_result_replacements(self._exec, records),
            session_id=tool_result_replacement_session_id(self._exec),
        )
        self._exec._native_messages[:] = repair_full_transcript(projected)
        self.evict_old_screenshots()

    @staticmethod
    def _tool_result_message_indices(messages: list[dict[str, Any]]) -> list[int]:
        indices: list[int] = []
        for index, message in enumerate(messages):
            if index == 0 or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result" for block in content
            ):
                indices.append(index)
        return indices

    def sanitize_for_api(
        self,
        *,
        trim_history: bool,
        delta_mode: bool = False,
        continuity_seen_tool_uses: set[str] | None = None,
    ) -> None:
        """Repair native Anthropic history immediately before an API call.

        Args:
            trim_history: If True, run the legacy full-transcript trim+repair path.
            delta_mode: If True, treat ``_native_messages`` as a delta whose matching
                assistant ``tool_use`` blocks may live in provider continuity state
                rather than in this local slice. Routes through ``repair_delta_messages``
                so a leading orphan ``tool_result`` is preserved when its id is known
                from continuity (Claude TS analog: ``ensureToolResultPairing`` is run
                over the FULL provider-bound stream, not a sliced delta).
            continuity_seen_tool_uses: Tool-use ids that the provider already saw via
                continuity state (e.g. OpenAI Responses ``response_items`` /
                ``previous_response_id``). Used only when ``delta_mode`` is True.
        """
        if trim_history:
            self.trim()
            return
        projection_kwargs = {
            "content_replacement_state": getattr(self._exec, "_content_replacement_state", None),
            "replacement_writer": lambda records: record_tool_result_replacements(self._exec, records),
            "session_id": tool_result_replacement_session_id(self._exec),
        }
        if delta_mode:
            projected = project_native_messages_for_provider(self._exec._native_messages, **projection_kwargs)
            self._exec._native_messages[:] = repair_delta_messages(
                projected,
                continuity_seen_tool_uses=continuity_seen_tool_uses,
            )
        else:
            projected = project_native_messages_for_provider(self._exec._native_messages, **projection_kwargs)
            self._exec._native_messages[:] = repair_full_transcript(projected)

        self.evict_old_screenshots()

    def evict_old_screenshots(self) -> None:
        """Replace all but the most recent N screenshot images with text placeholders.

        Screenshots are base64-encoded PNGs that cost thousands of image tokens.
        Only the most recent _MAX_SCREENSHOT_IMAGES are useful for decision-making.
        """
        msgs = self._exec._native_messages

        # Collect (msg_index, block_index) for all tool_result blocks with images
        image_locations: list[tuple[int, int, int]] = []  # (msg_idx, block_idx, sub_idx)
        for i, msg in enumerate(msgs):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for j, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                inner = block.get("content")
                if not isinstance(inner, list):
                    continue
                for k, sub in enumerate(inner):
                    if isinstance(sub, dict) and sub.get("type") == "image":
                        image_locations.append((i, j, k))

        # Keep only the last N images
        if len(image_locations) <= self._MAX_SCREENSHOT_IMAGES:
            return

        to_evict = image_locations[: -self._MAX_SCREENSHOT_IMAGES]
        for msg_idx, block_idx, sub_idx in to_evict:
            block = msgs[msg_idx]["content"][block_idx]
            inner = block["content"]
            # Get text description from the text block in the same tool_result
            text_desc = ""
            for sub in inner:
                if isinstance(sub, dict) and sub.get("type") == "text":
                    text_desc = sub.get("text", "")
                    break
            # Replace entire content with text-only summary
            block["content"] = "[screenshot evicted] %s" % text_desc
