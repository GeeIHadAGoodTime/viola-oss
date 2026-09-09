"""Blackboard coordination primitives for orchestration agents."""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, TextIO

from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)

ORCHESTRATION_DIR: Final[Path] = Path(settings.orchestration_data_dir)
BLACKBOARD_PATH: Final[Path] = ORCHESTRATION_DIR / "blackboard.md"
ARCHIVE_PATH: Final[Path] = ORCHESTRATION_DIR / "findings-archive.md"

SECTION_RE: Final[re.Pattern[str]] = re.compile(r"^## (.+)$", re.MULTILINE)
# `(Z)?` mirrors .viola/agents/update_blackboard.py's 2026-08 board-clock fix: a
# `Z`-suffixed stamp is UTC, a bare `HH:MM` is pre-fix legacy-local. This module is
# the retired codex-orchestration subsystem's OWN board (a different file than the
# live agent blackboard; not user-routable, see check_codex_orchestration_not_user_
# routable.py) and was never wired to update_blackboard's shared grammar, so it
# carries the same regex independently and needed the same widening to avoid
# silently stopping on any post-fix-shaped entry.
ENTRY_TS_RE: Final[re.Pattern[str]] = re.compile(r"- \[.+? (\d{2}):(\d{2})(Z)?\]")
ONLINE_TS_RE: Final[re.Pattern[str]] = re.compile(r"\| since (\d{2}):(\d{2})(Z)?\s*$")
COMPLETED_RE: Final[re.Pattern[str]] = re.compile(
    r"- \[.+? (\d{4})-(\d{2})-(\d{2}) \d{2}:\d{2}\]"
)  # naive-timestamp-ok: full date already present

ONLINE_STALE_MINUTES: Final[int] = 120
WARNING_STALE_MINUTES: Final[int] = 60
MESSAGE_STALE_MINUTES: Final[int] = 120
FINDING_STALE_MINUTES: Final[int] = 1440
RECENT_FINDING_WINDOW_MINUTES: Final[int] = 30
NEW_AGENT_WINDOW_MINUTES: Final[int] = 15
COMPLETED_STALE_DAYS: Final[int] = 7
BLACKBOARD_ARCHIVE_THRESHOLD_BYTES: Final[int] = 5120

TEMPLATE: Final[str] = (
    "# Agent Blackboard\n> Shared coordination file for all AI agents working on Viola.\n"
    "> Each agent reads this on start and updates their own entries only.\n\n"
    "## AGENTS ONLINE\n<!-- Format: - agent-name | task | touching: files | since HH:MM -->\n\n"
    "## WARNINGS\n<!-- Format: - [agent-name HH:MM] Warning message -->\n<!-- Clear warnings older than 1 hour -->\n\n"
    "## MESSAGES\n<!-- Format: - [agent-name HH:MM] @target message text -->\n<!-- Auto-pruned after 2 hours -->\n\n"
    "## FINDINGS\n<!-- Format: - [agent-name HH:MM] One-line finding with file:line evidence -->\n\n"
    "## COMPLETED\n<!-- Move findings here once resolved/implemented. Prune weekly. -->\n"
)


def _now_str() -> str:
    """Return the current time in the blackboard display format."""

    return datetime.now().strftime("%H:%M")


def _age_minutes(hour: int, minute: int, now: datetime) -> float:
    """Return the age of a HH:MM timestamp in minutes."""

    timestamp = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if timestamp > now:
        timestamp -= timedelta(days=1)
    return (now - timestamp).total_seconds() / 60


def _parse_sections(text: str) -> dict[str, tuple[int, int]]:
    """Parse section start/end offsets for the blackboard document."""

    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, tuple[int, int]] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = (start, end)
    return sections


def _filter_section(text: str, section: str, keep_fn: Callable[[str], bool]) -> str:
    """Filter the lines inside a specific blackboard section."""

    sections = _parse_sections(text)
    if section not in sections:
        return text
    start, end = sections[section]
    kept_lines = [line for line in text[start:end].splitlines(keepends=True) if keep_fn(line)]
    return text[:start] + "".join(kept_lines) + text[end:]


