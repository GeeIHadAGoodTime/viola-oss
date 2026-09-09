"""Thin tool handlers for the user's Workbench folder."""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from intent.tool_types import ToolResult

# Per-request search-result-ref cache, keyed by user_id (no direct cross-user
# bleed) but process-global and previously unbounded (#2776). Bounded to the
# most-recently-used users so a long-running process can't grow this dict
# without limit; eviction is LRU via OrderedDict.move_to_end/popitem.
_SEARCH_RESULT_REFS: OrderedDict[str, dict[str, str]] = OrderedDict()
_MAX_WORKBENCH_SEARCH_REF_USERS = 500
_MAX_WORKBENCH_TOOL_CONTENT_CHARS = 10000

# R5-P0-G: removed the parallel injection-pattern regex + [REDACTED]
# rewriter + [WORKBENCH_CONTENT_START]/...END prose wrapper that mirrored
# the deleted content_sanitizer redaction layer. Same doctrine applies:
# the runtime does not regex-redact tool-result content. Prompt-injection
# defense for tool-result content lives in the unified system prompt
# (services/llm/prompts/viola_unified.py).


def _resolve_user_id(user_id: str | None = None) -> str:
    """Resolve the user_id to scope a Workbench call to.

    Raises LookupError if no explicit user_id was passed and no user
    context is active — callers MUST catch this and return a clean
    ToolResult failure (#2776); it must never propagate as an uncaught
    exception out of a tool handler.
    """
    resolved = (user_id or "").strip()
    if resolved:
        return resolved
    from core.user_context import get_current_user_id

    return get_current_user_id()


def _workbench_dir(user_id: str):
    from services.workbench.dir import get_workbench_dir

    return get_workbench_dir(user_id)


