"""REST API endpoint for user feedback submission.

Provides POST /v1/feedback/submit for generic feedback and POST /v1/bug-report
for the bug-report-specific alias used by beta tooling. Feedback is appended to
a JSON file in the data directory.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, Field

from contracts.api_response import success_response
from core.logging_config import get_logger
from diagnostics.support_redaction import redact_support_payload
from fastapi import Depends
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import get_current_user_id, require_auth
from ui.api.routes.common import RouteToolbox
from ui.api.routes.error_handler import handle_route_error

log = get_logger(__name__)
FeedbackType = Literal["bug", "feature", "general"]
_FEEDBACK_FILE_LOCK = threading.Lock()
_BUG_TICKET_FILE_LOCK = threading.Lock()

FEEDBACK_SUBMIT_ROUTE = "/v1/feedback/submit"
BUG_REPORT_ROUTE = "/v1/bug-report"
_RECENT_TRACE_LIMIT = 3
_BUG_REPORT_MESSAGE_LIMIT = 1200

# Failure modes that may surface while best-effort-summarizing a recent task
# trace to decorate a bug report. Reading a trace decrypts (Fernet -> InvalidToken
# on a rotated/missing/corrupt key), decompresses, and parses JSON, then walks the
# TraceReader index — any of which can fail on a partial/legacy/foreign trace.
# A failure here must degrade that one trace to a read_error marker, never 500 the
# user's submission. InvalidToken is the gap the original tuple missed (it is NOT
# a subclass of OSError/ValueError/etc.), which sank an otherwise valid report.
_TRACE_SUMMARY_ERRORS = (
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
    AttributeError,
    KeyError,
    InvalidToken,
)


class FeedbackSubmitRequest(BaseModel):
    """Request body for submitting feedback."""

    type: FeedbackType = Field(..., description="Type of feedback")
    message: str = Field(..., min_length=1, max_length=5000, description="Feedback message")
    context: dict[str, Any] | None = Field(None, description="Optional context metadata")


class BugReportSubmitRequest(BaseModel):
    """Request body for the bug-report alias endpoint."""

    message: str = Field(..., min_length=1, max_length=5000, description="Bug report message")
    context: dict[str, Any] | None = Field(None, description="Optional context metadata")
    type: Literal["bug"] = Field("bug", description="Fixed type for bug report submissions")


def _get_feedback_file() -> Path:
    """Return the path to the feedback JSON file in the data directory."""
    from config.settings import settings

    data_dir = Path(settings.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "feedback.json"


def _get_bug_ticket_file() -> Path:
    """Return the bug-ticket DB path next to the current feedback store."""
    return _get_feedback_file().parent / "bug_tickets.db"


def _load_feedback(path: Path) -> list[dict[str, Any]]:
    """Load existing feedback entries from disk."""
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read feedback file, starting fresh: %s", exc)
    return []


def _save_feedback(path: Path, entries: list[dict[str, Any]]) -> None:
    """Atomically write feedback entries to disk."""
    import tempfile

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix="feedback_")
    try:
        with open(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2, ensure_ascii=False)
        # Atomic rename (works on same filesystem)
        tmp = Path(tmp_path)
        tmp.replace(path)
    except Exception:
        # Clean up temp file on failure
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _require_user_id(user_id: str | None) -> str:
    normalized = str(user_id or "").strip()
    if not normalized or normalized.lower() == "default":
        raise ValueError("Feedback submissions require a concrete non-default user_id")
    return normalized


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _coerce_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    if isinstance(context, dict):
        return dict(context)
    return {"submitted_context": context}


def _screen_capture_metadata(value: Any) -> dict[str, Any] | None:
    """Persist only screenshot metadata; raw pixels cannot be PII-redacted here."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return {
            "requested": bool(value),
            "provided": False,
            "storage": "metadata_only",
        }

    return {
        "requested": bool(value.get("requested", value.get("include", value.get("provided", True)))),
        "provided": bool(value.get("provided", value.get("included", False))),
        "storage": "metadata_only",
        "scope": _truncate(value.get("scope") or value.get("source") or "current_window", 80),
        "mime_type": _truncate(value.get("mime_type") or value.get("content_type") or "", 80),
        "byte_count": _positive_int(value.get("byte_count") or value.get("bytes")),
        "sha256": _truncate(value.get("sha256") or value.get("hash") or "", 96),
    }


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_bug_context(context: dict[str, Any] | None) -> dict[str, Any]:
    context_data = _coerce_context(context)
    capture_value = None
    for key in ("screenshot", "screen_capture", "screen_capture_metadata"):
        if key in context_data:
            capture_value = context_data.pop(key)
            break
    capture_metadata = _screen_capture_metadata(capture_value)
    if capture_metadata is not None:
        context_data["screen_capture_metadata"] = capture_metadata
    return context_data


