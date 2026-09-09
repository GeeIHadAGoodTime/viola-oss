"""
Autoplay package.

Provides queue auto-refill using YouTube Music search for related tracks.
This replaces the previous GPT-based AI autoplay system.
"""

from .ytmusic_radio import YouTubeMusicRadio, get_ytmusic_radio

__all__ = ["YouTubeMusicRadio", "get_ytmusic_radio"]
