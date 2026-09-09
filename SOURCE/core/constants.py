"""
Core constants for NOVVIOLA.

This module provides centralized constants to eliminate magic strings
throughout the codebase. Use these instead of hardcoded literals.

Usage:
    from core.constants import Status, Endpoints

    if result.status == Status.OK:
        return success_response(data)
"""

from __future__ import annotations

from enum import StrEnum

VIOLA_VERSION = "1.0.4"


class Status(StrEnum):
    """Standard status values for API responses and health checks."""

    OK = "ok"
    ERROR = "error"
    DEGRADED = "degraded"
    PENDING = "pending"
    UNKNOWN = "unknown"


class ErrorCodes(StrEnum):
    """Standardized error codes for API responses."""

    # General errors
    INTERNAL_ERROR = "INTERNAL_ERROR"
    OPERATION_FAILED = "OPERATION_FAILED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    TIMEOUT = "TIMEOUT"

    # Playback errors
    PLAY_FAILED = "PLAY_FAILED"
    PAUSE_FAILED = "PAUSE_FAILED"
    RESUME_FAILED = "RESUME_FAILED"
    STOP_FAILED = "STOP_FAILED"
    SEEK_FAILED = "SEEK_FAILED"
    VOLUME_FAILED = "VOLUME_FAILED"
    NEXT_FAILED = "NEXT_FAILED"
    PREVIOUS_FAILED = "PREVIOUS_FAILED"
    QUEUE_FAILED = "QUEUE_FAILED"

    # Calendar errors
    CALENDAR_ADD_FAILED = "CALENDAR_ADD_FAILED"
    CALENDAR_LIST_FAILED = "CALENDAR_LIST_FAILED"
    CALENDAR_DELETE_FAILED = "CALENDAR_DELETE_FAILED"
    CALENDAR_NOT_CONFIGURED = "CALENDAR_NOT_CONFIGURED"

    # Weather errors
    WEATHER_FETCH_FAILED = "WEATHER_FETCH_FAILED"
    WEATHER_LOCATION_INVALID = "WEATHER_LOCATION_INVALID"

    # Transcription errors
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED"
    TRANSCRIBER_INIT_FAILED = "TRANSCRIBER_INIT_FAILED"

    # Diagnostics errors
    DIAGNOSTICS_FAILED = "DIAGNOSTICS_FAILED"
    STATE_FETCH_FAILED = "STATE_FETCH_FAILED"

    # Auth errors
    AUTH_FAILED = "AUTH_FAILED"
    OAUTH_FAILED = "OAUTH_FAILED"
    SESSION_INVALID = "SESSION_INVALID"

    # Validation errors
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_INPUT = "INVALID_INPUT"

    # Context errors
    CONTEXT_SYNC_FAILED = "CONTEXT_SYNC_FAILED"

    # Plugin errors
    PLUGIN_LOAD_FAILED = "PLUGIN_LOAD_FAILED"
    PLUGIN_EXECUTION_FAILED = "PLUGIN_EXECUTION_FAILED"
    PLUGIN_NOT_FOUND = "PLUGIN_NOT_FOUND"
    PLUGIN_INSTALL_FAILED = "PLUGIN_INSTALL_FAILED"


class Endpoints:
    """API endpoint path constants."""

    # Health
    HEALTH = "/health"
    HEALTH_DETAILS = "/health/details"

    # Diagnostics
    DIAGNOSTICS = "/v1/diagnostics"
    DIAGNOSTICS_AI_DEBUG = "/v1/diagnostics/ai-debug"
    DIAGNOSTICS_WAKE_WORD = "/v1/diagnostics/wake-word"
    DIAGNOSTICS_CACHE = "/v1/diagnostics/cache"

    # Plugins
    PLUGINS = "/v1/plugins"
    PLUGIN_INSTALL = "/v1/plugins/install"
    PLUGIN_REMOVE = "/v1/plugins/{name}"
    PLUGIN_RELOAD = "/v1/plugins/{name}/reload"


# ========================================================================= #
# Validation Constants
# ========================================================================= #
# Shared input limits for command/query validation.

MAX_COMMAND_TEXT_LENGTH = 500
MAX_QUERY_TEXT_LENGTH = 500


# ========================================================================= #
# Network Configuration Constants
# ========================================================================= #
# These constants define network-related parameters including API endpoints,
# ports, and localhost aliases.

# Localhost aliases
LOCALHOST = "127.0.0.1"
LOCALHOST_NAME = "localhost"
BIND_ALL_INTERFACES = "0.0.0.0"  # nosec B104 - LAN access required for multiroom
LOCAL_ROOM_ID = "local"

# Default API configuration
DEFAULT_API_HOST = BIND_ALL_INTERFACES  # Bind all interfaces for LAN/multiroom access
DEFAULT_API_PORT = 8756
DEFAULT_API_BASE_URL = f"http://{LOCALHOST}:{DEFAULT_API_PORT}"

# Default WebSocket port
DEFAULT_WEBSOCKET_PORT = 3000

