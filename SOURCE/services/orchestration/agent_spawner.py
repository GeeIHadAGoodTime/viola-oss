"""Spawn orchestration worker agents using the legacy Claude worker launcher.

NOTE (founder order, 2026-07-12, ticket #1214): `.viola/agents/spawn_agent.py` — the
dev-fleet CLI this module dynamically loads — is PHYSICALLY DELETED and must never be
recreated; the built-in Agent tool is the only sanctioned dispatch path for Claude
Code fleet work now. This module is a separate, disabled-by-default (`orchestration_enabled=False`),
non-user-routable developer utility (`scripts/check_codex_orchestration_not_user_routable.py`
keeps it off every HTTP/route/MCP/UI surface) that is not wired into any live path —
it now fails closed with `ServiceError` the moment anything calls it, because its
legacy launcher asset is gone. Whether to retire this dormant subsystem entirely is a
separate decision outside #1214's scope; do not "fix" this by recreating spawn_agent.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Final, cast

from config.settings import settings
from core.exceptions import ErrorContext, ServiceError
from core.logging_config import get_logger

logger = get_logger(__name__)

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
LEGACY_AGENTS_DIR: Final[Path] = PROJECT_ROOT / ".viola" / "agents"
LEGACY_SPAWNER_PATH: Final[Path] = LEGACY_AGENTS_DIR / "spawn_agent.py"
LEGACY_SYSTEM_PROMPT_PATH: Final[Path] = LEGACY_AGENTS_DIR / "AGENT_SYSTEM_PROMPT.md"


def _get_orchestration_dir() -> Path:
    """Return the orchestration data directory."""

    return Path(settings.orchestration_data_dir)


def _get_logs_dir() -> Path:
    """Return the orchestration agent log directory."""

    return _get_orchestration_dir() / "agent-logs"


def _load_default_system_prompt() -> str | None:
    """Load the default worker system prompt from the legacy agent assets."""

    if not LEGACY_SYSTEM_PROMPT_PATH.exists():
        return None
    return LEGACY_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _load_legacy_module() -> ModuleType:
    """Load the legacy ``spawn_agent.py`` module from disk."""

    if not LEGACY_SPAWNER_PATH.exists():
        raise ServiceError(
            "Orchestration worker launcher is retired",
            ErrorContext(
                component="services.orchestration.agent_spawner",
                operation="load_legacy_module",
                params={"path": str(LEGACY_SPAWNER_PATH)},
                user_message="The orchestration worker is unavailable.",
                recovery_hint=(
                    "spawn_agent.py was deleted by founder order (#1214); it must not be "
                    "recreated. This dev-only, disabled-by-default subsystem needs a fresh "
                    "decision on its own dispatch mechanism, not a restored legacy file."
                ),
            ),
        )

    spec = importlib.util.spec_from_file_location("viola_legacy_spawn_agent", LEGACY_SPAWNER_PATH)
    if spec is None or spec.loader is None:
        raise ServiceError(
            "Failed to load orchestration worker",
            ErrorContext(
                component="services.orchestration.agent_spawner",
                operation="load_legacy_module",
                params={"path": str(LEGACY_SPAWNER_PATH)},
                user_message="The orchestration worker could not be loaded.",
                recovery_hint="Repair the installation, then try again.",
            ),
        )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configure_legacy_module(module: ModuleType) -> None:
    """Redirect legacy spawn state into the orchestration data directory."""

    orchestration_dir = _get_orchestration_dir()
    logs_dir = _get_logs_dir()
    orchestration_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    legacy_module = cast(Any, module)

    legacy_module.AGENTS_DIR = orchestration_dir
    legacy_module.PROJECT_ROOT = PROJECT_ROOT
    legacy_module.LOG_DIR = logs_dir
    legacy_module.LAST_SPAWN_FILE = orchestration_dir / ".last-spawn-time"
    legacy_module.SPAWN_HISTORY_LOG = logs_dir / "spawn-history.log"

    def _enforce_spawn_cooldown() -> None:
        cooldown_seconds = settings.orchestration_spawn_cooldown
        last_spawn_file = orchestration_dir / ".last-spawn-time"
        if not last_spawn_file.exists():
            return

        try:
            last_spawn = datetime.fromisoformat(last_spawn_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return

        elapsed = (datetime.now() - last_spawn).total_seconds()
        if elapsed < cooldown_seconds:
            wait_seconds = cooldown_seconds - elapsed
            logger.warning(
                "Orchestration spawn cooldown active for %.2fs before launching next worker",
                wait_seconds,
            )
            time.sleep(wait_seconds)

    legacy_module._enforce_spawn_cooldown = _enforce_spawn_cooldown


def _get_spawn_callable(module: ModuleType) -> Any:
    """Validate the legacy module and return its ``spawn_agent`` callable.

    Raises :exc:`ServiceError` if the function is absent or not callable.
    """
    spawn_impl = getattr(module, "spawn_agent", None)
    if not callable(spawn_impl):
        raise ServiceError(
            "Orchestration worker entry point is unavailable",
            ErrorContext(
                component="services.orchestration.agent_spawner",
                operation="spawn_worker_agent",
                params={"path": str(LEGACY_SPAWNER_PATH)},
                user_message="The orchestration worker is unavailable.",
                recovery_hint="Repair the installation, then try again.",
            ),
        )
    return spawn_impl


def _build_spawn_prompt(system_prompt: str | None) -> str | None:
    """Return the effective system prompt for the worker agent.

    Falls back to the default system prompt when *system_prompt* is ``None``.
    """
    return system_prompt if system_prompt is not None else _load_default_system_prompt()


def _invoke_spawn(
    spawn_impl: Any,
    task: str,
    codename: str,
    model: str,
    prompt: str | None,
) -> dict[str, Any]:
    """Call the legacy spawn function and wrap any exception as a :exc:`ServiceError`."""
    try:
        return cast(
            dict[str, Any],
            spawn_impl(
                task=task,
                codename=codename,
                model=model,
                system_prompt=prompt,
                cwd=str(PROJECT_ROOT),
                max_concurrent=settings.orchestration_max_agents,
            ),
        )
    except Exception as exc:
        logger.exception("Failed to spawn orchestration worker %s", codename)
        raise ServiceError(
            f"Failed to spawn worker agent {codename}",
            ErrorContext(
                component="services.orchestration.agent_spawner",
                operation="spawn_worker_agent",
                params={"codename": codename, "model": model},
                user_message="Try again in a moment",
                recovery_hint="Check orchestration worker availability, then try again.",
            ),
            cause=exc,
        ) from exc


def spawn_worker_agent(
    task: str,
    codename: str,
    model: str = "sonnet",
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Spawn a detached Claude worker agent for orchestration tasks."""
    module = _load_legacy_module()
    _configure_legacy_module(module)
    spawn_impl = _get_spawn_callable(module)
    prompt = _build_spawn_prompt(system_prompt)
    result = _invoke_spawn(spawn_impl, task, codename, model, prompt)
    logger.info("Spawned orchestration worker %s with pid=%s", codename, result.get("pid"))
    return result


async def spawn_worker_agent_async(
    task: str,
    codename: str,
    model: str = "sonnet",
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Async wrapper for :func:`spawn_worker_agent`."""

    return await asyncio.to_thread(spawn_worker_agent, task, codename, model, system_prompt)


__all__ = ["spawn_worker_agent", "spawn_worker_agent_async"]
