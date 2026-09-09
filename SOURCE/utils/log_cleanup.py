from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from core.platform import get_logs_dir

_SECONDS_PER_DAY = 24 * 60 * 60
_SEVEN_DAYS_SECONDS = 7 * _SECONDS_PER_DAY
_AGENT_TASK_FILE_LIMIT = 1000
_OVERSIZED_LOG_BYTES = 100 * 1024 * 1024
_BYTES_PER_MB = 1024 * 1024
_STARTUP_LOG_CLEANUP_COMPLETED = False


class LoggerLike(Protocol):
    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def exception(self, msg: str, *args: object, **kwargs: object) -> None: ...


@dataclass(slots=True)
class LogCleanupResult:
    status: str = "completed"
    files_deleted: int = 0
    bytes_reclaimed: int = 0
    wake_audio_deleted: int = 0
    agent_tasks_deleted: int = 0
    oversized_logs_deleted: int = 0


def cleanup_startup_logs(
    *,
    log_root: Path | None = None,
    logger: LoggerLike | None = None,
    active_log_paths: set[Path] | None = None,
    force: bool = False,
) -> LogCleanupResult:
    """Apply startup log retention without blocking application boot."""
    global _STARTUP_LOG_CLEANUP_COMPLETED

    cleanup_logger = logger or logging.getLogger("viola.log_cleanup")
    resolved_log_root = (log_root or get_logs_dir()).resolve()

    if _STARTUP_LOG_CLEANUP_COMPLETED and not force:
        cleanup_logger.debug(
            "Startup log cleanup already completed for %s; skipping",
            resolved_log_root,
        )
        return LogCleanupResult(status="skipped")

    try:
        result = LogCleanupResult()
        now = time.time()
        current_active_logs = (
            {_normalize_path(path) for path in active_log_paths}
            if active_log_paths is not None
            else _discover_active_log_paths()
        )

        for relative_dir in (
            Path("wake_audio_normalized"),
            Path("wake_captures"),
            Path("wake_audio"),
        ):
            deleted, reclaimed = _delete_files_older_than(
                resolved_log_root / relative_dir,
                now - _SEVEN_DAYS_SECONDS,
            )
            result.files_deleted += deleted
            result.bytes_reclaimed += reclaimed
            result.wake_audio_deleted += deleted
            if deleted:
                cleanup_logger.info(
                    "Startup log cleanup removed %d expired files from %s, reclaiming %.2f MB",
                    deleted,
                    resolved_log_root / relative_dir,
                    reclaimed / _BYTES_PER_MB,
                )

        agent_deleted, agent_reclaimed = _cap_directory_file_count(
            resolved_log_root / "agent_tasks",
            _AGENT_TASK_FILE_LIMIT,
        )
        result.files_deleted += agent_deleted
        result.bytes_reclaimed += agent_reclaimed
        result.agent_tasks_deleted += agent_deleted
        if agent_deleted:
            cleanup_logger.info(
                "Startup log cleanup trimmed %d files from %s to enforce the %d-file cap, reclaiming %.2f MB",
                agent_deleted,
                resolved_log_root / "agent_tasks",
                _AGENT_TASK_FILE_LIMIT,
                agent_reclaimed / _BYTES_PER_MB,
            )

        oversized_deleted, oversized_reclaimed = _delete_oversized_log_files(
            resolved_log_root,
            active_log_paths=current_active_logs,
            size_limit_bytes=_OVERSIZED_LOG_BYTES,
        )
        result.files_deleted += oversized_deleted
        result.bytes_reclaimed += oversized_reclaimed
        result.oversized_logs_deleted += oversized_deleted
        if oversized_deleted:
            cleanup_logger.info(
                "Startup log cleanup removed %d oversized log files above %d MB, reclaiming %.2f MB",
                oversized_deleted,
                _OVERSIZED_LOG_BYTES // _BYTES_PER_MB,
                oversized_reclaimed / _BYTES_PER_MB,
            )

        if result.files_deleted:
            cleanup_logger.info(
                "Startup log cleanup complete: %d files deleted, %.2f MB reclaimed",
                result.files_deleted,
                result.bytes_reclaimed / _BYTES_PER_MB,
            )
        else:
            cleanup_logger.debug(
                "Startup log cleanup found no files to remove under %s",
                resolved_log_root,
            )

        _STARTUP_LOG_CLEANUP_COMPLETED = True
        return result
    except Exception:
        cleanup_logger.exception(
            "Startup log cleanup failed for %s",
            resolved_log_root,
        )
        return LogCleanupResult(status="failed")


def _delete_files_older_than(directory: Path, cutoff_mtime: float) -> tuple[int, int]:
    if not directory.is_dir():
        return 0, 0

    deleted = 0
    reclaimed = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if stat_result.st_mtime > cutoff_mtime:
            continue
        if _unlink_file(path):
            deleted += 1
            reclaimed += stat_result.st_size

    return deleted, reclaimed


def _cap_directory_file_count(directory: Path, max_files: int) -> tuple[int, int]:
    if not directory.is_dir():
        return 0, 0

    files: list[tuple[float, int, Path]] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        files.append((stat_result.st_mtime, stat_result.st_size, path))

    excess = len(files) - max_files
    if excess <= 0:
        return 0, 0

    deleted = 0
    reclaimed = 0
    files.sort(key=lambda item: item[0])
    for _, size, path in files[:excess]:
        if _unlink_file(path):
            deleted += 1
            reclaimed += size

    return deleted, reclaimed


def _delete_oversized_log_files(
    log_root: Path,
    *,
    active_log_paths: set[Path],
    size_limit_bytes: int,
) -> tuple[int, int]:
    if not log_root.is_dir():
        return 0, 0

    deleted = 0
    reclaimed = 0

    for path in log_root.rglob("*"):
        if not path.is_file() or not _is_log_file(path):
            continue
        normalized = _normalize_path(path)
        if normalized in active_log_paths:
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if stat_result.st_size <= size_limit_bytes:
            continue
        if _unlink_file(path):
            deleted += 1
            reclaimed += stat_result.st_size

    return deleted, reclaimed


def _discover_active_log_paths() -> set[Path]:
    active_paths: set[Path] = set()

    for candidate_logger in _iter_loggers():
        for handler in candidate_logger.handlers:
            if not isinstance(handler, logging.FileHandler):
                continue
            base_filename = getattr(handler, "baseFilename", None)
            if not base_filename:
                continue
            active_paths.add(_normalize_path(Path(base_filename)))

    return active_paths


def _iter_loggers() -> list[logging.Logger]:
    root_logger = logging.getLogger()
    loggers: list[logging.Logger] = [root_logger]

    for candidate in root_logger.manager.loggerDict.values():
        if isinstance(candidate, logging.Logger):
            loggers.append(candidate)

    return loggers


def _is_log_file(path: Path) -> bool:
    suffixes = {suffix.lower() for suffix in path.suffixes}
    if {".log", ".jsonl", ".txt", ".ndjson", ".out"} & suffixes:
        return True
    name = path.name.lower()
    return ".log." in name


def _normalize_path(path: Path) -> Path:
    normalized = path.resolve(strict=False)
    return Path(os.path.normcase(str(normalized)))


def _unlink_file(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False


__all__ = ["LogCleanupResult", "cleanup_startup_logs"]