# Default CORS origin (React dev server)
DEFAULT_CORS_ORIGIN = f"http://{LOCALHOST_NAME}:{DEFAULT_WEBSOCKET_PORT}"

# Third-party service defaults
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"  # Ollama local LLM server


# ========================================================================= #
# Timeout Constants (in seconds)
# ========================================================================= #
# These constants define standard timeout values to eliminate magic numbers
# and ensure consistency across the codebase.

TIMEOUT_SHORT = 0.1  # Very short polling/quick checks
TIMEOUT_MEDIUM = 0.5  # Medium-length operations
TIMEOUT_DEFAULT = 1.0  # Default timeout for most operations
TIMEOUT_SHUTDOWN = 2.0  # Thread/process shutdown operations
TIMEOUT_GRACE = 3.0  # Grace period for cleanup
TIMEOUT_LONG = 5.0  # Long operations (network requests, service initialization)
TIMEOUT_EXTENDED = 10.0  # Extended operations (database, setup)
TIMEOUT_EXTENDED_PLUS = 12.0  # Slightly longer extended operations (weather API)
TIMEOUT_VERY_LONG = 30.0  # Very long operations (OAuth, external API calls)
TIMEOUT_MINUTE = 60.0  # One minute timeout (WebSocket receives, long polling)
TIMEOUT_LLM = 120.0  # LLM operations (Ollama, GPT API calls)
TIMEOUT_5_MINUTES = 300.0  # 5 minutes (background tasks, long-running operations)
TIMEOUT_10_MINUTES = 600.0  # 10 minutes (weather background refresh)
TIMEOUT_HOUR = 3600.0  # 1 hour (periodic background tasks)

# Browser / payment gate limits
PAYMENT_GATE_MAX_OVERRIDES = 1000


# ========================================================================= #
# Retry Configuration Constants
# ========================================================================= #
# These constants define standard retry behavior to eliminate magic numbers.

RETRY_COUNT_DEFAULT = 3  # Default retry attempts
RETRY_COUNT_EXTENDED = 5  # Extended retry attempts


# ========================================================================= #
# Buffer Size Constants (in bytes)
# ========================================================================= #
# These constants define standard buffer sizes for I/O operations.

BUFFER_SIZE_SMALL = 4096  # 4 KB
BUFFER_SIZE_MEDIUM = 8192  # 8 KB
BUFFER_SIZE_LARGE = 16384  # 16 KB


# ========================================================================= #
# Cache Configuration Constants
# ========================================================================= #
# These constants configure caching behavior.

# Weather cache staleness threshold (as fraction of TTL)
WEATHER_CACHE_THRESHOLD = 0.8  # Mark as stale at 80% of TTL


# ========================================================================= #
# Audio Constants
# ========================================================================= #
# These constants define standard audio parameters used throughout the
# audio processing pipeline to eliminate magic numbers and ensure consistency.

# Sample rates
SAMPLE_RATE_16K = 16000  # Wake word detection, voice processing (16 kHz)
SAMPLE_RATE_24K = 24000  # Kokoro TTS native output (24 kHz)
SAMPLE_RATE_44K = 44100  # CD quality audio (44.1 kHz)
SAMPLE_RATE_48K = 48000  # High quality audio, professional standard (48 kHz)

# Edge-TTS (Microsoft Edge Read-Aloud) voice ShortNames that Microsoft has
# retired server-side. A retired voice still completes the synthesis
# WebSocket handshake but never returns audio (edge_tts.exceptions.
# NoAudioReceived) -- a 100%, deterministic failure for that voice, not a
# transient/rate-limit condition (#3467). Confirmed live via
# edge_tts.list_voices() against the production box; ViolaWake's own
# EDGE_TTS_VOICES pruned the identical 7 in PR #15 (commit 8ca222c -- see
# docs/knowledge/conclusions/CL-20260717-b117.md). Keep this list in ONE
# place so the next Microsoft retirement is a one-line fix instead of a hunt
# across every wake-word sample generator that hardcodes a voice pool.
RETIRED_EDGE_TTS_VOICES: frozenset[str] = frozenset(
    {
        "en-US-DavisNeural",
        "en-US-AmberNeural",
        "en-US-BrandonNeural",
        "en-US-CoraNeural",
        "en-US-ElizabethNeural",
        "en-US-JacobNeural",
        "en-US-MonicaNeural",
    }
)

# Audio data constants
AUDIO_INT16_MAX = 32767  # Maximum value for int16 PCM samples (normalization)
AUDIO_INT16_SCALE = 32768  # Scaling factor for int16 normalization (2^15)

