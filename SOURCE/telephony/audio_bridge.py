"""Bidirectional audio bridge connecting two LoopbackTransports.

Creates two asyncio.Queues forming a crossover:
    Side A (Viola) TTS output → queue → Side B (Business) STT input
    Side B (Business) TTS output → queue → Side A (Viola) STT input
"""

from __future__ import annotations

import asyncio

from telephony.loopback_transport import LoopbackTransport


class AudioBridge:
    """Bidirectional audio connection between two Pipecat pipelines.

    Usage::

        bridge = AudioBridge()
        viola_transport = bridge.get_transport_a(name="viola")
        business_transport = bridge.get_transport_b(name="business")

        # Viola's TTS → business's STT (via a_to_b queue)
        # Business's TTS → Viola's STT (via b_to_a queue)
    """

    def __init__(self, maxsize: int = 2000) -> None:
        self.a_to_b: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.b_to_a: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    def get_transport_a(self, name: str = "viola") -> LoopbackTransport:
        """Transport for side A. Reads from b_to_a, writes to a_to_b."""
        return LoopbackTransport(
            input_queue=self.b_to_a,
            output_queue=self.a_to_b,
            name=name,
        )

    def get_transport_b(self, name: str = "business") -> LoopbackTransport:
        """Transport for side B. Reads from a_to_b, writes to b_to_a."""
        return LoopbackTransport(
            input_queue=self.a_to_b,
            output_queue=self.b_to_a,
            name=name,
        )
