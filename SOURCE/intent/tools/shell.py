"""Shell command tool for the agent.

Provides a ``run_command`` tool that executes shell commands after
command-language-aware validation, a per-command length cap, dedicated
audit logging, and explicit permission classification.
"""

from __future__ import annotations

import asyncio
import datetime
import fnmatch
import os
import shlex
import sys
from pathlib import Path
from typing import Literal

from core.logging_config import get_logger
from intent.log_redaction import redact_diagnostic_payload
from intent.permissions.shell_safety import (
    ShellName,
    ShellSafetyDecision,
    executable_name,
    split_command_segments,
    validate_shell_command,
)
from intent.tool_types import ToolResult

logger = get_logger(__name__)

_MAX_OUTPUT_CHARS = 4000
_MAX_COMMAND_LENGTH = 500
_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 120.0

# Viola project root (two levels up from intent/tools/shell.py)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_SENSITIVE_ENV_PATTERNS = (
    # Generic credential suffixes
    "*_API_KEY",
    "*_TOKEN",
    "*_SECRET",
    "*_PASSWORD",
    "*_PASS",
    "*_PRIVATE_KEY*",
    "*_REFRESH_TOKEN",
    "*_ACCESS_TOKEN",
    "*_CLIENT_SECRET",
    "*_WEBHOOK_SECRET",
    "*SECRET_KEY*",
    "*SECRET_ACCESS_KEY*",
    "*ACCESS_KEY_ID*",
    "*DATABASE_URL*",
    "*DATABASE_DSN*",
    "*POSTGRES_DSN*",
    "*CONNECTION_STRING*",
    # Known provider prefixes (catches all related credentials regardless of suffix)
    "OPENAI_*",
    "ANTHROPIC_*",
    "GOOGLE_*",
    "GROQ_*",
    "CEREBRAS_*",
    "DEEPSEEK_*",
    "XAI_*",
    "PERPLEXITY_*",
    "OPENROUTER_*",
    "MISTRAL_*",
    "COHERE_*",
    "STRIPE_*",
    "BTCPAY_*",
    "TWILIO_*",
    "TELNYX_*",
    "RESEND_*",
    "PUSHOVER_*",
    "PAGERDUTY_*",
    "CLOUDFLARE_*",
    "SUPABASE_*",
    "GOTRUE_*",
    "R2_*",
    "AWS_*",
    "GCS_*",
    "AZURE_*",
    "DROPBOX_*",
    "GITHUB_*",
    "GITLAB_*",
    "VERCEL_*",
    "NETLIFY_*",
    "FLY_*",
    "RENDER_*",
    "HEROKU_*",
    "SENTRY_*",
    "DATADOG_*",
    "DD_*",
    "NEWRELIC_*",
    "SPOTIFY_*",
    "YOUTUBE_*",
    # Viola-specific credential prefixes
    "VIOLA_AUTH_*",
    "VIOLA_JWT_*",
    "VIOLA_SECURITY_*",
    "VIOLA_ADMIN_*",
    "VIOLA_GOOGLE_*",
    "VIOLA_SPOTIFY_*",
    "VIOLA_APPLE_*",
    "VIOLA_OPENAI_*",
    "VIOLA_ANTHROPIC_*",
    "VIOLA_STRIPE_*",
    "VIOLA_BTCPAY_*",
    "VIOLA_TELNYX_*",
    "VIOLA_RESEND_*",
    "VIOLA_PUSHOVER_*",
    "VIOLA_PAGERDUTY_*",
    "VIOLA_CLOUDFLARE_*",
    "VIOLA_WAKEWORD_*",
    "VIOLA_APP_DATABASE_*",
    "VIOLA_DATABASE_*",
    "VIOLA_MIGRATION_DATABASE_*",
    "VIOLA_GOTRUE_DATABASE_*",
    "VIOLA_ALERT_*",
    "VIOLA_EXTERNAL_MONITOR_TOKEN",
    "VIOLA_API_KEY",
    "VIOLA_OPS_*",
    "POSTGRES_*",
    "PG*PASSWORD",
)

