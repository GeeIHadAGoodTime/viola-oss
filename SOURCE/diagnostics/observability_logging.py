from __future__ import annotations

"""
Structured logging bootstrap for NOVVIOLA.

This module centralizes stdlib logging configuration so entry points can enable
reproducible, machine-readable telemetry across the app without importing
third-party logger singletons.
"""

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class RepeatCollapseFilter(logging.Filter):
    """Keep a line that repeats from eating the log, and keep its count.

    One repeating line pushed everything else out of the retained logs: on the
    1.0.5 build-9 laptop, three 49 MB files held about a day, most of it two
    lines. Each kind of line (logger, level and text with the numbers taken
    out) gets a budget per minute and per hour. Past that, its lines are
    counted instead of written, and one summary line says how many were left
    out, so the evidence that it was happening, and how much, survives.

    Errors get a much larger budget: they are what the log is for.
    """

    PER_MINUTE = 12
    PER_HOUR = 120
    ERROR_PER_MINUTE = 60
    ERROR_PER_HOUR = 600
    SUMMARY_EVERY_SECONDS = 300.0
    SWEEP_EVERY_SECONDS = 15.0
    MAX_TRACKED = 2000
    _NUMBERS = re.compile(r"\d+")

    def __init__(self, handler: logging.Handler, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self._handler = handler
        self._clock = clock
        self._lock = threading.Lock()
        self._kinds: dict[tuple[str, int, str], _LineKind] = {}
        self._next_sweep = 0.0

    @classmethod
    def _key(cls, record: logging.LogRecord) -> tuple[str, int, str]:
        return record.name, record.levelno, cls._NUMBERS.sub("#", str(record.msg))[:120]

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "viola_repeat_summary", False):
            return True
        now = self._clock()
        with self._lock:
            allowed = self._allow(record, now)
            due = self._take_due_summaries(now)
        for summary in due:
            self._handler.handle(summary)
        return allowed

    def _allow(self, record: logging.LogRecord, now: float) -> bool:
        key = self._key(record)
        kind = self._kinds.get(key)
        if kind is None:
            if len(self._kinds) >= self.MAX_TRACKED:
                self._kinds.clear()
            kind = self._kinds[key] = _LineKind(minute_start=now, hour_start=now, summary_due=now)
        if now - kind.minute_start >= 60.0:
            kind.minute_start, kind.in_minute = now, 0
        if now - kind.hour_start >= 3600.0:
            kind.hour_start, kind.in_hour = now, 0
        is_error = record.levelno >= logging.ERROR
        per_minute = self.ERROR_PER_MINUTE if is_error else self.PER_MINUTE
        per_hour = self.ERROR_PER_HOUR if is_error else self.PER_HOUR
        if kind.in_minute < per_minute and kind.in_hour < per_hour:
            kind.in_minute += 1
            kind.in_hour += 1
            return True
        if kind.suppressed == 0:
            kind.sample = record
            kind.suppressed_since = now
            kind.summary_due = max(kind.summary_due, now + 60.0)
        kind.suppressed += 1
        return False

    def _take_due_summaries(self, now: float) -> list[logging.LogRecord]:
        if now < self._next_sweep:
            return []
        self._next_sweep = now + self.SWEEP_EVERY_SECONDS
        due = []
        for kind in self._kinds.values():
            if kind.suppressed and kind.sample is not None and now >= kind.summary_due:
                due.append(self._summary(kind, now))
                kind.suppressed = 0
                kind.sample = None
                kind.summary_due = now + self.SUMMARY_EVERY_SECONDS
        return due

    @staticmethod
    def _summary(kind: _LineKind, now: float) -> logging.LogRecord:
        sample = kind.sample
        assert sample is not None
        try:
            text = sample.getMessage()
        except Exception:  # noqa: BLE001, RUF100 - a bad format string must not break logging
            text = str(sample.msg)
        summary = logging.LogRecord(
            sample.name,
            sample.levelno,
            sample.pathname,
            sample.lineno,
            "%s [%d more like this in the last %ds]",
            (text[:300], kind.suppressed, int(now - kind.suppressed_since)),
            None,
            func=sample.funcName,
        )
        summary.viola_repeat_summary = True
        return summary