def _voice_note_filename(body: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", body.lower())[:6]
    words = [word for word in words if word not in {"remember", "this", "that", "the", "and", "for"}]
    if words:
        return "%s.txt" % "-".join(words[:5])
    return "voice-note-%s.txt" % datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _cache_refs(user_id: str, results: list[dict[str, Any]]) -> None:
    _SEARCH_RESULT_REFS[user_id] = {
        "r%d" % index: str(result["name"] if "name" in result else result["file"])
        for index, result in enumerate(results, start=1)
    }
    _SEARCH_RESULT_REFS.move_to_end(user_id)
    while len(_SEARCH_RESULT_REFS) > _MAX_WORKBENCH_SEARCH_REF_USERS:
        _SEARCH_RESULT_REFS.popitem(last=False)


def _resolve_result(user_id: str, result_id: str) -> str | None:
    return _SEARCH_RESULT_REFS.get(user_id, {}).get((result_id or "").strip())


def _no_user_context_result(exc: LookupError) -> ToolResult:
    """Clean, catchable tool failure for a Workbench call with no user context.

    #2776: previously _resolve_user_id's LookupError was uncaught at every
    call site and propagated out of the handler as a crash instead of a
    normal ToolResult failure.
    """
    return ToolResult(ok=False, data=None, error="Workbench requires a signed-in user context: %s" % exc)


def _frame_workbench_content(content: str) -> str:
    """Normalize and bound the size of workbench file content (post R5-P0-G).

    Only NFKC normalization and length truncation are applied. The model
    sees the file content verbatim; prompt-injection defense lives in the
    system prompt, not in a runtime content regex.
    """
    text = unicodedata.normalize("NFKC", content or "")
    if len(text) > _MAX_WORKBENCH_TOOL_CONTENT_CHARS:
        text = text[:_MAX_WORKBENCH_TOOL_CONTENT_CHARS] + "\n[TRUNCATED]"
    return text


async def workbench_remember_handler(
    content: str,
    title_hint: str = "",
    source: str = "voice",
    user_id: str = "",
    **_legacy_kwargs: Any,
) -> ToolResult:
    del source
    try:
        uid = _resolve_user_id(user_id)
    except LookupError as exc:
        return _no_user_context_result(exc)
    body = (content or "").strip()
    if not body:
        return ToolResult(ok=False, data=None, error="Workbench content cannot be empty.")
    filename = (title_hint or _voice_note_filename(body)).strip()
    if not filename.lower().endswith(".txt"):
        filename += ".txt"
    try:
        result = _workbench_dir(uid).add(filename, body.encode("utf-8"), replace=False)
        result["voice_summary"] = "Saved %s to your Workbench." % result["name"]
        return ToolResult(ok=True, data=result)
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def workbench_search_handler(
    query: str = "",
    tags: str = "",
    limit: int = 10,
    user_id: str = "",
    **_legacy_kwargs: Any,
) -> ToolResult:
    del tags
    try:
        uid = _resolve_user_id(user_id)
    except LookupError as exc:
        return _no_user_context_result(exc)
    try:
        results = _workbench_dir(uid).search(query)[: max(1, int(limit))]
        for index, result in enumerate(results, start=1):
            result["id"] = "r%d" % index
            result["filename"] = result.get("name") or result.get("file")
            if result.get("snippet"):
                result["snippet"] = _frame_workbench_content(str(result["snippet"]))
        _cache_refs(uid, results)
        return ToolResult(ok=True, data={"query": query, "count": len(results), "results": results})
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def workbench_path_for_handler(
    filename: str = "",
    result_id: str = "",
    user_id: str = "",
    **_legacy_kwargs: Any,
) -> ToolResult:
    try:
        uid = _resolve_user_id(user_id)
    except LookupError as exc:
        return _no_user_context_result(exc)
    name = filename.strip() or (_resolve_result(uid, result_id) or "")
    if not name:
        return ToolResult(ok=False, data=None, error="Provide filename or result_id.")
    try:
        path = _workbench_dir(uid).path_for(name)
        return ToolResult(ok=True, data={"filename": path.name, "path": str(path)})
    except (FileNotFoundError, OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def workbench_read_handler(
    result_id: str = "",
    item_id: str = "",
    filename: str = "",
    user_id: str = "",
    **_legacy_kwargs: Any,
) -> ToolResult:
    del item_id
    path_result = await workbench_path_for_handler(filename=filename, result_id=result_id, user_id=user_id)
    if not path_result.ok:
        return path_result
    data = dict(path_result.data or {})
    try:
        uid = _resolve_user_id(user_id)
    except LookupError as exc:
        return _no_user_context_result(exc)
    try:
        path = _workbench_dir(uid).path_for(str(data["filename"]))
        if path.suffix.lower() in {".txt", ".md", ".json", ".csv", ".log"}:
            data["content"] = _frame_workbench_content(path.read_text(encoding="utf-8", errors="replace"))
        data["voice_summary"] = "%s is ready; use read_file on the returned path for full contents." % path.name
        return ToolResult(ok=True, data=data)
    except (FileNotFoundError, OSError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def workbench_list_handler(limit: int = 20, user_id: str = "", **_legacy_kwargs: Any) -> ToolResult:
    try:
        uid = _resolve_user_id(user_id)
    except LookupError as exc:
        return _no_user_context_result(exc)
    try:
        items = _workbench_dir(uid).list()[: max(1, int(limit))]
        for item in items:
            item["filename"] = item["name"]
        return ToolResult(
            ok=True,
            data={
                "count": len(items),
                "items": items,
                "voice_summary": "Your Workbench has %d file%s." % (len(items), "" if len(items) == 1 else "s"),
            },
        )
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def workbench_forget_handler(
    item_id: str = "",
    filename: str = "",
    confirm: bool = False,
    user_id: str = "",
    **_legacy_kwargs: Any,
) -> ToolResult:
    del item_id
    if not confirm:
        return ToolResult(ok=False, data=None, error="confirm=true is required to delete a Workbench file.")
    try:
        uid = _resolve_user_id(user_id)
    except LookupError as exc:
        return _no_user_context_result(exc)
    name = filename.strip()
    if not name:
        return ToolResult(ok=False, data=None, error="Provide filename.")
    try:
        removed = _workbench_dir(uid).remove(name)
        if not removed:
            return ToolResult(ok=False, data=None, error="No matching file found.")
        return ToolResult(ok=True, data={"forgotten_count": 1, "filename": name, "voice_summary": "Deleted."})
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


__all__ = [
    "workbench_forget_handler",
    "workbench_list_handler",
    "workbench_path_for_handler",
    "workbench_read_handler",
    "workbench_remember_handler",
    "workbench_search_handler",
]