# Specific env names always stripped regardless of pattern matching above.
# Use this for vars whose names do not fit the credential suffix convention.
_SENSITIVE_ENV_EXACT = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "R2_ACCESS_KEY_ID",
        "R2_SECRET_ACCESS_KEY",
        "GCS_SERVICE_ACCOUNT_KEY",
        "DATABASE_URL",
        "POSTGRES_PASSWORD",
        "VIOLA_DATABASE_URL",
        "VIOLA_MIGRATION_DATABASE_URL",
        "VIOLA_GOTRUE_DATABASE_URL",
        "VIOLA_APP_DATABASE_PASSWORD",
        "GOTRUE_DB_PASSWORD",
        "GOTRUE_JWT_SECRET",
        "GOTRUE_SMTP_PASS",
        "GOTRUE_SMTP_USER",
        "GOTRUE_WEBHOOK_SECRET",
        "GOTRUE_EXTERNAL_GOOGLE_SECRET",
        "VIOLA_SMTP_PASS",
        "VIOLA_SMTP_USER",
        "VIOLA_BTCPAY_API_KEY",
        "VIOLA_BTCPAY_WEBHOOK_SECRET",
        "VIOLA_BTCPAY_STORE_ID",
        "VIOLA_STRIPE_SECRET_KEY",
        "VIOLA_STRIPE_PUBLISHABLE_KEY",
        "VIOLA_STRIPE_WEBHOOK_SECRET",
        "VIOLA_JWT_SECRET",
        "VIOLA_JWT_SECRET_PREVIOUS",
        "VIOLA_SECURITY_TOKEN_SECRET",
        "VIOLA_SECURITY_API_KEY",
        "VIOLA_API_KEY",
        "VIOLA_ADMIN_TOKEN",
        "VIOLA_ALERT_TELEGRAM_BOT_TOKEN",
        "VIOLA_ALERT_WEBHOOK_URL",
        "VIOLA_PAGERDUTY_ROUTING_KEY",
        "VIOLA_PUSHOVER_APP_TOKEN",
        "VIOLA_PUSHOVER_USER_KEY",
        "VIOLA_CLOUDFLARE_EMAIL_API_TOKEN",
        "VIOLA_EXTERNAL_MONITOR_TOKEN",
        "VIOLA_WAKEWORD_SERVICE_KEY",
        "VIOLA_TELNYX_API_KEY",
        "VIOLA_TELNYX_MESSAGING_PROFILE_ID",
        "VIOLA_RESEND_API_KEY",
        "VIOLA_TRAINING_UPLOAD_ENDPOINT",
        "VIOLA_APPLE_PRIVATE_KEY_PATH",
        "VIOLA_APPLE_TEAM_ID",
        "VIOLA_APPLE_KEY_ID",
        "VIOLA_APPLE_CLIENT_ID",
        "VIOLA_GOOGLE_CLIENT_SECRET",
        "VIOLA_GOOGLE_CLIENT_ID",
        "VIOLA_GOOGLE_API_KEY",
        "VIOLA_SPOTIFY_CLIENT_ID",
        "VIOLA_SPOTIFY_CLIENT_SECRET",
        "SPOTIFY_DEVICE_ID",
        "SPOTIFY_REFRESH_TOKEN",
        "OPENAI_API_KEY",
        "VIOLA_OPENAI_API_KEY",
        "VIOLA_ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY",
    }
)


def sanitized_env() -> dict[str, str]:
    """Return a copy of os.environ with sensitive credential keys stripped.

    Strips three classes of variables before exposing the environment to
    untrusted subprocesses (shell tool, self-management restart, spawned
    workers): exact names known to carry secrets, glob patterns covering
    third-party SDK conventions, and Viola's own ``VIOLA_*`` credential
    prefixes. The default is "deny when in doubt" — a leaked secret in a
    tool stdout is much more expensive than a subprocess that can't read a
    config value.
    """

    env = os.environ.copy()
    keys_to_remove = [
        k for k in env if k in _SENSITIVE_ENV_EXACT or any(fnmatch.fnmatch(k, pat) for pat in _SENSITIVE_ENV_PATTERNS)
    ]
    for key in keys_to_remove:
        del env[key]
    return env


_SHELL_LOG_DIR = _PROJECT_ROOT / "logs"
_SHELL_LOG_FILE = _SHELL_LOG_DIR / "shell_commands.log"


