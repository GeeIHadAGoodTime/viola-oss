"""In-process registry for long-running background agent tasks."""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from core.constants import TIMEOUT_HOUR
from core.logging_config import get_logger

logger = get_logger(__name__)

# S5-05/S5-10 parity: ``idle`` mirrors Claude Code's
# ``InProcessTeammateTaskState.isIdle`` — a subagent that has finished its
# current turn but is still alive, waiting on ``send_message`` input.
# Distinguishing this from ``running`` lets the parent decide whether to
# deliver another message synchronously vs. wake the child. ``killed`` mirrors
# Claude Code's user-cancellation status and is separate from ``failed``
# (crash/API error).
AgentStatus = Literal["running", "idle", "completed", "failed", "killed"]
AgentMode = Literal["fresh", "fork"]
AgentRunner = Callable[[asyncio.Event], Awaitable[str]]

_AGENT_ID_HEX_LENGTH = 8
_MAX_ACTIVE_AGENTS_PER_USER = 10
_MAX_ENTRIES_PER_USER = 50
_COMPLETED_RETENTION_SECONDS = 3_600
# ``idle`` is NOT terminal — an idle agent is alive and awaiting send_message.
# ``killed`` IS terminal (user-cancellation) and is distinct from ``cancelled``
# for parity with Claude Code's notification statuses.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "killed"})
_ACTIVE_STATUSES = frozenset({"running", "idle"})
_CANCEL_FORCE_GRACE_SECONDS = 1.0
_BOOT_RECOVERY_MAX_AGE_SECONDS = int(24 * TIMEOUT_HOUR)
_BOOT_RECOVERY_SCAN_LIMIT = 250
_CORRUPT_CHECKPOINT_WARNING_LIMIT = 5


def _freeze_metadata_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_metadata_value(item) for key, item in value.items()})


def _freeze_metadata_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_metadata_mapping(value)
    if isinstance(value, list | tuple):
        return tuple(_freeze_metadata_value(item) for item in value)
    return copy.deepcopy(value)


class AgentRegistryError(RuntimeError):
    """Base exception for background agent registry failures."""


class AgentLimitExceededError(AgentRegistryError):
    """Raised when a user already has the maximum number of active agents."""


@dataclass(frozen=True, slots=True)
class AgentTask:
    """Read-only view of a registered background agent task."""

    agent_id: str
    user_id: str
    task: str
    reason: str
    stream_id: str
    status: AgentStatus
    started_at: datetime
    mode: AgentMode = "fresh"
    session_id: str | None = None
    parent_session_id: str | None = None
    parent_agent_id: str | None = None
    source_frame_uuid: str | None = None
    fork_metadata: Mapping[str, Any] = field(default_factory=dict)
    subagent_type: str | None = None
    name: str | None = None
    completed_at: datetime | None = None
    result: str | None = None
    error_detail: str | None = None


@dataclass(slots=True)
class _AgentRecord:
    agent_id: str
    user_id: str
    task: str
    reason: str
    stream_id: str
    status: AgentStatus
    started_at: datetime
    cancel_event: asyncio.Event
    mode: AgentMode = "fresh"
    session_id: str | None = None
    parent_session_id: str | None = None
    parent_agent_id: str | None = None
    source_frame_uuid: str | None = None
    fork_metadata: dict[str, Any] = field(default_factory=dict)
    subagent_type: str | None = None
    name: str | None = None
    runner_task: asyncio.Task[None] | None = None
    completed_at: datetime | None = None
    result: str | None = None
    error_detail: str | None = None
    # S5-05 parity: track the foreground-task background-signal so callers can
    # background the agent at any point (mid-run) or auto-background after a
    # timeout, matching ``registerAgentForeground`` in Claude Code.
    is_foreground: bool = False
    background_signal: asyncio.Event | None = None
    background_auto_timer: asyncio.TimerHandle | None = None

    def snapshot(self) -> AgentTask:
        """Return an immutable view for callers."""
        return AgentTask(
            agent_id=self.agent_id,
            user_id=self.user_id,
            task=self.task,
            reason=self.reason,
            stream_id=self.stream_id,
            status=self.status,
            started_at=self.started_at,
            mode=self.mode,
            session_id=self.session_id,
            parent_session_id=self.parent_session_id,
            parent_agent_id=self.parent_agent_id,
            source_frame_uuid=self.source_frame_uuid,
            fork_metadata=_freeze_metadata_mapping(self.fork_metadata),
            subagent_type=self.subagent_type,
            name=self.name,
            completed_at=self.completed_at,
            result=self.result,
            error_detail=self.error_detail,
        )


