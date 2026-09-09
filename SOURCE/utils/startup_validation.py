"""
Startup Validation System
=========================

Comprehensive validation that runs at application startup to catch
configuration errors, missing dependencies, and invalid settings
immediately - "fix at first glance".

Usage:
    from utils.startup_validation import validate_startup

    # In main() or bootstrap()
    validate_startup()
    # If this doesn't raise, everything is OK to start
"""

from __future__ import annotations

import importlib
import os
import shutil
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from audio_core.portaudio_guard import portaudio_instance
from config import env
from core.constants import DEFAULT_API_PORT, LOCALHOST
from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger

logger = get_logger(__name__)

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    # Set UTF-8 encoding for stdout/stderr
    if hasattr(sys.stdout, "reconfigure"):
        try:
            stdout_reconfigure = getattr(sys.stdout, "reconfigure", None)
            stderr_reconfigure = getattr(sys.stderr, "reconfigure", None)
            if callable(stdout_reconfigure):
                stdout_reconfigure(encoding="utf-8")
            if callable(stderr_reconfigure):
                stderr_reconfigure(encoding="utf-8")
        except Exception as e:
            logger.debug("Failed to reconfigure console encoding: %s", e, exc_info=True)
            pass  # May fail in some contexts
    else:
        # Fallback for older Python versions
        try:
            import codecs

            sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "strict")
            sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "strict")
        except Exception as e:
            logger.debug("Failed to set UTF-8 encoding fallback: %s", e, exc_info=True)
            pass  # May fail in some contexts

ValidationError: type[Exception]
check_dependency: Callable[..., object]
validate: Callable[..., object]

try:
    from utils.failfast import (
        ValidationError as _ImportedValidationError,
        check_dependency as _imported_check_dependency,
        validate as _imported_validate,
    )
except ImportError:
    # Fallback if failfast not available
    class _FallbackValidationError(Exception):
        pass

    def _fallback_check_dependency(
        module_name: str,
        package_name: str | None = None,
        context_file: str | None = None,
    ) -> None:
        """Fallback dependency check."""
        try:
            __import__(module_name)
        except ImportError as e:
            raise _FallbackValidationError(
                f"Missing dependency: {package_name or module_name}\n  Fix: pip install {package_name or module_name}"
            ) from e

    def _fallback_validate(value: object, expected_type: type, **kwargs: object) -> object:
        """Fallback validation."""
        if not isinstance(value, expected_type):
            name_obj = kwargs.get("name", "value")
            name = name_obj if isinstance(name_obj, str) else "value"
            raise _FallbackValidationError(f"{name} must be {expected_type.__name__}, got {type(value).__name__}")
        return value

    ValidationError = _FallbackValidationError
    check_dependency = _fallback_check_dependency
    validate = _fallback_validate
else:
    ValidationError = _ImportedValidationError
    check_dependency = _imported_check_dependency
    validate = _imported_validate

from utils.dependency_manager import (
    DependencyInstallationError,
    ensure_voice_dependencies,
)


class StartupValidationError(Exception):
    """Raised when startup validation fails. Contains actionable error messages."""

    pass


class StartupIssue(TypedDict):
    category: str
    error: str
    fix: str
    severity: str
    location: str | None


