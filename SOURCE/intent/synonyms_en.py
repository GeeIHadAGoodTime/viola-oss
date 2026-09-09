# Language pack: English synonyms for command patterns.
# This module is intentionally tiny and dependency-free.
# Extend by adding keys or lists below.

from __future__ import annotations

SYNONYMS: dict[str, list[str]] = {
    "play": ["play", "queue", "add"],
    "pause": ["pause", "hold", "wait"],
    "resume": ["resume", "continue", "unpause"],
    "stop": ["stop", "halt"],
    "skip": ["skip", "next"],
    "volume": ["volume", "vol", "sound"],
    "up": ["up", "increase", "louder"],
    "down": ["down", "decrease", "quieter"],
    "status": ["status", "what's playing", "whats playing", "now playing"],
    "help": ["help", "commands", "what can you do"],
}
