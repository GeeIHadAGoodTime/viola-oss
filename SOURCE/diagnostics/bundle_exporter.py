"""
diagnostics/bundle_exporter.py
==============================

Utility for exporting a compact diagnostics bundle containing recent logs,
runtime failures, queue snapshots, hardware metadata, and a scrubbed settings
summary. Designed for user-facing support flows and nightly CI captures.
"""

from __future__ import annotations

import json
import platform
import re
import time
import types
import zipfile
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from config.facade import SettingsFacade
from core.logging_config import get_logger
from core.platform import get_logs_dir, get_project_root, get_temp_dir
from core.secrets_mask import mask_secrets_in_text
from diagnostics.runtime_metrics import RuntimeMetrics, get_runtime_metrics
from intent.log_redaction import redact_diagnostic_payload

logger = get_logger(__name__)

_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}]+)",
    re.IGNORECASE,
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)
_LOG_CANDIDATE_SUFFIXES = frozenset({".log", ".jsonl", ".err", ".out", ".trace", ".dump", ".stacktrace"})
_LOG_CANDIDATE_TEXT_SUFFIXES = frozenset({".txt", ".text", ""})
_LOG_CANDIDATE_NAME_MARKERS = ("log", "error", "err", "stderr", "stdout", "trace", "crash", "dump", "exception")
_ROTATED_LOG_NAME_RE = re.compile(r"(?:^|[._-])log(?:[._-]\d+)?$", re.IGNORECASE)

# Optional psutil support. Uses a module-typed variable to avoid type: ignore.
# psutil stubs exist (types-psutil) but we degrade gracefully when unavailable.
_psutil_module: types.ModuleType | None = None
_PSUTIL_AVAILABLE = False

try:  # Optional dependency; psutil may not be available on all platforms.
    import psutil as _imported_psutil

    _psutil_module = _imported_psutil
    _PSUTIL_AVAILABLE = True
except Exception:  # pragma: no cover - degrade gracefully without psutil
    pass


def _now_ts() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _redact_diagnostics_text(value: str) -> str:
    shared_redacted = redact_diagnostic_payload(value)
    redacted = shared_redacted if isinstance(shared_redacted, str) else str(shared_redacted)
    redacted = mask_secrets_in_text(redacted)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}****REDACTED****", redacted)


def _is_sensitive_field_name(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_FIELD_NAMES)


def _redact_diagnostics_payload(value: object) -> object:
    value = redact_diagnostic_payload(value)
    if isinstance(value, str):
        return _redact_diagnostics_text(value)
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, inner in value.items():
            str_key = str(key)
            if _is_sensitive_field_name(str_key):
                redacted[str_key] = "****REDACTED****"
            else:
                redacted[str_key] = _redact_diagnostics_payload(inner)
        return redacted
    if isinstance(value, list):
        return [_redact_diagnostics_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_diagnostics_payload(item) for item in value]
    return value


def _looks_like_log_file(path: Path) -> bool:
    suffix = path.suffix.lower()
    name = path.name.lower()
    stem = path.stem.lower()
    if suffix in _LOG_CANDIDATE_SUFFIXES:
        return True
    if ".log." in name or _ROTATED_LOG_NAME_RE.search(name):
        return True
    return suffix in _LOG_CANDIDATE_TEXT_SUFFIXES and any(marker in stem for marker in _LOG_CANDIDATE_NAME_MARKERS)


