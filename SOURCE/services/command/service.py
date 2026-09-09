from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from config.settings import settings
from core.logging_config import get_logger
from core.task_tracker import TaskTracker

logger = get_logger(__name__)
from .command_service_core import CommandServiceCoreExecutor
from .command_service_validation import (
    CommandServiceResponseHandler,
    CommandServiceValidator,
)
from .idempotency import IdempotencyLedger
from .types import CommandRequest, CommandResult


@dataclass(slots=True)
class CommandServiceSettings:
    """Configuration knobs for command execution."""

    direct_play_enabled: bool = True
    direct_play_media_root: Path | None = None
    direct_play_allowed_hosts: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> CommandServiceSettings:
        # Legacy settings are still accepted for compatibility, but command
        # text always routes through the unified model pipeline.
        direct_play_enabled = settings.direct_play_enabled

        media_root: Path | None = None
        if settings.direct_play_media_root:
            try:
                media_root = Path(os.path.expandvars(os.path.expanduser(settings.direct_play_media_root))).resolve(
                    strict=False
                )
            except Exception as exc:
                logger.warning(
                    "Invalid direct play media root '%s': %s",
                    settings.direct_play_media_root,
                    exc,
                )
                media_root = None

        # Convert list to tuple for allowed hosts
        allowed_hosts: tuple[str, ...] = tuple(
            host.strip().lower() for host in settings.direct_play_allowed_hosts if host.strip()
        )

        return cls(
            direct_play_enabled=direct_play_enabled,
            direct_play_media_root=media_root,
            direct_play_allowed_hosts=allowed_hosts,
        )


@dataclass(slots=True)
class CommandServiceContext:
    """Dependency bundle shared between HTTP stacks."""

    state: Any
    music: Any
    intent: Any
    hub: Any | None = None
    bindings: Any | None = None
    commands_total: Any | None = None
    play_events_total: Any | None = None
    idempotency_ledger: IdempotencyLedger | None = None


class CommandOrigin(str, Enum):
    """Origin of a command - determines response modality policy."""

    TYPED = "typed"
    VOICE_WAKE = "voice_wake"
    VOICE_PTT = "voice_ptt"


class CommandService:
    """Orchestrates command validation, pipeline execution, and dispatch."""

    def __init__(
        self,
        context: CommandServiceContext,
        *,
        settings: CommandServiceSettings | None = None,
        validator: CommandServiceValidator | None = None,
    ) -> None:
        self.context = context
        self.settings = settings or CommandServiceSettings.from_env()
        self.validator = validator or CommandServiceValidator()
        self._current_request: CommandRequest | None = None  # Track current request for origin
        self._tts_tasks = TaskTracker()

        # Initialize extracted handlers
        self._core_executor = CommandServiceCoreExecutor(self)
        self._response_handler = CommandServiceResponseHandler(self)

    async def execute(self, request: CommandRequest) -> CommandResult:
        """Execute a command request."""
        from services.llm.provider_router import (
            freeze_provider_selection_for_request,
            get_active_router,
        )

        async with freeze_provider_selection_for_request(get_active_router()):
            return await self._core_executor.execute_command(request)