def _ensure_messages_section(text: str) -> str:
    """Backfill the MESSAGES section if it is absent."""

    if "## MESSAGES" in text:
        return text
    findings_index = text.find("## FINDINGS")
    if findings_index == -1:
        return text
    insertion = (
        "## MESSAGES\n<!-- Format: - [agent-name HH:MM] @target message text -->\n"
        "<!-- Auto-pruned after 2 hours -->\n\n"
    )
    return text[:findings_index] + insertion + text[findings_index:]


def _ts_keeper(regex: re.Pattern[str], max_minutes: int) -> tuple[Callable[[str], bool], datetime]:
    """Return a filter that drops timestamped lines older than ``max_minutes``."""

    now = datetime.now()

    def keep(line: str) -> bool:
        match = regex.search(line) if regex is ONLINE_TS_RE else regex.match(line.strip())
        return not match or _age_minutes(int(match.group(1)), int(match.group(2)), now) < max_minutes

    return keep, now


def _agent_codename_path(agent: str) -> Path:
    """Return the codename tracking file for an agent."""

    return ORCHESTRATION_DIR / f".codename-{agent}"


def _cleanup(text: str) -> str:
    """Apply pruning and archival rules to the blackboard text."""

    text = _ensure_messages_section(text)

    keep_online, now = _ts_keeper(ONLINE_TS_RE, ONLINE_STALE_MINUTES)
    pruned_agents: list[str] = []

    def keep_online_and_track(line: str) -> bool:
        keep = keep_online(line)
        if not keep and line.strip().startswith("- "):
            parts = line.strip().lstrip("- ").split("|")
            if parts:
                pruned_agents.append(parts[0].strip())
        return keep

    text = _filter_section(text, "AGENTS ONLINE", keep_online_and_track)
    for agent in pruned_agents:
        try:
            _agent_codename_path(agent).unlink()
        except OSError:
            logger.debug("Failed to remove stale orchestration codename file for %s", agent)

    keep_warn, _ = _ts_keeper(ENTRY_TS_RE, WARNING_STALE_MINUTES)
    text = _filter_section(text, "WARNINGS", keep_warn)

    keep_message, _ = _ts_keeper(ENTRY_TS_RE, MESSAGE_STALE_MINUTES)
    text = _filter_section(text, "MESSAGES", keep_message)

    moved_findings: list[str] = []

    def keep_finding(line: str) -> bool:
        match = ENTRY_TS_RE.match(line.strip())
        if match and _age_minutes(int(match.group(1)), int(match.group(2)), now) >= FINDING_STALE_MINUTES:
            # `(Z)?` preserved into the replacement: without it, a post-2026-08
            # `[agent HH:MMZ]` finding stopped matching this substitution outright
            # (silently moved to COMPLETED un-dated) instead of just losing its
            # zone marker.
            dated = re.sub(
                r"(\[.+?) (\d{2}:\d{2})(Z)?\]",
                lambda m: f"{m.group(1)} {now.strftime('%Y-%m-%d')} {m.group(2)}{m.group(3) or ''}]",
                line.strip(),
            )
            moved_findings.append(dated)
            return False
        return True

    text = _filter_section(text, "FINDINGS", keep_finding)
    if moved_findings:
        text = _insert_lines(text, "COMPLETED", moved_findings)

    def keep_completed(line: str) -> bool:
        match = COMPLETED_RE.match(line.strip())
        if not match:
            return True
        completed_at = datetime(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
        return (now - completed_at).days < COMPLETED_STALE_DAYS

    text = _filter_section(text, "COMPLETED", keep_completed)

    if len(text.encode("utf-8")) > BLACKBOARD_ARCHIVE_THRESHOLD_BYTES:
        text = _auto_archive_findings(text, now)

    return text


def _auto_archive_findings(text: str, now: datetime) -> str:
    """Move the oldest half of findings to the archive file."""

    sections = _parse_sections(text)
    if "FINDINGS" not in sections:
        return text

    start, end = sections["FINDINGS"]
    lines = [line for line in text[start:end].splitlines(keepends=True) if line.strip().startswith("- ")]
    if len(lines) < 2:
        return text

    split_index = len(lines) // 2
    archive_lines = lines[:split_index]
    archive_header = "\n## Auto-archived %s\n\n" % now.strftime("%Y-%m-%d %H:%M")
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ARCHIVE_PATH.open("a", encoding="utf-8") as handle:
        handle.write(archive_header + "".join(archive_lines))

    kept = "".join(lines[split_index:])
    comments = [line for line in text[start:end].splitlines(keepends=True) if not line.strip().startswith("- ")]
    return text[:start] + "".join(comments) + kept + text[end:]


def _lock_file(handle: TextIO) -> None:
    """Apply a best-effort exclusive lock to a file handle."""

    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: TextIO) -> None:
    """Release a best-effort exclusive lock from a file handle."""

    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            logger.debug("Failed to unlock orchestration blackboard on Windows")
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read() -> str:
    """Read the blackboard, creating it if necessary."""

    if not BLACKBOARD_PATH.exists():
        BLACKBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        BLACKBOARD_PATH.write_text(TEMPLATE, encoding="utf-8")
    return BLACKBOARD_PATH.read_text(encoding="utf-8")


