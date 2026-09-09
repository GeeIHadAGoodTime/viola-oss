"""Bug reporting tool for the agent.

Provides a concrete tool the Tier 2 agent can call when a user asks to
file a bug report. The implementation reuses the existing feedback route
handler so reports land in the same ``feedback.json`` store as the REST API.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.api_response import ensure_envelope
from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

_AGENT_TASK_LOG_DIR = Path(__file__).resolve().parents[2] / "logs" / "agent_tasks"
_RECENT_AGENT_LOG_LIMIT = 3
_MAX_RECENT_AGENT_STEP_CHARS = 2000
_MAX_TOOL_PREVIEW_CHARS = 100


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in API-friendly ISO-8601 format."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _truncate_text(value: str, limit: int) -> str:
    """Trim a string to the requested size while keeping the result readable."""
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _preview_value(value: Any, limit: int = _MAX_TOOL_PREVIEW_CHARS) -> str:
    """Convert JSON-like tool fields into compact single-line previews."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return _truncate_text(" ".join(text.split()), limit)


def _recent_steps_size(entries: list[dict[str, Any]]) -> int:
    """Measure the serialized size of the recent-agent-steps payload."""
    return len(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))


def _iter_recent_agent_log_files() -> list[Path]:
    """Return the most recent agent task log files, newest first."""
    if not _AGENT_TASK_LOG_DIR.exists():
        return []
    try:
        files = [path for path in _AGENT_TASK_LOG_DIR.glob("*.jsonl") if path.is_file()]
        files.extend(path for path in _AGENT_TASK_LOG_DIR.glob("*.json") if path.is_file())
        return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)[:_RECENT_AGENT_LOG_LIMIT]
    except OSError:
        logger.exception("Failed to enumerate agent task logs from %s", _AGENT_TASK_LOG_DIR)
        return []


def _summarize_agent_task_log(path: Path) -> dict[str, Any] | None:
    """Read one agent task log and extract a compact, bug-report-friendly summary."""
    try:
        if path.suffix == ".jsonl":
            payload = None
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        candidate = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed JSONL line in agent task log %s", path)
                        continue
                    if isinstance(candidate, Mapping):
                        payload = candidate
            if payload is None:
                raise json.JSONDecodeError("no JSON object lines", "", 0)
        else:
            with path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        logger.exception("Failed to read agent task log %s", path)
        return None

    if not isinstance(payload, Mapping):
        logger.warning("Skipping malformed agent task log %s: top-level payload is not an object", path)
        return None

    raw_steps = payload.get("steps")
    step_summaries: list[dict[str, Any]] = []
    if isinstance(raw_steps, list):
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, Mapping):
                continue
            error = raw_step.get("error")
            step_summaries.append(
                {
                    "step": raw_step.get("step") if isinstance(raw_step.get("step"), int) else index,
                    "tool": _preview_value(raw_step.get("tool_name"), limit=80),
                    "input": _preview_value(raw_step.get("tool_input")),
                    "result": _preview_value(raw_step.get("tool_result_summary")),
                    "error": _preview_value(error, limit=120) if error else None,
                }
            )

    return {
        "task_id": _preview_value(payload.get("task_id") or path.stem, limit=80),
        "user_text": _preview_value(payload.get("user_text"), limit=160),
        "steps": step_summaries,
    }


