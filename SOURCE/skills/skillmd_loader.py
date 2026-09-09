"""Load Markdown skill packages into Viola's command interface.

Directory names provide stable command identities. YAML metadata controls display,
discovery and execution constraints; parsing never executes package content.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from skills.skillmd_command import SkillHookSpec, SkillMdCommand

logger = get_logger(__name__)
SKILL_FILENAME = "SKILL.md"


def _text(value: Any) -> str | None:
    return None if value is None else str(value).strip() or None


def _words(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = value.split(",") if "," in value else value.split()
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    return [str(item).strip() for item in values if str(item).strip()]


def _flag(value: Any, fallback: bool) -> bool:
    if isinstance(value, (bool, int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in ("yes", "true", "on", "1"):
        return True
    if normalized in ("no", "false", "off", "0"):
        return False
    return fallback


def _metadata(raw: str, source: Path) -> tuple[dict[str, Any], str]:
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, raw
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return {}, raw
    body = "".join(lines[closing + 1 :])
    try:
        import yaml

        parsed = yaml.safe_load("".join(lines[1:closing]))
    except Exception as exc:
        logger.warning("Cannot read skill metadata at %s: %s", source, exc)
        return {}, body
    return (parsed if isinstance(parsed, dict) else {}), body


def _hooks(value: Any) -> list[SkillHookSpec]:
    """Normalize either event-indexed hook groups or flat hook declarations."""
    rows: list[SkillHookSpec] = []
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, dict) and (event := _text(entry.get("event"))):
                rows.append(
                    SkillHookSpec(event, _text(entry.get("matcher")), _text(entry.get("command")), {"entry": entry})
                )
        return rows
    if not isinstance(value, dict):
        return rows
    for event, entries in value.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("hooks")
            if isinstance(nested, list) and nested:
                for hook in nested:
                    if isinstance(hook, dict):
                        rows.append(
                            SkillHookSpec(
                                str(event),
                                _text(entry.get("matcher")),
                                _text(hook.get("command")),
                                {"event": event, "entry": entry, "hook": hook},
                            )
                        )
            else:
                rows.append(
                    SkillHookSpec(
                        str(event),
                        _text(entry.get("matcher")),
                        _text(entry.get("command")),
                        {"event": event, "entry": entry},
                    )
                )
    return rows


def parse_skillmd(
    *, skill_name: str, source_file: Path, raw_content: str, loaded_from: str = "skills"
) -> SkillMdCommand:
    metadata, body = _metadata(raw_content, source_file)
    description = _text(metadata.get("description"))
    fallback_description = next(
        (line.strip()[:280] for line in body.splitlines() if line.strip() and not line.strip().startswith("#")),
        skill_name,
    )
    context = _text(metadata.get("context"))
    context = context if context in ("inline", "fork") else "inline"
    model = _text(metadata.get("model"))
    effort = metadata.get("effort")
    if effort is not None:
        if isinstance(effort, (float, int)):
            effort = str(int(effort))
        else:
            effort = str(effort).strip().lower()
            if effort not in ("minimal", "low", "medium", "high", "xhigh"):
                try:
                    effort = str(int(effort))
                except ValueError:
                    effort = None
    paths = [item.removesuffix("/**") for item in _words(metadata.get("paths"))]
    return SkillMdCommand(
        name=skill_name,
        description=description or fallback_description or skill_name,
        markdown_body=body,
        skill_root=source_file.parent,
        source_file=source_file,
        display_name=_text(metadata.get("name")),
        when_to_use=_text(metadata.get("when_to_use")),
        version=_text(metadata.get("version")),
        argument_hint=_text(metadata.get("argument-hint")),
        argument_names=_words(metadata.get("arguments") or metadata.get("argument-names")),
        allowed_tools=_words(metadata.get("allowed-tools")),
        model_override=None if model and model.lower() == "inherit" else model,
        agent=_text(metadata.get("agent")),
        effort=effort,
        execution_context=context,
        paths=[item for item in paths if item and item != "**"],
        shell_allowlist=_words(metadata.get("shell")),
        hooks=_hooks(metadata.get("hooks")),
        user_invocable=_flag(metadata.get("user-invocable"), True),
        disable_model_invocation=_flag(metadata.get("disable-model-invocation"), False),
        loaded_from=loaded_from,
        has_user_specified_description=description is not None,
    )


def load_skillmd_skill(skill_dir: Path, *, loaded_from: str = "skills") -> SkillMdCommand | None:
    directory = Path(skill_dir)
    source = directory / SKILL_FILENAME
    if not source.is_file():
        return None
    try:
        return parse_skillmd(
            skill_name=directory.name,
            source_file=source,
            raw_content=source.read_text(encoding="utf-8"),
            loaded_from=loaded_from,
        )
    except Exception as exc:
        logger.warning("Cannot load skill package %s: %s", source, exc)
        return None


def load_skillmd_skills_from_dir(root: Path, *, loaded_from: str = "skills") -> list[SkillMdCommand]:
    directory = Path(root)
    if not directory.is_dir():
        return []
    result = []
    for child in sorted(directory.iterdir()):
        if child.is_dir():
            command = load_skillmd_skill(child, loaded_from=loaded_from)
            if command is not None:
                result.append(command)
    return result


def iter_unique(skills: Iterable[SkillMdCommand]) -> list[SkillMdCommand]:
    retained = {}
    for command in skills:
        retained.setdefault(command.name, command)
    return list(retained.values())


def load_skillmd_skills_from_roots(roots: Sequence[Path | str], *, loaded_from: str = "skills") -> list[SkillMdCommand]:
    """Earlier roots take precedence; discovery order is deterministic."""
    return iter_unique(
        command for root in roots for command in load_skillmd_skills_from_dir(Path(root), loaded_from=loaded_from)
    )


def default_skill_roots(project_root: Path | str | None = None) -> list[Path]:
    project = Path.cwd() if project_root is None else Path(project_root)
    return [Path.home() / ".claude" / "skills", project / ".claude" / "skills", project / "skills" / "packages"]
