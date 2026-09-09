"""
Base Skill Interface for Viola Plugin System

This module provides the abstract base class for all Viola skills.
Skills are self-contained, pluggable modules that handle specific intents.

Example:
    class WeatherSkill(Skill):
        name = "weather"
        description = "Provides weather information"
        priority = 50

        def patterns(self):
            return [
                r"what'?s? the weather",
                r"weather (?:in|at) (?P<location>.+)",
            ]

        async def execute(self, intent):
            location = intent.params.get('location', 'here')
            weather = await self.get_weather(location)
            return Response(
                message=f"It's {weather.temp}°F and {weather.condition}",
                spoken=True
            )
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from re import Pattern
from typing import Any


@dataclass
class Intent:
    """
    Represents a parsed user intent.

    Attributes:
        type: Intent type/name (e.g., 'play', 'weather', 'timer')
        text: Original user input
        params: Extracted parameters from pattern matching
        confidence: Match confidence (0.0 to 1.0)
        context: Additional context (history, user info, etc.)
    """

    type: str
    text: str
    params: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    context: dict[str, Any] | None = None


@dataclass
class Response:
    """
    Skill execution response.

    Attributes:
        message: Response message to display/speak
        spoken: Whether response was already spoken (avoid duplicate TTS)
        success: Whether skill executed successfully
        data: Additional response data (for UI display, etc.)
    """

    message: str
    spoken: bool = False
    success: bool = True
    data: dict[str, Any] | None = None


class SkillContext:
    """
    Runtime dependencies available to skills.

    Provides access to core subsystems without tight coupling.
    """

    def __init__(
        self,
        music_player: Any | None = None,
        tts_engine: Any | None = None,
        gpt_handler: Any | None = None,
        settings: Any | None = None,
        state: Any | None = None,
    ):
        self.music = music_player
        self.tts = tts_engine
        self.gpt = gpt_handler
        self.settings = settings
        self.state = state

    def has(self, feature: str) -> bool:
        """Check if a feature is available."""
        return getattr(self, feature, None) is not None


class Skill(ABC):
    """
    Abstract base class for all Viola skills.

    Skills must implement:
    - patterns(): Return list of regex patterns to match
    - execute(): Handle the matched intent

    Skills should define:
    - name: Unique skill identifier
    - description: Human-readable description
    - priority: Execution priority (higher = earlier, default 50)
    - enabled: Whether skill is active (default True)
    """

    # Class attributes (override in subclass)
    name: str = "unnamed_skill"
    description: str = "No description"
    priority: int = 50  # Higher priority skills are tried first
    enabled: bool = True

    def __init__(self, context: SkillContext | None = None):
        """
        Initialize skill with runtime context.

        Args:
            context: SkillContext with access to music, TTS, etc.
        """
        self.context = context or SkillContext()
        self._compiled_patterns: list[Pattern] | None = None

    @abstractmethod
    def patterns(self) -> list[str]:
        """
        Return list of regex patterns this skill can handle.

        Patterns can include named groups for parameter extraction:
        - r"play (?P<query>.+)" -> matches "play jazz" with query="jazz"
        - r"weather in (?P<location>\\w+)" -> extracts location

        Returns:
            List of regex pattern strings (will be compiled with re.IGNORECASE)
        """
        pass

    @abstractmethod
    async def execute(self, intent: Intent) -> Response:
        """
        Execute the skill for the given intent.

        Args:
            intent: Parsed intent with type, text, params, etc.

        Returns:
            Response with message, spoken flag, success status

        Raises:
            Exception: If execution fails (caught by skill manager)
        """
        pass

    def can_handle(self, text: str) -> Intent | None:
        """
        Check if this skill can handle the given text.

        Args:
            text: User input text

        Returns:
            Intent if matched, None otherwise
        """
        from core.logging_config import get_logger

        logger = get_logger(__name__)

        if not self.enabled:
            return None

        # Compile patterns once
        if self._compiled_patterns is None:
            self._compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.patterns()]

        # Try each pattern
        for pattern in self._compiled_patterns:
            match = pattern.search(text)
            if match:
                logger.info(
                    "DEBUG: Skill %s matched text=%s with pattern=%s params=%s",
                    self.name,
                    text[:30],
                    pattern.pattern[:30],
                    match.groupdict(),
                )
                return Intent(
                    type=self.name,
                    text=text,
                    params=match.groupdict(),
                    confidence=1.0,
                )

        logger.debug("DEBUG: Skill %s did NOT match text=%s", self.name, text[:30])
        return None

    async def validate(self) -> bool:
        """
        Validate skill dependencies and configuration.

        Override this to check if required dependencies are available.
        For example, a weather skill might check for API keys.

        Returns:
            True if skill is ready, False if dependencies missing
        """
        return True

    def to_dict(self) -> dict[str, Any]:
        """Convert skill to dictionary representation."""
        return {
            "name": self.name,
            "description": self.description,
            "priority": self.priority,
            "enabled": self.enabled,
            "patterns": self.patterns(),
        }

    def __repr__(self) -> str:
        return f"<Skill: {self.name} (priority={self.priority}, enabled={self.enabled})>"