def _first_recent_trace_id(trace_context: Mapping[str, Any]) -> str:
    recent_trace_ids = trace_context.get("recent_trace_ids")
    if not isinstance(recent_trace_ids, list):
        return ""
    for trace_id in recent_trace_ids:
        value = _truncate(trace_id, 120)
        if value:
            return value
    return ""


def _derive_recent_action(context: Mapping[str, Any], trace_context: Mapping[str, Any]) -> dict[str, Any]:
    supplied = context.get("recent_action")
    if isinstance(supplied, Mapping):
        return dict(supplied)

    agent_task = context.get("agent_task")
    if isinstance(agent_task, Mapping) and agent_task.get("active"):
        return {
            "kind": "agent_task",
            "phase": _truncate(agent_task.get("phase"), 60),
            "status": _truncate(agent_task.get("status"), 160),
            "description": _truncate(agent_task.get("description"), 160),
        }

    recent_traces = trace_context.get("recent_traces")
    if isinstance(recent_traces, list) and recent_traces:
        last_trace = recent_traces[0]
        if isinstance(last_trace, Mapping):
            last_step = last_trace.get("last_step")
            if isinstance(last_step, Mapping):
                return {
                    "kind": "trace_step",
                    "trace_id": _truncate(last_trace.get("task_id"), 120),
                    "step_kind": _truncate(last_step.get("step_kind"), 80),
                    "tool_name": _truncate(last_step.get("tool_name"), 120),
                    "ok": bool(last_step.get("ok")),
                    "page_url": _truncate(last_step.get("page_url"), 500),
                }

    if context.get("display_mode") or context.get("stage_mode"):
        return {
            "kind": "ui_stage",
            "display_mode": _truncate(context.get("display_mode"), 80),
            "stage_mode": _truncate(context.get("stage_mode"), 80),
        }

    return {
        "kind": "ui_surface",
        "surface": _truncate(context.get("surface"), 80),
        "entrypoint": _truncate(context.get("ui_entrypoint"), 80),
    }


def _trace_task_id_from_path(path: Path) -> str:
    name = path.name
    for suffix in (".trace.jsonl.zst.enc", ".trace.jsonl"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def _iter_recent_trace_files(user_id: str) -> list[Path]:
    try:
        from intent.task_trace import (
            DEFAULT_TASK_TRACE_DIR,
            DEFAULT_TASK_TRACE_V2_DIR,
            _safe_path_segment,
            _user_id_hash,
        )
    except (ImportError, AttributeError):
        log.debug("Task trace paths unavailable for bug report context", exc_info=True)
        return []

    candidates: list[Path] = []
    try:
        user_hash = _user_id_hash(user_id)
        v2_user_dir = DEFAULT_TASK_TRACE_V2_DIR / user_hash
        if v2_user_dir.exists():
            candidates.extend(path for path in v2_user_dir.glob("*/*.trace.jsonl.zst.enc") if path.is_file())
    except (OSError, RuntimeError, TypeError, ValueError):
        log.debug("Could not enumerate trace-v2 files for bug report context", exc_info=True)

    try:
        user_segment = _safe_path_segment(user_id)
        v1_user_dir = DEFAULT_TASK_TRACE_DIR / user_segment
        if v1_user_dir.exists():
            candidates.extend(path for path in v1_user_dir.glob("*/*.trace.jsonl") if path.is_file())
    except (OSError, RuntimeError, TypeError, ValueError):
        log.debug(
            "Could not enumerate legacy trace files for bug report context",
            exc_info=True,
        )

    try:
        return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)[:_RECENT_TRACE_LIMIT]
    except OSError:
        log.debug("Could not sort recent trace files for bug report context", exc_info=True)
        return candidates[:_RECENT_TRACE_LIMIT]


