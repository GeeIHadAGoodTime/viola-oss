"""Structured per-task session memory for tool-call context."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)

_FILTERED_MEMORY_PAYLOAD = "[filtered sensitive memory payload]"


@dataclass
class SessionMemory:
    """Per-task agent memory scoped to a specific user.

    INT-14: `user_id` is required.  It disambiguates memory instances in
    logs and lets callers assert the memory they hold belongs to the user
    they're serving — a defense-in-depth complement to the existing
    function-scope lifetime (a fresh SessionMemory is created per agent
    run, so no cross-user leak path exists today; the explicit user_id
    makes that invariant auditable rather than structural-only).
    """

    SECTIONS = ["task_description", "current_state", "errors_and_corrections", "key_results"]
    MAX_TOTAL_TOKENS = 2000
    MAX_PER_SECTION_TOKENS = 500

    user_id: str
    session_id: str = "default"
    root: Path | None = None
    task_description: str = ""
    current_state: str = ""
    errors_and_corrections: list[str] = field(default_factory=list)
    key_results: list[str] = field(default_factory=list)
    file_path: Path | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.file_path = self._resolve_file_path()
        if self.file_path.exists():
            self._load_markdown()
        else:
            self.save()

    def assert_user(self, expected_user_id: str) -> None:
        """Raise if this memory belongs to a different user than expected.

        INT-14: defense against cross-user access if a future caller stores
        a SessionMemory in a shared structure and mis-routes it.
        """
        if self.user_id != expected_user_id:
            raise PermissionError("SessionMemory belongs to %r, not %r" % (self.user_id, expected_user_id))

    def update_after_tool(self, tool_name: str, tool_args: Any, tool_result: Any) -> None:
        args_text = self._safe_memory_text(tool_args, 160)
        result_text = self._safe_memory_text(tool_result, 320)
        self.current_state = "Ran %s(%s). Result: %s" % (
            tool_name or "unknown_tool",
            args_text or "no args",
            result_text or "no result",
        )

        failed, error_text = self._detect_error(tool_result)
        if failed:
            self.errors_and_corrections.append("%s failed: %s" % (tool_name or "tool", error_text))
        elif result_text:
            self.key_results.append("%s: %s" % (tool_name or "tool", result_text))

        self.errors_and_corrections = self._trim_entries(self.errors_and_corrections, self.MAX_PER_SECTION_TOKENS)
        self.key_results = self._trim_entries(self.key_results, self.MAX_PER_SECTION_TOKENS)
        self.save()
        logger.debug("Session memory updated after tool %s", tool_name)

    def set_task_description(self, text: str) -> None:
        self.task_description = self._safe_memory_text(text, 500)
        self.save()

    def to_context_string(self) -> str:
        task_text = self._fit_text(self.task_description, self.MAX_PER_SECTION_TOKENS)
        state_text = self._fit_text(self.current_state, self.MAX_PER_SECTION_TOKENS)
        error_entries = self._trim_entries(self.errors_and_corrections, self.MAX_PER_SECTION_TOKENS)
        result_entries = self._trim_entries(self.key_results, self.MAX_PER_SECTION_TOKENS)

        rendered = self._render(task_text, state_text, error_entries, result_entries)
        if self._estimate_tokens(rendered) <= self.MAX_TOTAL_TOKENS:
            return rendered

        result_entries = self._trim_entries(self.key_results[-3:], max(1, self.MAX_PER_SECTION_TOKENS // 2))
        rendered = self._render(task_text, state_text, error_entries, result_entries)
        if self._estimate_tokens(rendered) <= self.MAX_TOTAL_TOKENS:
            return rendered

        base_without_task = self._render("", state_text, error_entries, result_entries)
        remaining = max(0, self.MAX_TOTAL_TOKENS - self._estimate_tokens(base_without_task))
        task_text = self._fit_text(self.task_description, remaining)
        rendered = self._render(task_text, state_text, error_entries, result_entries)
        if self._estimate_tokens(rendered) <= self.MAX_TOTAL_TOKENS:
            return rendered

        base_priority = self._render("", state_text, error_entries, [])
        remaining = max(0, self.MAX_TOTAL_TOKENS - self._estimate_tokens(base_priority))
        result_entries = self._trim_entries(self.key_results[-3:], remaining)
        return self._render(task_text, state_text, error_entries, result_entries)

    def clear(self) -> None:
        self.task_description = ""
        self.current_state = ""
        self.errors_and_corrections.clear()
        self.key_results.clear()
        self.save()
        logger.debug("Session memory cleared")

    def save(self) -> None:
        if self.file_path is None:
            return
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.file_path.write_text(self._render_markdown(), encoding="utf-8", newline="\n")

    @property
    def allowed_file_path(self) -> str:
        return str(self.file_path or "")

    def _resolve_file_path(self) -> Path:
        from services.memory.dir import get_memory_dir

        safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", (self.session_id or "default").strip()).strip("_")
        if not safe_session:
            safe_session = "default"
        return get_memory_dir(self.user_id, root=self.root).root / "session_memory" / ("%s.md" % safe_session)

    def _render_markdown(self) -> str:
        return "\n".join(
            [
                "# Session Memory",
                "",
                "user_id: %s" % self.user_id,
                "session_id: %s" % self.session_id,
                "",
                "## task_description",
                self.task_description or "",
                "",
                "## current_state",
                self.current_state or "",
                "",
                "## errors_and_corrections",
                self._format_entries(self.errors_and_corrections),
                "",
                "## key_results",
                self._format_entries(self.key_results),
                "",
            ]
        )

    def _load_markdown(self) -> None:
        if self.file_path is None:
            return
        text = self.file_path.read_text(encoding="utf-8", errors="replace")
        sections: dict[str, list[str]] = {}
        current = ""
        for line in text.splitlines():
            match = re.match(r"^##\s+(.+?)\s*$", line)
            if match:
                current = match.group(1).strip()
                sections[current] = []
                continue
            if current:
                sections[current].append(line)
        self.task_description = self._section_text(sections, "task_description")
        self.current_state = self._section_text(sections, "current_state")
        self.errors_and_corrections = self._section_entries(sections, "errors_and_corrections")
        self.key_results = self._section_entries(sections, "key_results")

    def _section_text(self, sections: dict[str, list[str]], key: str) -> str:
        return "\n".join(line for line in sections.get(key, []) if line.strip()).strip()

    def _section_entries(self, sections: dict[str, list[str]], key: str) -> list[str]:
        entries: list[str] = []
        for line in sections.get(key, []):
            stripped = line.strip()
            if not stripped or stripped == "(none)":
                continue
            entries.append(stripped.removeprefix("-").strip())
        return entries

    def _render(
        self,
        task_text: str,
        state_text: str,
        error_entries: list[str],
        result_entries: list[str],
    ) -> str:
        return "\n".join(
            [
                "Session Memory",
                "[task_description]",
                task_text or "(empty)",
                "[current_state]",
                state_text or "(empty)",
                "[errors_and_corrections]",
                self._format_entries(error_entries),
                "[key_results]",
                self._format_entries(result_entries),
            ]
        )

    def _format_entries(self, entries: list[str]) -> str:
        return "\n".join("- %s" % entry for entry in entries) if entries else "(none)"

    def _detect_error(self, tool_result: Any) -> tuple[bool, str]:
        if isinstance(tool_result, BaseException):
            return True, self._safe_memory_text("%s: %s" % (type(tool_result).__name__, tool_result), 220)
        if isinstance(tool_result, dict):
            status = str(tool_result.get("status", "")).strip().lower()
            if (
                tool_result.get("success") is False
                or tool_result.get("ok") is False
                or tool_result.get("failed") is True
            ):
                return True, self._safe_memory_text(
                    tool_result.get("error") or tool_result.get("message") or tool_result, 220
                )
            if status in {"error", "failed", "failure"}:
                return True, self._safe_memory_text(
                    tool_result.get("error") or tool_result.get("message") or tool_result, 220
                )
            if tool_result.get("error"):
                return True, self._safe_memory_text(tool_result["error"], 220)
        return False, ""

    def _trim_entries(self, entries: list[str], token_budget: int) -> list[str]:
        kept: list[str] = []
        used = 0
        for entry in reversed(entries):
            shortened = self._fit_text(entry, min(120, token_budget))
            cost = self._estimate_tokens(shortened)
            if kept and used + cost > token_budget:
                break
            if not kept and cost > token_budget:
                kept.append(self._fit_text(shortened, token_budget))
                break
            kept.append(shortened)
            used += cost
        kept.reverse()
        return kept

    def _fit_text(self, text: Any, token_budget: int) -> str:
        normalized = self._compact_value(text, max(0, token_budget) * 4)
        if token_budget <= 0 or not normalized:
            return ""
        if len(normalized) <= max(4, token_budget * 4):
            return normalized
        max_chars = max(4, token_budget * 4)
        return normalized[: max_chars - 3].rstrip() + "..."

    def _compact_value(self, value: Any, max_chars: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
            except TypeError:
                text = str(value)
        text = " ".join(text.split())
        if len(text) <= max_chars:
            return text
        return text[: max_chars - 3].rstrip() + "..."

    def _safe_memory_text(self, value: Any, max_chars: int) -> str:
        compact = self._compact_value(value, max_chars)
        if not compact:
            return ""
        from services.memory.extract_memories import sanitize_memory_candidate_text

        sanitized = sanitize_memory_candidate_text(compact)
        if sanitized is None:
            return _FILTERED_MEMORY_PAYLOAD
        if len(sanitized) <= max_chars:
            return sanitized
        return sanitized[: max_chars - 3].rstrip() + "..."

    def _estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0