class DiagnosticsBundleExporter:
    """
    Exporter that aggregates core diagnostics artifacts into a bounded zip bundle.
    """

    def __init__(
        self,
        *,
        logs_dir: Path | None = None,
        output_dir: Path | None = None,
        runtime_metrics_factory: Callable[[], RuntimeMetrics] = get_runtime_metrics,
        settings_factory: Callable[[], SettingsFacade] = SettingsFacade,
        max_logs: int = 5,
        log_tail_bytes: int = 512 * 1024,
        bundle_size_limit_mb: float = 25.0,
    ) -> None:
        self._logs_dir = logs_dir or get_logs_dir()
        self._output_dir = output_dir or get_project_root() / "reports" / "diagnostics"
        self._runtime_metrics_factory = runtime_metrics_factory
        self._settings_factory = settings_factory
        self._max_logs = max(1, max_logs)
        self._log_tail_bytes = max(32 * 1024, log_tail_bytes)
        self._bundle_size_limit_bytes = int(bundle_size_limit_mb * 1024 * 1024)

    # ------------------------------------------------------------------ public
    def export(self, *, output_path: Path | None = None) -> Path:
        """
        Create a diagnostics bundle and return the output zip path.
        """
        start = time.perf_counter()
        if output_path is None:
            self._output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
            output_path = self._output_dir / f"diagnostics-bundle-{timestamp}.zip"

        temp_root = get_temp_dir() / "diagnostics_bundle"
        temp_root.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="diagnostics_bundle_", dir=str(temp_root)) as tmp_dir:
            tmp_path = Path(tmp_dir)
            self._collect_logs(tmp_path / "logs")
            runtime_snapshot = self._collect_runtime(tmp_path / "runtime")
            self._collect_failure_data(tmp_path / "runtime", runtime_snapshot)
            self._collect_settings(tmp_path / "config")
            self._collect_hardware(tmp_path / "system")
            self._write_metadata(tmp_path / "metadata.json")

            self._write_zip(output_path, tmp_path)

        elapsed = time.perf_counter() - start
        size_mb = output_path.stat().st_size / (1024 * 1024)
        logger.info(
            "Diagnostics bundle created at %s (%.2f MB) in %.2fs",
            output_path,
            size_mb,
            elapsed,
        )
        if output_path.stat().st_size > self._bundle_size_limit_bytes:
            logger.warning(
                "Diagnostics bundle exceeds configured size limit of %.2f MB (actual %.2f MB)",
                self._bundle_size_limit_bytes / (1024 * 1024),
                size_mb,
            )
        return output_path

    # ----------------------------------------------------------------- helpers
    def _collect_logs(self, dest_dir: Path) -> None:
        if not self._logs_dir.exists():
            logger.debug("Logs directory %s missing; skipping log collection", self._logs_dir)
            return

        dest_dir.mkdir(parents=True, exist_ok=True)
        log_files = list(self._iter_candidate_log_files())
        if not log_files:
            logger.debug("No log files found under %s", self._logs_dir)
            return

        log_files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for source in log_files[: self._max_logs]:
            try:
                tail = redact_diagnostic_payload(self._read_tail(source, self._log_tail_bytes))
                safe_name = self._bundle_log_name(source)
                tail_text = tail if isinstance(tail, str) else str(tail)
                tail = _redact_diagnostics_text(tail_text)
                (dest_dir / f"{safe_name}.tail.log").write_text(tail, encoding="utf-8")
            except Exception:  # pragma: no cover - defensive logging
                logger.debug("Failed to capture log tail for %s", source, exc_info=True)

    def _collect_runtime(self, dest_dir: Path) -> dict[str, object]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        metrics = self._runtime_metrics_factory()
        snapshot = metrics.snapshot()
        runtime_payload = {
            "generated_at": snapshot.get("generated_at"),
            "process": snapshot.get("process"),
            "queue": snapshot.get("queue"),
            "music": snapshot.get("music"),
            "heartbeats": snapshot.get("heartbeats"),
        }
        self._write_json(dest_dir / "runtime_snapshot.json", runtime_payload)
        return snapshot

    def _collect_failure_data(self, dest_dir: Path, snapshot: dict[str, object]) -> None:
        failures = snapshot.get("failures") or []
        self._write_json(dest_dir / "failures.json", failures)

        queue = snapshot.get("queue") or {}
        self._write_json(dest_dir / "queue_state.json", queue)

        music = snapshot.get("music") or {}
        self._write_json(dest_dir / "music_stats.json", music)

    def _collect_settings(self, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            settings = self._settings_factory()
            if hasattr(settings, "model_dump_sanitized"):
                payload = settings.model_dump_sanitized()
            elif hasattr(settings, "to_public_dict"):
                payload = settings.to_public_dict()
            else:
                payload = {}
            self._write_json(dest_dir / "settings_sanitized.json", payload)
        except Exception as e:  # pragma: no cover - defensive logging
            logger.warning("Failed to capture sanitized settings snapshot: %s", e)

    def _collect_hardware(self, dest_dir: Path) -> None:
        dest_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "collected_at": _now_ts(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "processor": platform.processor(),
            },
        }

        if _PSUTIL_AVAILABLE and _psutil_module is not None:
            try:
                info["hardware"] = {
                    "cpu_count": _psutil_module.cpu_count(logical=True),
                    "cpu_physical_cores": _psutil_module.cpu_count(logical=False),
                    "cpu_frequency_mhz": (
                        getattr(_psutil_module.cpu_freq(), "current", None)
                        if hasattr(_psutil_module, "cpu_freq")
                        else None
                    ),
                    "memory_total_mb": round(_psutil_module.virtual_memory().total / (1024 * 1024), 2),
                    "memory_available_mb": round(_psutil_module.virtual_memory().available / (1024 * 1024), 2),
                    "disk_usage": self._summarise_disks(),
                }
            except Exception:  # pragma: no cover - psutil edge cases
                logger.debug("Failed to capture psutil hardware snapshot", exc_info=True)
        self._write_json(dest_dir / "hardware.json", info)

    def _write_metadata(self, path: Path) -> None:
        payload = {
            "generated_at": _now_ts(),
            "bundle_spec": "v1",
            "logs_dir_name": self._logs_dir.name,
        }
        self._write_json(path, payload)

    def _write_zip(self, output_path: Path, source_dir: Path) -> None:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in self._iter_files(source_dir):
                archive.write(file_path, file_path.relative_to(source_dir))

    # --------------------------------------------------------------- utilities
    @staticmethod
    def _write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = redact_diagnostic_payload(payload)
        safe_payload = _redact_diagnostics_payload(safe_payload)
        path.write_text(json.dumps(safe_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _iter_files(root: Path) -> Iterable[Path]:
        for entry in root.rglob("*"):
            if entry.is_file():
                yield entry

    def _iter_candidate_log_files(self) -> Iterable[Path]:
        try:
            root = self._logs_dir.resolve()
        except OSError:
            logger.debug("Failed to resolve logs directory %s", self._logs_dir, exc_info=True)
            return

        seen: set[Path] = set()
        for entry in self._logs_dir.rglob("*"):
            try:
                if entry.is_symlink() or not entry.is_file():
                    continue
                resolved = entry.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                logger.debug("Skipping diagnostics log candidate outside logs root: %s", entry, exc_info=True)
                continue
            if resolved in seen or not _looks_like_log_file(entry):
                continue
            seen.add(resolved)
            yield entry

    def _bundle_log_name(self, source: Path) -> str:
        try:
            relative = source.relative_to(self._logs_dir)
        except ValueError:
            relative = Path(source.name)
        name = relative.as_posix()
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)

    @staticmethod
    def _read_tail(path: Path, max_bytes: int) -> str:
        data = path.read_bytes()
        if len(data) <= max_bytes:
            return data.decode("utf-8", errors="replace")
        return data[-max_bytes:].decode("utf-8", errors="replace")

    @staticmethod
    def _summarise_disks() -> Sequence[dict[str, object]]:
        if not _PSUTIL_AVAILABLE or _psutil_module is None:
            return []
        usage = []
        for part in _psutil_module.disk_partitions():
            try:
                stats = _psutil_module.disk_usage(part.mountpoint)
            except Exception as e:
                logger.debug("Skipping disk partition %s: %s", part.mountpoint, e)
                continue
            usage.append(
                {
                    "mount": part.mountpoint,
                    "filesystem": part.fstype,
                    "total_mb": round(stats.total / (1024 * 1024), 2),
                    "used_mb": round(stats.used / (1024 * 1024), 2),
                    "free_mb": round(stats.free / (1024 * 1024), 2),
                    "percent": stats.percent,
                }
            )
        return usage


__all__ = ["DiagnosticsBundleExporter"]