class AgentRegistry:
    """Per-user registry for in-process background agent tasks."""

    def __init__(
        self,
        *,
        active_limit: int = _MAX_ACTIVE_AGENTS_PER_USER,
        retention_seconds: int = _COMPLETED_RETENTION_SECONDS,
        max_entries_per_user: int = _MAX_ENTRIES_PER_USER,
        clock: Callable[[], datetime] | None = None,
        recover_interrupted_tasks: bool = True,
    ) -> None:
        self._agents_by_user: dict[str, dict[str, _AgentRecord]] = {}
        self._lock = asyncio.Lock()
        self._active_limit = active_limit
        self._retention = timedelta(seconds=retention_seconds)
        self._max_entries_per_user = max_entries_per_user
        self._clock = clock or (lambda: datetime.now(UTC))
        if recover_interrupted_tasks:
            self._recover_interrupted_tasks_on_boot()

    async def start(
        self,
        user_id: str,
        task: str,
        reason: str,
        stream_id: str,
        runner: AgentRunner,
        *,
        agent_id: str | None = None,
        mode: AgentMode = "fresh",
        session_id: str | None = None,
        parent_session_id: str | None = None,
        parent_agent_id: str | None = None,
        source_frame_uuid: str | None = None,
        fork_metadata: Mapping[str, Any] | None = None,
        subagent_type: str | None = None,
        name: str | None = None,
    ) -> str:
        """Start a background agent runner and return its short agent id."""
        self._validate_user_id(user_id)
        normalized_agent_id = str(agent_id or "").strip() or self._new_agent_id()
        cancel_event = asyncio.Event()
        record = _AgentRecord(
            agent_id=normalized_agent_id,
            user_id=user_id,
            task=task,
            reason=reason,
            stream_id=stream_id,
            status="running",
            started_at=self._now(),
            cancel_event=cancel_event,
            mode=mode if mode in ("fresh", "fork") else "fresh",
            session_id=str(session_id or "").strip() or None,
            parent_session_id=str(parent_session_id or "").strip() or None,
            parent_agent_id=str(parent_agent_id or "").strip() or None,
            source_frame_uuid=str(source_frame_uuid or "").strip() or None,
            fork_metadata=copy.deepcopy(dict(fork_metadata or {})),
            subagent_type=str(subagent_type or "").strip() or None,
            name=str(name or "").strip() or None,
        )

        async with self._lock:
            self._gc_user_locked(user_id)
            agents = self._agents_by_user.setdefault(user_id, {})
            existing = agents.get(normalized_agent_id)
            if existing is not None and existing.status in _ACTIVE_STATUSES:
                raise AgentLimitExceededError(
                    "Agent %s is already running for user %s" % (normalized_agent_id, user_id)
                )
            active_count = sum(1 for item in agents.values() if item.status in _ACTIVE_STATUSES)
            if active_count >= self._active_limit:
                raise AgentLimitExceededError(
                    "User %s already has %d active background agents" % (user_id, self._active_limit)
                )

            record.runner_task = asyncio.create_task(self._run_agent(record, runner))
            agents[normalized_agent_id] = record

        logger.info(
            "Background agent started: user_id=%s agent_id=%s stream_id=%s reason=%s",
            user_id,
            normalized_agent_id,
            stream_id,
            reason,
        )
        return normalized_agent_id

    async def cancel(self, user_id: str, agent_id: str) -> bool:
        """Request cooperative cancellation for a running or idle background agent."""
        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None or record.status not in _ACTIVE_STATUSES:
                return False
            record.cancel_event.set()
            record.status = "killed"
            self._schedule_force_cancel(record.runner_task)

        logger.info(
            "Background agent cancellation requested: user_id=%s agent_id=%s",
            user_id,
            agent_id,
        )
        return True

    async def get(self, user_id: str, agent_id: str) -> AgentTask | None:
        """Return a read-only background agent view, if it exists for this user."""
        self._validate_user_id(user_id)
        async with self._lock:
            self._gc_user_locked(user_id)
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None:
                return None
            return record.snapshot()

    async def list_active(self, user_id: str) -> list[AgentTask]:
        """Return read-only views for currently running or idle agents owned by this user."""
        self._validate_user_id(user_id)
        async with self._lock:
            self._gc_user_locked(user_id)
            records = self._agents_by_user.get(user_id, {}).values()
            active = [record.snapshot() for record in records if record.status in _ACTIVE_STATUSES]
            return sorted(active, key=lambda item: item.started_at, reverse=True)

    def list_active_sync(self, user_id: str) -> list[AgentTask]:
        """Lock-free read of active agents — for synchronous callers (ContextBuilder).

        Trades strict consistency for callability from sync code: a record
        transitioning between statuses mid-read may be momentarily missed
        or seen, but Python dict reads are atomic so the structure won't
        corrupt. Acceptable for context display; use the async ``list_active``
        for any decision-bearing logic.
        """
        if not user_id:
            return []
        records = list(self._agents_by_user.get(user_id, {}).values())
        active = [record.snapshot() for record in records if record.status in _ACTIVE_STATUSES]
        return sorted(active, key=lambda item: item.started_at, reverse=True)

    async def list_recent_completed(self, user_id: str, limit: int = 10) -> list[AgentTask]:
        """Return recent terminal agent records for this user."""
        self._validate_user_id(user_id)
        async with self._lock:
            self._gc_user_locked(user_id)
            records = self._agents_by_user.get(user_id, {}).values()
            completed = [
                record.snapshot()
                for record in records
                if record.status in _TERMINAL_STATUSES and record.completed_at is not None
            ]
            return sorted(
                completed,
                key=lambda item: item.completed_at or item.started_at,
                reverse=True,
            )[:limit]

    async def register_foreground(
        self,
        user_id: str,
        task: str,
        reason: str,
        *,
        agent_id: str | None = None,
        mode: AgentMode = "fresh",
        session_id: str | None = None,
        parent_session_id: str | None = None,
        parent_agent_id: str | None = None,
        source_frame_uuid: str | None = None,
        fork_metadata: Mapping[str, Any] | None = None,
        subagent_type: str | None = None,
        name: str | None = None,
        auto_background_seconds: float | None = None,
    ) -> tuple[str, asyncio.Event, asyncio.Event]:
        """Register a synchronous (foreground) subagent so it can be backgrounded mid-run.

        Returns ``(agent_id, cancel_event, background_signal)``. ``background_signal``
        is set when ``background_now()`` is invoked or when the optional
        ``auto_background_seconds`` timer fires — callers race their normal
        await against ``background_signal.wait()`` to convert to async.

        Mirrors Claude Code's ``registerAgentForeground`` flow in
        ``LocalAgentTask.tsx`` (``src/tasks/LocalAgentTask/LocalAgentTask.tsx:526-651``).
        """

        self._validate_user_id(user_id)
        normalized_agent_id = str(agent_id or "").strip() or self._new_agent_id()
        agent_session_id = str(session_id or "").strip() or "agent:%s" % normalized_agent_id
        cancel_event = asyncio.Event()
        background_signal = asyncio.Event()
        normalized_mode: AgentMode = mode if mode in ("fresh", "fork") else "fresh"
        record = _AgentRecord(
            agent_id=normalized_agent_id,
            user_id=user_id,
            task=task,
            reason=reason,
            stream_id=normalized_agent_id,
            status="running",
            started_at=self._now(),
            cancel_event=cancel_event,
            mode=normalized_mode,
            session_id=agent_session_id,
            parent_session_id=str(parent_session_id or "").strip() or None,
            parent_agent_id=str(parent_agent_id or "").strip() or None,
            source_frame_uuid=str(source_frame_uuid or "").strip() or None,
            fork_metadata=copy.deepcopy(dict(fork_metadata or {})),
            subagent_type=str(subagent_type or "").strip() or None,
            name=str(name or "").strip() or None,
            is_foreground=True,
            background_signal=background_signal,
        )

        async with self._lock:
            self._gc_user_locked(user_id)
            agents = self._agents_by_user.setdefault(user_id, {})
            existing = agents.get(normalized_agent_id)
            if existing is not None and existing.status in _ACTIVE_STATUSES:
                raise AgentLimitExceededError(
                    "Agent %s is already running for user %s" % (normalized_agent_id, user_id)
                )
            active_count = sum(1 for item in agents.values() if item.status in _ACTIVE_STATUSES)
            if active_count >= self._active_limit:
                raise AgentLimitExceededError(
                    "User %s already has %d active background agents" % (user_id, self._active_limit)
                )
            agents[normalized_agent_id] = record

        if auto_background_seconds is not None and auto_background_seconds > 0:
            try:
                loop = asyncio.get_running_loop()
                record.background_auto_timer = loop.call_later(
                    float(auto_background_seconds),
                    self._background_now_sync,
                    user_id,
                    normalized_agent_id,
                )
            except RuntimeError:
                logger.debug("Cannot install auto-background timer: no running loop")

        logger.info(
            "Foreground agent registered: user_id=%s agent_id=%s session_id=%s mode=%s",
            user_id,
            normalized_agent_id,
            agent_session_id,
            record.mode,
        )
        return normalized_agent_id, cancel_event, background_signal

    def _background_now_sync(self, user_id: str, agent_id: str) -> None:
        agents = self._agents_by_user.get(user_id, {})
        record = agents.get(agent_id)
        if record is None or not record.is_foreground:
            return
        if record.background_signal is not None and not record.background_signal.is_set():
            record.background_signal.set()

    async def background_now(self, user_id: str, agent_id: str) -> bool:
        """Convert a registered foreground agent into background mode.

        Returns True if the signal was newly set. Callers race their next
        ``await`` against ``background_signal`` to detect this transition.
        """

        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None or not record.is_foreground:
                return False
            if record.background_signal is None or record.background_signal.is_set():
                return False
            record.background_signal.set()
            if record.background_auto_timer is not None:
                record.background_auto_timer.cancel()
                record.background_auto_timer = None
        logger.info("Foreground agent backgrounded: user_id=%s agent_id=%s", user_id, agent_id)
        return True

    async def attach_runner_task(
        self,
        user_id: str,
        agent_id: str,
        runner_task: asyncio.Task[None],
    ) -> None:
        """Attach a runner Task to a foreground record so cancel/kill can act on it."""

        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None:
                return
            record.runner_task = runner_task

    async def mark_idle(self, user_id: str, agent_id: str) -> bool:
        """Transition a running agent to ``idle`` (alive, awaiting send_message)."""

        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None or record.status != "running":
                return False
            record.status = "idle"
            logger.info("Background agent idle: user_id=%s agent_id=%s", user_id, agent_id)
            return True

    async def mark_running(self, user_id: str, agent_id: str) -> bool:
        """Transition an idle agent back to ``running``."""

        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None or record.status != "idle":
                return False
            record.status = "running"
            return True

    async def mark_complete_explicit(
        self,
        user_id: str,
        agent_id: str,
        result: str,
    ) -> bool:
        """Finalize a foreground agent that completed via the inline await path."""

        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None:
                return False
            if record.background_auto_timer is not None:
                record.background_auto_timer.cancel()
                record.background_auto_timer = None
            record.result = result
            record.completed_at = self._now()
            if record.status in _ACTIVE_STATUSES:
                record.status = "completed"
            self._gc_user_locked(user_id)
        return True

    async def mark_failed_explicit(
        self,
        user_id: str,
        agent_id: str,
        error_detail: str,
        *,
        killed: bool = False,
    ) -> bool:
        """Finalize a foreground agent that failed or was killed mid-run."""

        self._validate_user_id(user_id)
        async with self._lock:
            record = self._agents_by_user.get(user_id, {}).get(agent_id)
            if record is None:
                return False
            if record.background_auto_timer is not None:
                record.background_auto_timer.cancel()
                record.background_auto_timer = None
            record.error_detail = error_detail
            record.completed_at = self._now()
            record.status = "killed" if killed else "failed"
            self._gc_user_locked(user_id)
        return True

    async def _run_agent(self, record: _AgentRecord, runner: AgentRunner) -> None:
        try:
            result = await runner(record.cancel_event)
        except asyncio.CancelledError:
            await self._mark_cancelled(record)
        except Exception as exc:
            await self._mark_error(record, exc)
        else:
            await self._mark_complete(record, result)

    async def _mark_complete(self, record: _AgentRecord, result: str) -> None:
        async with self._lock:
            current = self._agents_by_user.get(record.user_id, {}).get(record.agent_id)
            if current is None:
                return
            current.result = result
            current.completed_at = self._now()
            if current.status in _ACTIVE_STATUSES:
                current.status = "completed"
            self._gc_user_locked(record.user_id)

        logger.info(
            "Background agent completed: user_id=%s agent_id=%s status=%s",
            record.user_id,
            record.agent_id,
            record.status,
        )

    async def _mark_cancelled(self, record: _AgentRecord) -> None:
        async with self._lock:
            current = self._agents_by_user.get(record.user_id, {}).get(record.agent_id)
            if current is None:
                return
            current.status = "killed"
            current.completed_at = self._now()
            self._gc_user_locked(record.user_id)

        logger.info(
            "Background agent cancelled: user_id=%s agent_id=%s",
            record.user_id,
            record.agent_id,
        )

    async def _mark_error(self, record: _AgentRecord, exc: Exception) -> None:
        error_detail = "%s: %s" % (type(exc).__name__, exc)
        async with self._lock:
            current = self._agents_by_user.get(record.user_id, {}).get(record.agent_id)
            if current is None:
                return
            current.status = "failed"
            current.error_detail = error_detail
            current.completed_at = self._now()
            self._gc_user_locked(record.user_id)

        logger.info(
            "Background agent error: user_id=%s agent_id=%s error=%s",
            record.user_id,
            record.agent_id,
            error_detail,
        )

    def _schedule_force_cancel(self, runner_task: asyncio.Task[None] | None) -> None:
        if runner_task is None or runner_task.done():
            return
        loop = asyncio.get_running_loop()

        def _force_cancel() -> None:
            if not runner_task.done():
                runner_task.cancel()

        loop.call_later(_CANCEL_FORCE_GRACE_SECONDS, _force_cancel)

    def _recover_interrupted_tasks_on_boot(self) -> None:
        """Mark persisted in-progress checkpoints as interrupted after a restart."""
        try:
            from intent import task_checkpoint as checkpoints

            checkpoint_dir = checkpoints.CHECKPOINT_DIR
            if not checkpoint_dir.exists():
                return
            enc = checkpoints._get_checkpoint_encryption()
            recovered = 0
            corrupt = 0
            for path in self._iter_checkpoint_paths(checkpoint_dir):
                data, was_corrupt = self._read_checkpoint_data(
                    path,
                    enc,
                    log_corrupt=corrupt < _CORRUPT_CHECKPOINT_WARNING_LIMIT,
                )
                if was_corrupt:
                    corrupt += 1
                if not data or data.get("status") != "in_progress":
                    continue
                user_id = self._checkpoint_user_id(checkpoint_dir, path, data)
                if not user_id:
                    logger.warning(
                        "Skipping checkpoint recovery without concrete user_id: %s",
                        self._checkpoint_path_for_log(path),
                    )
                    continue
                task_id = str(data.get("task_id") or path.stem).strip()
                if not task_id:
                    continue
                agent_id = str(data.get("agent_id") or task_id).strip()
                if agent_id in self._agents_by_user.get(user_id, {}):
                    continue

                now = self._now()
                metadata = data.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["recovered_from_crash"] = True
                data["metadata"] = metadata
                data["status"] = "interrupted"
                data["updated_at"] = now.isoformat()
                checkpoints._write_checkpoint_data(path, data, enc)
                self._append_checkpoint_resume_frame(
                    user_id=user_id,
                    task_id=task_id,
                    data=data,
                )

                session_id = self._checkpoint_session_id(data)
                parent_session_id = self._checkpoint_session_id(data, keys=("parent_session_id",))
                parent_agent_id = self._checkpoint_text_value(data, "parent_agent_id")
                source_frame_uuid = self._checkpoint_text_value(data, "source_frame_uuid")
                mode_raw = self._checkpoint_text_value(data, "mode")
                mode: AgentMode = "fork" if mode_raw == "fork" else "fresh"
                fork_metadata = self._checkpoint_mapping(data, "fork_metadata")

                record = _AgentRecord(
                    agent_id=agent_id,
                    user_id=user_id,
                    task=str(data.get("task_description") or task_id),
                    reason="Recovered from crash checkpoint",
                    stream_id=str(data.get("stream_id") or ""),
                    status="failed",
                    started_at=self._parse_checkpoint_datetime(data.get("created_at"), fallback=now),
                    cancel_event=asyncio.Event(),
                    mode=mode,
                    session_id=session_id or None,
                    parent_session_id=parent_session_id or None,
                    parent_agent_id=parent_agent_id or None,
                    source_frame_uuid=source_frame_uuid or None,
                    fork_metadata=copy.deepcopy(fork_metadata),
                    subagent_type=self._checkpoint_text_value(data, "subagent_type") or None,
                    name=self._checkpoint_text_value(data, "name") or None,
                    completed_at=self._parse_checkpoint_datetime(data.get("updated_at"), fallback=now),
                    result="Task interrupted by application restart.",
                )
                self._agents_by_user.setdefault(user_id, {})[agent_id] = record
                recovered += 1
            if recovered:
                logger.info("Recovered %d interrupted background agent checkpoint(s)", recovered)
            if corrupt > _CORRUPT_CHECKPOINT_WARNING_LIMIT:
                logger.warning(
                    "Skipped %d additional corrupt checkpoint(s) during registry recovery",
                    corrupt - _CORRUPT_CHECKPOINT_WARNING_LIMIT,
                )
        except Exception:
            logger.exception("Interrupted task recovery failed during AgentRegistry init")

    def _append_checkpoint_resume_frame(
        self,
        *,
        user_id: str,
        task_id: str,
        data: dict[str, Any],
    ) -> None:
        session_id = self._checkpoint_session_id(data)
        if not session_id:
            return
        try:
            from services.conversation.repair import build_transcript_continue_frame
            from services.conversation.state_manager import ConversationStateManager

            manager = ConversationStateManager(user_id=user_id, session_id=session_id)
            existing = manager.get_message_chain(behavioral_only=False)
            if any(
                frame.origin == "transcript_recovery"
                and isinstance(frame.extra, dict)
                and frame.extra.get("checkpoint_task_id") == task_id
                for frame in existing
            ):
                return
            manager.append_frame(
                build_transcript_continue_frame(
                    session_id=session_id,
                    task_id=task_id,
                    reason="checkpoint_interrupted",
                    checkpoint_task_id=task_id,
                )
            )
        except Exception:
            logger.exception("Failed to append transcript resume frame for checkpoint %s", task_id)

    @staticmethod
    def _iter_checkpoint_paths(checkpoint_dir: Path) -> list[Path]:
        candidates = list(checkpoint_dir.glob("*.json"))
        candidates.extend(checkpoint_dir.glob("*/*.json"))
        cutoff = datetime.now(UTC).timestamp() - _BOOT_RECOVERY_MAX_AGE_SECONDS
        recent: list[tuple[float, Path]] = []
        stale = 0
        unreadable = 0
        for path in set(candidates):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                unreadable += 1
                continue
            if mtime < cutoff:
                stale += 1
                continue
            recent.append((mtime, path))

        if stale:
            logger.info("Skipped %d stale checkpoint(s) during registry recovery", stale)
        if unreadable:
            logger.warning(
                "Skipped %d unreadable checkpoint path(s) during registry recovery",
                unreadable,
            )

        recent.sort(key=lambda item: (-item[0], str(item[1])))
        if len(recent) > _BOOT_RECOVERY_SCAN_LIMIT:
            logger.warning(
                "Registry recovery scanning newest %d of %d recent checkpoint(s)",
                _BOOT_RECOVERY_SCAN_LIMIT,
                len(recent),
            )
            recent = recent[:_BOOT_RECOVERY_SCAN_LIMIT]
        return [path for _, path in recent]

    @staticmethod
    def _read_checkpoint_data(path: Path, enc: Any, *, log_corrupt: bool = True) -> tuple[dict[str, Any] | None, bool]:
        try:
            from intent import task_checkpoint as checkpoints

            data = checkpoints._read_checkpoint_data(path, enc)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            if log_corrupt:
                logger.warning(
                    "Skipping corrupt checkpoint during registry recovery: %s",
                    AgentRegistry._checkpoint_path_for_log(path),
                )
            return None, True
        return (data if isinstance(data, dict) else None), False

    @staticmethod
    def _checkpoint_path_for_log(path: Path) -> str:
        from core.user_context import legacy_local_user_id

        return str(path).replace(legacy_local_user_id(), "retired-desktop-user")

    @staticmethod
    def _checkpoint_user_id(checkpoint_dir: Path, path: Path, data: dict[str, Any]) -> str:
        from core.user_context import (
            get_current_or_device_user_id,
            is_legacy_local_user_id,
            user_id_or_none,
        )

        def resolve_candidate(value: object) -> str:
            normalized = user_id_or_none(value)
            if normalized is not None:
                return normalized
            if is_legacy_local_user_id(value):
                try:
                    return get_current_or_device_user_id()
                except LookupError:
                    return ""
            return ""

        user_id = resolve_candidate(data.get("user_id"))
        if user_id:
            return user_id
        try:
            relative = path.relative_to(checkpoint_dir)
        except ValueError:
            return ""
        if len(relative.parts) >= 2:
            return resolve_candidate(relative.parts[0])
        return ""

    @classmethod
    def _checkpoint_session_id(cls, data: dict[str, Any], *, keys: tuple[str, ...] | None = None) -> str:
        from services.conversation.session_identity import coerce_session_id

        candidate_keys = keys or (
            "session_id",
            "conversation_session_id",
            "transcript_session_id",
        )
        for value in cls._checkpoint_values(data, candidate_keys):
            try:
                candidate = coerce_session_id(value)
            except ValueError:
                continue
            if candidate:
                return candidate
        return ""

    @classmethod
    def _checkpoint_text_value(cls, data: dict[str, Any], key: str) -> str:
        for value in cls._checkpoint_values(data, (key,)):
            candidate = str(value or "").strip()
            if candidate:
                return candidate
        return ""

    @classmethod
    def _checkpoint_mapping(cls, data: dict[str, Any], key: str) -> dict[str, Any]:
        for value in cls._checkpoint_values(data, (key,)):
            if isinstance(value, Mapping):
                return dict(value)
        return {}

    @staticmethod
    def _checkpoint_values(data: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
        values: list[Any] = []
        for key in keys:
            if key in data:
                values.append(data.get(key))
        for container_key in ("metadata", "context", "llm_continuity"):
            container = data.get(container_key)
            if not isinstance(container, Mapping):
                continue
            for key in keys:
                if key in container:
                    values.append(container.get(key))
        return values

    @staticmethod
    def _parse_checkpoint_datetime(value: Any, *, fallback: datetime) -> datetime:
        if not isinstance(value, str) or not value.strip():
            return fallback
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return fallback
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _gc_user_locked(self, user_id: str) -> None:
        agents = self._agents_by_user.get(user_id)
        if not agents:
            return

        cutoff = self._now() - self._retention
        for agent_id, record in list(agents.items()):
            if record.status != "running" and record.completed_at is not None and record.completed_at < cutoff:
                agents.pop(agent_id, None)

        if len(agents) > self._max_entries_per_user:
            removable = sorted(
                (
                    record
                    for record in agents.values()
                    if record.status != "running" and record.completed_at is not None
                ),
                key=lambda item: item.completed_at or item.started_at,
            )
            overflow = len(agents) - self._max_entries_per_user
            for record in removable[:overflow]:
                agents.pop(record.agent_id, None)

        if not agents:
            self._agents_by_user.pop(user_id, None)

    def _new_agent_id(self) -> str:
        return uuid.uuid4().hex[:_AGENT_ID_HEX_LENGTH]

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    @staticmethod
    def _validate_user_id(user_id: str) -> None:
        from core.user_context import is_placeholder_user_id

        if is_placeholder_user_id(user_id):
            raise ValueError("Background agents require a concrete user_id")


agent_registry = AgentRegistry()
