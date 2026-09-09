"""
Diagnostics Collection Tool

Collects all diagnostics information into a single bundle for support.
Creates a zip file with logs, config, crash dumps, debug trace, etc.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from core.logging_config import get_logger
from core.platform import get_logs_dir, get_project_root
from utils.debug.ring_buffer import get_debug_ring_buffer

from .self_check import SelfCheck

logger = get_logger(__name__)


class DiagnosticsCollector:
    """
    Collect diagnostics for support

    Features:
    - Collect logs (redacted)
    - Config snapshot (secrets excluded)
    - Crash dumps
    - Debug trace
    - Self-check report
    - Package as single zip file
    """

    def __init__(self):
        """Initialize diagnostics collector"""
        self.logs_dir = get_logs_dir()
        self.dumps_dir = get_logs_dir() / "crash_dumps"
        self.config_dir = get_project_root() / "config"

    def collect(self, output_path: Path | None = None) -> Path:
        """
        Collect diagnostics and create zip file

        Args:
            output_path: Output file path (default: diagnostics_TIMESTAMP.zip)

        Returns:
            Path to created zip file
        """
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = Path(f"diagnostics_{timestamp}.zip")

        logger.info("📦 Collecting diagnostics to: %s", output_path)

        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            # Add self-check report first (as summary)
            self._add_self_check(zipf)

            # Add logs (redacted)
            self._add_logs(zipf)

            # Add config snapshot (redacted)
            self._add_config(zipf)

            # Add crash dumps
            self._add_crash_dumps(zipf)

            # Add debug trace
            self._add_debug_trace(zipf)

            # Add metadata
            self._add_metadata(zipf)

        logger.info("✅ Diagnostics bundle created: %s", output_path.absolute())
        return output_path

    def _add_self_check(self, zipf: zipfile.ZipFile):
        """Add self-check report"""
        try:
            checker = SelfCheck()
            results = checker.run_all_checks()

            report = {
                "timestamp": datetime.now().isoformat(),
                "checks": [result.to_dict() for result in results],
                "summary": {
                    "total": len(results),
                    "ok": sum(1 for r in results if r.status.value == "ok"),
                    "warnings": sum(1 for r in results if r.status.value == "warning"),
                    "errors": sum(1 for r in results if r.status.value == "error"),
                },
            }

            report_content = json.dumps(report, indent=2)
            zipf.writestr("self_check_report.json", report_content)

        except Exception as e:
            logger.warning("Failed to add self-check report: %s", e)

    def _add_logs(self, zipf: zipfile.ZipFile):
        """Add logs (redacted)"""
        if not self.logs_dir.exists():
            return

        try:
            # Find recent log files
            log_files = list(self.logs_dir.glob("*.log"))
            log_files += list(self.logs_dir.glob("**/*.log"))

            # Limit to most recent 10 files
            log_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            log_files = log_files[:10]

            for log_file in log_files:
                try:
                    # Read and redact
                    content = self._redact_log_content(log_file.read_text(encoding="utf-8", errors="ignore"))
                    zipf.writestr(f"logs/{log_file.name}", content)
                except Exception as e:
                    logger.warning("Failed to add log %s: %s", log_file, e)

        except Exception as e:
            logger.warning("Failed to add logs: %s", e)

    def _add_config(self, zipf: zipfile.ZipFile):
        """Add config snapshot (secrets excluded)"""
        try:
            from config.facade import SettingsFacade

            settings = SettingsFacade()
            config_dict = settings.model_dump_sanitized() if hasattr(settings, "model_dump_sanitized") else {}

            content = json.dumps(config_dict, indent=2)
            zipf.writestr("config_snapshot.json", content)

        except Exception as e:
            logger.warning("Failed to add config snapshot: %s", e)

    def _add_crash_dumps(self, zipf: zipfile.ZipFile):
        """Add crash dumps"""
        if not self.dumps_dir.exists():
            return

        try:
            dump_files = list(self.dumps_dir.glob("*.json"))

            # Limit to most recent 5
            dump_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            dump_files = dump_files[:5]

            for dump_file in dump_files:
                try:
                    zipf.write(dump_file, f"crash_dumps/{dump_file.name}")
                except Exception as e:
                    logger.warning("Failed to add crash dump %s: %s", dump_file, e)

        except Exception as e:
            logger.warning("Failed to add crash dumps: %s", e)

    def _add_debug_trace(self, zipf: zipfile.ZipFile):
        """Add debug trace"""
        try:
            ring_buffer = get_debug_ring_buffer()

            trace = ring_buffer.export()
            stats = ring_buffer.stats()

            content = json.dumps({"stats": stats, "events": trace}, indent=2)

            zipf.writestr("debug_trace.json", content)

        except Exception as e:
            logger.warning("Failed to add debug trace: %s", e)

    def _add_metadata(self, zipf: zipfile.ZipFile):
        """Add metadata about collection"""
        import platform
        import sys

        metadata = {
            "collection_time": datetime.now().isoformat(),
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
            },
            "python": {
                "version": sys.version,
                "executable": sys.executable,
            },
        }

        content = json.dumps(metadata, indent=2)
        zipf.writestr("metadata.json", content)

    def _redact_log_content(self, content: str) -> str:
        """Redact sensitive information from log content"""
        import re

        patterns = [
            (
                r'api[_-]?key["\']?\s*[:=]\s*["\']?([^"\'\s]+)',
                r'api_key="***REDACTED***"',
            ),
            (
                r'password["\']?\s*[:=]\s*["\']?([^"\'\s]+)',
                r'password="***REDACTED***"',
            ),
            (r'token["\']?\s*[:=]\s*["\']?([^"\'\s]+)', r'token="***REDACTED***"'),
            (r"sk-[a-zA-Z0-9]{20,}", r"sk-***REDACTED***"),
        ]

        redacted = content
        for pattern, replacement in patterns:
            redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)

        return redacted