def _write(text: str) -> None:
    """Write blackboard contents with a file lock."""

    BLACKBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BLACKBOARD_PATH.open("w", encoding="utf-8") as handle:
        _lock_file(handle)
        try:
            handle.write(text)
        finally:
            _unlock_file(handle)


def _insert_lines(text: str, section: str, lines: list[str]) -> str:
    """Insert one or more lines at the end of a section."""

    sections = _parse_sections(text)
    if section not in sections:
        return text
    _, end = sections[section]
    insert_at = end
    while insert_at > 0 and text[insert_at - 1] in ("\n", "\r", " "):
        insert_at -= 1
    return text[:insert_at] + "\n" + "\n".join(lines) + "\n" + text[end:]


def _insert_line(text: str, section: str, line: str) -> str:
    """Insert a single line into a section."""

    return _insert_lines(text, section, [line])


def read_blackboard_text() -> str:
    """Return the cleaned blackboard text and persist pruning updates."""

    text = _cleanup(_read())
    _write(text)
    return text


def cmd_online(agent: str, task: str, files: str) -> None:
    """Mark an agent as online on the orchestration blackboard."""

    text = _cleanup(_read())
    text = re.sub(rf"^- {re.escape(agent)} \|.*$\n?", "", text, flags=re.MULTILINE)
    text = _insert_line(
        text,
        "AGENTS ONLINE",
        f"- {agent} | {task} | touching: {files} | since {_now_str()}",
    )
    _write(text)
    _agent_codename_path(agent).write_text(agent, encoding="utf-8")
    logger.info("Orchestration agent %s is online", agent)


def cmd_offline(agent: str) -> None:
    """Mark an agent as offline on the orchestration blackboard."""

    text = _cleanup(_read())
    text = re.sub(rf"^- {re.escape(agent)} \|.*$\n?", "", text, flags=re.MULTILINE)
    _write(text)
    try:
        _agent_codename_path(agent).unlink()
    except OSError:
        logger.debug("Codename file missing while marking %s offline", agent)
    logger.info("Orchestration agent %s is offline", agent)


def cmd_warn(agent: str, message: str) -> None:
    """Append a warning entry to the orchestration blackboard."""

    text = _cleanup(_read())
    _write(_insert_line(text, "WARNINGS", f"- [{agent} {_now_str()}] {message}"))
    logger.info("Orchestration warning posted by %s", agent)


