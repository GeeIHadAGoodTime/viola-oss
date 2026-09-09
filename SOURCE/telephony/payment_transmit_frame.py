"""Pipecat frame types for secure phone payment transmission."""

from __future__ import annotations

from pipecat.frames.frames import Frame, TTSSpeakFrame


class PaymentTransmitTTSFrame(TTSSpeakFrame):
    """TTS-only payment frame that must not be persisted to transcripts."""

    viola_skip_transcript = True

    def __init__(self, text: str, *, segment_id: str | None = None) -> None:
        super().__init__(text)
        self.viola_payment_segment_id = segment_id


class PaymentTransmitEndFrame(Frame):
    """Boundary frame that closes a payment-sensitive segment after TTS audio."""

    viola_skip_transcript = True

    def __init__(self, *, segment_id: str) -> None:
        super().__init__()
        self.viola_payment_segment_id = segment_id