def _fit_recent_agent_steps(
    task_summaries: list[dict[str, Any]],
    max_chars: int = _MAX_RECENT_AGENT_STEP_CHARS,
) -> list[dict[str, Any]]:
    """Keep the structured step context under the configured size budget."""
    recent_steps: list[dict[str, Any]] = []
    for task_summary in task_summaries:
        base_task = {
            "task_id": _truncate_text(str(task_summary.get("task_id") or ""), 80),
            "user_text": _truncate_text(str(task_summary.get("user_text") or ""), 160),
            "steps": [],
        }
        if _recent_steps_size(recent_steps + [base_task]) > max_chars:
            compact_base = {
                "task_id": _truncate_text(base_task["task_id"], 40),
                "user_text": _truncate_text(base_task["user_text"], 80),
                "steps": [],
            }
            if _recent_steps_size(recent_steps + [compact_base]) > max_chars:
                break
            base_task = compact_base

        for step in task_summary.get("steps", []):
            if not isinstance(step, Mapping):
                continue
            full_step = {
                "step": step.get("step"),
                "tool": _truncate_text(str(step.get("tool") or ""), 80),
                "input": _truncate_text(str(step.get("input") or ""), _MAX_TOOL_PREVIEW_CHARS),
                "result": _truncate_text(str(step.get("result") or ""), _MAX_TOOL_PREVIEW_CHARS),
                "error": (_truncate_text(str(step.get("error")), 120) if step.get("error") is not None else None),
            }
            candidate_task = {
                "task_id": base_task["task_id"],
                "user_text": base_task["user_text"],
                "steps": [*base_task["steps"], full_step],
            }
            if _recent_steps_size(recent_steps + [candidate_task]) <= max_chars:
                base_task = candidate_task
                continue

            compact_step = {
                "step": full_step["step"],
                "tool": _truncate_text(full_step["tool"], 40),
                "input": _truncate_text(full_step["input"], 40),
                "result": _truncate_text(full_step["result"], 40),
                "error": (_truncate_text(full_step["error"], 60) if full_step["error"] is not None else None),
            }
            compact_candidate = {
                "task_id": base_task["task_id"],
                "user_text": base_task["user_text"],
                "steps": [*base_task["steps"], compact_step],
            }
            if _recent_steps_size(recent_steps + [compact_candidate]) <= max_chars:
                base_task = compact_candidate
            break

        recent_steps.append(base_task)
        if _recent_steps_size(recent_steps) >= max_chars:
            break

    return recent_steps


def _build_bug_report_context() -> dict[str, Any]:
    """Assemble bug-report metadata plus recent agent tool activity."""
    context: dict[str, Any] = {
        "source": "tier2_agent_tool",
        "filed_at": _utc_now_iso(),
        "recent_agent_steps": [],
    }

    task_summaries: list[dict[str, Any]] = []
    for path in _iter_recent_agent_log_files():
        summary = _summarize_agent_task_log(path)
        if summary is not None:
            task_summaries.append(summary)

    context["recent_agent_steps"] = _fit_recent_agent_steps(task_summaries)
    return context


def _resolve_current_user_id() -> str:
    from core.user_context import get_current_user_id

    return get_current_user_id()


async def file_bug_report_handler(message: str) -> ToolResult:
    """File a local bug report and return the generated bug ID.

    Args:
        message: The user-provided bug description.
    """
    message = message.strip()
    if not message:
        return ToolResult(ok=False, error="message is required")

    context = _build_bug_report_context()
    try:
        user_id = _resolve_current_user_id()
    except LookupError:
        return ToolResult(ok=False, error="Bug reports require an authenticated user.")

    try:
        # Lazy import: ui.api.routes.feedback drags the desktop UI/onboarding
        # chain (ui.api.context -> ui.ux_manager -> ui.onboarding ->
        # audio_core.portaudio_guard), which the cloud image does not ship. A
        # module-level import here broke the entire core-tools MCP server on
        # cloud (ModuleNotFoundError: audio_core), stripping the agent of every
        # tool. Importing lazily lets core-tools load on cloud; the bug-report
        # tool itself degrades gracefully via the except below if the desktop
        # feedback route is unavailable.
        from ui.api.routes.feedback import BUG_REPORT_ROUTE, handle_feedback_submission

        response = await handle_feedback_submission(
            user_id=user_id,
            feedback_type="bug",
            message=message,
            context=context,
            route=BUG_REPORT_ROUTE,
        )
    except Exception as exc:
        logger.exception("file_bug_report_handler failed")
        return ToolResult(ok=False, error="Failed to file bug report: %s" % exc)

    try:
        envelope = ensure_envelope(response)
    except Exception as exc:
        logger.exception("Bug report handler returned invalid envelope")
        return ToolResult(ok=False, error="Bug report submission returned invalid response: %s" % exc)

    if not envelope["ok"]:
        error_obj = envelope.get("error") or {}
        error_message = "Bug report submission failed"
        if isinstance(error_obj, Mapping):
            error_message = str(error_obj.get("message") or error_obj.get("code") or error_message)
        return ToolResult(ok=False, error=error_message)

    data = envelope.get("data")
    if not isinstance(data, Mapping):
        return ToolResult(ok=False, error="Bug report submission returned malformed data")

    bug_id = str(data.get("id") or "").strip()
    if not bug_id:
        return ToolResult(ok=False, error="Bug report submission succeeded without an ID")

    return ToolResult(
        ok=True,
        data={
            "bug_id": bug_id,
            "received": bool(data.get("received", False)),
            "type": data.get("type", "bug"),
            "message": "Bug report filed successfully.",
        },
    )
