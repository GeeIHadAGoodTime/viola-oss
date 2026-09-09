"""SourceAudioBroadcaster - re-export of HubAudioBroadcaster under the new name.

Origin: commit ``09523c15`` (2026-05-07) renamed the source-side broadcaster
in ``pipeline_wiring.py`` from ``HubAudioBroadcaster`` to ``SourceAudioBroadcaster``
as part of the source/device naming refactor, but the new module file was never
created. The import at ``pipeline_wiring.py:37,345`` raised ``ModuleNotFoundError``
from then on, which ``backend/fastapi_app.py:286-302``'s broad ``except Exception:``
swallowed - the production result was that ``/ws/audio-stream`` was never
registered and every browser spoke that tried to connect was rejected.

The Round 1 Condition-1 isolation audit on 2026-05-29 (Opus, live-verified)
identified the missing module by exercising the import path that the unit
tests bypass. ``HubAudioBroadcaster`` has the exact API the wiring expects -
``__init__(event_hub, room_id)``, ``start()``, ``stop()``, ``set_event_loop()``,
``on_pcm_data()`` - so the smallest reversible fix is to alias it under the
new name here.

The ``check_audio_stream_route_registered`` ratchet pins the regression class.
"""

from __future__ import annotations

from .hub_broadcaster import HubAudioBroadcaster as SourceAudioBroadcaster

__all__ = ["SourceAudioBroadcaster"]
