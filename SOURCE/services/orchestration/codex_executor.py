"""Execute and monitor Codex orchestration tasks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypedDict

from config.settings import settings
from core.constants import TIMEOUT_5_MINUTES, TIMEOUT_DEFAULT
from core.exceptions import ErrorContext, ServiceError, ServiceTimeoutError
from core.logging_config import get_logger
from core.platform import get_data_dir
from core.subprocess_utils import popen_silent, run_silent
from scripts import proc_tree

logger = get_logger(__name__)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
CODEX_LOG_DIR_NAME: Final[str] = "codex-logs"
OUTPUT_SUFFIX: Final[str] = ".log"
METADATA_SUFFIX: Final[str] = ".json"
TASK_ID_PREFIX_LENGTH: Final[int] = 12
POLL_INTERVAL_SECONDS: Final[float] = TIMEOUT_DEFAULT


class CodexTaskMetadata(TypedDict, total=False):
    """Persisted metadata for a Codex task."""

    task_id: str
    pid: int
    project_root: str
    prompt: str
    output_file: str
    report_file: str
    background: bool
    started_at: str
    completed_at: str
    returncode: int


def _get_codex_logs_dir() -> Path:
    """Return the Codex logs directory."""

    path = Path(settings.orchestration_data_dir) / CODEX_LOG_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _metadata_path(task_id: str) -> Path:
    """Return the metadata path for a task id."""

    return _get_codex_logs_dir() / f"{task_id}{METADATA_SUFFIX}"


def _output_path(task_id: str) -> Path:
    """Return the output log path for a task id."""

    return _get_codex_logs_dir() / f"{task_id}{OUTPUT_SUFFIX}"


def _report_path(task_id: str) -> Path:
    """Return the explicit Codex report path used to guard 0-byte stdout."""

    base = Path(os.environ.get("VIOLA_CODEX_REPORT_DIR") or (get_data_dir() / "codex_reports"))
    base.mkdir(parents=True, exist_ok=True)
    return base / f"codex_{task_id}_report.md"


def _prompt_with_report_requirement(task_prompt: str, report_file: Path) -> str:
    """Append mandatory report instructions to Codex prompts."""

    return "\n\n".join(
        [
            task_prompt,
            "Required output capture: write a concise Markdown report to `%s` before exiting. "
            "Verify the file exists and is non-empty; if the task fails, still write the failure reason there."
            % report_file,
        ]
    )


def _report_status(report_file: Path) -> dict[str, Any]:
    exists = report_file.exists()
    size = report_file.stat().st_size if exists else 0
    return {
        "report_file": str(report_file),
        "report_exists": exists,
        "report_size_bytes": size,
        "report_ok": exists and size > 0,
    }


def _timestamp() -> str:
    """Return an ISO timestamp in UTC."""

    return datetime.now(UTC).isoformat()


def _new_task_id() -> str:
    """Generate a stable short task identifier."""

    return uuid.uuid4().hex[:TASK_ID_PREFIX_LENGTH]


def _write_metadata(metadata: CodexTaskMetadata) -> None:
    """Persist task metadata to disk."""

    path = _metadata_path(metadata["task_id"])
    path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")


def _read_metadata(task_id: str) -> CodexTaskMetadata:
    """Load task metadata from disk."""

    path = _metadata_path(task_id)
    if not path.exists():
        raise ServiceError(
            f"Codex task {task_id} was not found",
            ErrorContext(
                component="services.orchestration.codex_executor",
                operation="read_metadata",
                params={"task_id": task_id, "path": str(path)},
                user_message="That Codex task could not be found.",
                recovery_hint="Check the task id or start the Codex task again.",
            ),
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _pid_is_alive(pid: int) -> bool:
    """Check whether a PID is still running."""

    if sys.platform == "win32":
        try:
            result = run_silent(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=POLL_INTERVAL_SECONDS,
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


def execute_codex_task(
    task_prompt: str,
    project_root: str | None = None,
    timeout: int = int(TIMEOUT_5_MINUTES),
    background: bool = True,
) -> dict[str, Any]:
    """Launch a Codex task and persist its output to orchestration logs."""

    if not settings.codex_enabled:
        raise ServiceError(
            "Codex execution is disabled",
            ErrorContext(
                component="services.orchestration.codex_executor",
                operation="execute_codex_task",
                user_message="Codex delegation is turned off.",
                recovery_hint="Turn on Codex delegation before starting Codex tasks.",
            ),
        )

    task_id = _new_task_id()
    resolved_root = Path(project_root) if project_root else PROJECT_ROOT
    output_file = _output_path(task_id)
    report_file = _report_path(task_id)
    command = ["codex", "exec", "-C", str(resolved_root), _prompt_with_report_requirement(task_prompt, report_file)]

    metadata: CodexTaskMetadata = {
        "task_id": task_id,
        "project_root": str(resolved_root),
        "prompt": task_prompt,
        "output_file": str(output_file),
        "report_file": str(report_file),
        "background": background,
        "started_at": _timestamp(),
    }

    logger.info("Launching Codex task %s in %s", task_id, resolved_root)

    try:
        if background:
            output_handle = output_file.open("w", encoding="utf-8")
            kwargs: dict[str, Any] = {"cwd": str(resolved_root)}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            process = popen_silent(
                command,
                stdout=output_handle,
                stderr=subprocess.STDOUT,
                text=True,
                **kwargs,
            )
            output_handle.close()

            metadata["pid"] = process.pid
            _write_metadata(metadata)
            return {
                "task_id": task_id,
                "output_file": str(output_file),
                "report_file": str(report_file),
                "metadata_file": str(_metadata_path(task_id)),
                "pid": process.pid,
                "background": True,
            }

        # codex exec is a full agentic CLI (it runs shell commands/tools) -- a real
        # orphan risk on timeout even though output goes to a real file handle, not a
        # PIPE (the classic pipe-drain-forever mechanism doesn't apply here, but a raw
        # subprocess.run/run_silent still only kills the direct child on timeout,
        # leaking any grandchild codex itself spawned). Routed through proc_tree.run's
        # tree-killing runner; raise_on_timeout=True preserves the existing
        # `except subprocess.TimeoutExpired` contract below.
        result = proc_tree.run(
            command,
            cwd=str(resolved_root),
            timeout=timeout,
            combine_stderr=True,
            raise_on_timeout=True,
        )
        output_file.write_text(result.stdout, encoding="utf-8")
        completed = result

        metadata["completed_at"] = _timestamp()
        metadata["returncode"] = completed.returncode
        metadata.update(_report_status(report_file))
        _write_metadata(metadata)
        return {
            "task_id": task_id,
            "output_file": str(output_file),
            "report_file": str(report_file),
            "metadata_file": str(_metadata_path(task_id)),
            "background": False,
            "returncode": completed.returncode,
            **_report_status(report_file),
        }
    except subprocess.TimeoutExpired as exc:
        logger.exception("Codex task %s timed out", task_id)
        # Preserve the pre-fix behavior of a real stdout file handle: whatever partial
        # output existed at the moment of the kill lands in output_file, same as it did
        # when the child process wrote directly into that handle as it ran.
        partial_output = exc.output if isinstance(exc.output, str) else ""
        if partial_output:
            try:
                output_file.write_text(partial_output, encoding="utf-8")
            except OSError:
                logger.debug("Could not write partial Codex output for task %s", task_id)
        raise ServiceTimeoutError("codex", timeout, "exec") from exc
    except Exception as exc:
        logger.exception("Failed to execute Codex task %s", task_id)
        raise ServiceError(
            f"Failed to execute Codex task {task_id}",
            ErrorContext(
                component="services.orchestration.codex_executor",
                operation="execute_codex_task",
                params={"task_id": task_id, "project_root": str(resolved_root)},
                user_message="Try again in a moment",
                recovery_hint="Make sure Codex is set up on this device, then try again.",
            ),
            cause=exc,
        ) from exc


def get_codex_output(task_id: str, wait: bool = False, timeout: int | None = None) -> str:
    """Return persisted Codex output, optionally waiting for task completion."""

    metadata = _read_metadata(task_id)
    output_file = Path(metadata["output_file"])

    if wait and "pid" in metadata:
        resolved_timeout = float(timeout if timeout is not None else settings.codex_timeout)
        deadline = time.monotonic() + resolved_timeout
        while _pid_is_alive(metadata["pid"]):
            if time.monotonic() >= deadline:
                raise ServiceTimeoutError("codex", resolved_timeout, "wait_for_output")
            time.sleep(POLL_INTERVAL_SECONDS)
        raw_report_file = metadata.get("report_file")
        if raw_report_file:
            report_file = Path(raw_report_file)
            metadata.update(_report_status(report_file))
            metadata["completed_at"] = metadata.get("completed_at") or _timestamp()
            _write_metadata(metadata)

    if not output_file.exists():
        return ""
    return output_file.read_text(encoding="utf-8")


async def execute_codex_task_async(
    task_prompt: str,
    project_root: str | None = None,
    timeout: int = int(TIMEOUT_5_MINUTES),
    background: bool = True,
) -> dict[str, Any]:
    """Async wrapper for :func:`execute_codex_task`."""

    return await asyncio.to_thread(execute_codex_task, task_prompt, project_root, timeout, background)


async def get_codex_output_async(
    task_id: str,
    wait: bool = False,
    timeout: int | None = None,
) -> str:
    """Async wrapper for :func:`get_codex_output`."""

    return await asyncio.to_thread(get_codex_output, task_id, wait, timeout)


__all__ = [
    "execute_codex_task",
    "execute_codex_task_async",
    "get_codex_output",
    "get_codex_output_async",
]
