"""Render Markdown skill commands while preserving caller-owned execution policy.

Skill text and arguments are data. Only a trusted bundled source, an explicit
caller opt-in, and the shell allowlist together authorize inline execution.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)
SKILL_DIR_VAR_VIOLA = "$" + "{VIOLA_SKILL_DIR}"
SKILL_DIR_VAR_CLAUDE = "$" + "{CLAUDE_SKILL_DIR}"
SESSION_ID_VAR_VIOLA = "$" + "{VIOLA_SESSION_ID}"
SESSION_ID_VAR_CLAUDE = "$" + "{CLAUDE_SESSION_ID}"
_SHELL_TIMEOUT_SECONDS = 30
_SHELL_TRUSTED_SOURCES = frozenset({"bundled"})
USER_INVOCATION_REFUSAL = "This skill can only be invoked by Claude, not directly by users."
_INLINE_SHELL_RE = re.compile("!" + chr(96) + "([^" + chr(96) + r"\n]+)" + chr(96))
_FENCED_SHELL_RE = re.compile(chr(96) * 3 + r"\s*!\s*\n([\s\S]*?)\n" + chr(96) * 3, re.MULTILINE)


@dataclass(frozen=True)
class SkillHookSpec:
    event: str
    matcher: str | None = None
    command: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillMdCommand:
    name: str
    description: str
    markdown_body: str
    skill_root: Path
    source_file: Path
    display_name: str | None = None
    when_to_use: str | None = None
    version: str | None = None
    argument_hint: str | None = None
    argument_names: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    model_override: str | None = None
    agent: str | None = None
    effort: str | None = None
    execution_context: str = "inline"
    paths: list[str] = field(default_factory=list)
    shell_allowlist: list[str] = field(default_factory=list)
    hooks: list[SkillHookSpec] = field(default_factory=list)
    user_invocable: bool = True
    disable_model_invocation: bool = False
    loaded_from: str = "skills"
    has_user_specified_description: bool = False
    priority: int = 50

    @property
    def is_user_invocable(self) -> bool:
        return self.user_invocable

    @property
    def is_model_invocable(self) -> bool:
        return not self.disable_model_invocation

    @property
    def is_shell_trusted(self) -> bool:
        return self.loaded_from in _SHELL_TRUSTED_SOURCES

    def matches_path(self, file_path: str) -> bool:
        target = file_path.replace("\\", "/")
        patterns = [item.replace("\\", "/").rstrip("/") for item in self.paths]
        return any(pattern and (fnmatch(target, pattern) or fnmatch(target, pattern + "/*")) for pattern in patterns)

    def _invocation_names(self) -> set[str]:
        return {slug for name in (self.name, self.display_name) if (slug := _normalize_invocation_name(name or ""))}

    def matches_invocation(self, text: str) -> bool:
        pieces = text.strip().split(None, 1) if text else []
        return bool(
            pieces
            and pieces[0].startswith("/")
            and _normalize_invocation_name(pieces[0][1:]) in self._invocation_names()
        )

    def extract_args(self, text: str) -> str:
        pieces = text.strip().split(None, 1)
        return pieces[1] if len(pieces) == 2 and pieces[0].startswith("/") else ""

    @staticmethod
    def _INLINE_SHELL_RE_PRESENT(content: str) -> bool:
        return bool(_INLINE_SHELL_RE.search(content) or _FENCED_SHELL_RE.search(content))

    def get_prompt_for_command(
        self,
        args: str = "",
        *,
        session_id: str | None = None,
        execute_shell: bool = False,
        shell_executor: Any | None = None,
    ) -> str:
        rendered = _substitute_arguments(
            "Base directory for this skill: %s\n\n%s" % (self.skill_root, self.markdown_body),
            args,
            self.argument_names,
        )
        variables = {
            SKILL_DIR_VAR_VIOLA: str(self.skill_root).replace("\\", "/"),
            SKILL_DIR_VAR_CLAUDE: str(self.skill_root).replace("\\", "/"),
            SESSION_ID_VAR_VIOLA: session_id or "",
            SESSION_ID_VAR_CLAUDE: session_id or "",
        }
        for token, value in variables.items():
            rendered = rendered.replace(token, value)
        if execute_shell and self.is_shell_trusted:
            return _execute_inline_shell(
                rendered,
                allowlist=self.shell_allowlist,
                executor=shell_executor or _default_shell_executor,
                cwd=self.skill_root,
            )
        if execute_shell and self._INLINE_SHELL_RE_PRESENT(rendered):
            logger.warning("Inline shell denied for skill source %s (%s)", self.loaded_from, self.name)
        return rendered

    def to_dict(self) -> dict[str, Any]:
        result = {
            key: getattr(self, key)
            for key in (
                "name",
                "display_name",
                "description",
                "when_to_use",
                "version",
                "agent",
                "effort",
                "user_invocable",
                "disable_model_invocation",
                "loaded_from",
            )
        }
        result.update(
            allowed_tools=list(self.allowed_tools),
            model=self.model_override,
            context=self.execution_context,
            paths=list(self.paths),
            skill_root=str(self.skill_root),
            source_file=str(self.source_file),
            hooks=[{"event": h.event, "matcher": h.matcher, "command": h.command} for h in self.hooks],
        )
        result["shell"] = list(self.shell_allowlist)
        return result

    def __repr__(self) -> str:
        return "<SkillMdCommand: %s (model=%s, allowed_tools=%d, user_invocable=%s)>" % (
            self.name,
            self.model_override,
            len(self.allowed_tools),
            self.user_invocable,
        )


def _normalize_invocation_name(value: str) -> str:
    return re.sub("[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


def _substitute_arguments(content: str, args: str, named: Sequence[str]) -> str:
    """Resolve argument placeholders once, without reinterpreting inserted data."""
    try:
        values = shlex.split(args)
    except ValueError:
        values = args.split()
    names = {name: index for index, name in enumerate(named)}
    used = False
    token = re.compile(r"\$ARGUMENTS\[(\d+)\]|\$ARGUMENTS|\$(\d+)|\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)")

    def replace(match: re.Match[str]) -> str:
        nonlocal used
        indexed, position, braced, bare = match.groups()
        if match.group() in ("$ARGUMENTS", "$0"):
            used = True
            return args
        if indexed is not None:
            index = int(indexed)
        elif position is not None:
            index = int(position) - 1
        else:
            index = names.get(braced or bare, -1)
        if 0 <= index < len(values):
            used = True
            return values[index]
        return match.group()

    rendered = token.sub(replace, content)
    return rendered if used or not args else rendered.rstrip() + "\n\nARGUMENTS: " + args


def _shell_command_allowed(command: str, allowlist: Iterable[str], *, cwd: Path | None = None) -> bool:
    permitted = {item.strip() for item in allowlist if item and item.strip()}
    if not permitted or not command.strip():
        return False
    if "*" in permitted:
        from intent.permissions.shell_safety import validate_shell_command

        return validate_shell_command(command, "bash", str(cwd or Path.cwd()), "default").behavior == "allow"
    if any(character in command for character in ";&|" + chr(96) + "$()<>"):
        return False
    return command.split(None, 1)[0] in permitted


def _default_shell_executor(command: str, *, cwd: Path | None = None, timeout: float = _SHELL_TIMEOUT_SECONDS) -> str:
    try:
        # A bundled skill and the shell policy must authorize this call.
        process = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )  # nosec B602
    except subprocess.TimeoutExpired:
        return "[shell timeout: %s]" % command
    except Exception as exc:
        return "[shell error: %s]" % exc
    output = process.stdout.rstrip("\n")
    if process.returncode and process.stderr:
        output += "\n[stderr] " + process.stderr.rstrip()
    return output


def _execute_inline_shell(content: str, *, allowlist: Iterable[str], executor: Any, cwd: Path | None = None) -> str:
    permitted = tuple(allowlist)

    def render(match: re.Match[str]) -> str:
        command = match.group(1).strip()
        if not _shell_command_allowed(command, permitted, cwd=cwd):
            return "[shell blocked: %s]" % command
        try:
            return executor(command, cwd=cwd)
        except Exception as exc:
            logger.exception("Skill shell expansion failed")
            return "[shell error: %s]" % exc

    # Fenced and inline forms are processed in their established order.
    return _INLINE_SHELL_RE.sub(render, _FENCED_SHELL_RE.sub(render, content))