@dataclass
class _LineKind:
    minute_start: float
    hour_start: float
    summary_due: float
    in_minute: int = 0
    in_hour: int = 0
    suppressed: int = 0
    suppressed_since: float = 0.0
    sample: logging.LogRecord | None = None


class WindowsSafeRotatingFileHandler(RotatingFileHandler):
    """
    A RotatingFileHandler that gracefully handles Windows file locking errors.

    On Windows, files cannot be renamed while open by another process.
    This handler catches PermissionError during rotation and continues
    logging to the current file instead of crashing.
    """

    def doRollover(self) -> None:
        """Override to catch Windows PermissionError during rotation."""
        try:
            super().doRollover()
        except PermissionError:
            # On Windows, file may be locked by another process/handler.
            # Skip rotation and continue logging to the current file.
            # This is safe because we use dated filenames anyway.
            pass
        except OSError as e:
            # Handle other OS errors gracefully (e.g., disk full, permissions)
            if e.errno in (13, 32):  # Permission denied, file in use
                pass
            else:
                raise


_STD_RECORD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
}

_CONFIGURED = False
_CONFIG: ObservabilityConfig | None = None


@dataclass(slots=True)
class ObservabilityConfig:
    """Snapshot of the active observability configuration."""

    log_root: Path
    structured_path: Path | None
    ai_debug_path: Path | None
    console_level: str
    rotation: str
    retention: str
    enqueue: bool
    telemetry_enabled: bool

    def as_dict(self) -> dict[str, str]:
        return {
            "log_root": str(self.log_root),
            "structured_path": (str(self.structured_path) if self.structured_path else ""),
            "ai_debug_path": str(self.ai_debug_path) if self.ai_debug_path else "",
            "console_level": self.console_level,
            "rotation": self.rotation,
            "retention": self.retention,
            "enqueue": str(self.enqueue),
            "telemetry_enabled": str(self.telemetry_enabled),
        }


def _safe_json(value: Any) -> Any:
    value = _redact_json_value(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return _redact_log_text(repr(value))


def _redact_log_text(value: str) -> str:
    redacted = value
    try:
        from intent.log_redaction import redact_diagnostic_payload
    except ImportError:
        pass
    else:
        redacted = str(redact_diagnostic_payload(redacted))
    try:
        from core.secrets_mask import mask_secrets_in_text

        return mask_secrets_in_text(redacted)
    except ImportError:
        return redacted


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_log_text(value)
    if isinstance(value, dict):
        try:
            from core.secrets_mask import mask_dict_secrets

            value = mask_dict_secrets(dict(value))
        except ImportError:
            pass
        return {str(k): _redact_json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_json_value(item) for item in value]
    return value


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_log_text(super().format(record))


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_log_text(record.getMessage()),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_RECORD_FIELDS}
        if extras:
            payload["extra"] = _safe_json(extras)
        if record.exc_info:
            payload["exception"] = _redact_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=True)


_AI_DEBUG_MODULES = {
    "embed",
    "youtube",
    "webview",
    "music_player",
    "backend_manager",
    "state_applier",
    "controls_widget",
    "event_handler",
}

_AI_DEBUG_EXCLUDE_LOGGERS = {"uvicorn.access"}
_AI_DEBUG_EXCLUDE_PREFIXES = ("httpcore", "httpx", "supervisor", "voice.")
_AI_DEBUG_EXCLUDE_PATTERNS = (
    "heartbeat",
    "health",
    "wake word",
    "wake_word",
    "wake accepted",
    "entering listening",
    "accepted wake",
    "wake recorded",
    "activating voice pipeline",
)


class AIDebugFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        name = record.name
        message_lower = message.lower()

        for pattern in _AI_DEBUG_EXCLUDE_PATTERNS:
            if pattern in message_lower:
                return False
        if name in _AI_DEBUG_EXCLUDE_LOGGERS:
            return False
        if name.startswith(_AI_DEBUG_EXCLUDE_PREFIXES):
            return False

        if record.levelno >= logging.WARNING:
            return True
        if message.startswith("UI:"):
            return True
        for module in _AI_DEBUG_MODULES:
            if module in name:
                return True
        return False


_SIZE_RE = re.compile(r"^(?P<num>\\d+(?:\\.\\d+)?)\\s*(?P<unit>kb|mb|gb|b)?$", re.IGNORECASE)