class StartupValidator:
    """Validates application prerequisites at startup."""

    def __init__(self, strict: bool = True):
        """
        Initialize validator.

        Args:
            strict: If True, fail on warnings. If False, only fail on errors.
        """
        self.strict = strict
        self.issues: list[StartupIssue] = []

    def add_issue(
        self,
        category: str,
        error: str,
        fix: str,
        severity: str = "error",
        location: str | None = None,
    ) -> None:
        """
        Add a validation issue.

        Args:
            category: Issue category (e.g., "dependency", "config", "permissions")
            error: Error description
            fix: How to fix it
            severity: "error" or "warning"
            location: File/module where issue occurs
        """
        self.issues.append(
            {
                "category": category,
                "error": error,
                "fix": fix,
                "severity": severity,
                "location": location,
            }
        )

    def check_python_version(self, min_version: tuple[int, int] = (3, 9)) -> None:
        """Check Python version."""
        if sys.version_info < min_version:
            self.add_issue(
                "prerequisites",
                f"Python {min_version[0]}.{min_version[1]}+ required, got {sys.version_info.major}.{sys.version_info.minor}",
                f"Upgrade Python to {min_version[0]}.{min_version[1]}+ (current: {sys.version})",
                location="startup",
            )

    def check_dependencies(self, required: list[tuple[str, str]] | None = None) -> None:
        """
        Check required dependencies.

        Args:
            required: List of (module_name, package_name) tuples. If None, checks common deps.
        """
        if required is None:
            required = [
                ("fastapi", "fastapi"),
                ("uvicorn", "uvicorn"),
                ("loguru", "loguru"),
            ]

        for module_name, package_name in required:
            try:
                check_dependency(module_name, package_name, "startup_validation.py")
            except ValidationError:
                self.add_issue(
                    "dependency",
                    f"Missing dependency: {package_name}",
                    f"pip install {package_name}",
                    location="requirements",
                )
            except Exception as e:
                logger.debug("Error checking dependency %s: %s", package_name, e, exc_info=True)
                self.add_issue(
                    "dependency",
                    f"Error checking {package_name}",
                    f"pip install {package_name}",
                    severity="warning",
                )

        try:
            missing_voice = ensure_voice_dependencies(auto_install=True, strict=False)
        except DependencyInstallationError as exc:
            for spec in exc.missing:
                self.add_issue(
                    "dependency",
                    f"Voice dependency install failed: {spec.import_name}",
                    "Check network access or install manually via 'pip install '" + " ".join(spec.packages),
                    location="voice pipeline",
                )
            missing_voice = []
        except Exception as exc:
            self.add_issue(
                "dependency",
                f"Voice dependency auto-install raised unexpected error: {exc}",
                "Check logs for details and install voice dependencies manually.",
                location="voice pipeline",
            )
            missing_voice = []

        for spec in missing_voice:
            self.add_issue(
                "dependency",
                f"Missing voice dependency '{spec.import_name}' ({', '.join(spec.packages)}). {spec.reason}",
                "pip install " + " ".join(spec.packages),
                location="voice pipeline",
            )

    def check_configuration(self) -> None:
        """Check configuration values."""
        try:
            from config import (
                ConfigurationError,
                get_settings,
                settings as exported_settings,
            )
        except ImportError as e:
            self.add_issue(
                "configuration",
                f"Failed to import config: {e}",
                "Check config/settings.py exists and is valid",
                location="config/settings.py",
            )
            return

        cfg = None
        if exported_settings is not None:
            cfg = exported_settings

        if cfg is None:
            try:
                cfg = get_settings()
            except ConfigurationError as exc:
                self.add_issue(
                    "configuration",
                    f"Configuration error: {exc}",
                    "Provide required environment variables or update the .env file.",
                    location="config/settings.py",
                )
                return

        # Check API port
        try:
            port = getattr(cfg, "api_port", None)
            if port is None:
                self.add_issue(
                    "configuration",
                    "api_port not set",
                    "Set VIOLA_API_PORT environment variable or configure in settings",
                    location="config/settings.py",
                )
            else:
                try:
                    validate(port, int, min=1, max=65535, name="api_port")
                except ValidationError as e:
                    self.add_issue(
                        "configuration",
                        f"Invalid api_port: {e}",
                        "Set VIOLA_API_PORT to a value between 1 and 65535",
                        location="config/settings.py",
                    )
        except Exception as e:
            logger.debug("Error validating api_port: %s", e, exc_info=True)
            self.add_issue(
                "configuration",
                "Error validating api_port",
                "Check config/settings.py",
                severity="warning",
            )

        # Check OpenAI API key (warning only - optional)
        api_key = getattr(cfg, "openai_api_key", None)
        if not api_key:
            self.add_issue(
                "configuration",
                "No OpenAI API key configured",
                "Set OPENAI_API_KEY environment variable (optional, but required for AI features)",
                severity="warning",
                location="config/settings.py",
            )
        elif not isinstance(api_key, str):
            self.add_issue(
                "configuration",
                "Invalid OpenAI API key type",
                "OPENAI_API_KEY must be a string",
                location="config/settings.py",
            )
        elif not api_key.startswith("sk-"):
            self.add_issue(
                "configuration",
                "OpenAI API key format looks invalid (should start with 'sk-')",
                "Check your OPENAI_API_KEY environment variable",
                severity="warning",
                location="config/settings.py",
            )

    def check_file_permissions(self) -> None:
        """Check file system permissions."""
        # Check log directory
        log_dir = env.get("VIOLA_LOG_DIR", "logs")
        log_path = Path(log_dir)

        try:
            # Try to create directory if it doesn't exist
            log_path.mkdir(parents=True, exist_ok=True)

            # Check if we can write to it
            test_file = log_path / ".write_test"
            try:
                test_file.write_text("test")
                test_file.unlink()
            except Exception as e:
                logger.debug("Cannot write to log directory %s: %s", log_dir, e, exc_info=True)
                self.add_issue(
                    "permissions",
                    f"Cannot write to log directory: {log_dir}",
                    f"Fix permissions: chmod +w {log_dir} or set VIOLA_LOG_DIR to a writable path",
                    location="logs/",
                )
        except Exception as e:
            logger.debug("Cannot create log directory %s: %s", log_dir, e, exc_info=True)
            self.add_issue(
                "permissions",
                f"Cannot create log directory: {log_dir}",
                f"Fix: mkdir -p {log_dir} && chmod +w {log_dir}",
                location="logs/",
            )

    def check_port_availability(self, port: int) -> None:
        """Check if port is available."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex((LOCALHOST, port))
            sock.close()

            if result == 0:
                self.add_issue(
                    "network",
                    f"Port {port} is already in use",
                    f"Kill process using port {port} or set VIOLA_API_PORT to a different port",
                    location="backend/runtime.py",
                )
        except Exception as e:
            # Port check failed - might be OK (firewall, etc.)
            self.add_issue(
                "network",
                f"Could not check port {port}: {e}",
                "Check if port is available manually",
                severity="warning",
                location="backend/runtime.py",
            )

    def check_module_imports(self, critical_modules: list[str] | None = None) -> None:
        """Check that critical modules can be imported."""
        if critical_modules is None:
            critical_modules = [
                "backend",
                "services.llm",
            ]

        for module_name in critical_modules:
            try:
                __import__(module_name)
            except ImportError as e:
                self.add_issue(
                    "imports",
                    f"Cannot import {module_name}: {e}",
                    f"Check {module_name}.py exists and dependencies are installed",
                    location=module_name,
                )
            except Exception as e:
                self.add_issue(
                    "imports",
                    f"Error importing {module_name}: {e}",
                    f"Check {module_name}.py for syntax errors or missing dependencies",
                    severity="error" if self.strict else "warning",
                    location=module_name,
                )

    def validate_all(self, check_port: bool = True, port: int = DEFAULT_API_PORT) -> None:
        """
        Run all validation checks.

        Args:
            check_port: If True, check if port is available
            port: Port number to check
        """
        logger.info("🔍 Running startup validation...")

        self.check_python_version()
        self.check_dependencies()
        self.check_configuration()
        self.check_file_permissions()
        self.check_system_resources()
        self.check_audio_devices()
        if check_port:
            self.check_port_availability(port)
        self.check_module_imports()

        # Report results
        errors = [i for i in self.issues if i["severity"] == "error"]
        warnings = [i for i in self.issues if i["severity"] == "warning"]

        if errors or (warnings and self.strict):
            error_msg = self._format_issues()

            if errors:
                logger.error("❌ Startup validation failed:\n%s", error_msg)
                raise StartupValidationError(error_msg)
            elif warnings:
                logger.warning("⚠️ Startup validation warnings:\n%s", error_msg)
                if self.strict:
                    raise StartupValidationError(error_msg)

        if warnings and not self.strict:
            logger.warning("⚠️ %s warning(s) (non-blocking):", len(warnings))
            for issue in warnings:
                logger.warning("  - %s", issue["error"])
                logger.warning("    Fix: %s", issue["fix"])
        else:
            logger.info("✅ Startup validation passed")

    def _format_issues(self) -> str:
        """Format issues into readable error message."""
        errors = [i for i in self.issues if i["severity"] == "error"]
        warnings = [i for i in self.issues if i["severity"] == "warning"]

        lines = []

        if errors:
            lines.append(f"\n❌ {len(errors)} Error(s):")
            for issue in errors:
                lines.append(f"\n  Category: {issue['category'].upper()}")
                lines.append(f"  Error: {issue['error']}")
                lines.append(f"  Fix: {issue['fix']}")
                if issue.get("location"):
                    lines.append(f"  Location: {issue['location']}")

        if warnings:
            lines.append(f"\n⚠️ {len(warnings)} Warning(s):")
            for issue in warnings:
                lines.append(f"\n  Category: {issue['category'].upper()}")
                lines.append(f"  Warning: {issue['error']}")
                lines.append(f"  Fix: {issue['fix']}")
                if issue.get("location"):
                    lines.append(f"  Location: {issue['location']}")

        return "\n".join(lines)

    def get_summary(self) -> JsonDict:
        """Get validation summary."""
        errors = [i for i in self.issues if i["severity"] == "error"]
        warnings = [i for i in self.issues if i["severity"] == "warning"]
        errors_payload: list[JsonValue] = [
            {
                "category": issue["category"],
                "error": issue["error"],
                "fix": issue["fix"],
                "severity": issue["severity"],
                "location": issue["location"],
            }
            for issue in errors
        ]
        warnings_payload: list[JsonValue] = [
            {
                "category": issue["category"],
                "error": issue["error"],
                "fix": issue["fix"],
                "severity": issue["severity"],
                "location": issue["location"],
            }
            for issue in warnings
        ]

        return {
            "passed": len(errors) == 0 and (len(warnings) == 0 or not self.strict),
            "error_count": len(errors),
            "warning_count": len(warnings),
            "errors": errors_payload,
            "warnings": warnings_payload,
        }

    def check_system_resources(
        self,
        *,
        min_disk_mb: int = 1024,
        min_memory_mb: int = 512,
    ) -> None:
        """
        Ensure the host has enough disk space and memory before starting.

        Args:
            min_disk_mb: Minimum free disk space (megabytes) required beneath VIOLA_DATA_DIR.
            min_memory_mb: Minimum available memory (megabytes) considered safe.
        """
        data_root = Path(env.get("VIOLA_DATA_DIR", "data"))
        try:
            data_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.debug("Unable to create data directory %s: %s", data_root, e, exc_info=True)
            self.add_issue(
                "resources",
                f"Unable to create data directory '{data_root}'.",
                f"Fix permissions or set VIOLA_DATA_DIR to a writable path (current: {data_root})",
                location=str(data_root),
            )
            return

        try:
            usage = shutil.disk_usage(str(data_root))
            free_mb = usage.free / (1024 * 1024)
            if free_mb < min_disk_mb:
                self.add_issue(
                    "resources",
                    f"Low disk space under {data_root} ({free_mb:.0f} MB free, required ≥ {min_disk_mb} MB).",
                    f"Free up disk space or relocate VIOLA_DATA_DIR (current: {data_root}).",
                    location=str(data_root),
                )
            elif free_mb < min_disk_mb * 1.5:
                self.add_issue(
                    "resources",
                    f"Disk space under {data_root} is getting low ({free_mb:.0f} MB free).",
                    "Plan cleanup or increase storage before long-running sessions.",
                    severity="warning",
                    location=str(data_root),
                )
        except Exception as exc:
            self.add_issue(
                "resources",
                f"Failed to inspect disk usage for {data_root}: {exc}",
                "Confirm the path exists and is accessible.",
                severity="warning",
                location=str(data_root),
            )

        try:
            psutil_module = importlib.import_module("psutil")
        except Exception as e:
            logger.debug("psutil not available: %s", e, exc_info=True)
            self.add_issue(
                "resources",
                "Could not verify available memory (psutil not installed).",
                "pip install psutil to enable memory guardrails.",
                severity="warning",
                location="utils/startup_validation.py",
            )
            return

        try:
            available_mb = psutil_module.virtual_memory().available / (1024 * 1024)
            if available_mb < min_memory_mb:
                self.add_issue(
                    "resources",
                    f"Low available memory detected ({available_mb:.0f} MB free, required ≥ {min_memory_mb} MB).",
                    "Close other applications or upgrade RAM before launching Viola.",
                    location="system-memory",
                )
            elif available_mb < min_memory_mb * 1.5:
                self.add_issue(
                    "resources",
                    f"Available memory is below recommended buffer ({available_mb:.0f} MB free).",
                    "Monitor memory usage; consider running in lightweight profile.",
                    severity="warning",
                    location="system-memory",
                )
        except Exception as exc:
            self.add_issue(
                "resources",
                f"Failed to inspect system memory: {exc}",
                "Verify psutil can access memory statistics.",
                severity="warning",
                location="system-memory",
            )

    def check_audio_devices(self) -> None:
        """
        Verify that at least one microphone/input device is available.

        Emits warnings instead of hard failures when the environment is explicitly
        headless (VIOLA_ALLOW_HEADLESS_AUDIO=1).
        """
        allow_headless = env.get("VIOLA_ALLOW_HEADLESS_AUDIO", "").lower() in {
            "1",
            "true",
            "yes",
        }

        def _probe_sounddevice() -> list[JsonDict] | None:
            try:
                sounddevice = importlib.import_module("sounddevice")
            except Exception as e:
                logger.debug("sounddevice not available: %s", e, exc_info=True)
                return None
            try:
                from audio_core.portaudio_guard import sounddevice_guard

                with sounddevice_guard():
                    devices = sounddevice.query_devices()
                return [
                    {
                        "name": to_json_value(getattr(device, "get", lambda _k, _d=None: None)("name")),
                        "max_input_channels": int(
                            getattr(device, "get", lambda _k, _d=None: 0)("max_input_channels", 0) or 0
                        ),
                    }
                    for device in devices
                ]
            except Exception as exc:  # pragma: no cover - defensive
                self.add_issue(
                    "audio",
                    f"Failed to query sounddevice devices: {exc}",
                    "Ensure sounddevice can enumerate audio hardware or uninstall if unused.",
                    severity="warning",
                    location="listener",
                )
                return []

        def _probe_pyaudio() -> list[JsonDict] | None:
            try:
                importlib.import_module("pyaudio")
            except Exception as e:
                logger.debug("pyaudio not available: %s", e, exc_info=True)
                return None
            try:
                # portaudio_instance() serializes Pa_Initialize/Pa_Terminate under
                # the process-wide lock and terminates on context exit.
                with portaudio_instance() as pa:
                    device_count = pa.get_device_count()
                    devices: list[JsonDict] = []
                    for index in range(device_count):
                        info = pa.get_device_info_by_index(index)
                        devices.append(
                            {
                                "name": to_json_value(info.get("name")),
                                "max_input_channels": int(info.get("maxInputChannels", 0) or 0),
                            }
                        )
                    return devices
            except Exception as exc:  # pragma: no cover - defensive
                self.add_issue(
                    "audio",
                    f"Failed to query PyAudio devices: {exc}",
                    "Ensure PyAudio can access audio hardware.",
                    severity="warning",
                    location="listener",
                )
                return []

        candidates = _probe_sounddevice()
        if candidates is None:
            candidates = _probe_pyaudio()

        if candidates is None:
            self.add_issue(
                "audio",
                "Unable to verify microphone availability (sounddevice/PyAudio not installed).",
                "Install sounddevice or PyAudio, or set VIOLA_ALLOW_HEADLESS_AUDIO=1 for kiosk/headless deployments.",
                severity="warning" if allow_headless else "error",
                location="listener",
            )
            return

        input_devices: list[JsonDict] = []
        for device in candidates:
            channels_obj = device.get("max_input_channels", 0)
            channels = int(channels_obj) if isinstance(channels_obj, (int, float, bool, str)) else 0
            if channels > 0:
                input_devices.append(device)
        if not input_devices:
            severity = "warning" if allow_headless else "error"
            self.add_issue(
                "audio",
                "No microphone/input devices detected.",
                "Connect a microphone or configure VIOLA_AUDIO_DEVICE to a valid source.",
                severity=severity,
                location="listener",
            )


def validate_startup(strict: bool = True, check_port: bool = True, port: int = DEFAULT_API_PORT) -> None:
    """
    Convenience function to validate startup.

    Args:
        strict: If True, fail on warnings
        check_port: If True, check port availability
        port: Port number to check

    Raises:
        StartupValidationError: If validation fails

    Example:
        >>> validate_startup()
        # If this doesn't raise, startup is OK
    """
    validator = StartupValidator(strict=strict)
    validator.validate_all(check_port=check_port, port=port)


def run_diagnostics() -> JsonDict:
    """
    Run comprehensive diagnostics and return results.

    Returns:
        Dict with diagnostic results

    Example:
        >>> results = run_diagnostics()
        >>> logger.info("Errors: %s", results["error_count"])
    """
    validator = StartupValidator(strict=False)
    validator.validate_all()
    return validator.get_summary()
