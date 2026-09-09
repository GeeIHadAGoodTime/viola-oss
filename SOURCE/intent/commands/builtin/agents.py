"""``/agents`` — list or invoke a registered subagent.

Parity reference: ``src/commands/agents/``. Claude's ``/agents`` enumerates
the agent registry (frontmatter-defined agents in ``.claude/agents/`` plus
plugins) and lets the user invoke one explicitly. Viola wires through the
existing skill/agent surface so the output is a structured list rather than
free-form text.

The handler is read-only — invoking an agent goes through the Agent tool
in the model loop, NOT through the slash command surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from intent.commands.registry import CommandInvocation, CommandSpec


def command_spec(*, pipeline: Any) -> CommandSpec:
    """Build the ``/agents`` spec bound to ``pipeline``."""

    def _handler(invocation: CommandInvocation) -> dict[str, object]:
        return _handle(invocation, pipeline)

    return CommandSpec(
        name="agents",
        aliases=("/agents", "/plugins", "/marketplace"),
        source="builtin",
        handler=_handler,
        remote_safe=True,
        description="List available subagents.",
        command_type="local-jsx",
        argument_hint="[name]",
        extra={"view": "agent-directory"},
    )


def _handle(invocation: CommandInvocation, pipeline: Any) -> dict[str, object]:
    agents = _list_agents(pipeline)
    if not agents:
        return {
            "message": "No subagents are currently registered.",
            "data": {"command": "agents", "agents": []},
        }
    summary = ", ".join(agent.get("name", "unnamed") for agent in agents)
    return {
        "message": "Registered subagents: %s." % summary,
        "data": {"command": "agents", "agents": agents},
    }


def _list_agents(pipeline: Any) -> list[dict[str, object]]:
    agents: list[dict[str, object]] = []
    registry = getattr(pipeline, "agent_registry", None)
    if registry is not None and hasattr(registry, "list_agents"):
        try:
            for entry in registry.list_agents():
                agents.append(_normalize_agent_entry(entry))
        except Exception:
            pass
    if not agents:
        agents.extend(_scan_filesystem_agents())
    return agents


def _normalize_agent_entry(entry: Any) -> dict[str, object]:
    if isinstance(entry, dict):
        return {
            "name": entry.get("name"),
            "description": entry.get("description"),
            "source": entry.get("source"),
            "model": entry.get("model"),
        }
    return {
        "name": getattr(entry, "name", None),
        "description": getattr(entry, "description", None),
        "source": getattr(entry, "source", None),
        "model": getattr(entry, "model", None),
    }


def _scan_filesystem_agents() -> list[dict[str, object]]:
    """Fall back to a filesystem scan of ``.claude/agents/`` / ``.viola/agents``."""

    results: list[dict[str, object]] = []
    for candidate_dir in (
        Path.cwd() / ".claude" / "agents",
        Path.cwd() / ".viola" / "agents",
    ):
        if not candidate_dir.exists():
            continue
        for entry in sorted(candidate_dir.glob("*.md")):
            try:
                first_line = entry.read_text(encoding="utf-8").splitlines()[0]
            except OSError:
                first_line = ""
            results.append(
                {
                    "name": entry.stem,
                    "description": first_line.lstrip("# ").strip() or None,
                    "source": "frontmatter",
                    "path": str(entry),
                }
            )
    return results


__all__ = ["command_spec"]
