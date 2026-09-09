"""Agent tool handlers for markdown-first user memory."""

from __future__ import annotations

import re

from core.logging_config import get_logger
from intent.tool_types import ToolResult
from services.memory.store import SENSITIVE_CONTENT_RE

logger = get_logger(__name__)

_RAW_CREDENTIAL_RE = re.compile(
    r"^(?:"
    r"(?:sk|pk|api|key|tok|sec|pat)[_-][A-Za-z0-9]{8,}"
    r"|[A-Za-z]+\d+[A-Za-z0-9!@#$%^&*()_+=.-]*"
    r"|[0-9]+[A-Za-z]+[A-Za-z0-9!@#$%^&*()_+=.-]*"
    r"|[a-fA-F0-9]{16,}"
    r"|[A-Za-z0-9+/=]{20,}"
    r")$"
)


def _resolve_user_id(user_id: str | None = None) -> str:
    resolved = (user_id or "").strip()
    if resolved:
        return resolved
    from core.user_context import get_current_user_id

    try:
        return get_current_user_id()
    except LookupError as exc:
        raise ValueError("user_id is required for memory operations") from exc


def _sensitive_error(content: str) -> str | None:
    if SENSITIVE_CONTENT_RE.search(content):
        return (
            "Cannot store sensitive credentials, payment data, SSNs, PINs, bank details, "
            "API keys, tokens, or passwords in plaintext memory."
        )
    words = content.split()
    if len(words) == 1 and _RAW_CREDENTIAL_RE.match(content.strip()):
        return (
            "That looks like a raw credential value. Passwords, API keys, and tokens "
            "belong in a password manager or the appropriate vault, not memory."
        )
    return None


def _strip_frontmatter(content: str) -> str:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return content
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :])
    return content


def _format_recalled_memory(path: str, content: str) -> str:
    body = _strip_frontmatter(content).strip()
    if not body:
        return ""
    return "%s\n%s" % (path, body)


def _recall_memory(uid: str, query: str, limit: int = 5) -> ToolResult:
    from services.memory.dir import get_memory_dir
    from services.memory.selector import select_relevant_memory_headers

    memory = get_memory_dir(uid)
    selected = select_relevant_memory_headers(memory, query, limit=limit, user_id=uid)
    chunks: list[str] = []
    files: list[str] = []
    for header in selected:
        path = "topics/%s" % header.filename
        rendered = _format_recalled_memory("memory/%s" % path, memory.read(path))
        if not rendered:
            continue
        chunks.append(rendered)
        files.append("memory/%s" % path)
    return ToolResult(
        ok=True,
        data={"content": "\n\n".join(chunks), "files": files, "match_count": len(files), "matched": bool(files)},
    )


async def memory_handler(
    action: str = "read",
    path: str = "MEMORY.md",
    query: str = "",
    content: str = "",
    where: str = "memory",
    position: str = "append",
    find: str = "",
    replace: str = "",
    line_number: int = 0,
    section_title: str = "",
    limit: int = 20,
    user_id: str = "",
) -> ToolResult:
    """Run one markdown-memory action."""
    try:
        uid = _resolve_user_id(user_id)
    except ValueError as exc:
        return ToolResult(ok=False, data=None, error=str(exc))
    from services.memory.dir import get_memory_dir

    memory = get_memory_dir(uid)
    normalized_action = (action or "read").strip().lower()
    try:
        if normalized_action == "read":
            if query and query.strip():
                return _recall_memory(uid, query, limit=limit)
            return ToolResult(ok=True, data={"content": memory.read(path=path)})
        if normalized_action == "recall":
            return _recall_memory(uid, query, limit=limit)
        if normalized_action in {"search", "grep"}:
            path_text = (path or "").strip()
            search_path = path if path_text not in {"", "memory", "memory/", "MEMORY.md", "memory/MEMORY.md"} else None
            content_matches = memory.search_body(query, path=search_path)
            match_count = len([line for line in content_matches.splitlines() if line.strip()])
            data = {
                "content": content_matches,
                "match_count": match_count,
                "matched": match_count > 0,
                "scope": search_path or "all_memory",
            }
            if match_count == 0:
                data["message"] = "No matching memory lines found."
            if query.strip():
                data["query"] = query.strip()
            if search_path:
                data["path"] = search_path
            return ToolResult(ok=True, data=data)
        if normalized_action == "write":
            error = _sensitive_error(content)
            if error:
                return ToolResult(ok=False, data=None, error=error)
            # #2776: read the returned ok flag instead of hardcoding
            # ok=True. memory.write() currently only ever raises on
            # failure and returns {"ok": True, ...} on success, so this
            # is a no-op today -- but it matches the edit/delete pattern
            # below and stops a future fail-soft return from being
            # silently reported as success.
            write_result = memory.write(content, where=where, position=position)
            return ToolResult(ok=bool(write_result.get("ok", True)), data=write_result, error=write_result.get("error"))
        if normalized_action == "edit":
            error = _sensitive_error(replace)
            if error:
                return ToolResult(ok=False, data=None, error=error)
            result = memory.edit(path, find, replace)
            return ToolResult(ok=bool(result.get("ok")), data=result, error=result.get("error"))
        if normalized_action == "delete":
            if line_number > 0:
                result = memory.delete_line(path, line_number)
            elif section_title.strip():
                result = memory.delete_section(path, section_title)
            else:
                return ToolResult(ok=False, data=None, error="Provide line_number or section_title for delete.")
            return ToolResult(ok=bool(result.get("ok")), data=result, error=result.get("error"))
        if normalized_action == "list":
            return ToolResult(ok=True, data={"files": memory.list()})
        if normalized_action == "stats":
            return ToolResult(ok=True, data=memory.stats())
        if normalized_action == "audit":
            return ToolResult(ok=True, data={"entries": memory.audit(limit=limit)})
        return ToolResult(ok=False, data=None, error="Unknown memory action: %s" % action)
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def memory_store_handler(content: str, category: str = "fact", user_id: str = "") -> ToolResult:
    error = _sensitive_error(content)
    if error:
        return ToolResult(ok=False, data=None, error=error)
    try:
        uid = _resolve_user_id(user_id)
    except ValueError as exc:
        return ToolResult(ok=False, data=None, error=str(exc))
    from services.memory.dir import get_memory_dir

    try:
        memory = get_memory_dir(uid)
        return ToolResult(ok=True, data=memory.store_semantic_memory(content, category=category))
    except (OSError, ValueError) as exc:
        return ToolResult(ok=False, data=None, error=str(exc))


async def memory_recall_handler(query: str, user_id: str = "") -> ToolResult:
    return await memory_handler(action="recall", query=query, user_id=user_id)


async def memory_forget_handler(query: str, user_id: str = "") -> ToolResult:
    search = await memory_handler(action="search", query=query, user_id=user_id)
    if not search.ok:
        return search
    content = ""
    if isinstance(search.data, dict):
        content = str(search.data.get("content") or "")
    first = next((line for line in content.splitlines() if line.strip()), "")
    match = re.match(r"(.+?):(\d+):", first)
    if match is None:
        return ToolResult(ok=True, data="No matching memory lines found.")
    return await memory_handler(
        action="delete",
        path=match.group(1),
        line_number=int(match.group(2)),
        user_id=user_id,
    )


async def memory_list_handler(category: str = "", user_id: str = "") -> ToolResult:
    del category
    return await memory_handler(action="list", user_id=user_id)


__all__ = [
    "memory_forget_handler",
    "memory_handler",
    "memory_list_handler",
    "memory_recall_handler",
    "memory_store_handler",
]
