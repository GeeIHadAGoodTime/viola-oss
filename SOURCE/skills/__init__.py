# viola/skills/__init__.py
# This file makes the 'skills' directory a Python package.

from __future__ import annotations

from .base import Intent, Response, Skill, SkillContext
from .manager import SkillManager, get_skill_manager
from .skillmd_command import SkillHookSpec, SkillMdCommand
from .skillmd_loader import (
    default_skill_roots,
    load_skillmd_skill,
    load_skillmd_skills_from_dir,
    load_skillmd_skills_from_roots,
    parse_skillmd,
)

__all__ = [
    "Intent",
    "Response",
    "Skill",
    "SkillContext",
    "SkillHookSpec",
    "SkillManager",
    "SkillMdCommand",
    "default_skill_roots",
    "get_skill_manager",
    "load_skillmd_skill",
    "load_skillmd_skills_from_dir",
    "load_skillmd_skills_from_roots",
    "parse_skillmd",
]