def _parse_size(value: str) -> int:
    raw = value.strip().lower().replace(" ", "")
    match = _SIZE_RE.match(raw)
    if not match:
        return 10 * 1024 * 1024
    num = float(match.group("num"))
    unit = (match.group("unit") or "b").lower()
    scale = {"b": 1, "kb": 1024, "mb": 1024 * 1024, "gb": 1024 * 1024 * 1024}[unit]
    return int(num * scale)


def _parse_retention(value: str) -> int:
    raw = value.strip().lower()
    match = re.match(r"^(\\d+)\\s*(day|days|d)$", raw)
    if match:
        return int(match.group(1))
    match = re.match(r"^(\\d+)\\s*(file|files)$", raw)
    if match:
        return int(match.group(1))
    return 7


def _default_log_root() -> Path:
    # PHONE-17 follow-up: route through get_logs_dir() so the resolver
    # honors VIOLA_LOG_DIR (deploy override) and cascades through
    # VIOLA_DATA_DIR when set. Previously hardcoded <project>/logs,
    # which on the cloud Docker container resolves to /app/logs — a
    # read-only-fs path. Caused "BOOTSTRAP: Failed to configure
    # observability: [Errno 30] Read-only file system: '/app/logs'"
    # at startup and a fallback to basic console-only logging.
    from core.platform import get_logs_dir

    return get_logs_dir()


