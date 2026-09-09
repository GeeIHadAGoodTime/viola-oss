"""
Application-wide constants.

Keep this module minimal: only constants that are referenced from runtime code
or tests belong here. User-tunable defaults live in `config/defaults.py`.
"""

from __future__ import annotations

from config import env
from core.constants import SAMPLE_RATE_16K
from core.platform import get_cache_dir, get_data_dir

# =============================================================================
# MUSIC PLAYER
# =============================================================================

MAX_QUEUE_SIZE = 10
"""Maximum number of songs in the queue."""

AUTOPLAY_MAX_QUEUE = 15
"""Maximum songs autoplay can add (prevents API spam)."""

# =============================================================================
# AUDIO
# =============================================================================

AUDIO_SAMPLE_RATE = SAMPLE_RATE_16K
"""Sample rate for audio recording (16kHz is standard for speech)."""

AUDIO_CHUNK_SIZE = 1024
"""Samples per audio buffer chunk."""

# =============================================================================
# USER INTERFACE
# =============================================================================

UI_MODE_NATIVE_QT = "native_qt"
"""Native Qt UI mode (`viola_qt.py`)."""

UI_MODE_WEB_EMBEDDED = "web_embedded"
"""Web UI embedded in Qt WebEngine."""

ACTIVE_UI_MODE = UI_MODE_NATIVE_QT
"""Currently active UI mode."""

UI_UPDATE_INTERVAL_MS = 16
"""UI update interval for smooth 60fps rendering (~16ms)."""

# =============================================================================
# DEVELOPMENT / DEBUG
# =============================================================================

DEBUG_MODE = False
"""Enable debug logging and features."""

# =============================================================================
# PATHS
# =============================================================================

DATA_DIR = str(get_data_dir())
"""Base directory for application data."""

CACHE_DIR = env.get("VIOLA_CACHE_DIR", str(get_cache_dir()))
"""Directory for cached files."""

# =============================================================================
# INPUT CONSTRAINTS
# =============================================================================

MAX_COMMAND_LENGTH = 500
"""Maximum length for user commands."""

# =============================================================================
# SUBPROCESS SECURITY
# =============================================================================

SUBPROCESS_TIMEOUT_SECONDS = 30
"""Maximum time allowed for subprocess operations."""
