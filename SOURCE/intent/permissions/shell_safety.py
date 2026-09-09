"""Command-language-aware shell safety validation.

This module mirrors the relevant Claude Code shell-safety mechanics for Viola:
parse command language first, classify read-only commands by command-specific
rules, and fail closed on exfiltration or destructive composition before a
subprocess is launched.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PermissionBehavior = Literal["allow", "deny", "ask"]
ShellName = Literal["bash", "powershell", "cmd"]

# "provably" (#860 audit note): this claim is only as strong as the tokenizer
# the "read_only"/hard-deny checks reason about ACTUALLY matching the
# interpreter that will execute the command. Before this fix it did not --
# a bash-classified command was validated with shlex.split(posix=True) but a
# multi-segment one runs via asyncio.create_subprocess_shell, which on
# Windows invokes cmd.exe (verified: %CD% expands, a raw backslash path
# survives byte-for-byte), an interpreter that never treats backslash as an
# escape at all. The fix (see _tokenize_literal_bash) closes the SPECIFIC,
# verified gap that opened -- a Windows-path-spelled sensitive-path/
# sandbox-escape check being silently defeated -- by reading every
# path-shaped token in its backslash-preserving form. It does NOT constitute
# a full audit of every OTHER way cmd.exe's quoting/metacharacter/expansion
# model could diverge from the bash-shaped assumptions baked into
# _bash_forbidden_expansion_reason and friends (e.g. cmd.exe's own %VAR%
# expansion, ^ escaping, or different quote-nesting rules) -- that remains
# open scope this fix does not claim to close. "Provably" is honest for the
# defect this fix targets; it is not yet honest as a claim about every
# interpreter-divergence shape that could exist.
_READONLY_REASON = "Command is provably read-only."
_ASK_REASON = "Command is not provably read-only; explicit shell permission is required."


@dataclass(frozen=True)
class ShellSafetyDecision:
    """Result of shell command validation."""

    behavior: PermissionBehavior
    normalized_command: str
    shell: ShellName
    cwd: str
    read_only: bool
    reason: str | None = None
    required_permission: str | None = None


@dataclass(frozen=True)
class _Token:
    text: str
    quote: Literal["single", "double"] | None = None


@dataclass(frozen=True)
class _SimpleCommand:
    raw: str
    argv: tuple[_Token, ...]
    stripped_argv: tuple[str, ...]
    redirection: bool = False
    # S9-15 / #860: the SAME positions as ``stripped_argv``, but read with a
    # backslash-preserving tokenizer instead of ``shlex.split(..., posix=True)``.
    # Bash-shape commands are validated with ``stripped_argv`` (POSIX escape
    # semantics: correct for keyword/flag/subcommand matching) but every check
    # that treats a token as a REAL FILESYSTEM PATH must read
    # ``literal_stripped_argv`` instead -- see ``_tokenize_literal_bash`` for why.
    # Defaults to ``stripped_argv`` itself for the powershell/cmd parsers, whose
    # own tokenizers never mangle a backslash in the first place (PowerShell:
    # backtick is the only escape; cmd: ``shlex.split(..., posix=False)``).
    literal_stripped_argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.literal_stripped_argv:
            object.__setattr__(self, "literal_stripped_argv", self.stripped_argv)

    @property
    def executable(self) -> str:
        if not self.stripped_argv:
            return ""
        return _executable_name(self.stripped_argv[0])


@dataclass(frozen=True)
class _ShellAnalysis:
    normalized_command: str
    read_only: bool
    hard_deny: bool = False
    reason: str | None = None


_BASH_READONLY_SIMPLE = frozenset(
    {
        "echo",
        "printf",
        "pwd",
        "ls",
        "dir",
        "whoami",
        "hostname",
        "uname",
        "which",
        "where",
        "cat",
        "head",
        "tail",
        "wc",
        "true",
        "false",
        "ping",
    }
)

_BASH_READONLY_FILESYSTEM_COMMANDS = frozenset({"cat", "head", "tail", "wc", "ls", "dir", "rg"})

_POWERSHELL_ALIAS_MAP = {
    "cat": "get-content",
    "cd": "set-location",
    "chdir": "set-location",
    "cls": "clear-host",
    "copy": "copy-item",
    "cp": "copy-item",
    "curl": "invoke-webrequest",
    "del": "remove-item",
    "dir": "get-childitem",
    "echo": "write-output",
    "erase": "remove-item",
    "gci": "get-childitem",
    "gc": "get-content",
    "gcm": "get-command",
    "gl": "get-location",
    "gp": "get-itemproperty",
    "gps": "get-process",
    "ls": "get-childitem",
    "mi": "move-item",
    "move": "move-item",
    "mv": "move-item",
    "pwd": "get-location",  # pragma: allowlist secret - PowerShell alias, not a credential
    "rm": "remove-item",
    "rmdir": "remove-item",
    "select": "select-object",
    "sl": "set-location",
    "sort": "sort-object",
    "type": "get-content",
    "where": "where-object",
    "?": "where-object",
    "%": "foreach-object",
}

_POWERSHELL_READONLY_CMDLETS = frozenset(
    {
        "clear-host",
        "compare-object",
        "convertfrom-json",
        "convertto-json",
        "findstr",
        "foreach-object",
        "format-list",
        "format-table",
        "get-alias",
        "get-childitem",
        "get-command",
        "get-content",
        "get-date",
        "get-item",
        "get-itemproperty",
        "get-location",
        "get-process",
        "get-service",
        "measure-object",
        "out-host",
        "resolve-path",
        "select-object",
        "select-string",
        "sort-object",
        "test-path",
        "where-object",
        "write-host",
        "write-output",
    }
)

_POWERSHELL_CWD_CHANGERS = frozenset(
    {
        "set-location",
        "push-location",
        "pop-location",
        "new-psdrive",
    }
)

_POWERSHELL_READONLY_FILESYSTEM_CMDLETS = frozenset(
    {
        "get-childitem",
        "get-content",
        "get-item",
        "get-itemproperty",
        "resolve-path",
        "select-string",
        "test-path",
    }
)

_CMD_READONLY_FILESYSTEM_COMMANDS = frozenset({"dir", "type"})

_WRITE_COMMANDS = frozenset(
    {
        "add-content",
        "copy-item",
        "mkdir",
        "move-item",
        "new-item",
        "out-file",
        "remove-item",
        "rename-item",
        "set-content",
        "set-item",
        "tee-object",
    }
)

_GIT_READONLY_NO_ARG = frozenset(
    {
        "branch",
        "help",
        "log",
        "reflog",
        "remote",
        "shortlog",
        "show-branch",
        "status",
        "tag",
    }
)

_GIT_READONLY_WITH_ARGS = frozenset(
    {
        "blame",
        "cat-file",
        "diff",
        "diff-tree",
        "grep",
        "ls-files",
        "ls-remote",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "stash",
    }
)

_GIT_WRITE_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "bisect",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "fetch",
        "merge",
        "mv",
        "pull",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "switch",
        "worktree",
    }
)

_GH_READONLY = {
    "auth": frozenset({"status"}),
    "issue": frozenset({"list", "view"}),
    "pr": frozenset({"checks", "diff", "list", "status", "view"}),
    "repo": frozenset({"list", "view"}),
    "run": frozenset({"list", "view"}),
    "workflow": frozenset({"list", "view"}),
}

_SENSITIVE_PATH_FRAGMENTS = (
    "/.env",
    "/.git/config",
    "/.git/hooks",
    "/audit/",
    "/audits/",
    "/.ssh/",
    "/id_dsa",
    "/id_ecdsa",
    "/id_ed25519",
    "/id_rsa",
    "/credentials",
    "/secrets",
)

_PROTECTED_BRANCHES = frozenset({"main", "master", "trunk", "prod", "production"})
_REMOTE_INSTALL_FETCHERS = frozenset({"curl", "wget", "invoke-webrequest", "iwr"})
_REMOTE_INSTALL_RUNNERS = frozenset({"bash", "sh", "zsh", "powershell", "pwsh", "iex", "invoke-expression"})
_BASH_CROSS_SHELLS = frozenset({"bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"})
_POWERSHELL_CROSS_SHELLS = frozenset({"bash", "sh", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"})


def validate_shell_command(
    command: str,
    shell: ShellName,
    cwd: str,
    permission_mode: str,
) -> ShellSafetyDecision:
    """Validate a shell command before launch."""

    normalized_shell = _normalize_shell_name(shell)
    cwd_text = str(Path(cwd).expanduser()) if cwd else ""
    command = command.strip()
    if not command:
        return ShellSafetyDecision(
            behavior="deny",
            normalized_command="",
            shell=normalized_shell,  # nosec B604 - dataclass field, not subprocess shell=True
            cwd=cwd_text,
            read_only=False,
            reason="Empty shell command.",
        )

    analysis = _analyze(command, normalized_shell, cwd_text)
    required_permission = None
    behavior: PermissionBehavior

    if analysis.hard_deny:
        behavior = "deny"
    elif analysis.read_only or permission_mode == "bypassPermissions":
        behavior = "allow"
    elif permission_mode == "acceptEdits" and _accept_edits_allowed(command, normalized_shell):
        # S9-13: Claude's acceptEdits mode auto-allows filesystem-modifying
        # shell commands (mkdir/touch/rm/rmdir/mv/cp/sed for bash; the
        # PowerShell New-Item / Set-Content family for powershell). All
        # other non-read-only commands still fall through to ``ask``.
        behavior = "allow"
    elif permission_mode in {"dontAsk", "plan"}:
        behavior = "deny"
    else:
        behavior = "ask"
        required_permission = _required_permission(normalized_shell, analysis.normalized_command)

    reason = analysis.reason
    if behavior == "allow" and reason is None:
        if analysis.read_only:
            reason = _READONLY_REASON
        elif permission_mode == "acceptEdits":
            reason = "Allowed by acceptEdits mode (filesystem-modifying command)."
        else:
            reason = "Allowed by permission mode."
    if behavior == "ask" and reason is None:
        reason = _ASK_REASON
    if behavior == "deny" and reason is None:
        reason = "Command denied by shell safety policy."

    return ShellSafetyDecision(
        behavior=behavior,
        normalized_command=analysis.normalized_command,
        shell=normalized_shell,  # nosec B604 - dataclass field, not subprocess shell=True
        cwd=cwd_text,
        read_only=analysis.read_only,
        reason=reason,
        required_permission=required_permission,
    )


def classify_read_only(command: str, shell: str) -> bool:
    """Return True only when a command is provably read-only for the shell."""

    try:
        normalized_shell = _normalize_shell_name(shell)
    except ValueError:
        return False
    analysis = _analyze(command.strip(), normalized_shell, "")
    return analysis.read_only and not analysis.hard_deny


def normalize_command(command: str, shell: str) -> str:
    """Normalize a shell command for permission matching."""

    try:
        normalized_shell = _normalize_shell_name(shell)
    except ValueError:
        return _collapse_ws(command)
    return _analyze(command.strip(), normalized_shell, "").normalized_command


def split_command_segments(command: str, shell: str = "bash") -> list[str]:
    """Expose parser segmentation for compatibility tests and sandbox checks."""

    try:
        normalized_shell = _normalize_shell_name(shell)
    except ValueError:
        normalized_shell = "bash"
    if normalized_shell == "powershell":
        return _powershell_segment_texts(command)
    return _bash_segment_texts(command)


def executable_name(token: str) -> str:
    """Normalize an executable token across Windows and POSIX paths."""

    return _executable_name(token)


def _normalize_shell_name(shell: str) -> ShellName:
    lower = shell.lower()
    if lower in {"bash", "sh", "zsh"}:
        return "bash"
    if lower in {"powershell", "pwsh"}:
        return "powershell"
    if lower in {"cmd", "cmd.exe"}:
        return "cmd"
    raise ValueError("Unsupported shell: %s" % shell)


def _analyze(command: str, shell: ShellName, cwd: str) -> _ShellAnalysis:
    if "\x00" in command:
        return _ShellAnalysis(
            _collapse_ws(command),
            read_only=False,
            hard_deny=True,
            reason="Null bytes are denied.",
        )
    if shell == "powershell":
        return _analyze_powershell(command, cwd)
    if shell == "cmd":
        return _analyze_cmd(command, cwd)
    return _analyze_bash(command, cwd)


def _analyze_bash(command: str, cwd: str) -> _ShellAnalysis:
    expansion_reason = _bash_forbidden_expansion_reason(command)
    if expansion_reason:
        return _ShellAnalysis(
            _collapse_ws(command),
            read_only=False,
            hard_deny=True,
            reason=expansion_reason,
        )
    pipe_reason = _remote_install_pipe_reason(command, "bash")
    if pipe_reason:
        return _ShellAnalysis(_collapse_ws(command), read_only=False, hard_deny=True, reason=pipe_reason)

    segments = _parse_bash(command)
    if not segments:
        return _ShellAnalysis(
            _collapse_ws(command),
            read_only=False,
            hard_deny=True,
            reason="Unable to parse command.",
        )

    normalized_parts: list[str] = []
    has_cd = False
    has_git = False
    read_only = True

    for segment in segments:
        normalized_parts.append(" ".join(segment.stripped_argv) if segment.stripped_argv else _collapse_ws(segment.raw))
        if segment.redirection:
            read_only = False
        if not segment.stripped_argv:
            read_only = False
            continue
        exe = segment.executable
        if exe == "cd":
            has_cd = True
        if exe == "git":
            has_git = True
        hard_deny = _bash_hard_deny(segment, cwd)
        if hard_deny:
            return _ShellAnalysis(
                " | ".join(normalized_parts),
                read_only=False,
                hard_deny=True,
                reason=hard_deny,
            )
        if not _bash_command_is_read_only(segment, cwd):
            read_only = False

    if len(segments) > 1 and has_cd and has_git:
        return _ShellAnalysis(
            " | ".join(normalized_parts),
            read_only=False,
            reason="Compound commands with cd and git require approval.",
        )

    return _ShellAnalysis(" | ".join(normalized_parts), read_only=read_only)


def _analyze_powershell(command: str, cwd: str) -> _ShellAnalysis:
    pipe_reason = _remote_install_pipe_reason(command, "powershell")
    if pipe_reason:
        return _ShellAnalysis(_collapse_ws(command), read_only=False, hard_deny=True, reason=pipe_reason)

    parsed = _parse_powershell(command)
    if not parsed:
        return _ShellAnalysis(
            _collapse_ws(command),
            read_only=False,
            hard_deny=True,
            reason="Unable to parse command.",
        )

    all_commands = [cmd for statement in parsed for cmd in statement]
    if not all_commands:
        return _ShellAnalysis(
            _collapse_ws(command),
            read_only=False,
            hard_deny=True,
            reason="Empty PowerShell command.",
        )

    normalized_parts: list[str] = []
    total_commands = len(all_commands)
    has_cwd_changer = False
    read_only = True

    for command_ast in all_commands:
        normalized_parts.append(" ".join(token.text for token in command_ast.argv))
        variable_reason = _powershell_variable_exfil_reason(command_ast.argv)
        if variable_reason:
            return _ShellAnalysis(
                " | ".join(normalized_parts),
                read_only=False,
                hard_deny=True,
                reason=variable_reason,
            )
        if command_ast.redirection:
            read_only = False
        name = _powershell_canonical_name(command_ast.executable)
        if name in _POWERSHELL_CWD_CHANGERS:
            has_cwd_changer = True
        hard_deny = _powershell_hard_deny(command_ast, cwd)
        if hard_deny:
            return _ShellAnalysis(
                " | ".join(normalized_parts),
                read_only=False,
                hard_deny=True,
                reason=hard_deny,
            )
        if not _powershell_command_is_read_only(command_ast, cwd):
            read_only = False

    if total_commands > 1 and has_cwd_changer:
        return _ShellAnalysis(
            " | ".join(normalized_parts),
            read_only=False,
            reason="Compound PowerShell commands that change cwd require approval.",
        )

    return _ShellAnalysis(" | ".join(normalized_parts), read_only=read_only)


def _analyze_cmd(command: str, cwd: str) -> _ShellAnalysis:
    pipe_reason = _remote_install_pipe_reason(command, "cmd")
    if pipe_reason:
        return _ShellAnalysis(_collapse_ws(command), read_only=False, hard_deny=True, reason=pipe_reason)

    segments = _parse_cmd(command)
    if not segments:
        return _ShellAnalysis(
            _collapse_ws(command),
            read_only=False,
            hard_deny=True,
            reason="Unable to parse command.",
        )

    normalized_parts: list[str] = []
    read_only = True
    for segment in segments:
        normalized_parts.append(" ".join(segment.stripped_argv) if segment.stripped_argv else _collapse_ws(segment.raw))
        if segment.redirection:
            read_only = False
        hard_deny = _cmd_hard_deny(segment, cwd)
        if hard_deny:
            return _ShellAnalysis(
                " & ".join(normalized_parts),
                read_only=False,
                hard_deny=True,
                reason=hard_deny,
            )
        if not _cmd_command_is_read_only(segment, cwd):
            read_only = False

    return _ShellAnalysis(" & ".join(normalized_parts), read_only=read_only)


# S9-15 / #860: characters where an unquoted backslash is genuinely
# load-bearing in POSIX shell (whitespace, quote chars, `$`, backtick, the
# control operators, backslash itself). Before an ORDINARY character -- a
# letter, digit, `:`, `/`, `.`, `-`, `_` -- an unquoted backslash is a bash
# no-op, and it is ALSO a Windows path separator. Mirrors
# .claude/hooks/_shell_tokenize.py's _POSIX_ESCAPABLE (same defect class,
# same fix shape); duplicated here rather than imported because .claude/hooks/
# is dev-tooling for this checkout and must stay import-free of intent/ (and
# vice versa) per hooks-dev's "stdlib-only, no project imports" convention.
_POSIX_ESCAPABLE = set(" \t\r\n'\"`$\\|&;()<>*?[]{}~#!")
_DQUOTE_ESCAPABLE = set('$`"\\\n')


def _tokenize_literal_bash(text: str) -> list[str]:
    """``shlex.split(text, posix=True)``'s word boundaries, but preserving a
    backslash bash would silently drop as a no-op before an ordinary
    character, so a native Windows path token survives intact.

    This is the DIVERGENCE fix (not just a symptom patch): a bash-classified,
    multi-segment command is validated here but is NOT necessarily executed
    by bash -- ``intent/tools/shell.py`` dispatches a multi-segment command to
    ``asyncio.create_subprocess_shell``, which on Windows invokes ``cmd.exe``
    (confirmed: ``echo %CD%`` expands and a raw ``C:\\Users\\x\\.ssh\\id_rsa``
    argument survives byte-for-byte through it), NOT bash. ``cmd.exe`` does
    not treat backslash as an escape at all, so the string this validator
    reasons about and the string that actually reaches the filesystem
    diverged: ``_sensitive_path_reason`` checked the mangled
    ``shlex.split(..., posix=True)`` reading, found no ``/``-delimited
    fragment, and a `.ssh`/`.env`/`credentials`/`id_rsa` path sailed through
    as "not sensitive" while cmd.exe resolved the real file.

    Every check in this module that treats a token as a REAL FILESYSTEM PATH
    (never one that only matches a bare keyword/flag) must read this instead
    of the escape-stripped ``stripped_argv``. This is safe to use
    UNCONDITIONALLY, not just on Windows: on a single-segment command
    (direct-exec via the SAME ``shlex.split(..., posix=True)`` argv this
    validator saw) or on POSIX where multi-segment execution goes through
    ``/bin/sh`` (real POSIX escape semantics, matching the escape-stripped
    reading), preserving the backslash here only makes the sensitive-path/
    sandbox-escape checks MORE conservative than the string that will
    actually execute -- it can turn an allow into an ask, never the reverse.
    Genuine POSIX escapes (an escaped space, quote, `$`, backtick, operator)
    are still resolved the bash way; only a backslash before an ordinary
    character -- ambiguous between "POSIX no-op" and "Windows separator" --
    is kept, which is exactly the class this function exists to preserve."""
    words: list[str] = []
    word: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            else:
                word.append(ch)
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < len(text):
                nxt = text[i + 1]
                if nxt in _DQUOTE_ESCAPABLE:
                    word.append(nxt)
                else:
                    word.append(ch)
                    word.append(nxt)
                i += 2
                continue
            if ch == '"':
                quote = None
            else:
                word.append(ch)
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            i += 1
            continue
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt not in _POSIX_ESCAPABLE:
                word.append(ch)
            word.append(nxt)
            i += 2
            continue
        if ch in " \t\r\n":
            if word:
                words.append("".join(word))
                word = []
            i += 1
            continue
        word.append(ch)
        i += 1
    if word:
        words.append("".join(word))
    return words


def _parse_bash(command: str) -> list[_SimpleCommand]:
    segments: list[_SimpleCommand] = []
    for text in _bash_segment_texts(command):
        try:
            argv = tuple(_Token(part) for part in shlex.split(text, posix=True))
        except ValueError:
            return []
        literal_words = _tokenize_literal_bash(text)
        if len(literal_words) != len(argv):  # pragma: no cover - defensive fallback
            literal_words = [token.text for token in argv]
        literal_argv = tuple(_Token(w) for w in literal_words)
        stripped, redirection = _strip_redirections(argv)
        literal_stripped, _literal_redirection = _strip_redirections(literal_argv)
        if len(literal_stripped) != len(stripped):  # pragma: no cover - defensive fallback
            literal_stripped = stripped
        stripped = tuple(_strip_bash_wrappers(list(stripped)))
        literal_stripped = tuple(_strip_bash_wrappers(list(literal_stripped)))
        if len(literal_stripped) != len(stripped):  # pragma: no cover - defensive fallback
            literal_stripped = stripped
        segments.append(
            _SimpleCommand(
                raw=text,
                argv=argv,
                stripped_argv=stripped,
                redirection=redirection,
                literal_stripped_argv=literal_stripped,
            )
        )
    return segments


def _parse_cmd(command: str) -> list[_SimpleCommand]:
    segments: list[_SimpleCommand] = []
    for text in _bash_segment_texts(command):
        try:
            argv = tuple(_Token(part) for part in shlex.split(text, posix=False))
        except ValueError:
            return []
        stripped, redirection = _strip_redirections(argv)
        segments.append(
            _SimpleCommand(
                raw=text,
                argv=argv,
                stripped_argv=tuple(token.text for token in stripped),
                redirection=redirection,
            )
        )
    return segments


def _parse_powershell(command: str) -> list[list[_SimpleCommand]]:
    parsed: list[list[_SimpleCommand]] = []
    for statement in _split_shell_like(command, (";", "&&", "||"), escape_char="`"):
        pipeline: list[_SimpleCommand] = []
        for part in _split_shell_like(statement, ("|",), escape_char="`"):
            tokens = tuple(_tokenize_powershell(part))
            stripped, redirection = _strip_redirections(tokens)
            if stripped:
                pipeline.append(
                    _SimpleCommand(
                        raw=part,
                        argv=tokens,
                        stripped_argv=tuple(token.text for token in stripped),
                        redirection=redirection,
                    )
                )
        if pipeline:
            parsed.append(pipeline)
    return parsed


def _bash_segment_texts(command: str) -> list[str]:
    segments: list[str] = []
    for chain_part in _split_shell_like(command, ("&&", "||", ";", "&"), escape_char="\\"):
        segments.extend(_split_shell_like(chain_part, ("|",), escape_char="\\"))
    return segments


def _powershell_segment_texts(command: str) -> list[str]:
    segments: list[str] = []
    for chain_part in _split_shell_like(command, (";", "&&", "||"), escape_char="`"):
        segments.extend(_split_shell_like(chain_part, ("|",), escape_char="`"))
    return segments


def _split_shell_like(text: str, operators: tuple[str, ...], *, escape_char: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    sorted_ops = tuple(sorted(operators, key=len, reverse=True))
    while i < len(text):
        char = text[i]
        if quote is None and any(text.startswith(op, i) for op in sorted_ops):
            op = next(op for op in sorted_ops if text.startswith(op, i))
            part = "".join(buf).strip()
            if part:
                parts.append(part)
            buf = []
            i += len(op)
            continue
        if char == escape_char:
            buf.append(char)
            if i + 1 < len(text):
                i += 1
                buf.append(text[i])
            i += 1
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            buf.append(char)
            i += 1
            continue
        buf.append(char)
        i += 1
    part = "".join(buf).strip()
    if part:
        parts.append(part)
    return parts


def _tokenize_powershell(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    buf: list[str] = []
    quote: Literal["single", "double"] | None = None
    active_quote: Literal["single", "double"] | None = None
    i = 0
    while i < len(text):
        char = text[i]
        if quote is None and char.isspace():
            if buf:
                tokens.append(_Token("".join(buf), active_quote))
                buf = []
                active_quote = None
            i += 1
            continue
        if char == "`":
            if i + 1 < len(text):
                i += 1
                buf.append(text[i])
            i += 1
            continue
        if char == "'" and quote is None:
            quote = "single"
            active_quote = active_quote or quote
            i += 1
            continue
        if char == '"' and quote is None:
            quote = "double"
            active_quote = active_quote or quote
            i += 1
            continue
        if char == "'" and quote == "single":
            quote = None
            i += 1
            continue
        if char == '"' and quote == "double":
            quote = None
            i += 1
            continue
        buf.append(char)
        i += 1
    if buf:
        tokens.append(_Token("".join(buf), active_quote))
    return tokens


def _strip_redirections(argv: tuple[_Token, ...]) -> tuple[tuple[_Token, ...], bool]:
    stripped: list[_Token] = []
    redirection = False
    skip_next = False
    for token in argv:
        if skip_next:
            skip_next = False
            continue
        text = token.text
        if _is_redirection_token(text):
            redirection = True
            if _redirection_needs_target(text):
                skip_next = True
            continue
        stripped.append(token)
    return tuple(stripped), redirection


def _is_redirection_token(token: str) -> bool:
    lower = token.lower()
    if lower in {"<", ">", ">>", "1>", "1>>", "2>", "2>>", "&>", "*>", "2>&1", "1>&2"}:
        return True
    return lower.startswith((">", ">>", "1>", "2>", "&>", "*>"))


def _redirection_needs_target(token: str) -> bool:
    return token in {"<", ">", ">>", "1>", "1>>", "2>", "2>>", "&>", "*>"}


def _strip_bash_wrappers(argv: list[_Token]) -> list[str]:
    words = [token.text for token in argv]
    changed = True
    while changed and words:
        changed = False
        while words and _is_env_assignment(words[0]):
            words = words[1:]
            changed = True
        if not words:
            break
        exe = _executable_name(words[0])
        if exe in {"time", "nohup"}:
            words = words[2:] if len(words) > 1 and words[1] == "--" else words[1:]
            changed = True
        elif exe == "nice":
            words = _strip_nice(words)
            changed = True
        elif exe == "timeout":
            stripped = _strip_timeout(words)
            if stripped == words:
                break
            words = stripped
            changed = True
    return words


def _strip_nice(words: list[str]) -> list[str]:
    if len(words) >= 3 and words[1] == "-n":
        return words[3:]
    if len(words) >= 2 and words[1].startswith("-"):
        return words[2:]
    return words[1:]


def _strip_timeout(words: list[str]) -> list[str]:
    i = 1
    while i < len(words):
        arg = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if arg in {"--foreground", "--preserve-status", "--verbose", "-v"}:
            i += 1
        elif arg.startswith("--kill-after=") or arg.startswith("--signal="):
            if not _timeout_flag_value_safe(arg.split("=", 1)[1]):
                return words
            i += 1
        elif arg in {"--kill-after", "--signal", "-k", "-s"} and nxt and _timeout_flag_value_safe(nxt):
            i += 2
        elif arg.startswith(("-k", "-s")) and _timeout_flag_value_safe(arg[2:]):
            i += 1
        elif arg == "--":
            i += 1
            break
        elif arg.startswith("-"):
            return words
        else:
            break
    if i >= len(words):
        return words
    # Duration token.
    if not _timeout_flag_value_safe(words[i]):
        return words
    return words[i + 1 :]


def _timeout_flag_value_safe(value: str) -> bool:
    return bool(value) and all(char.isalnum() or char in "_.+-" for char in value)


def _is_env_assignment(token: str) -> bool:
    if "=" not in token or token.startswith("="):
        return False
    name = token.split("=", 1)[0]
    return bool(name) and all(char == "_" or char.isalnum() for char in name) and not name[0].isdigit()


def _bash_forbidden_expansion_reason(command: str) -> str | None:
    quote: str | None = None
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\":
            i += 2
            continue
        if char == "'" and quote is None:
            quote = "'"
            i += 1
            continue
        if char == "'" and quote == "'":
            quote = None
            i += 1
            continue
        if char == '"' and quote is None:
            quote = '"'
            i += 1
            continue
        if char == '"' and quote == '"':
            quote = None
            i += 1
            continue
        if quote != "'":
            if char == "`":
                return "Backtick command substitution is denied."
            if _starts_dollar_paren(command, i) or command.startswith("<(", i) or command.startswith(">(", i):
                return "Shell command or process substitution is denied."
            if command.startswith("${", i) or command.startswith("$((", i):
                return "Shell expansion is denied because it can hide exfiltration."
            if char == "$" and i + 1 < len(command) and (command[i + 1].isalpha() or command[i + 1] == "_"):
                return "Shell variable expansion is denied because it can exfiltrate environment data."
        i += 1
    return None


def _bash_hard_deny(command: _SimpleCommand, cwd: str) -> str | None:
    argv = command.stripped_argv
    if not argv:
        return None
    exe = command.executable
    lower_args = [arg.lower() for arg in argv[1:]]
    # #860: every check below that treats a token as a REAL FILESYSTEM PATH
    # (never one that only matches a bare keyword/flag) reads literal_args,
    # not argv -- see _tokenize_literal_bash for why argv alone is not safe
    # to use as a path here.
    literal_args = list(command.literal_stripped_argv[1:])

    if exe == "rm" and _has_recursive_force(lower_args) and any(_is_root_path(arg) for arg in literal_args):
        return "Recursive deletion of a filesystem root is denied."
    if (
        exe == "rmdir"
        and any(arg.lower() in {"/s", "/q"} for arg in argv[1:])
        and any(_is_root_path(arg) for arg in literal_args)
    ):
        return "Recursive removal of a drive root is denied."
    if exe.startswith("mkfs") or exe in {"shutdown", "reboot", "halt"}:
        return "System-destructive command is denied."
    if exe == "init" and lower_args[:1] == ["0"]:
        return "System shutdown command is denied."
    if exe == "format":
        return "Disk format command is denied."
    if exe == "dd" and any(arg.startswith("of=/dev/") for arg in lower_args):
        return "Raw device overwrite is denied."
    if exe == "reg" and lower_args[:1] == ["delete"]:
        return "Registry delete command is denied."
    if exe == "net" and "user" in lower_args and "/delete" in lower_args:
        return "User deletion command is denied."
    if exe in {"python", "python3"} and any(arg == "-c" for arg in lower_args):
        return "Inline Python execution is denied."
    if exe == "node" and any(arg in {"-e", "--eval", "--input-type", "-r", "--require"} for arg in lower_args):
        return "Inline Node execution is denied."
    if exe == "git" and lower_args[:1] == ["ls-remote"] and _repo_arg_is_dangerous(list(argv[2:])):
        return "git ls-remote to external URL-like repositories is denied."
    git_push_reason = _direct_git_push_reason(lower_args)
    if exe == "git" and git_push_reason:
        return git_push_reason
    if exe == "gh" and _repo_arg_is_dangerous(list(argv[1:])):
        return "GitHub CLI repository arguments that can exfiltrate to another host are denied."
    aws_reason = _aws_recursive_delete_reason(lower_args)
    if exe == "aws" and aws_reason:
        return aws_reason
    kubectl_reason = _kubectl_production_delete_reason(lower_args)
    if exe == "kubectl" and kubectl_reason:
        return kubectl_reason
    if exe in {"rm", "rmdir"}:
        destructive_reason = _destructive_sensitive_path_reason(literal_args)
        if destructive_reason:
            return destructive_reason
    if exe in {"powershell", "pwsh"}:
        return _nested_powershell_guard(argv[1:], cwd)
    if exe in {"bash", "sh", "zsh", "cmd", "cmd.exe"}:
        return _nested_cross_shell_guard(exe, argv[1:], cwd)
    if exe in _BASH_READONLY_FILESYSTEM_COMMANDS:
        escape_reason = _readonly_path_escape_reason(literal_args, cwd, raw=command.raw)
        if escape_reason:
            return escape_reason
    return _sensitive_path_reason(literal_args)


def _powershell_hard_deny(command: _SimpleCommand, cwd: str) -> str | None:
    argv = command.stripped_argv
    if not argv:
        return None
    name = _powershell_canonical_name(command.executable)
    args = list(argv[1:])
    lower_args = [arg.lower() for arg in args]

    if name in {"invoke-expression", "iex"}:
        return "PowerShell dynamic expression execution is denied."
    if name in _WRITE_COMMANDS and _contains_string_built_path(command.argv):
        return "PowerShell string-built file operation is denied."
    if name == "git":
        git_push_reason = _direct_git_push_reason(lower_args)
        if git_push_reason:
            return git_push_reason
    if name == "aws":
        aws_reason = _aws_recursive_delete_reason(lower_args)
        if aws_reason:
            return aws_reason
    if name == "kubectl":
        kubectl_reason = _kubectl_production_delete_reason(lower_args)
        if kubectl_reason:
            return kubectl_reason
    if name == "remove-item" and any(arg in {"-recurse", "-r"} for arg in lower_args):
        return "PowerShell recursive deletion requires a separately verified filesystem operation."
    if name in {"remove-item", "del", "erase", "rmdir"}:
        destructive_reason = _destructive_sensitive_path_reason(args)
        if destructive_reason:
            return destructive_reason
    if name in _POWERSHELL_CROSS_SHELLS:
        return _nested_cross_shell_guard(name, args, cwd)
    if name in _POWERSHELL_READONLY_FILESYSTEM_CMDLETS:
        escape_reason = _readonly_path_escape_reason(args, cwd, raw=command.raw)
        if escape_reason:
            return escape_reason
    return _sensitive_path_reason(args)


def _cmd_hard_deny(command: _SimpleCommand, cwd: str) -> str | None:
    argv = command.stripped_argv
    if not argv:
        return None
    exe = command.executable
    args = list(argv[1:])
    lower_args = [arg.lower().strip('"') for arg in args]
    if exe in {"del", "erase", "rmdir", "rd"}:
        return "Destructive cmd.exe filesystem command is denied."
    if exe == "format":
        return "Disk format command is denied."
    if exe == "gh" and _repo_arg_is_dangerous(args):
        return "GitHub CLI repository arguments that can exfiltrate to another host are denied."
    if exe == "git" and lower_args[:1] == ["ls-remote"] and _repo_arg_is_dangerous(args[1:]):
        return "git ls-remote to external URL-like repositories is denied."
    if exe == "git":
        git_push_reason = _direct_git_push_reason(lower_args)
        if git_push_reason:
            return git_push_reason
    if exe == "aws":
        aws_reason = _aws_recursive_delete_reason(lower_args)
        if aws_reason:
            return aws_reason
    if exe == "kubectl":
        kubectl_reason = _kubectl_production_delete_reason(lower_args)
        if kubectl_reason:
            return kubectl_reason
    if exe == "cmd" and any(arg in {"/c", "/r"} for arg in lower_args):
        nested = _join_after_switch(args, {"/c", "/r"})
        if nested:
            decision = validate_shell_command(nested, "cmd", cwd, "default")
            if decision.behavior != "allow":
                return "Nested cmd.exe command is not provably read-only."
    if exe in _POWERSHELL_CROSS_SHELLS:
        return _nested_cross_shell_guard(exe, args, cwd)
    if exe in _CMD_READONLY_FILESYSTEM_COMMANDS:
        escape_reason = _readonly_path_escape_reason(args, cwd, raw=command.raw)
        if escape_reason:
            return escape_reason
    return _sensitive_path_reason(args)


def _nested_powershell_guard(args: tuple[str, ...], cwd: str) -> str | None:
    lower_args = [arg.lower() for arg in args]
    if any(arg in {"-encodedcommand", "-enc", "-e"} for arg in lower_args):
        return "PowerShell encoded commands are denied."
    nested = _join_after_switch(list(args), {"-command", "-c"})
    if nested is None:
        return None
    decision = validate_shell_command(nested, "powershell", cwd, "default")
    if decision.behavior == "allow":
        return None
    return "Nested PowerShell command is not provably read-only."


def _nested_cross_shell_guard(exe: str, args: list[str] | tuple[str, ...], cwd: str) -> str | None:
    lower_args = [arg.lower() for arg in args]
    if exe in {"powershell", "pwsh"}:
        return _nested_powershell_guard(tuple(args), cwd)
    if exe in {"bash", "sh", "zsh"} and any(arg in {"-c", "-command"} for arg in lower_args):
        nested = _join_after_switch(list(args), {"-c", "-command"})
        if nested:
            decision = validate_shell_command(nested, "bash", cwd, "default")
            if decision.behavior != "allow":
                return "Nested POSIX shell command is not provably read-only."
    if exe in {"cmd", "cmd.exe"} and any(arg in {"/c", "/r"} for arg in lower_args):
        nested = _join_after_switch(list(args), {"/c", "/r"})
        if nested:
            decision = validate_shell_command(nested, "cmd", cwd, "default")
            if decision.behavior != "allow":
                return "Nested cmd.exe command is not provably read-only."
    return None


def _join_after_switch(args: list[str], switches: set[str]) -> str | None:
    for i, arg in enumerate(args):
        if arg.lower() in switches and i + 1 < len(args):
            return " ".join(args[i + 1 :]).strip()
    return None


def _bash_command_is_read_only(command: _SimpleCommand, cwd: str) -> bool:
    argv = command.stripped_argv
    if not argv or command.redirection:
        return False
    exe = command.executable
    args = list(argv[1:])
    # #860: sensitive-path detection reads the literal (backslash-preserving)
    # args, not the escape-stripped ones -- see _tokenize_literal_bash.
    if _sensitive_path_reason(list(command.literal_stripped_argv[1:])):
        return False
    if exe == "cd":
        return False
    if exe in _BASH_READONLY_SIMPLE:
        return True
    return _external_command_is_read_only(exe, args, cwd)


def _powershell_command_is_read_only(command: _SimpleCommand, cwd: str) -> bool:
    argv = command.stripped_argv
    if not argv or command.redirection:
        return False
    name = _powershell_canonical_name(command.executable)
    args = list(argv[1:])
    if _sensitive_path_reason(args):
        return False
    if name in _POWERSHELL_CWD_CHANGERS or name in _WRITE_COMMANDS:
        return False
    if name in _POWERSHELL_READONLY_CMDLETS:
        return True
    return _external_command_is_read_only(name, args, cwd)


def _cmd_command_is_read_only(command: _SimpleCommand, cwd: str) -> bool:
    argv = command.stripped_argv
    if not argv or command.redirection:
        return False
    exe = command.executable
    args = list(argv[1:])
    if "%" in " ".join(args):
        return False
    if _sensitive_path_reason(args):
        return False
    if exe in {
        "dir",
        "echo",
        "hostname",
        "ping",
        "type",
        "uname",
        "ver",
        "where",
        "which",
        "whoami",
    }:
        return True
    return _external_command_is_read_only(exe, args, cwd)


def _external_command_is_read_only(exe: str, args: list[str], cwd: str) -> bool:
    del cwd
    if exe == "git":
        return _git_is_read_only(args)
    if exe == "gh":
        return _gh_is_read_only(args)
    if exe == "docker":
        return _docker_is_read_only(args)
    if exe == "rg":
        return _rg_is_read_only(args)
    if exe == "pyright":
        return _pyright_is_read_only(args)
    if exe in {"python", "python3"}:
        return args in (["--version"], ["-V"], ["-VV"]) or not args
    if exe in {"pip", "pip3"}:
        return bool(args) and args[0] in {"list", "show", "freeze", "--version", "-V"}
    if exe == "npm":
        return bool(args) and args[0] in {"list", "view", "ls", "--version", "-v"}
    if exe == "node":
        return args in (["--version"], ["-v"]) or not args
    if exe == "ffprobe":
        return True
    if exe in {"ffmpeg", "ffplay"}:
        return not args or any(arg in {"-version", "-h", "--help", "-help"} for arg in args)
    if exe == "npx":
        return bool(args) and args[-1] in {"--version", "-v"}
    return False


def _git_is_read_only(args: list[str]) -> bool:
    if not args:
        return False
    subcommand = args[0].lower()
    if subcommand in _GIT_WRITE_SUBCOMMANDS:
        return False
    # S9-12: per-flag write detection. ``git diff --output=patch.txt`` (and
    # the equivalent ``-o patch.txt`` / ``--output patch.txt``) writes the
    # diff payload to a file even though ``diff`` itself only reads the
    # working tree. The same goes for ``git format-patch``-style options
    # that exit through a "write to file" code path. Match BEFORE the
    # _GIT_READONLY allowlists below so the false-allow is closed.
    if _git_args_have_output_write(args[1:]):
        return False
    if subcommand in {"branch", "tag"}:
        # `git tag foo` and `git branch foo` create refs. Only listing modes are safe.
        return len(args) == 1 or all(arg.startswith("-") for arg in args[1:])
    if subcommand == "remote":
        return len(args) == 1 or args[1].lower() in {"-v", "show", "get-url"}
    if subcommand == "stash":
        return len(args) > 1 and args[1].lower() in {"list", "show"}
    if subcommand == "ls-remote" and _repo_arg_is_dangerous(args[1:]):
        return False
    return subcommand in _GIT_READONLY_NO_ARG or subcommand in _GIT_READONLY_WITH_ARGS


# S9-12: git flag forms that write payload to a file. Matches both
# ``--output=path``/``-O=path`` (=value) and ``--output path``/``-o path``
# (space-separated value).  Conservative — when in doubt we fail to ``ask``.
_GIT_WRITE_FLAG_EQUALS: frozenset[str] = frozenset(
    {
        "--output",
        "--output-directory",
        "-o",
        "-O",
    }
)
_GIT_WRITE_FLAG_VALUE: frozenset[str] = frozenset(
    {
        "--output",
        "--output-directory",
        "-o",
        "-O",
    }
)


def _git_args_have_output_write(args: list[str]) -> bool:
    """Return True if subcommand args include a "write payload to file" flag."""

    i = 0
    while i < len(args):
        token = args[i]
        if "=" in token:
            flag = token.split("=", 1)[0]
            if flag in _GIT_WRITE_FLAG_EQUALS:
                return True
        elif token in _GIT_WRITE_FLAG_VALUE and i + 1 < len(args):
            value = args[i + 1]
            # `-o` without a value is meaningless for git; `-o /dev/stdout`
            # is technically a stdout redirect but we keep the policy strict.
            if value and not value.startswith("-"):
                return True
        i += 1
    return False


def _gh_is_read_only(args: list[str]) -> bool:
    if not args or _repo_arg_is_dangerous(args):
        return False
    group = args[0].lower()
    action = args[1].lower() if len(args) > 1 else ""
    return action in _GH_READONLY.get(group, frozenset())


def _docker_is_read_only(args: list[str]) -> bool:
    if not args:
        return False
    if args[:1] in (["ps"], ["images"]):
        return True
    return len(args) >= 2 and args[0] in {"logs", "inspect"}


def _rg_is_read_only(args: list[str]) -> bool:
    unsafe_flags = {"--files-with-matches-and-replace", "--replace", "-r"}
    return not any(arg in unsafe_flags or arg.startswith("--replace=") for arg in args)


def _pyright_is_read_only(args: list[str]) -> bool:
    return not any(arg in {"--watch", "-w"} for arg in args)


def _repo_arg_is_dangerous(args: list[str]) -> bool:
    for token in args:
        value = token
        if token.startswith("-"):
            if "=" not in token:
                continue
            value = token.split("=", 1)[1]
        if "://" in value or "@" in value:
            return True
        if value.count("/") >= 2:
            return True
    return False


def _starts_dollar_paren(command: str, index: int) -> bool:
    if command[index] != "$":
        return False
    i = index + 1
    while i < len(command) and command[i].isspace():
        i += 1
    return i < len(command) and command[i] == "("


def _powershell_variable_exfil_reason(argv: tuple[_Token, ...]) -> str | None:
    for token in argv:
        if token.quote != "single" and "$" in token.text:
            return "PowerShell variable expansion is denied because it can exfiltrate environment data."
    return None


def _contains_string_built_path(argv: tuple[_Token, ...]) -> bool:
    for token in argv:
        if token.quote != "single" and any(marker in token.text for marker in {"$", "$(", "+", "{", "}"}):
            return True
    return False


def _powershell_canonical_name(name: str) -> str:
    lower = _executable_name(name).lower()
    if lower.endswith(".exe"):
        return lower
    return _POWERSHELL_ALIAS_MAP.get(lower, lower)


def _readonly_path_escape_reason(args: list[str] | tuple[str, ...], cwd: str, *, raw: str = "") -> str | None:
    """Reject auto-approved read-only commands that target outside cwd."""

    raw_path = raw.replace("\\", "/").lower()
    if re.search(r"(^|[\s\"'])//[^/\s\"']+/[^/\s\"']+", raw_path):
        return "Read-only command targets a UNC/network path."
    if re.search(r"(^|\s)\.\.(?:/|$)", raw_path):
        return "Read-only command contains parent-directory traversal."

    cwd_path: Path | None = None
    if cwd:
        try:
            cwd_path = Path(cwd).expanduser().resolve()
        except OSError:
            cwd_path = None
    for arg in args:
        if not arg or arg.startswith("-"):
            continue
        value = arg.strip("\"'")
        if "\x00" in value or any(ord(char) < 32 for char in value):
            return "Read-only command argument contains control characters."
        normalized = value.replace("\\", "/")
        lower = normalized.lower()
        if lower.startswith("//"):
            return "Read-only command targets a UNC/network path."
        if lower.startswith("~"):
            return "Read-only command targets a home-directory path outside the sandbox."
        if lower in {"..", "."}:
            if lower == "..":
                return "Read-only command targets a parent directory outside the sandbox."
            continue
        if lower.startswith("../") or "/../" in lower or lower.endswith("/.."):
            return "Read-only command contains parent-directory traversal."
        if re.match(r"^[a-z]:", lower):
            if not Path(value).is_absolute():
                return "Read-only command targets a Windows drive path outside the sandbox."
        if re.match(r"^[a-z]:", lower) or normalized.startswith("/"):
            if cwd_path is None:
                return "Read-only command targets an absolute path."
            try:
                candidate = Path(value).expanduser().resolve()
                candidate.relative_to(cwd_path)
            except (OSError, ValueError):
                return "Read-only command targets an absolute path outside the sandbox."
            continue
        if cwd_path is not None:
            try:
                candidate = (cwd_path / value).resolve()
                candidate.relative_to(cwd_path)
            except (OSError, ValueError):
                return "Read-only command targets a resolved path outside the sandbox."
    return None


def _sensitive_path_reason(args: list[str] | tuple[str, ...]) -> str | None:
    for arg in args:
        if not arg or arg.startswith("-"):
            continue
        normalized = arg.strip("\"'").replace("\\", "/")
        lower = normalized.lower()
        if lower.startswith("//"):
            return "UNC/network paths require explicit approval."
        comparable = "/" + lower.removeprefix("./").removeprefix("/")
        if any(fragment in lower or fragment in comparable for fragment in _SENSITIVE_PATH_FRAGMENTS):
            return "Command references a sensitive local path."
    return None


def _destructive_sensitive_path_reason(args: list[str] | tuple[str, ...]) -> str | None:
    reason = _sensitive_path_reason(args)
    if reason:
        return "Destructive command targets a sensitive local path."
    return None


def _remote_install_pipe_reason(command: str, shell: ShellName) -> str | None:
    if "|" not in command:
        return None
    segments = _powershell_segment_texts(command) if shell == "powershell" else _bash_segment_texts(command)
    saw_fetcher = False
    for segment in segments:
        executable = _segment_executable(segment, shell)
        if not executable:
            continue
        if executable in _REMOTE_INSTALL_FETCHERS:
            saw_fetcher = True
            continue
        if saw_fetcher and executable in _REMOTE_INSTALL_RUNNERS:
            return "Piping a remote fetcher into a shell or expression evaluator is denied."
    return None


def _segment_executable(segment: str, shell: ShellName) -> str:
    try:
        if shell == "powershell":
            tokens = tuple(_tokenize_powershell(segment))
            stripped, _redirection = _strip_redirections(tokens)
            if not stripped:
                return ""
            return _powershell_canonical_name(stripped[0].text)
        parts = shlex.split(segment, posix=shell != "cmd")
    except ValueError:
        return ""
    if not parts:
        return ""
    stripped = _strip_bash_wrappers([_Token(part) for part in parts]) if shell == "bash" else parts
    if not stripped:
        return ""
    return _executable_name(stripped[0])


def _direct_git_push_reason(lower_args: list[str]) -> str | None:
    if lower_args[:1] != ["push"]:
        return None
    targets = [arg for arg in lower_args[1:] if arg and not arg.startswith("-")]
    for target in targets:
        branch = target.split(":", 1)[-1].rsplit("/", 1)[-1]
        if branch in _PROTECTED_BRANCHES:
            return "Direct git push to a protected branch is denied."
    return None


def _aws_recursive_delete_reason(lower_args: list[str]) -> str | None:
    if len(lower_args) < 3 or lower_args[:2] != ["s3", "rm"]:
        return None
    if any(arg in {"--recursive", "--include", "--exclude"} for arg in lower_args):
        return "Recursive or wildcard S3 deletion is denied."
    return None


def _kubectl_production_delete_reason(lower_args: list[str]) -> str | None:
    if lower_args[:1] != ["delete"]:
        return None
    if any(arg in {"prod", "production"} for arg in lower_args[1:]):
        return "kubectl delete against a production namespace is denied."
    for index, arg in enumerate(lower_args[1:], start=1):
        if (
            arg in {"-n", "--namespace"}
            and index + 1 < len(lower_args)
            and lower_args[index + 1]
            in {
                "prod",
                "production",
            }
        ):
            return "kubectl delete against a production namespace is denied."
        if arg.startswith("--namespace=") and arg.split("=", 1)[1] in {
            "prod",
            "production",
        }:
            return "kubectl delete against a production namespace is denied."
    return None


def _has_recursive_force(args: list[str]) -> bool:
    flags = "".join(arg[1:] for arg in args if arg.startswith("-"))
    return "r" in flags and "f" in flags


def _is_root_path(value: str) -> bool:
    stripped = value.strip("\"'").replace("\\", "/").rstrip()
    return stripped in {"/", "/*"} or (len(stripped) == 2 and stripped[1] == ":") or stripped.endswith(":/")


def _executable_name(token: str) -> str:
    token = token.strip().strip("\"'")
    name = ntpath.basename(posixpath.basename(token))
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name.lower()


def _collapse_ws(command: str) -> str:
    return " ".join(command.strip().split())


def _required_permission(shell: ShellName, command: str) -> str:
    label = {"bash": "Bash", "powershell": "PowerShell", "cmd": "Cmd"}[shell]
    return "%s(%s:*)" % (label, command)


# S9-13: filesystem-modifying commands auto-allowed in Claude's acceptEdits
# mode. Mirrors src/tools/BashTool/modeValidation.ts (mkdir/touch/rm/rmdir/
# mv/cp/sed) and src/tools/PowerShellTool/modeValidation.ts's "Set-Content/
# Out-File/Remove-Item/New-Item" family. Compound shell chains are NOT
# auto-allowed — Claude's split-command loop runs per-subcommand and
# bails the first time one fails the allowlist. We require every segment
# to be in the allowlist before granting the auto-allow.
_ACCEPT_EDITS_BASH_ALLOWED: frozenset[str] = frozenset(
    {
        "mkdir",
        "touch",
        "rm",
        "rmdir",
        "mv",
        "cp",
        "sed",
    }
)
_ACCEPT_EDITS_POWERSHELL_ALLOWED: frozenset[str] = frozenset(
    {
        "new-item",
        "remove-item",
        "set-content",
        "add-content",
        "out-file",
        "copy-item",
        "move-item",
        "rename-item",
        "set-itemproperty",
    }
)


def _accept_edits_allowed(command: str, shell: ShellName) -> bool:
    """Return True when every segment of `command` is fs-modifying.

    #860: re-parses the ORIGINAL `command` string (not the already-joined,
    already-escape-stripped `normalized_command`) via `_parse_bash`/
    `_parse_powershell`, the same parsers `validate_shell_command` itself
    uses, so this check can read `literal_stripped_argv` for every
    path-sensitive comparison below -- exactly like `_bash_hard_deny`/
    `_bash_command_is_read_only`. The old implementation re-split the
    ALREADY-mangled `normalized_command` on plain whitespace, which is a
    second, worse loss: it inherited the escape-stripped values AND lost
    quote-awareness on top of that.
    """

    if not command:
        return False
    if shell == "cmd":
        # cmd has no acceptEdits allowlist; fall through to ``ask``.
        return False
    allowed = _ACCEPT_EDITS_BASH_ALLOWED if shell == "bash" else _ACCEPT_EDITS_POWERSHELL_ALLOWED
    segments = _parse_bash(command) if shell == "bash" else [cmd for stmt in _parse_powershell(command) for cmd in stmt]
    if not segments:
        return False
    for segment in segments:
        if not segment.stripped_argv:
            return False
        if not _accept_edits_segment_allowed(segment, shell, allowed):
            return False
    return True


def _accept_edits_segment_allowed(segment: _SimpleCommand, shell: ShellName, allowed: frozenset[str]) -> bool:
    tokens = segment.stripped_argv
    base = tokens[0].lower()
    if shell == "powershell":
        base = base.split("\\")[-1].split("/")[-1]
        if base.endswith(".exe"):
            base = base[:-4]
    if base not in allowed:
        return False

    # #860: path-sensitive comparisons read the literal (backslash-preserving)
    # args, not the escape-stripped ones -- see _tokenize_literal_bash.
    args = list(segment.literal_stripped_argv[1:])
    if any(_is_root_path(arg) for arg in args):
        return False
    if _sensitive_path_reason(args):
        return False
    if shell == "bash" and base in {"rm", "rmdir"}:
        if _has_recursive_force(args) or any(arg in {"-r", "-R", "--recursive"} for arg in args):
            return False
    if shell == "powershell" and base == "remove-item":
        lowered = [arg.lower() for arg in args]
        if any(arg in {"-recurse", "-r"} for arg in lowered):
            return False
    return True