def cmd_find(agent: str, finding: str) -> None:
    """Append a finding entry to the orchestration blackboard."""

    text = _cleanup(_read())
    _write(_insert_line(text, "FINDINGS", f"- [{agent} {_now_str()}] {finding}"))
    logger.info("Orchestration finding posted by %s", agent)


def cmd_msg(agent: str, message: str) -> None:
    """Append a message entry to the orchestration blackboard."""

    text = _cleanup(_read())
    _write(_insert_line(text, "MESSAGES", f"- [{agent} {_now_str()}] {message}"))
    logger.info("Orchestration message posted by %s", agent)


def cmd_read(agent_name: str | None = None) -> str:
    """Read the current orchestration blackboard contents."""

    text = read_blackboard_text()
    if agent_name:
        sections = _parse_sections(text)
        if "MESSAGES" in sections:
            start, end = sections["MESSAGES"]
            if f"@{agent_name}" in text[start:end]:
                logger.info("Orchestration agent %s has pending messages", agent_name)
    return text


def cmd_check() -> str:
    """Return whether the manager should be notified of blackboard changes."""

    text = _cleanup(_read())
    sections = _parse_sections(text)
    now = datetime.now()

    if "FINDINGS" in sections:
        start, end = sections["FINDINGS"]
        for line in text[start:end].splitlines():
            if "BLOCKED" not in line:
                continue
            match = ENTRY_TS_RE.match(line.strip())
            if match and _age_minutes(int(match.group(1)), int(match.group(2)), now) < RECENT_FINDING_WINDOW_MINUTES:
                return "BLOCKED"

    if "MESSAGES" in sections:
        start, end = sections["MESSAGES"]
        for line in text[start:end].splitlines():
            if "@manager" not in line.lower():
                continue
            match = ENTRY_TS_RE.match(line.strip())
            if match and _age_minutes(int(match.group(1)), int(match.group(2)), now) < RECENT_FINDING_WINDOW_MINUTES:
                return "MESSAGE"

    if "AGENTS ONLINE" in sections:
        start, end = sections["AGENTS ONLINE"]
        for line in text[start:end].splitlines():
            match = ONLINE_TS_RE.search(line)
            if match and _age_minutes(int(match.group(1)), int(match.group(2)), now) < NEW_AGENT_WINDOW_MINUTES:
                return "NEW_AGENT"

    return "NO_TRIGGER"


async def cmd_online_async(agent: str, task: str, files: str) -> None:
    """Async wrapper for :func:`cmd_online`."""

    await asyncio.to_thread(cmd_online, agent, task, files)


async def cmd_offline_async(agent: str) -> None:
    """Async wrapper for :func:`cmd_offline`."""

    await asyncio.to_thread(cmd_offline, agent)


async def cmd_warn_async(agent: str, message: str) -> None:
    """Async wrapper for :func:`cmd_warn`."""

    await asyncio.to_thread(cmd_warn, agent, message)


async def cmd_find_async(agent: str, finding: str) -> None:
    """Async wrapper for :func:`cmd_find`."""

    await asyncio.to_thread(cmd_find, agent, finding)


async def cmd_msg_async(agent: str, message: str) -> None:
    """Async wrapper for :func:`cmd_msg`."""

    await asyncio.to_thread(cmd_msg, agent, message)


async def cmd_read_async(agent_name: str | None = None) -> str:
    """Async wrapper for :func:`cmd_read`."""

    return await asyncio.to_thread(cmd_read, agent_name)


__all__ = [
    "ARCHIVE_PATH",
    "BLACKBOARD_PATH",
    "cmd_check",
    "cmd_find",
    "cmd_find_async",
    "cmd_msg",
    "cmd_msg_async",
    "cmd_offline",
    "cmd_offline_async",
    "cmd_online",
    "cmd_online_async",
    "cmd_read",
    "cmd_read_async",
    "cmd_warn",
    "cmd_warn_async",
    "read_blackboard_text",
]