def _extract_executable(token: str) -> str:
    """Normalize an executable token: strip path prefix and .exe suffix."""

    return executable_name(token)


def _split_command_segments(command: str) -> list[str]:
    """Split a command string into parser-level command segments."""

    return split_command_segments(command, _infer_shell(command, None))


def _is_denied(command: str) -> str | None:
    """Return a human-readable denial or permission reason, if not auto-allowed."""

    decision = validate_shell_command(command, _infer_shell(command, None), str(_PROJECT_ROOT), "default")
    if decision.behavior == "allow":
        return None
    if decision.behavior == "ask":
        return "Command blocked pending shell permission: %s" % (
            decision.required_permission or decision.normalized_command
        )
    return "Command blocked by shell safety policy: %s" % (decision.reason or "denied")


def _infer_shell(command: str, shell: str | None) -> ShellName:
    """Infer the command language used for safety validation."""

    if shell:
        lower = shell.lower()
        if lower in {"powershell", "pwsh"}:
            return "powershell"
        if lower in {"cmd", "cmd.exe"}:
            return "cmd"
        return "bash"

    first_exe = _extract_executable(_first_token(command))
    if _looks_like_powershell(command, first_exe):
        return "powershell"
    if "$" in command or "`" in command:
        return "bash"
    if sys.platform == "win32":
        return "cmd"
    return "bash"


