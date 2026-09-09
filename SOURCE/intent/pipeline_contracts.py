from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeAlias

from models.player import PlayerState, QueueItem

if TYPE_CHECKING:
    from services.conversation.context_frames import PromptFrameBundle

TTSKwarg: TypeAlias = str | int | float | bool | None


class PolicyViolation(Exception):
    """Raised when an intent violates system policy."""

    def __init__(self, message: str, intent: str) -> None:
        super().__init__(message)
        self.intent = intent


# ============================================================================
# PROTOCOL CONTRACTS
# ============================================================================

# INTENTIONAL STUBS: These are Protocol definitions using typing.Protocol.
# The `...` (ellipsis) is the required syntax for protocol stub methods.
#
# These enable structural typing (duck typing) - any object with matching methods
# automatically satisfies the protocol, enabling flexible dependency injection.
#


class MusicPort(Protocol):
    """
    Protocol for music player interface (structural typing).

    This is a TYPE CONTRACT, not a base class. The `...` syntax is REQUIRED.
    Every object implementing these methods satisfies this protocol.

    See: music/player/core.py:MusicPlayer for implementation.
    """

    def play(self, query: str, source: str | None = None) -> QueueItem | None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    def skip(self) -> None: ...

    def previous(self) -> None: ...

    def seek(self, seconds: int) -> None: ...

    def set_volume(self, level: int) -> int: ...

    def state(self) -> PlayerState: ...


class IntentTTSPort(Protocol):
    """
    Protocol for TTS (text-to-speech) interface used by the intent pipeline (structural typing).

    This is a TYPE CONTRACT, not a base class. The `...` syntax is REQUIRED.
    Used for dependency injection to allow multiple TTS implementations.

    Note: This is distinct from ``voice.synthesis.factory.TTSPort`` which defines
    the async synthesize/speak interface for the voice synthesis subsystem.
    """

    def say(self, text: str, **kwargs: TTSKwarg) -> None: ...


# Backward-compatible alias so existing imports continue to work.
TTSPort = IntentTTSPort


class GPTPort(Protocol):
    """
    Protocol for GPT handler interface (structural typing).

    This is a TYPE CONTRACT, not a base class. The `...` syntax is REQUIRED.
    Used for dependency injection to allow multiple GPT handler implementations.
    """

    async def route_command(
        self,
        text: str,
        history: list[dict[str, object]] | None = None,
        context_bundle: PromptFrameBundle | None = None,
    ) -> dict[str, object]: ...

    async def ask(self, question: str, history: list[dict[str, object]] | None = None) -> str: ...


class StatePort(Protocol):
    """Application state interface"""

    now_playing: str | None
    is_playing: bool
    queue: list[object]


# ============================================================================
# PIPELINE RESULT
# ============================================================================


@dataclass
class PipelineResult:
    """
    Standardized result from intent pipeline.

    Per PRD v5.3 Section 5.1, this implements the canonical intent envelope schema.

    Attributes:
        ok: Success/failure flag
        intent: Command that was executed (maps to Intent enum value)
        data: Result data (command-specific)
        error: Error message if failed
        source: Where the intent came from (instant, rule, ai, execution)
        bypass_ai: Whether AI was bypassed
        requires_clarification: Whether the intent requires user clarification
        policy_flags: List of policy rule IDs that matched (per PRD)
        id: UUID for traceability (per PRD §5.1)
        confidence: Confidence score [0,1] (per PRD §5.1)
        priority: Priority score, higher wins (per PRD §5.3)
        source_node_id: Origin Spoke node ID (per PRD §5.1)
        timestamp: Hub clock timestamp (per PRD §5.1)
        suggestion: Optional follow-up suggestion metadata for the UI
                    (text + action). Never spoken; surfaced as chips/cards.
    """

    ok: bool
    intent: str
    data: dict[str, object]
    error: str | None = None
    source: str = "unknown"  # instant, rule, ai, execution
    bypass_ai: bool = False
    requires_clarification: bool = False
    policy_flags: list[str] = field(default_factory=list)
    # PRD §5.1 required fields
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    confidence: float = 1.0  # [0,1] default to high confidence
    priority: int = 50  # Default priority (per PRD §5.3: media controls = 50)
    source_node_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    suggestion: dict[str, str | None] | None = None

    def to_dict(self) -> dict[str, object]:
        """Convert to dictionary for API responses"""
        d: dict[str, object] = {
            "ok": self.ok,
            "id": self.id,
            "intent": self.intent,
            "data": self.data if self.ok else {},
            "error": self.error,
            "source": self.source,
            "bypass_ai": self.bypass_ai,
            "requires_clarification": self.requires_clarification,
            "policy_flags": self.policy_flags,
            "confidence": self.confidence,
            "priority": self.priority,
            "source_node_id": self.source_node_id,
            "timestamp": self.timestamp,
        }
        if self.suggestion is not None:
            d["suggestion"] = self.suggestion
        return d
