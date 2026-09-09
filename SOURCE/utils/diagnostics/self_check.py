"""
Self-Check Diagnostic Utility

Automated health checks for all Viola components.
Used for diagnostics and troubleshooting.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class CheckStatus(Enum):
    """Check result status"""

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class CheckResult:
    """Health check result"""

    name: str
    status: CheckStatus
    message: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary"""
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details or {},
        }


class SelfCheck:
    """
    Self-check utility for diagnostics

    Runs comprehensive health checks on all Viola components
    """

    def __init__(self):
        """Initialize self-check"""
        self.checks: list[CheckResult] = []

    def run_all_checks(self) -> list[CheckResult]:
        """
        Run all health checks

        Returns:
            List of CheckResult objects
        """
        self.checks = []

        logger.info("🔍 Running self-check diagnostics...")

        # Component health checks
        self.checks.append(self.check_backend())
        self.checks.append(self.check_asr())
        self.checks.append(self.check_tts())
        self.checks.append(self.check_music_player())
        self.checks.append(self.check_ui_server())

        # Configuration validation
        self.checks.append(self.check_config())

        # Dependency verification
        self.checks.append(self.check_dependencies())

        # Resource usage
        self.checks.append(self.check_resources())

        # File system
        self.checks.append(self.check_filesystem())

        # Summary
        total = len(self.checks)
        ok = sum(1 for c in self.checks if c.status == CheckStatus.OK)
        warnings = sum(1 for c in self.checks if c.status == CheckStatus.WARNING)
        errors = sum(1 for c in self.checks if c.status == CheckStatus.ERROR)

        logger.info(
            "✅ Self-check complete: %s/%s OK, %s warnings, %s errors",
            ok,
            total,
            warnings,
            errors,
        )

        return self.checks

    def check_backend(self) -> CheckResult:
        """Check backend LLM provider health."""
        try:
            from services.llm.factory import create_provider_from_settings

            provider = create_provider_from_settings()

            if provider is None:
                return CheckResult(
                    name="backend",
                    status=CheckStatus.WARNING,
                    message="No LLM provider configured",
                    details={"provider": None},
                )

            provider_name = (
                provider.get_provider_name() if hasattr(provider, "get_provider_name") else type(provider).__name__
            )
            available = provider.is_available() if hasattr(provider, "is_available") else True
            if not available:
                return CheckResult(
                    name="backend",
                    status=CheckStatus.WARNING,
                    message="LLM provider configured but unavailable",
                    details={"provider": provider_name},
                )
            return CheckResult(
                name="backend",
                status=CheckStatus.OK,
                message="LLM provider ready",
                details={"provider": provider_name},
            )

        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="backend",
                status=CheckStatus.ERROR,
                message=f"LLM provider initialization failed: {e}",
                details={"error": str(e)},
            )

    def check_asr(self) -> CheckResult:
        """Check speech recognition (ASR) health"""
        try:
            # Try to import STT module
            import voice.transcription as stt

            return CheckResult(
                name="asr",
                status=CheckStatus.OK,
                message="STT module available",
                details={"module_ready": True},
            )
        except ImportError:
            return CheckResult(
                name="asr",
                status=CheckStatus.WARNING,
                message="STT not available",
                details={"error": "ImportError"},
            )
        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="asr",
                status=CheckStatus.ERROR,
                message=f"ASR check failed: {e}",
                details={"error": str(e)},
            )

    def check_tts(self) -> CheckResult:
        """Check text-to-speech health"""
        try:
            import pyttsx3

            engine = pyttsx3.init()
            voices = engine.getProperty("voices")

            return CheckResult(
                name="tts",
                status=CheckStatus.OK,
                message="TTS engine ready",
                details={"voices_available": len(voices) if voices else 0},
            )

        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="tts",
                status=CheckStatus.ERROR,
                message=f"TTS check failed: {e}",
                details={"error": str(e)},
            )

    def check_music_player(self) -> CheckResult:
        """Check music player health"""
        try:
            import vlc

            instance = vlc.Instance()
            if instance is None:
                return CheckResult(
                    name="music_player",
                    status=CheckStatus.ERROR,
                    message="VLC instance creation failed",
                )

            return CheckResult(name="music_player", status=CheckStatus.OK, message="VLC player ready")

        except ImportError:
            return CheckResult(
                name="music_player",
                status=CheckStatus.ERROR,
                message="VLC not installed",
            )
        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="music_player",
                status=CheckStatus.ERROR,
                message=f"Music player check failed: {e}",
                details={"error": str(e)},
            )

    def check_ui_server(self) -> CheckResult:
        """Check UI server components"""
        try:
            from utils.monitoring import get_health_checker

            health = get_health_checker()
            results = health.check_all()

            is_healthy = results.get("status") == "healthy"

            return CheckResult(
                name="ui_server",
                status=CheckStatus.OK if is_healthy else CheckStatus.WARNING,
                message=f"UI server: {results.get('status', 'unknown')}",
                details={"checks": results.get("checks", {})},
            )

        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="ui_server",
                status=CheckStatus.ERROR,
                message=f"UI server check failed: {e}",
                details={"error": str(e)},
            )

    def check_config(self) -> CheckResult:
        """Check configuration validation"""
        try:
            from config.facade import SettingsFacade

            settings = SettingsFacade()
            if settings is None:
                return CheckResult(
                    name="config",
                    status=CheckStatus.ERROR,
                    message="Settings facade initialization failed",
                )

            return CheckResult(name="config", status=CheckStatus.OK, message="Configuration loaded")

        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="config",
                status=CheckStatus.ERROR,
                message=f"Configuration check failed: {e}",
                details={"error": str(e)},
            )

    def check_dependencies(self) -> CheckResult:
        """Check critical dependencies"""
        dependencies = {
            "PySide6": "PySide6",
            "loguru": "loguru",
            "openai": "openai",
            "fastapi": "fastapi",
            "vlc": "python-vlc",
            "pyttsx3": "pyttsx3",
            "keyring": "keyring",
            "cryptography": "cryptography",
        }

        missing = []
        present = []

        for name, module in dependencies.items():
            try:
                __import__(module)
                present.append(name)
            except ImportError:
                missing.append(name)

        if missing:
            return CheckResult(
                name="dependencies",
                status=CheckStatus.ERROR,
                message=f"Missing dependencies: {', '.join(missing)}",
                details={"missing": missing, "present": present},
            )

        return CheckResult(
            name="dependencies",
            status=CheckStatus.OK,
            message=f"All dependencies present ({len(present)})",
            details={"present": present},
        )

    def check_resources(self) -> CheckResult:
        """Check resource usage"""
        try:
            import psutil

            process = psutil.Process()

            cpu = process.cpu_percent(interval=0.1)
            memory = process.memory_info().rss / 1024 / 1024  # MB
            threads = process.num_threads()

            details = {
                "cpu_percent": round(cpu, 2),
                "memory_mb": round(memory, 2),
                "num_threads": threads,
            }

            # Check for excessive usage
            if memory > 1000:  # > 1GB
                status = CheckStatus.WARNING
                message = f"High memory usage: {memory:.0f}MB"
            elif cpu > 90:
                status = CheckStatus.WARNING
                message = f"High CPU usage: {cpu:.0f}%"
            else:
                status = CheckStatus.OK
                message = "Resource usage normal"

            return CheckResult(name="resources", status=status, message=message, details=details)

        except ImportError:
            return CheckResult(
                name="resources",
                status=CheckStatus.UNKNOWN,
                message="psutil not available for resource checks",
            )
        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="resources",
                status=CheckStatus.ERROR,
                message=f"Resource check failed: {e}",
                details={"error": str(e)},
            )

    def check_filesystem(self) -> CheckResult:
        """Check file system access"""
        try:
            from pathlib import Path

            # Check critical directories
            critical_dirs = ["logs", "config"]

            accessible = []
            missing = []

            for directory in critical_dirs:
                path = Path(directory)
                if path.exists():
                    accessible.append(directory)
                else:
                    try:
                        path.mkdir(exist_ok=True)
                    except Exception as e:
                        logger.debug(
                            "Failed to create directory %s: %s",
                            directory,
                            e,
                            exc_info=True,
                        )
                        missing.append(directory)
                    else:
                        accessible.append(directory)

            if missing:
                return CheckResult(
                    name="filesystem",
                    status=CheckStatus.WARNING,
                    message=f"Cannot access: {', '.join(missing)}",
                    details={"missing": missing, "accessible": accessible},
                )

            return CheckResult(
                name="filesystem",
                status=CheckStatus.OK,
                message="File system access OK",
                details={"accessible": accessible},
            )

        except Exception as e:
            logger.debug("Operation failed: %s", e, exc_info=True)
            return CheckResult(
                name="filesystem",
                status=CheckStatus.ERROR,
                message=f"File system check failed: {e}",
                details={"error": str(e)},
            )
