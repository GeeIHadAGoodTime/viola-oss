from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

from .models import SkillResult

logger = get_logger(__name__)


async def _maybe_await(value: Any) -> Any:
    """Await coroutine-like objects or return synchronous values unchanged."""
    import asyncio

    if asyncio.iscoroutine(value):
        return await value
    return value


class SkillOrchestrator:
    """Adapter around the optional skill manager."""

    def __init__(self, manager: Any | None) -> None:
        self._manager = manager

    @classmethod
    def from_manager(cls, manager: Any | None) -> SkillOrchestrator:
        return cls(manager)

    def available(self) -> bool:
        return self._manager is not None

    async def process(self, text: str) -> SkillResult | None:
        if not self._manager:
            logger.warning("DEBUG: SkillOrchestrator has no manager")
            return None

        try:
            logger.info(
                "DEBUG: SkillOrchestrator.process called with text=%s",
                text[:50] if text else None,
            )
            response = await _maybe_await(self._manager.process(text))
            logger.info(
                "DEBUG: SkillManager.process returned: %s, success=%s",
                type(response).__name__,
                getattr(response, "success", None) if response else None,
            )
        except Exception as exc:
            logger.warning("Skill processing failed: %s", exc)
            return None

        if not response:
            logger.info("DEBUG: Skill returned no response, falling through to interpreter")
            return None

        # CB-2 fix: When a skill matched and executed but reported failure
        # (e.g. "track not found in local library"), we must return the
        # error message instead of discarding it and falling through to the
        # interpreter, which would blindly say "Enqueued: ..." without
        # checking whether anything was actually enqueued.
        if not getattr(response, "success", False):
            message = getattr(response, "message", "")
            if message:
                logger.info(
                    "DEBUG: Skill executed with success=False, returning error: %s",
                    message[:80],
                )
                return SkillResult(
                    message=message,
                    spoken=bool(getattr(response, "spoken", False)),
                    data=getattr(response, "data", None),
                )
            # No meaningful message — fall through to interpreter
            logger.info("DEBUG: Skill returned success=False with no message, falling through to interpreter")
            return None

        return SkillResult(
            message=getattr(response, "message", "Done"),
            spoken=bool(getattr(response, "spoken", False)),
            data=getattr(response, "data", None),
        )