def ensure_log_directories(log_root: Path | None = None) -> dict[str, Path]:
    if log_root is None:
        log_root = _default_log_root()
    log_root = log_root.resolve()
    structured_dir = log_root / "structured"
    debug_dir = log_root / "debug_events"
    artifacts_dir = log_root / "artifacts"
    ai_debug_dir = log_root / "ai_debug"

    for directory in (log_root, structured_dir, debug_dir, artifacts_dir, ai_debug_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return {
        "root": log_root,
        "structured": structured_dir,
        "debug_events": debug_dir,
        "artifacts": artifacts_dir,
        "ai_debug": ai_debug_dir,
    }


def configure_observability(
    *,
    log_root: Path | None = None,
    console_level: str = "INFO",
    rotation: str = "10 MB",
    retention: str = "7 days",
    enqueue: bool = True,
    structured_filename: str | None = None,
    telemetry_enabled: bool = True,
) -> ObservabilityConfig:
    global _CONFIGURED, _CONFIG

    if _CONFIGURED and _CONFIG is not None:
        return _CONFIG

    directories = ensure_log_directories(log_root)
    structured_dir: Path = directories["structured"]

    if not structured_filename:
        timestamp = datetime.now(UTC).strftime("%Y%m%d")
        structured_filename = f"viola-{timestamp}.jsonl"

    # Wrap sys.stderr in a UTF-8 TextIOWrapper so that log messages
    # containing CJK characters, emoji, or other non-cp1252 codepoints
    # do not raise UnicodeEncodeError and crash the process on Windows.
    # On Linux/macOS or when stderr is already UTF-8 this is a no-op because
    # reconfigure() is called first and the wrapper detects matching encoding.
    _stderr = sys.stderr
    _reconfigure = getattr(_stderr, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    console_handler = logging.StreamHandler(_stderr)
    console_handler.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    console_handler.setFormatter(
        RedactingFormatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    handlers: list[logging.Handler] = [console_handler]

    structured_path: Path | None = None
    if telemetry_enabled:
        structured_path = structured_dir / structured_filename
        structured_handler = WindowsSafeRotatingFileHandler(
            structured_path,
            maxBytes=_parse_size(rotation),
            backupCount=_parse_retention(retention),
            encoding="utf-8",
            delay=True,  # Delay file opening until first write
        )
        structured_handler.setLevel(logging.DEBUG)
        structured_handler.setFormatter(JsonLineFormatter())
        structured_handler.addFilter(RepeatCollapseFilter(structured_handler))
        handlers.append(structured_handler)

    project_root = directories["root"].parent
    ai_debug_path = project_root / "scripts" / "ai_debug_session.log"
    ai_debug_path.parent.mkdir(parents=True, exist_ok=True)
    ai_debug_handler = logging.FileHandler(ai_debug_path, mode="w", encoding="utf-8")
    ai_debug_handler.setLevel(logging.DEBUG)
    ai_debug_handler.addFilter(AIDebugFilter())
    ai_debug_handler.setFormatter(RedactingFormatter(fmt="%(asctime)s | %(levelname)-8s | %(message)s"))
    handlers.append(ai_debug_handler)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    for handler in handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)

    # Keep env variable compatibility with prior implementation.
    # Lazy import to avoid circular dependency: config.settings imports core.logging_config
    try:
        from config.settings import settings

        if settings.no_color:
            pass
    except ImportError:
        # Settings not available yet (during bootstrap), skip no_color check
        pass

    # Suppress noisy loggers that produce high-volume DEBUG/INFO output.
    # Only WARNING+ will pass through for these loggers.
    _NOISY_LOGGERS = {
        # Heartbeat OK messages (~10/sec)
        "viola.diagnostics.bus": logging.WARNING,
        # Health probe polling (~every 2-3s)
        "viola.ui.qt_native.startup_controller": logging.WARNING,
        # Device discovery repeats (~every 3s)
        "viola.services.multiroom.discovery": logging.INFO,
        # Hub state reinitialization (~every 3s)
        "viola.core.hub_state_authority": logging.WARNING,
        # Wake policy poller (~every 1s)
        "viola.core.voice_orchestrator_wake": logging.WARNING,
        # Request tracing: every GET /health logged at INFO
        "viola.utils.enhancements.request_tracing": logging.WARNING,
        # ProcTap retry spam during idle
        "viola.audio_core.capture.proctap_provider": logging.WARNING,
        "viola.audio_core.capture._proctap_subprocess": logging.WARNING,
        # OpenAI SDK debug logging: _base_client.py:486 serialises entire
        # request payloads (50K+ chars for browser screenshots) into a
        # debug message.  AIDebugFilter.filter() materialises that string
        # via getMessage() then calls .lower(), doubling allocations.
        # On long agent runs with Playwright snapshots this causes
        # MemoryError / heap fragmentation.  Suppress at WARNING+.
        "openai._base_client": logging.WARNING,
        "openai": logging.WARNING,
        # Third-party noise: numba JIT, asyncio internals, HTTP client
        "numba": logging.WARNING,
        "numba.core.byteflow": logging.WARNING,
        "numba.core.ssa": logging.WARNING,
        "numba.core.interpreter": logging.WARNING,
        "asyncio": logging.WARNING,
        "httpx": logging.WARNING,
        "httpcore": logging.WARNING,
    }
    for logger_name, level in _NOISY_LOGGERS.items():
        logging.getLogger(logger_name).setLevel(level)

    _CONFIG = ObservabilityConfig(
        log_root=directories["root"],
        structured_path=structured_path,
        ai_debug_path=ai_debug_path,
        console_level=console_level,
        rotation=rotation,
        retention=retention,
        enqueue=enqueue,
        telemetry_enabled=telemetry_enabled,
    )
    _CONFIGURED = True

    # Run log retention cleanup on startup
    _cleanup_old_logs(directories)

    return _CONFIG


def get_observability_config() -> ObservabilityConfig | None:
    return _CONFIG


def _cleanup_old_logs(directories: dict[str, Path]) -> None:
    """Remove stale log files on startup to prevent unbounded disk growth.

    - JSONL files in logs/structured/ older than 14 days
    - Agent task JSON files in logs/agent_tasks/ older than 30 days
    """
    import time as _time

    now = _time.time()
    retention_logger = logging.getLogger("viola.log_retention")

    # JSONL retention: 14 days
    structured_dir = directories.get("structured")
    if structured_dir and structured_dir.is_dir():
        max_age = 14 * 86400
        removed = 0
        for f in structured_dir.glob("viola-*.jsonl*"):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            retention_logger.info("Log retention: removed %d JSONL files older than 14 days", removed)

    # Agent tasks retention: 30 days
    agent_dir = directories["root"] / "agent_tasks"
    if agent_dir.is_dir():
        max_age = 30 * 86400
        removed = 0
        for f in agent_dir.glob("*.json"):
            try:
                if now - f.stat().st_mtime > max_age:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            retention_logger.info(
                "Log retention: removed %d agent task files older than 30 days",
                removed,
            )


__all__ = [
    "ObservabilityConfig",
    "configure_observability",
    "ensure_log_directories",
    "get_observability_config",
]
