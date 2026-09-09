"""Request-scoped context for user-authored routine execution."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TrackedCapabilityRun:
    """A run_start audit event waiting for a task-level completion event."""

    user_id: str
    root: Path | None
    capability_id: str
    started_at: str
    plan_steps_count: int


_current_surface: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_capabilities_current_surface",
    default=None,
)
_tracked_runs: contextvars.ContextVar[tuple[TrackedCapabilityRun, ...]] = contextvars.ContextVar(
    "user_capabilities_tracked_runs",
    default=(),
)


def normalize_surface(value: object) -> str | None:
    """Normalize channel/surface names used by the agent runtime."""
    surface = str(value or "").strip().lower()
    if not surface:
        return None
    if surface == "http":
        return "web"
    return surface


def get_current_surface() -> str | None:
    """Return the current request surface without importing the agent executor."""
    explicit = _current_surface.get()
    if explicit:
        return explicit
    try:
        from messaging.channel import get_request_channel

        channel = get_request_channel()
    except Exception as exc:
        logger.debug("Current capability surface lookup skipped: %s", exc)
        return None
    return normalize_surface(getattr(channel, "channel_type", None))


def set_current_surface(surface: object) -> contextvars.Token[str | None]:
    """Publish the current tool-execution surface for this async context."""
    return _current_surface.set(normalize_surface(surface))


def reset_current_surface(token: contextvars.Token[str | None]) -> None:
    """Restore the previous surface context."""
    _current_surface.reset(token)


def remember_run_for_completion(
    *,
    user_id: str,
    root: Path | None,
    capability_id: str,
    started_at: str,
    plan_steps_count: int,
) -> None:
    """Track a successful run_start so the agent can emit run_complete later."""
    run = TrackedCapabilityRun(
        user_id=user_id,
        root=root,
        capability_id=capability_id,
        started_at=started_at,
        plan_steps_count=plan_steps_count,
    )
    _tracked_runs.set((*_tracked_runs.get(), run))


def _step_tool(step: Mapping[str, Any]) -> str:
    return str(step.get("tool") or step.get("tool_name") or "").strip()


def _step_args(step: Mapping[str, Any]) -> Mapping[str, Any]:
    args = step.get("args")
    if args is None:
        args = step.get("tool_input")
    return args if isinstance(args, Mapping) else {}


def _matches_run_step(step: Mapping[str, Any], capability_id: str) -> bool:
    if _step_tool(step) != "run_user_capability":
        return False
    args = _step_args(step)
    requested_id = str(args.get("id") or args.get("capability_id") or "").strip()
    return requested_id == capability_id


def _executed_steps_after_run(
    steps: list[Mapping[str, Any]],
    capability_id: str,
) -> list[dict[str, Any]]:
    start_index = -1
    for index, step in enumerate(steps):
        if _matches_run_step(step, capability_id):
            start_index = index
            break

    candidates = steps[start_index + 1 :] if start_index >= 0 else steps
    executed: list[dict[str, Any]] = []
    for step in candidates:
        tool = _step_tool(step)
        if not tool or tool in {"run_user_capability", "final_answer"}:
            continue
        executed.append(
            {
                "step": step.get("step"),
                "tool": tool,
                "ok": not bool(step.get("error")),
                "error": step.get("error"),
            }
        )
    return executed


def audit_tracked_run_completions(
    *,
    status: str,
    steps: Iterable[Mapping[str, Any]],
    task_id: str | None = None,
) -> None:
    """Emit run_complete audit entries for tracked routine starts."""
    runs = _tracked_runs.get()
    if not runs:
        return
    _tracked_runs.set(())

    step_list = list(steps)
    for run in runs:
        try:
            from .storage import CapabilityStore

            store = CapabilityStore(run.user_id, root=run.root, create=True)
            store.audit_run_complete(
                run.capability_id,
                started_at=run.started_at,
                plan_steps_count=run.plan_steps_count,
                status=status,
                executed_steps=_executed_steps_after_run(step_list, run.capability_id),
                task_id=task_id,
            )
        except Exception as exc:
            logger.debug("Capability run_complete audit skipped for %s: %s", run.capability_id, exc)
