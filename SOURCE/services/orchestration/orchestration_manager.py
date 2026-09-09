"""High-level orchestration manager for multi-agent workflows."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Final

from config.settings import settings
from core.exceptions import ErrorContext, ServiceError
from core.logging_config import get_logger
from core.subprocess_utils import run_silent
from scripts import proc_tree
from services.orchestration.agent_spawner import spawn_worker_agent_async
from services.orchestration.blackboard import cmd_offline_async, cmd_read_async
from services.orchestration.codex_executor import execute_codex_task_async

logger = get_logger(__name__)

PID_FILE_PREFIX: Final[str] = ".pid-"
WORKER_CODENAME_PREFIX: Final[str] = "worker"

_manager_singleton: OrchestrationManager | None = None
_manager_singleton_lock = Lock()


def _orchestration_dir() -> Path:
    """Return the orchestration data directory."""

    path = Path(settings.orchestration_data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pid_file(codename: str) -> Path:
    """Return the pid file for an orchestration agent."""

    return _orchestration_dir() / f"{PID_FILE_PREFIX}{codename}"


def _pid_is_alive(pid: int) -> bool:
    """Check whether a PID is still active."""

    if os.name == "nt":
        try:
            result = run_silent(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )  # proc-tree-ok: single tasklist binary, no shell, no grandchildren
        except Exception:
            return False
        return str(pid) in result.stdout

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class OrchestrationManager:
    """Coordinate orchestration workers, Codex tasks, and blackboard state."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def _ensure_orchestration_enabled(self) -> None:
        """Raise when orchestration is disabled."""

        if settings.orchestration_enabled:
            return
        raise ServiceError(
            "Orchestration is disabled",
            ErrorContext(
                component="services.orchestration.orchestration_manager",
                operation="ensure_orchestration_enabled",
                user_message="Multi-agent orchestration is disabled.",
                recovery_hint="Turn on multi-agent orchestration before starting orchestration tasks.",
            ),
        )

    def _generate_codename(self, prefix: str) -> str:
        """Generate a stable codename suffix for a new orchestration agent."""

        return "%s-%s" % (prefix, datetime.now(UTC).strftime("%H%M%S"))

    async def spawn_worker(
        self,
        task: str,
        codename: str | None = None,
        model: str = "sonnet",
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Spawn an orchestration worker agent."""

        self._ensure_orchestration_enabled()
        resolved_codename = codename or self._generate_codename(WORKER_CODENAME_PREFIX)
        async with self._lock:
            return await spawn_worker_agent_async(task, resolved_codename, model, system_prompt)

    async def execute_codex(
        self,
        prompt: str,
        project_root: str | None = None,
        timeout: int | None = None,
        background: bool = True,
    ) -> dict[str, Any]:
        """Execute a Codex task from orchestration."""

        self._ensure_orchestration_enabled()
        resolved_timeout = timeout if timeout is not None else settings.codex_timeout
        return await execute_codex_task_async(prompt, project_root, resolved_timeout, background)

    async def get_blackboard_state(self) -> dict[str, Any]:
        """Return parsed blackboard state for monitoring UIs and APIs."""

        text = await cmd_read_async()
        sections: dict[str, list[str] | str] = {
            "agents_online": [],
            "warnings": [],
            "messages": [],
            "findings": [],
            "completed": [],
            "raw_text": text,
        }
        current_section: str | None = None

        for line in text.splitlines():
            if line == "## AGENTS ONLINE":
                current_section = "agents_online"
                continue
            if line == "## WARNINGS":
                current_section = "warnings"
                continue
            if line == "## MESSAGES":
                current_section = "messages"
                continue
            if line == "## FINDINGS":
                current_section = "findings"
                continue
            if line == "## COMPLETED":
                current_section = "completed"
                continue
            if current_section is None:
                continue
            if not line.strip().startswith("- "):
                continue
            current_entries = sections[current_section]
            if isinstance(current_entries, list):
                current_entries.append(line.strip())

        return sections

    def _read_agent_pid(self, codename: str) -> int | None:
        """Read and validate the PID file for an orchestration agent.

        Returns the PID as an int, or None if no pid file exists.
        Raises ServiceError if the pid file is present but contains invalid data.
        """

        pid_path = _pid_file(codename)
        if not pid_path.exists():
            logger.info("No pid file found for orchestration agent %s", codename)
            return None

        try:
            return int(pid_path.read_text(encoding="utf-8").strip())
        except ValueError as exc:
            raise ServiceError(
                f"Invalid orchestration state for {codename}",
                ErrorContext(
                    component="services.orchestration.orchestration_manager",
                    operation="stop_agent",
                    params={"codename": codename, "pid_file": str(pid_path)},
                    user_message="The orchestration agent could not be stopped cleanly.",
                    recovery_hint="Restart Viola, then try again.",
                ),
                cause=exc,
            ) from exc

    def _terminate_process(self, codename: str, pid: int) -> None:
        """Send a termination signal to the given process.

        Raises ServiceError wrapping OSError if the OS-level kill fails.
        """

        try:
            if os.name == "nt":
                # Unbounded before this fix (no timeout=): taskkill is a leaf binary and
                # unlikely to hang, but a call with capture_output=True and NO timeout at
                # all is worse-shaped than the classic wedge -- there isn't even a timeout
                # to fire. proc_tree.run bounds it and tree-kills on the (unlikely) hang.
                proc_tree.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    timeout=30,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.exception("Failed stopping orchestration agent %s pid=%s", codename, pid)
            raise ServiceError(
                f"Failed to stop orchestration agent {codename}",
                ErrorContext(
                    component="services.orchestration.orchestration_manager",
                    operation="stop_agent",
                    params={"codename": codename, "pid": pid},
                    user_message="The orchestration agent could not be stopped.",
                    recovery_hint="Wait a moment, then try again. If it keeps failing, restart Viola.",
                ),
                cause=exc,
            ) from exc

    async def stop_agent(self, codename: str) -> bool:
        """Terminate a tracked orchestration worker agent."""

        pid = self._read_agent_pid(codename)
        if pid is None:
            await cmd_offline_async(codename)
            return False

        self._terminate_process(codename, pid)

        pid_path = _pid_file(codename)
        pid_path.unlink(missing_ok=True)
        await cmd_offline_async(codename)
        logger.info("Stopped orchestration agent %s pid=%s", codename, pid)
        return True

    async def cleanup_dead_agents(self) -> list[str]:
        """Remove stale pid files and offline entries for dead agents."""

        cleaned: list[str] = []
        for path in _orchestration_dir().glob(f"{PID_FILE_PREFIX}*"):
            codename = path.name[len(PID_FILE_PREFIX) :]
            try:
                pid = int(path.read_text(encoding="utf-8").strip())
            except ValueError:
                path.unlink(missing_ok=True)
                cleaned.append(codename)
                await cmd_offline_async(codename)
                continue

            if _pid_is_alive(pid):
                continue

            path.unlink(missing_ok=True)
            cleaned.append(codename)
            await cmd_offline_async(codename)

        if cleaned:
            logger.info("Cleaned dead orchestration agents: %s", ", ".join(cleaned))
        return cleaned


def get_orchestration_manager() -> OrchestrationManager:
    """Get or create the global orchestration manager singleton."""

    global _manager_singleton
    if _manager_singleton is None:
        with _manager_singleton_lock:
            if _manager_singleton is None:
                _manager_singleton = OrchestrationManager()
    return _manager_singleton


__all__ = ["OrchestrationManager", "get_orchestration_manager"]
