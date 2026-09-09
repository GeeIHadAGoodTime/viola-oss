"""Canonical ordered frame chain for provider-bound conversation state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from services.conversation.context_frames import Frame, FrameRole, PromptFrameBundle
from services.conversation.frame_rendering import (
    normalize_prompt_bundle_for_provider,
    render_for_anthropic,
    render_for_openai_responses,
)

ProviderMode = Literal["full", "delta"]


@dataclass(frozen=True)
class CanonicalFrameChain:
    """Single ordered stream of model-message frames.

    System-role frames may ride alongside the stream for provider renderers,
    but behavioral and meta user/assistant/tool frames retain their original
    order in ``frames``.
    """

    frames: tuple[Frame, ...]

    def __init__(self, frames: Sequence[Frame] = ()) -> None:
        object.__setattr__(self, "frames", tuple(frame for frame in frames if isinstance(frame, Frame)))

    def append(self, frame: Frame) -> CanonicalFrameChain:
        """Return a new chain with ``frame`` appended."""

        return CanonicalFrameChain((*self.frames, frame))

    def with_runtime_context(self, frames: Sequence[Frame]) -> CanonicalFrameChain:
        """Return a new chain with runtime meta frames prepended."""

        runtime_frames = tuple(frame for frame in frames if isinstance(frame, Frame))
        return CanonicalFrameChain((*runtime_frames, *self.frames))

    def repair_for_provider(self, mode: ProviderMode = "full") -> CanonicalFrameChain:
        """Apply the provider-boundary frame normalizer once."""

        bundle = normalize_prompt_bundle_for_provider(
            PromptFrameBundle(frames=list(self.frames)),
            mode=mode,
        )
        return CanonicalFrameChain(bundle.frames)

    def to_prompt_bundle(self, *, normalized: bool = False) -> PromptFrameBundle:
        """Return the canonical provider input shape for this chain."""

        system_frames = [frame for frame in self.frames if frame.role is FrameRole.SYSTEM]
        message_frames = [frame for frame in self.frames if frame.role is not FrameRole.SYSTEM]
        return PromptFrameBundle(
            system_dynamic_blocks=system_frames,
            frames=message_frames,
            provider_normalized=normalized,
        )

    def render(self, provider: str, *, mode: ProviderMode = "full") -> dict[str, Any]:
        """Render the chain for a provider family."""

        repaired = self.repair_for_provider(mode)
        bundle = repaired.to_prompt_bundle(normalized=True)
        provider_name = str(provider or "").strip().lower()
        if provider_name in {"anthropic", "claude"}:
            return render_for_anthropic(bundle)
        return render_for_openai_responses(bundle)


__all__ = ["CanonicalFrameChain", "ProviderMode"]
