"""Execute a ``command`` (shell) hook from settings/session config.

Parity reference: ``src/utils/hooks.ts`` — the original spawns the hook in a
shell (``bash`` or ``pwsh``) and pipes the hook envelope JSON over stdin.
Stdout is parsed back through ``validateHookJson`` and folded into the
aggregated :class:`HookResult`.

Viola's implementation keeps the same shape:

* Spawn via :mod:`asyncio` subprocesses.
* Write the envelope JSON to stdin.
* Read up to a fixed cap from stdout / stderr.
* Parse stdout as JSON if it looks like JSON; otherwise treat it as
  ``additional_context``.
* Honor ``hook.timeout_seconds``; default to 30s.

Hook commands run on the user's machine, NOT the cloud Postgres tier — they
are inherently Tier 3 / local-only. The runner is invoked from the
in-process pipeline so a worktree-shaped cwd is implicit.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import gettempdir
from typing import Any

from core.logging_config import get_logger
from intent.hooks.settings_runner import HookCommand, HookExecutionContext

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_STDOUT_BYTES = 256 * 1024  # 256 KiB
_BASHLIKE_SHELLS = {"bash", "sh", "zsh"}

ExecCommandHook = Callable[[HookCommand, str, HookExecutionContext], Awaitable[Any]]


async def default_exec_command(
    hook: HookCommand,
    envelope_json: str,
    context: HookExecutionContext,
) -> Any:
    """Execute a ``command`` hook and return raw/parsed stdout."""

    if not hook.command:
        return None
    shell_name = (hook.shell or "bash").lower()
    timeout = hook.timeout_seconds or _DEFAULT_TIMEOUT_SECONDS
    cwd = context.cwd or os.getcwd()
    env = _filtered_env(hook, cwd=cwd, shell_name=shell_name, envelope_json=envelope_json)
    command = _prepare_command_for_shell(hook.command, shell_name)

    try:
        proc = await _spawn(command, shell_name, env=env, cwd=cwd)
    except FileNotFoundError as exc:
        logger.warning("Hook command shell not available: %s", exc)
        return None

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(envelope_json.encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        logger.warning("Hook command exceeded %.0fs timeout: %s", timeout, hook.describe())
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None

    stdout_text = stdout_bytes[:_MAX_STDOUT_BYTES].decode("utf-8", errors="replace")
    stderr_text = stderr_bytes[:_MAX_STDOUT_BYTES].decode("utf-8", errors="replace") if stderr_bytes else ""
    parsed_json = _parse_stdout_json(stdout_text)
    if parsed_json is not None:
        return parsed_json

    if proc.returncode and proc.returncode != 0:
        # Exit code 2 is Claude's "blocking error" signal.
        if proc.returncode == 2:
            return {
                "permissionBehavior": "deny",
                "reason": (stderr_text or stdout_text or "Hook blocked execution.").strip()[:2000],
            }
        logger.debug("Hook command exit=%s stderr=%s", proc.returncode, stderr_text[:200])
        return None

    return _coerce_stdout(stdout_text, stderr_text)


def _parse_stdout_json(stdout_text: str) -> Any:
    stripped = stdout_text.strip()
    if stripped and stripped[0] in {"{", "["}:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None
    return None


def _coerce_stdout(stdout_text: str, stderr_text: str) -> Any:
    stripped = stdout_text.strip()
    if not stripped:
        if stderr_text.strip():
            return {"additionalContext": stderr_text.strip()[:2000]}
        return None
    if stripped[0] in {"{", "["}:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return {"additionalContext": stripped[:2000]}


def _filtered_env(
    hook: HookCommand,
    *,
    cwd: str | None = None,
    shell_name: str | None = None,
    envelope_json: str | None = None,
) -> dict[str, str]:
    base = {key: value for key, value in os.environ.items()}
    if not hook.allowed_env_vars:
        filtered = base
    else:
        allowed = set(hook.allowed_env_vars)
        filtered = {key: value for key, value in base.items() if key in allowed or _is_safe_env(key)}
    filtered.update(_claude_hook_env(hook, cwd=cwd, shell_name=shell_name, envelope_json=envelope_json))
    return filtered


def _is_safe_env(key: str) -> bool:
    return key in {"PATH", "HOME", "USERPROFILE", "TMP", "TEMP", "TZ", "LANG", "LC_ALL"}


def _claude_hook_env(
    hook: HookCommand,
    *,
    cwd: str | None,
    shell_name: str | None,
    envelope_json: str | None,
) -> dict[str, str]:
    project_dir = cwd or os.getcwd()
    bash_on_windows = _is_windows_bashlike_shell(shell_name or "")
    env: dict[str, str] = {
        "CLAUDE_PROJECT_DIR": _format_path_for_hook_env(project_dir, bash_on_windows=bash_on_windows),
        "CLAUDE_ENV_FILE": _format_path_for_hook_env(
            _hook_env_file_path(envelope_json),
            bash_on_windows=bash_on_windows,
        ),
    }
    if hook.skill_root:
        env["CLAUDE_PLUGIN_ROOT"] = _format_path_for_hook_env(hook.skill_root, bash_on_windows=bash_on_windows)
    return env


def _hook_env_file_path(envelope_json: str | None) -> str:
    event_name = "hook"
    session_id = "session"
    if envelope_json:
        try:
            payload = json.loads(envelope_json)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            event_name = str(
                payload.get("hookEventName") or payload.get("hook_event_name") or payload.get("event") or event_name
            )
            session_id = str(payload.get("sessionId") or payload.get("session_id") or session_id)
    safe_event = re.sub(r"[^A-Za-z0-9_.-]+", "_", event_name).strip("._") or "hook"
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("._") or "session"
    env_dir = Path(gettempdir()) / "viola-hooks"
    env_dir.mkdir(parents=True, exist_ok=True)
    return str(env_dir / ("%s_%s.env" % (safe_event, safe_session)))


def _format_path_for_hook_env(path: str, *, bash_on_windows: bool) -> str:
    if bash_on_windows:
        return _windows_path_to_posix(path)
    return path


def _prepare_command_for_shell(command: str, shell_name: str) -> str:
    if not _is_windows_bashlike_shell(shell_name):
        return command
    converted = _convert_windows_paths_for_bash(command)
    first_token = _first_command_token(converted)
    if first_token and _is_shell_script_token(first_token):
        return "bash %s" % converted
    return converted


def _first_command_token(command: str) -> str | None:
    try:
        tokens = shlex.split(command, posix=False)
    except ValueError:
        tokens = command.strip().split(maxsplit=1)
    if not tokens:
        return None
    return tokens[0].strip("'\"")


def _is_shell_script_token(token: str) -> bool:
    lowered = token.lower()
    if lowered in _BASHLIKE_SHELLS:
        return False
    return lowered.endswith(".sh")


def _convert_windows_paths_for_bash(command: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return _windows_path_to_posix(match.group(1))

    return re.sub(r"(?<![\w/\\])([A-Za-z]:[\\/][^ \t\r\n'\";|&<>]+)", replace, command)


def _windows_path_to_posix(path: str) -> str:
    text = str(path).replace("\\", "/")
    if text.startswith("//"):
        return text
    drive_match = re.match(r"^([A-Za-z]):/?(.*)$", text)
    if drive_match:
        drive = drive_match.group(1).lower()
        rest = drive_match.group(2)
        return "/%s/%s" % (drive, rest) if rest else "/%s" % drive
    return text


def _is_windows_bashlike_shell(shell_name: str) -> bool:
    return sys.platform == "win32" and shell_name in _BASHLIKE_SHELLS


async def _spawn(
    command: str,
    shell_name: str,
    *,
    env: dict[str, str],
    cwd: str,
) -> asyncio.subprocess.Process:
    if shell_name in {"powershell", "pwsh"}:
        exe = "powershell.exe" if sys.platform == "win32" else "pwsh"
        return await asyncio.create_subprocess_exec(
            exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    # bash / sh / zsh — use create_subprocess_shell so the command can use
    # shell features (pipes, redirections, etc.) as Claude does.
    if _is_windows_bashlike_shell(shell_name):
        exe = _resolve_shell_executable(shell_name)
        if not exe:
            raise FileNotFoundError("Git Bash executable not found for hook shell %s" % shell_name)
        return await asyncio.create_subprocess_exec(
            exe,
            "-lc",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    return await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
        executable=_resolve_shell_executable(shell_name),
    )


def _resolve_shell_executable(shell_name: str) -> str | None:
    if sys.platform == "win32":
        if shell_name in _BASHLIKE_SHELLS:
            return _find_windows_git_bash()
        return None
    if shell_name in _BASHLIKE_SHELLS:
        # Honor $SHELL but fall back to /bin/bash like Claude.
        env_shell = os.environ.get("SHELL")
        if env_shell:
            return env_shell
        return "/bin/bash"
    return None


def _find_windows_git_bash() -> str | None:
    explicit = os.environ.get("GIT_BASH")
    if explicit and _path_exists(explicit):
        return explicit
    for candidate in ("bash.exe", "bash"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    for candidate in _windows_git_bash_candidates():
        if _path_exists(candidate):
            return candidate
    return None


def _windows_git_bash_candidates() -> tuple[str, ...]:
    roots = (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMW6432"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "J:\\Git",
    )
    candidates: list[str] = []
    for root in roots:
        if not root:
            continue
        base = Path(root)
        if base.name.lower() == "git":
            git_root = base
        elif base.name.lower() == "programs":
            git_root = base / "Git"
        else:
            git_root = base / "Git"
        candidates.extend((str(git_root / "bin" / "bash.exe"), str(git_root / "usr" / "bin" / "bash.exe")))
    return tuple(dict.fromkeys(candidates))


def _path_exists(path: str) -> bool:
    return Path(path).exists()


__all__ = ["ExecCommandHook", "default_exec_command"]


# Reference shlex so accidental imports don't strip it; used in test/debug paths.
_ = shlex