# Quiet-hours defaults
QUIET_HOURS_DEFAULT_ENABLED = True
QUIET_HOURS_DEFAULT_START = "22:00"
QUIET_HOURS_DEFAULT_END = "07:00"
QUIET_HOURS_DEFAULT_TIMEZONE = "auto"
QUIET_HOURS_TIME_OVERRIDE_ENV = "VIOLA_TIME_OF_DAY_OVERRIDE"
QUIET_HOURS_TTS_VOLUME_SCALE = 0.30
WAKE_SENSITIVITY_MIN = 0.05
QUIET_HOURS_WAKE_THRESHOLD_BOOST = 0.08
QUIET_HOURS_WAKE_THRESHOLD_MAX = 0.98

# Buffer/chunk sizes
AUDIO_CHUNK_SIZE = 1024  # Default chunk size in samples

# Channel configurations
AUDIO_CHANNELS_MONO = 1  # Mono audio (1 channel)
AUDIO_CHANNELS_STEREO = 2  # Stereo audio (2 channels)

# Multiroom sync timing constants
# Hub buffer delay: how long hub-local playback is delayed so hub and spokes
# hear audio simultaneously.  Tuned to match spoke steady-state gap (~80ms).
HUB_BUFFER_DEFAULT_MS = 80  # ms — was 140ms; reduced with timestamp anchoring
# ProcTap system-loopback capture delay (hub audio → WASAPI loopback → stamper)
PROCTAP_CAPTURE_DELAY_MS = 30  # ms — measured in sync_tuning.md
# WebSocket binary frame transit time (LAN, hub→spoke)
WS_TRANSIT_DELAY_MS = 10  # ms — typical 1–15ms on WiFi LAN

# Local playlist/media import support
LOCAL_AUDIO_EXTENSIONS = (
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
)


# ========================================================================= #
# Plugin Constants
# ========================================================================= #

PLUGIN_API_VERSION = "1.0"
PLUGIN_MAX_CRASHES = 3
PLUGIN_SANDBOX_TIMEOUT = 30.0
PLUGIN_USER_DIR = "plugins/user"
PLUGIN_BUILTIN_DIR = "plugins/builtin"
PLUGIN_REGISTRY_PATH = "registry/registry.json"
PLUGIN_CONFIG_FILENAME = "config.yaml"
PLUGIN_MANIFEST_FILENAME = "plugin.json"


# ========================================================================= #
# LLM / Prompt Constants
# ========================================================================= #

# Approximate token count of the full build_agent_system_prompt output.
# Used by build_lean_agent_prompt to compute token savings vs the full prompt.
# Update this value if the full prompt grows significantly.
FULL_AGENT_PROMPT_TOKENS_APPROX = 8237


__all__ = [
    "AUDIO_CHANNELS_MONO",
    "AUDIO_CHANNELS_STEREO",
    "AUDIO_CHUNK_SIZE",
    "AUDIO_INT16_MAX",
    "AUDIO_INT16_SCALE",
    "BIND_ALL_INTERFACES",
    "BUFFER_SIZE_LARGE",
    "BUFFER_SIZE_MEDIUM",
    "BUFFER_SIZE_SMALL",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "DEFAULT_CORS_ORIGIN",
    "DEFAULT_WEBSOCKET_PORT",
    "FULL_AGENT_PROMPT_TOKENS_APPROX",
    "HUB_BUFFER_DEFAULT_MS",
    "LOCALHOST",
    "LOCALHOST_NAME",
    "LOCAL_AUDIO_EXTENSIONS",
    "LOCAL_ROOM_ID",
    "OLLAMA_DEFAULT_BASE_URL",
    "PAYMENT_GATE_MAX_OVERRIDES",
    "PLUGIN_API_VERSION",
    "PLUGIN_BUILTIN_DIR",
    "PLUGIN_CONFIG_FILENAME",
    "PLUGIN_MANIFEST_FILENAME",
    "PLUGIN_MAX_CRASHES",
    "PLUGIN_REGISTRY_PATH",
    "PLUGIN_SANDBOX_TIMEOUT",
    "PLUGIN_USER_DIR",
    "PROCTAP_CAPTURE_DELAY_MS",
    "RETRY_COUNT_DEFAULT",
    "RETRY_COUNT_EXTENDED",
    "SAMPLE_RATE_16K",
    "SAMPLE_RATE_24K",
    "SAMPLE_RATE_44K",
    "SAMPLE_RATE_48K",
    "TIMEOUT_5_MINUTES",
    "TIMEOUT_10_MINUTES",
    "TIMEOUT_DEFAULT",
    "TIMEOUT_EXTENDED",
    "TIMEOUT_EXTENDED_PLUS",
    "TIMEOUT_GRACE",
    "TIMEOUT_HOUR",
    "TIMEOUT_LLM",
    "TIMEOUT_LONG",
    "TIMEOUT_MEDIUM",
    "TIMEOUT_MINUTE",
    "TIMEOUT_SHORT",
    "TIMEOUT_SHUTDOWN",
    "TIMEOUT_VERY_LONG",
    "VIOLA_VERSION",
    "WAKE_SENSITIVITY_MIN",
    "WEATHER_CACHE_THRESHOLD",
    "WS_TRANSIT_DELAY_MS",
    "Endpoints",
    "ErrorCodes",
    "Status",
]
