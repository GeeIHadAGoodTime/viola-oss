"""
Skill Manager for Viola Plugin System

Handles skill discovery, registration, and routing.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

from .base import Response, Skill, SkillContext
from .skillmd_command import USER_INVOCATION_REFUSAL, SkillMdCommand
from .skillmd_loader import (
    default_skill_roots,
    load_skillmd_skill,
    load_skillmd_skills_from_dir,
    load_skillmd_skills_from_roots,
)

logger = get_logger(__name__)


class SkillManager:
    """
    Centralized manager for all skills.

    Responsibilities:
    - Discover and load skills from skills/ directory
    - Route user input to appropriate skill
    - Handle skill execution and error handling
    - Provide skill enable/disable functionality
    """

    def __init__(self, context: SkillContext | None = None):
        """
        Initialize skill manager.

        Args:
            context: Runtime context with music, TTS, etc.
        """
        self.context = context or SkillContext()
        self._skills: list[Skill] = []
        self._skills_by_name: dict[str, Skill] = {}
        # SKILL.md (Claude-compatible) prompt-commands live alongside the
        # Python-class skills. They are NOT regex-pattern handlers — they're
        # invoked by name (`/skill-name [args]`) and dispatched by re-issuing
        # a parameterized prompt through the agent loop.
        self._skillmd_commands: list[SkillMdCommand] = []
        self._skillmd_by_name: dict[str, SkillMdCommand] = {}
        # Optional dispatch callback. When provided, the manager will call
        # `dispatcher(skill_md_command, args, context)` and surface the result
        # as a Response. When None, manager.process() still records the match
        # and returns a Response with the rendered prompt so the caller can
        # route it themselves.
        self._skillmd_dispatcher: Any | None = None

    def register(self, skill_class: type[Skill]) -> None:
        """
        Register a skill class.

        Args:
            skill_class: Skill class (not instance) to register

        Note:
            Validation is skipped during registration to avoid asyncio.run() conflicts.
            Skills are validated when first executed via the async execute() path.
        """
        try:
            # Instantiate with context
            skill = skill_class(self.context)

            # Skip validation during registration (asyncio.run() causes event loop conflicts)
            # Validation will happen on first execution via async execute() path

            # Add to registry
            self._skills.append(skill)
            self._skills_by_name[skill.name] = skill

            # Sort by priority (higher first)
            self._skills.sort(key=lambda s: s.priority, reverse=True)

            logger.info(
                "✅ Registered skill: %s (priority=%s, patterns=%s)",
                skill.name,
                skill.priority,
                len(skill.patterns()),
            )

        except Exception as e:
            logger.error("❌ Failed to register skill %s: %s", skill_class.__name__, e)

    # ------------------------------------------------------------------
    # SKILL.md (Claude-compatible) registration
    # ------------------------------------------------------------------
    def set_skillmd_dispatcher(self, dispatcher: Any | None) -> None:
        """Wire in a callable to actually execute SKILL.md skills.

        The dispatcher should accept (skill: SkillMdCommand, args: str, context: dict | None)
        and return either a `skills.base.Response` (or any object with `message` /
        `success` / `spoken` attributes), or `None` to fall through. When unset,
        `process()` simply returns the rendered prompt and a flag indicating the
        SKILL.md was matched — the calling registry or intent pipeline is responsible for routing.
        """
        self._skillmd_dispatcher = dispatcher

    def register_skillmd(self, cmd: SkillMdCommand) -> None:
        """Register a parsed SKILL.md command.

        Name collisions: a SKILL.md command shadows nothing in the Python-class
        skill list; the two namespaces are kept distinct because they're
        invoked differently (regex match vs `/name` invocation). However we
        DO refuse to register two SKILL.md skills with the same name.
        """
        if cmd.name in self._skillmd_by_name:
            existing = self._skillmd_by_name[cmd.name]
            logger.warning(
                "SKILL.md '%s' already registered from %s; skipping %s",
                cmd.name,
                existing.source_file,
                cmd.source_file,
            )
            return
        self._skillmd_commands.append(cmd)
        self._skillmd_by_name[cmd.name] = cmd
        logger.info(
            "[SKILL.md] Registered '%s' (model=%s, agent=%s, allowed_tools=%d, user_invocable=%s, disable_model_invocation=%s)",
            cmd.name,
            cmd.model_override,
            cmd.agent,
            len(cmd.allowed_tools),
            cmd.user_invocable,
            cmd.disable_model_invocation,
        )

    def discover_and_load_skillmd(
        self,
        roots: Sequence[Path | str] | None = None,
        *,
        project_root: Path | str | None = None,
    ) -> int:
        """Scan one or more skill roots for `<name>/SKILL.md` packages and register them.

        Returns the number of newly-registered SKILL.md skills.

        If `roots` is None, falls back to `default_skill_roots(project_root)`.
        Errors loading any individual skill are logged but do not abort the scan.
        """
        if roots is None:
            roots = default_skill_roots(project_root)
        loaded = load_skillmd_skills_from_roots(roots)
        count = 0
        for cmd in loaded:
            before = len(self._skillmd_commands)
            self.register_skillmd(cmd)
            if len(self._skillmd_commands) > before:
                count += 1
        logger.info("[SKILL.md] discovery complete: %d skills registered from %d roots", count, len(list(roots)))
        return count

    def get_skillmd(self, name: str) -> SkillMdCommand | None:
        return self._skillmd_by_name.get(name)

    def list_skillmd(self) -> list[SkillMdCommand]:
        return list(self._skillmd_commands)

    def command_specs(self) -> list[Any]:
        """Expose SKILL.md packages as slash-command registry specs."""

        from intent.commands.registry import CommandSpec

        specs: list[Any] = []
        for cmd in self._skillmd_commands:
            aliases = ["/%s" % cmd.name]
            if cmd.display_name:
                display_slug = "-".join(part for part in cmd.display_name.lower().replace("_", " ").split())
                if display_slug and display_slug != cmd.name:
                    aliases.append("/%s" % display_slug)

            def _handler(invocation: Any, *, _cmd: SkillMdCommand = cmd) -> dict[str, Any]:
                if not _cmd.user_invocable:
                    return {
                        "ok": False,
                        "message": USER_INVOCATION_REFUSAL,
                        "error": USER_INVOCATION_REFUSAL,
                        "data": {"skillmd": _cmd.to_dict(), "user_invocable": False},
                    }
                raw_args = str(invocation.args.get("raw_args") or "")
                prompt = _cmd.get_prompt_for_command(raw_args, session_id=invocation.session_id)
                return {
                    "ok": True,
                    "message": prompt,
                    "data": {
                        "skillmd": _cmd.to_dict(),
                        "args": raw_args,
                        "model_override": _cmd.model_override,
                        "allowed_tools": list(_cmd.allowed_tools),
                        "agent": _cmd.agent,
                        "effort": _cmd.effort,
                        "context": _cmd.execution_context,
                    },
                }

            specs.append(
                CommandSpec(
                    name=cmd.name,
                    aliases=tuple(aliases),
                    source="skill",
                    handler=_handler,
                    description=cmd.description,
                    command_type="prompt",
                    progress_message="Running skill %s..." % (cmd.display_name or cmd.name),
                    user_facing=cmd.user_invocable,
                    argument_hint=cmd.argument_hint,
                    extra={
                        "skillmd": cmd.to_dict(),
                        "allowed_tools": list(cmd.allowed_tools),
                        "model": cmd.model_override,
                        "agent": cmd.agent,
                        "effort": cmd.effort,
                        "hooks": [{"event": h.event, "matcher": h.matcher, "command": h.command} for h in cmd.hooks],
                    },
                )
            )
        return specs

    def register_all(self, skill_classes: list[type[Skill]]) -> None:
        """
        Register multiple skill classes at once.

        Args:
            skill_classes: List of skill classes to register
        """
        for skill_class in skill_classes:
            self.register(skill_class)

    def discover_and_load(self, skills_dir: Path | None = None) -> None:
        """
        Discover and load skills from directory.

        Looks for Python modules in skills/builtin/ that define Skill subclasses.

        Args:
            skills_dir: Directory to search (default: skills/builtin/)
        """
        if skills_dir is None:
            skills_dir = Path(__file__).parent / "builtin"

        if not skills_dir.exists():
            logger.warning("Skills directory not found: %s", skills_dir)
            return

        # Import all Python files in builtin/
        for skill_file in skills_dir.glob("*.py"):
            if skill_file.name.startswith("_"):
                continue

            try:
                # Dynamic import
                import importlib.util

                spec = importlib.util.spec_from_file_location(f"skills.builtin.{skill_file.stem}", skill_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    # Find Skill subclasses in module
                    for name in dir(module):
                        obj = getattr(module, name)
                        if isinstance(obj, type) and issubclass(obj, Skill) and obj is not Skill:
                            self.register(obj)

            except Exception as e:
                logger.error("Failed to load skill from %s: %s", skill_file, e)

    async def process(self, text: str, context: dict | None = None) -> Response | None:
        """
        Process user input and route to appropriate skill.

        Args:
            text: User input text
            context: Optional context (history, user info, etc.)

        Returns:
            Response from matched skill, or None if no match
        """
        if not text or not text.strip():
            return None

        text = text.strip()
        logger.debug("Processing text: '%s...'", text[:50])

        # SKILL.md (Claude-compatible) skills first — user-typed `/skill-name`
        # invocations are unambiguous and should never be shadowed by a regex
        # match in a Python-class skill.
        if text.startswith("/"):
            for cmd in self._skillmd_commands:
                if cmd.matches_invocation(text):
                    if not cmd.user_invocable:
                        return Response(
                            message=USER_INVOCATION_REFUSAL,
                            success=False,
                            spoken=False,
                            data={"skillmd": cmd.to_dict(), "user_invocable": False},
                        )
                    args = cmd.extract_args(text)
                    logger.info("[SKILL.md] Matched '%s' (args=%r)", cmd.name, args[:60])
                    if self._skillmd_dispatcher is None:
                        # No dispatcher wired — surface the rendered prompt so
                        # the caller (intent pipeline / slash-command registry)
                        # can route it. The Response is flagged success=True so
                        # downstream doesn't try to swallow it.
                        try:
                            prompt = cmd.get_prompt_for_command(
                                args,
                                execute_shell=False,
                            )
                        except Exception as exc:
                            logger.exception("[SKILL.md] prompt render failed: %s", exc)
                            return Response(
                                message=f"Skill '{cmd.name}' prompt render failed: {exc}",
                                success=False,
                            )
                        return Response(
                            message=prompt,
                            success=True,
                            spoken=False,
                            data={
                                "skillmd": cmd.to_dict(),
                                "args": args,
                                "model_override": cmd.model_override,
                                "allowed_tools": list(cmd.allowed_tools),
                                "agent": cmd.agent,
                                "effort": cmd.effort,
                                "context": cmd.execution_context,
                                "hooks": [
                                    {"event": h.event, "matcher": h.matcher, "command": h.command} for h in cmd.hooks
                                ],
                            },
                        )
                    try:
                        result = self._skillmd_dispatcher(cmd, args, context)
                    except Exception as exc:
                        logger.exception("[SKILL.md] dispatcher failed: %s", exc)
                        return Response(
                            message=f"Skill '{cmd.name}' execution failed: {exc}",
                            success=False,
                        )
                    if result is None:
                        return None
                    if isinstance(result, Response):
                        return result
                    # Allow dispatcher to return a duck-typed object — coerce to Response.
                    return Response(
                        message=getattr(result, "message", str(result)),
                        success=bool(getattr(result, "success", True)),
                        spoken=bool(getattr(result, "spoken", False)),
                        data=getattr(result, "data", None),
                    )

        # Try each skill in priority order
        for skill in self._skills:
            if not skill.enabled:
                continue

            # Check if skill can handle
            intent = skill.can_handle(text)
            if intent:
                logger.info("🎯 Matched skill: %s", skill.name)

                # Add context
                if context:
                    intent.context = context

                try:
                    # Execute skill
                    response = await skill.execute(intent)
                    if response is None:
                        # Skill matched the pattern but could not handle
                        # the text internally — fall through to next stage.
                        logger.info(
                            "Skill %s matched but did not handle: %s",
                            skill.name,
                            text[:50],
                        )
                        return None
                    logger.info(
                        "Skill %s executed: success=%s, spoken=%s",
                        skill.name,
                        response.success,
                        response.spoken,
                    )
                    return response

                except Exception as e:
                    logger.exception("❌ Skill %s execution failed: %s", skill.name, e)
                    return Response(message="Skill '%s' failed: %s" % (skill.name, e), success=False)

        # No skill matched
        logger.debug("No skill matched for: '%s...'", text[:50])
        return None

    def get_skill(self, name: str) -> Skill | None:
        """Get skill by name."""
        return self._skills_by_name.get(name)

    def get_skills(self, include_disabled: bool = True) -> list[Skill]:
        """Get skills, optionally filtering by enabled status."""
        if include_disabled:
            return list(self._skills)
        return [skill for skill in self._skills if skill.enabled]

    def list_skills(self) -> list[Skill]:
        """List all registered skills."""
        return list(self._skills)

    def enable_skill(self, name: str) -> bool:
        """Enable a skill."""
        skill = self.get_skill(name)
        if skill:
            skill.enabled = True
            logger.info("Enabled skill: %s", name)
            return True
        return False

    def disable_skill(self, name: str) -> bool:
        """Disable a skill."""
        skill = self.get_skill(name)
        if skill:
            skill.enabled = False
            logger.info("Disabled skill: %s", name)
            return True
        return False

    def toggle_skill(self, name: str) -> Skill:
        """Toggle skill enabled/disabled state."""
        skill = self.get_skill(name)
        if not skill:
            raise ValueError(f"Skill not found: {name}")
        skill.enabled = not skill.enabled
        logger.info("Toggled skill %s: enabled=%s", name, skill.enabled)
        return skill

    def get_stats(self) -> dict[str, Any]:
        """Get skill manager statistics."""
        return {
            "total_skills": len(self._skills),
            "enabled_skills": sum(1 for s in self._skills if s.enabled),
            "disabled_skills": sum(1 for s in self._skills if not s.enabled),
            "skills": [
                {
                    "name": s.name,
                    "description": s.description,
                    "priority": s.priority,
                    "enabled": s.enabled,
                    "patterns": len(s.patterns()),
                }
                for s in self._skills
            ],
            "skillmd_total": len(self._skillmd_commands),
            "skillmd": [
                {
                    "name": c.name,
                    "description": c.description,
                    "model": c.model_override,
                    "agent": c.agent,
                    "user_invocable": c.user_invocable,
                    "disable_model_invocation": c.disable_model_invocation,
                    "allowed_tools": list(c.allowed_tools),
                    "skill_root": str(c.skill_root),
                }
                for c in self._skillmd_commands
            ],
        }


# Global skill manager instance
_skill_manager: SkillManager | None = None


def get_skill_manager() -> SkillManager:
    """Get global skill manager instance."""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
        # Auto-discover Python-class skills (skills/builtin/*.py).
        _skill_manager.discover_and_load()
        # Auto-discover Claude-compatible SKILL.md packages from default roots.
        # Failures here MUST NOT prevent the Python skill system from booting.
        try:
            _skill_manager.discover_and_load_skillmd()
        except Exception as exc:
            logger.warning("SKILL.md auto-discovery failed: %s", exc)
    return _skill_manager
