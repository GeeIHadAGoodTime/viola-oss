"""
PCM Audio Streaming for Multi-Room Sync.

This module provides components for streaming timestamped PCM audio from a
Hub (audio source) to multiple Spoke devices (playback endpoints) for
synchronized multi-room playback.

Architecture:
    Hub Side:
        - AudioTee: Splits decoded PCM to local playback and network broadcast
        - HubAudioBroadcaster: Creates timestamped chunks and broadcasts via WebSocket

    Spoke Side:
        - SpokeAudioReceiver: Receives chunks from WebSocket
        - PlaybackScheduler: Buffers and schedules chunks for playback
        - LateChunkPolicy: Decides whether to play or drop late chunks

    Protocol:
        - PCMChunk: 20ms audio chunks at 48kHz stereo 16-bit
        - Wire format: 16-byte header + 3840 bytes PCM data = 3856 bytes per frame
        - Header: play_at (float64), sequence (uint32), flags (uint16), reserved (uint16)

Usage:
    # Hub side
    from audio_core.streaming import AudioTee, HubAudioBroadcaster

    tee = AudioTee()
    broadcaster = HubAudioBroadcaster(event_hub, room_id="living_room")
    tee.add_output(broadcaster.on_pcm_data)
    broadcaster.start()

    # Spoke side
    from audio_core.streaming import (
        SpokeAudioReceiver,
        PlaybackScheduler,
        LateChunkPolicy,
    )

    policy = LateChunkPolicy()
    scheduler = PlaybackScheduler(policy)
    receiver = SpokeAudioReceiver(scheduler)
    receiver.start()

    # On WebSocket message:
    receiver.on_websocket_message(payload)

    # Audio output reads from scheduler:
    pcm_data = scheduler.read(4096)
"""

from __future__ import annotations

from .audio_tee import AudioTee
from .chunk_protocol import (
    BYTES_PER_SAMPLE,
    CHANNELS,
    CHUNK_DURATION_MS,
    CHUNK_SIZE_BYTES,
    FLAG_SILENCE,
    FRAME_SIZE,
    HEADER_SIZE,
    SAMPLE_RATE,
    SAMPLES_PER_CHUNK,
    SILENCE_PCM,
    PCMChunk,
    PCMChunkHeader,
    bytes_to_duration_ms,
    chunk_duration_seconds,
    deserialize,
    duration_ms_to_bytes,
    serialize,
)
from .exceptions import (
    BufferOverrunError,
    BufferUnderrunError,
    ChunkDeserializationError,
    StreamingError,
)
from .hub_broadcaster import LEAD_TIME_MS, HubAudioBroadcaster
from .late_chunk_policy import LATE_THRESHOLD_MS, LateChunkPolicy
from .playback_scheduler import MAX_BUFFER_MS, TARGET_BUFFER_MS, PlaybackScheduler
from .source_broadcaster import SourceAudioBroadcaster
from .spoke_receiver import SpokeAudioReceiver

__all__ = [
    # Chunk Protocol
    "BYTES_PER_SAMPLE",
    "CHANNELS",
    "CHUNK_DURATION_MS",
    "CHUNK_SIZE_BYTES",
    "FLAG_SILENCE",
    "FRAME_SIZE",
    "HEADER_SIZE",
    # Late Chunk Policy
    "LATE_THRESHOLD_MS",
    "LEAD_TIME_MS",
    # Playback Scheduler
    "MAX_BUFFER_MS",
    "SAMPLES_PER_CHUNK",
    "SAMPLE_RATE",
    "SILENCE_PCM",
    "TARGET_BUFFER_MS",
    # Audio Tee
    "AudioTee",
    # Exceptions
    "BufferOverrunError",
    "BufferUnderrunError",
    "ChunkDeserializationError",
    # Hub Broadcaster
    "HubAudioBroadcaster",
    "LateChunkPolicy",
    "PCMChunk",
    "PCMChunkHeader",
    "PlaybackScheduler",
    # Source Broadcaster (alias of HubAudioBroadcaster, post 09523c15 rename)
    "SourceAudioBroadcaster",
    # Spoke Receiver
    "SpokeAudioReceiver",
    "StreamingError",
    "bytes_to_duration_ms",
    "chunk_duration_seconds",
    "deserialize",
    "duration_ms_to_bytes",
    "serialize",
]