def _first_token(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    if stripped[0] in {'"', "'"}:
        end = stripped.find(stripped[0], 1)
        return stripped[1:end] if end > 0 else stripped[1:]
    return stripped.split(None, 1)[0]


def _looks_like_powershell(command: str, first_exe: str) -> bool:
    if first_exe in {"powershell", "pwsh"}:
        return False
    return (
        first_exe.startswith("get-")
        or first_exe.startswith("select-")
        or first_exe.startswith("set-")
        or first_exe in {"cat", "gci", "gc", "gps", "ls", "pwd", "type", "write-output", "where-object"}
        or "$env:" in command
    )


def _requires_shell_interpretation(command: str, shell: ShellName) -> bool:
    if shell == "powershell":
        return True
    if shell == "cmd" and _extract_executable(_first_token(command)) in {"dir", "echo", "type", "ver"}:
        return True
    return len(_split_command_segments(command)) > 1


def _decision_payload(decision: ShellSafetyDecision) -> dict[str, str | bool | None]:
    return {
        "behavior": decision.behavior,
        "normalized_command": decision.normalized_command,
        "shell": decision.shell,
        "cwd": decision.cwd,
        "read_only": decision.read_only,
        "reason": decision.reason,
        "required_permission": decision.required_permission,
    }


def _audit_log(
    command: str,
    working_directory: str,
    exit_code: int | None,
    *,
    blocked: bool = False,
    status_override: str | None = None,
) -> bool:
    """Append an entry to the shell command audit log."""

    try:
        _SHELL_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(tz=datetime.UTC).isoformat()
        status = status_override or ("BLOCKED" if blocked else ("OK" if exit_code == 0 else "FAIL(%s)" % exit_code))
        safe_cwd = _redact_shell_log_value(working_directory)
        safe_command = _redact_shell_log_value(command)
        line = "[%s] %s | cwd=%s | cmd=%s\n" % (ts, status, safe_cwd, safe_command)
        with open(_SHELL_LOG_FILE, "a", encoding="utf-8") as file:
            file.write(line)
        return True
    except OSError:
        logger.debug("Could not write shell audit log")
        return False


def _redact_shell_log_value(value: str) -> str:
    """Redact secrets and low-ambiguity PII before writing shell audit logs."""

    return str(redact_diagnostic_payload(str(value or "")))


async def run_command(
    command: str,
    timeout: float = _DEFAULT_TIMEOUT,
    working_directory: str = "",
    shell: Literal["bash", "powershell", "cmd"] | None = None,
    permission_mode: str = "default",
) -> ToolResult:
    """Execute a shell command and return its output.

    Args:
        command: Shell command to execute (max 500 chars).
        timeout: Timeout in seconds (default 30, max 120).
        working_directory: Directory to run in (defaults to Viola project root).
        shell: Optional explicit command language for validation/execution.
        permission_mode: Claude-style permission mode for non-read-only commands.
    """

    if len(command) > _MAX_COMMAND_LENGTH:
        return ToolResult(
            ok=False,
            error="Command exceeds maximum length of %d characters" % _MAX_COMMAND_LENGTH,
        )

    if working_directory:
        cwd = Path(working_directory).expanduser().resolve()
        if not cwd.is_dir():
            return ToolResult(ok=False, error="Working directory does not exist: %s" % cwd)
        cwd_str = str(cwd)
    else:
        cwd_str = str(_PROJECT_ROOT)

    timeout = min(max(float(timeout), 1.0), _MAX_TIMEOUT)
    decision = validate_shell_command(command, _infer_shell(command, shell), cwd_str, permission_mode)
    if decision.behavior != "allow":
        logger.warning("Denied shell command: %s", _redact_shell_log_value(command[:100]))
        _audit_log(command, cwd_str, exit_code=None, blocked=True)
        # S9-14: surface ``ask`` as the canonical Claude permission-prompt
        # signal — callers (agent loop, MCP approval bridge) can distinguish
        # "user needs to approve" from "policy denies" by inspecting
        # ``permission_behavior``. The error string keeps the legacy text so
        # existing model-visible diagnostics stay stable.
        if decision.behavior == "ask":
            error = "Command blocked pending shell permission: %s" % (
                decision.required_permission or decision.normalized_command
            )
            data = {
                "shell_safety": _decision_payload(decision),
                "permission_behavior": "ask",
                "permission_rule": decision.required_permission or "",
                "reason": decision.reason or "",
            }
        else:
            error = "Command blocked by shell safety policy: %s" % (decision.reason or "denied")
            data = {
                "shell_safety": _decision_payload(decision),
                "permission_behavior": "deny",
                "reason": decision.reason or "",
            }
        return ToolResult(ok=False, error=error, data=data)

    env = sanitized_env()
    python_dir = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    if python_dir.lower() not in path.lower():
        env["PATH"] = python_dir + os.pathsep + path

    if not _audit_log(command, cwd_str, exit_code=None, status_override="STARTED"):
        return ToolResult(ok=False, error="Shell audit log unavailable; command was not executed")

    use_shell = _requires_shell_interpretation(command, decision.shell)
    proc: asyncio.subprocess.Process | None = None

    try:
        if decision.shell == "powershell":
            powershell_exe = "powershell.exe" if sys.platform == "win32" else "pwsh"
            proc = await asyncio.create_subprocess_exec(
                powershell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd_str,
                env=env,
            )
        elif use_shell:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd_str,
                env=env,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(command, posix=(decision.shell == "bash")),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd_str,
                env=env,
            )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        logger.warning("Shell command timed out after %.0fs: %s", timeout, _redact_shell_log_value(command[:100]))
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                logger.debug("Timed-out process was already terminated")
        _audit_log(command, cwd_str, exit_code=None)
        return ToolResult(ok=False, error="Command timed out after %.0f seconds" % timeout)
    except OSError as exc:
        _audit_log(command, cwd_str, exit_code=None)
        return ToolResult(ok=False, error="Failed to execute command: %s" % exc)

    stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
    stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""

    truncated = False
    if len(stdout_text) > _MAX_OUTPUT_CHARS:
        stdout_text = stdout_text[:_MAX_OUTPUT_CHARS] + "\n... [output truncated]"
        truncated = True
    if len(stderr_text) > _MAX_OUTPUT_CHARS:
        stderr_text = stderr_text[:_MAX_OUTPUT_CHARS] + "\n... [stderr truncated]"
        truncated = True

    returncode = proc.returncode or 0
    _audit_log(command, cwd_str, exit_code=returncode)

    result_data = {
        "command": command,
        "normalized_command": decision.normalized_command,
        "shell": decision.shell,
        "read_only": decision.read_only,
        "permission_behavior": decision.behavior,
        "working_directory": cwd_str,
        "returncode": returncode,
        "stdout": stdout_text,
        "shell_safety": _decision_payload(decision),
    }
    if stderr_text:
        result_data["stderr"] = stderr_text

    return ToolResult(
        ok=returncode == 0,
        data=result_data,
        error=("Command failed with exit code %d" % returncode if returncode != 0 else None),
        truncated=truncated,
    )