def _iter_recent_disabled_trace_markers(user_id: str) -> list[dict[str, Any]]:
    """Return the plaintext markers for traces that were deliberately skipped.

    A bug report with no trace context used to be indistinguishable from a
    user who simply had not run anything. When trace writing is disabled
    mid-run the writer drops a metadata-only marker beside the trace it never
    wrote, so the report can say WHY it carries no trace instead of silently
    shipping an empty context (#4793).
    """
    try:
        from intent.task_trace import (
            DEFAULT_TASK_TRACE_V2_DIR,
            TRACE_DISABLED_MARKER_SUFFIX,
            _user_id_hash,
        )
    except (ImportError, AttributeError):
        log.debug("Task trace paths unavailable for disabled-trace markers", exc_info=True)
        return []

    try:
        user_dir = DEFAULT_TASK_TRACE_V2_DIR / _user_id_hash(user_id)
        if not user_dir.exists():
            return []
        markers = sorted(
            (path for path in user_dir.glob("*/*%s" % TRACE_DISABLED_MARKER_SUFFIX) if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:_RECENT_TRACE_LIMIT]
    except (OSError, RuntimeError, TypeError, ValueError):
        log.debug("Could not enumerate disabled-trace markers for bug report context", exc_info=True)
        return []

    records: list[dict[str, Any]] = []
    for path in markers:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            log.debug("Could not read disabled-trace marker %s", path, exc_info=True)
            continue
        if not isinstance(record, dict):
            continue
        records.append(
            {
                "task_id": _truncate(record.get("task_id"), 120),
                "reason": _truncate(record.get("reason"), 400),
                "ts": _truncate(record.get("ts"), 64),
            }
        )
    return records


def _summarize_recent_traces(user_id: str) -> dict[str, Any]:
    trace_files = _iter_recent_trace_files(user_id)
    disabled_traces = _iter_recent_disabled_trace_markers(user_id)
    if not trace_files:
        return {
            "recent_trace_ids": [],
            "recent_traces": [],
            "disabled_traces": disabled_traces,
        }

    recent_traces: list[dict[str, Any]] = []
    for path in trace_files:
        task_id = _trace_task_id_from_path(path)
        summary: dict[str, Any] = {"task_id": task_id, "path_name": path.name}
        try:
            from intent.task_trace_reader import TraceReader
            from services.persistence.trace_keys import KeyProvider

            key_provider = KeyProvider(user_id)
            if path.name.endswith(".trace.jsonl.zst.enc") and not key_provider.trace_key_path.exists():
                summary["read_error"] = "trace_key_unavailable"
                recent_traces.append(summary)
                continue

            index = TraceReader(task_id, user_id, key_provider).index()
            summary.update(
                {
                    "schema_version": index.schema_version,
                    "started_at": index.started_at,
                    "completed_at": index.completed_at,
                    "outcome": index.outcome,
                    "total_steps": index.total_steps,
                    "event_counts": index.event_counts,
                }
            )
            if index.step_index:
                last_step = index.step_index[-1]
                summary["last_step"] = {
                    "step": last_step.step,
                    "step_kind": last_step.step_kind,
                    "tool_name": last_step.tool_name,
                    "ok": last_step.ok,
                    "page_url": last_step.page_url,
                }
        except _TRACE_SUMMARY_ERRORS as exc:
            # Trace summarization is best-effort diagnostic decoration on a bug
            # report — any single-trace read failure degrades to a read_error
            # marker and never propagates to 500 the user's submission. The
            # previous tuple omitted cryptography.fernet.InvalidToken (raised when
            # a trace's encryption key is rotated/missing/corrupt), which is NOT a
            # subclass of OSError/ValueError/etc., so it escaped and sank the whole
            # submission — InvalidToken is now in _TRACE_SUMMARY_ERRORS.
            log.debug("Could not summarize trace %s for bug report context: %s", task_id, exc)
            summary["read_error"] = exc.__class__.__name__
        recent_traces.append(summary)

    return {
        "recent_trace_ids": [trace.get("task_id") for trace in recent_traces if trace.get("task_id")],
        "recent_traces": recent_traces,
        "disabled_traces": disabled_traces,
    }


def _execution_stage_from_context(context: Mapping[str, Any], trace_context: Mapping[str, Any]) -> str:
    for key in ("execution_stage", "stage"):
        value = _truncate(context.get(key), 80)
        if value:
            return value

    agent_task = context.get("agent_task")
    if isinstance(agent_task, Mapping) and agent_task.get("active"):
        phase = _truncate(agent_task.get("phase"), 60)
        if phase:
            return "agent_task:%s" % phase
        return "agent_task"

    recent_traces = trace_context.get("recent_traces")
    if isinstance(recent_traces, list) and recent_traces:
        last_trace = recent_traces[0]
        if isinstance(last_trace, Mapping):
            last_step = last_trace.get("last_step")
            if isinstance(last_step, Mapping):
                step_kind = _truncate(last_step.get("step_kind"), 60)
                if step_kind:
                    return "trace:%s" % step_kind
    return "user_report"


def _severity_from_context(context: Mapping[str, Any]) -> str:
    value = _truncate(context.get("severity"), 20).lower()
    return value if value in {"low", "medium", "high", "critical"} else "medium"


def _build_bug_report_diagnosis(*, feedback_id: str, route: str, severity: str) -> Any:
    from services.agent.self_diagnosis import DiagnosisResult

    return DiagnosisResult(
        root_cause="User-submitted bug report",
        category="user_report",
        user_explanation="The user reported a bug from the app.",
        developer_detail="User-initiated bug report accepted from %s with feedback_id=%s" % (route, feedback_id),
        severity=severity,
        is_transient=False,
        suggested_retry=False,
        diagnosis_succeeded=True,
    )


async def _file_bug_ticket_from_submission(
    *,
    owner_user_id: str,
    feedback_id: str,
    message: str,
    context: dict[str, Any] | None,
    route: str,
) -> Any:
    import asyncio

    from services.agent.bug_tickets import BugTicketStore
    from services.agent.diagnostic_context import DiagnosticContext

    # The user-authored body is stored VERBATIM (no redaction): a bug report is a
    # deliberate message the user typed to our own support channel, so redacting it
    # destroys exactly what they chose to send us. Only bound its length. The marker
    # DiagnosticContext.user_authored=True (below) tells the shared bug-ticket store
    # to keep this body verbatim while still redacting the auto-captured context.
    verbatim_message = _truncate(message, _BUG_REPORT_MESSAGE_LIMIT)
    context_data = _normalize_bug_context(context)
    # Trace enrichment is optional diagnostic decoration — never let it block the
    # user's report from being filed. If summarization fails wholesale (corrupt
    # key, unexpected store error), degrade to empty trace context rather than
    # raising and 500-ing the submission. The actual report data (message +
    # submitted context) is what must always survive.
    try:
        trace_context = await asyncio.to_thread(_summarize_recent_traces, owner_user_id)
    except _TRACE_SUMMARY_ERRORS:
        # Defense-in-depth: _summarize_recent_traces already catches per-trace
        # failures, but if the enrichment step fails wholesale (e.g. enumerating
        # the trace dir), still file the user's report with empty trace context
        # rather than 500-ing the submission.
        log.warning("Trace summarization failed for bug report; filing without trace context", exc_info=True)
        trace_context = {"recent_trace_ids": [], "recent_traces": [], "summary_error": True}
    if not isinstance(trace_context, dict):
        trace_context = {"recent_trace_ids": [], "recent_traces": []}
    current_trace_id = _truncate(context_data.get("current_trace_id"), 120) or _first_recent_trace_id(trace_context)
    if current_trace_id:
        context_data["current_trace_id"] = current_trace_id
    context_data["recent_action"] = _derive_recent_action(context_data, trace_context)
    context_data.update(
        {
            "feedback_id": feedback_id,
            "support_route": route,
            "source": context_data.get("source") or "user_bug_report_ui",
            "submitted_at": _utc_now_iso(),
            "trace_context": trace_context,
        }
    )
    redacted_context = redact_support_payload(context_data)
    if not isinstance(redacted_context, dict):
        redacted_context = {"context": redacted_context}

    execution_stage = _execution_stage_from_context(redacted_context, trace_context)
    diagnostic_context = DiagnosticContext(
        timestamp=_utc_now_iso(),
        user_request=verbatim_message[:500],
        execution_stage=execution_stage,
        user_id=owner_user_id,
        error_type="UserBugReport",
        error_message=verbatim_message[:1000],
        # This body is user-authored -> the shared ticket store keeps it verbatim.
        user_authored=True,
        system_state={
            "overall_status": "user_reported",
            "feedback_id": feedback_id,
            "route": route,
            "support_context": redacted_context,
        },
        recent_failures=["User submitted bug report via %s" % _truncate(redacted_context.get("surface") or route, 80)],
    )
    diagnosis = _build_bug_report_diagnosis(
        feedback_id=feedback_id,
        route=route,
        severity=_severity_from_context(redacted_context),
    )

    def _sync_file_ticket() -> Any:
        with _BUG_TICKET_FILE_LOCK:
            return BugTicketStore(db_path=_get_bug_ticket_file()).file_ticket(
                diagnostic_context,
                diagnosis,
                user_id=owner_user_id,
            )

    return await asyncio.to_thread(_sync_file_ticket)


def export_feedback_for_user(user_id: str) -> list[dict[str, Any]]:
    """Export all feedback entries owned by the given user."""
    owner_user_id = _require_user_id(user_id)
    path = _get_feedback_file()
    with _FEEDBACK_FILE_LOCK:
        return [dict(entry) for entry in _load_feedback(path) if entry.get("user_id") == owner_user_id]


def delete_feedback_for_user(user_id: str) -> int:
    """Delete all feedback entries owned by the given user."""
    owner_user_id = _require_user_id(user_id)
    path = _get_feedback_file()
    with _FEEDBACK_FILE_LOCK:
        entries = _load_feedback(path)
        kept = [entry for entry in entries if entry.get("user_id") != owner_user_id]
        removed = len(entries) - len(kept)
        if removed:
            _save_feedback(path, kept)
        return removed


async def handle_feedback_submission(
    *,
    user_id: str,
    feedback_type: FeedbackType,
    message: str,
    context: dict[str, Any] | None,
    route: str,
    toolbox: RouteToolbox | None = None,
) -> Any:
    """Persist a feedback entry and optionally wrap the call with route metrics."""

    async def _inner():
        import asyncio
        import time

        try:
            owner_user_id = _require_user_id(user_id)
            feedback_id = f"fb-{uuid.uuid4().hex[:12]}"
            bug_ticket = None
            if feedback_type == "bug":
                bug_ticket = await _file_bug_ticket_from_submission(
                    owner_user_id=owner_user_id,
                    feedback_id=feedback_id,
                    message=message,
                    context=context,
                    route=route,
                )

            entry: dict[str, Any] = {
                "id": feedback_id,
                "user_id": owner_user_id,
                "type": feedback_type,
                # Verbatim: the user typed this body as a deliberate message to our
                # own support channel. Redacting it would destroy exactly what they
                # chose to send. The auto-captured context below stays redacted.
                "message": message,
                "context": (redact_support_payload(context) if context is not None else None),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

            def _sync_write() -> None:
                path = _get_feedback_file()
                with _FEEDBACK_FILE_LOCK:
                    entries = _load_feedback(path)
                    entries.append(entry)
                    _save_feedback(path, entries)

            await asyncio.to_thread(_sync_write)

            log.info(
                "Feedback submitted: id=%s type=%s route=%s",
                feedback_id,
                feedback_type,
                route,
            )
            if feedback_type == "bug":
                # Real-time founder notification: the gated GlitchTip mirror
                # (below) is OFF by default, so it notifies nobody. Route the
                # report to whichever founder-reaching sink is live in this
                # process -- a direct operator page on the cloud, or a cloud
                # upload from a desktop install (where the report otherwise never
                # leaves the user's machine). Runs in a thread because the desktop
                # branch makes a short outbound HTTP call. Best-effort and
                # fail-open: notify_bug_report never raises, so a notification
                # failure can never break the user's submission (still 200).
                bug_context = context if isinstance(context, dict) else {}
                try:
                    from diagnostics.bug_report_alert import notify_bug_report

                    # Anonymized diagnostic minimum attaches to the user-initiated
                    # report on the opt-out baseline (gated by the master flag +
                    # opt-out via diagnostics.diagnostic_consent; returns None when
                    # not allowed). It is allowlist-only and identity-free by
                    # construction, so it is safe to ride the founder notification.
                    diagnostic_min = None
                    try:
                        from diagnostics.diagnostic_dispatch import dispatch_bug_report_minimum

                        diagnostic_min = dispatch_bug_report_minimum(
                            app_state=bug_context if isinstance(bug_context, dict) else None,
                            surface=bug_context.get("surface"),
                        )
                    except Exception:  # noqa: BLE001, RUF100 - diagnostics attach is best-effort
                        log.debug("Bug-report diagnostic minimum build failed", exc_info=True)

                    def _notify_founder() -> None:
                        notify_bug_report(
                            message=message,
                            route=route,
                            origin="app",
                            feedback_id=feedback_id,
                            user_id=owner_user_id,
                            surface=bug_context.get("surface"),
                            severity=bug_context.get("severity"),
                            # User-consented actionable fields (app version, OS,
                            # contact, prompted repro) the client collected in a
                            # visible form. notify_bug_report allowlists these,
                            # forwards them to the cloud upload / founder page,
                            # and never fabricates any missing field -- so a
                            # report reaches the founder followupable.
                            report_context=bug_context,
                            trace_refs={
                                "current_trace_id": bug_context.get("current_trace_id"),
                                "bug_ticket_id": (getattr(bug_ticket, "id", None) if bug_ticket is not None else None),
                            },
                            extra_context=({"diagnostic_minimum": diagnostic_min} if diagnostic_min else None),
                        )

                    await asyncio.to_thread(_notify_founder)
                except Exception:
                    log.exception("Bug-report founder notification failed for feedback_id=%s", feedback_id)
                try:
                    from core.sentry_integration import capture_user_bug_report

                    capture_user_bug_report(
                        user_id=owner_user_id,
                        feedback_id=feedback_id,
                        message=message,
                        context=context,
                        route=route,
                        bug_ticket_id=(getattr(bug_ticket, "id", None) if bug_ticket is not None else None),
                    )
                except Exception:
                    log.exception("Sentry bug-report mirror failed for feedback_id=%s", feedback_id)
            response_data: dict[str, Any] = {
                "id": feedback_id,
                "received": True,
                "type": feedback_type,
            }
            if bug_ticket is not None:
                response_data["bug_ticket_id"] = bug_ticket.id
            return success_response(response_data)
        except Exception as exc:
            log.debug("Submit feedback failed on %s: %s", route, exc)
            return handle_route_error(exc, "submit_feedback")

    if toolbox is None:
        return await _inner()
    return await toolbox.record_and_call(_inner, route=route, method="POST")


def register_feedback_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register feedback submission REST endpoints."""
    router = context.router

    @router.post(FEEDBACK_SUBMIT_ROUTE, dependencies=[Depends(require_auth)])
    async def submit_feedback(body: FeedbackSubmitRequest, user_id: str = Depends(get_current_user_id)):
        """Submit user feedback (bug report, feature request, or general)."""
        return await handle_feedback_submission(
            user_id=user_id,
            feedback_type=body.type,
            message=body.message,
            context=body.context,
            route=FEEDBACK_SUBMIT_ROUTE,
            toolbox=toolbox,
        )

    @router.post(BUG_REPORT_ROUTE, dependencies=[Depends(require_auth)])
    async def submit_bug_report(body: BugReportSubmitRequest, user_id: str = Depends(get_current_user_id)):
        """Submit a bug report using the beta-friendly bug-only schema."""
        return await handle_feedback_submission(
            user_id=user_id,
            feedback_type="bug",
            message=body.message,
            context=body.context,
            route=BUG_REPORT_ROUTE,
            toolbox=toolbox,
        )

    log.info("Feedback routes registered")


__all__ = [
    "BUG_REPORT_ROUTE",
    "FEEDBACK_SUBMIT_ROUTE",
    "BugReportSubmitRequest",
    "FeedbackSubmitRequest",
    "delete_feedback_for_user",
    "export_feedback_for_user",
    "handle_feedback_submission",
    "register_feedback_routes",
]
