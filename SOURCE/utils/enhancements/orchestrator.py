"""
Enhancement Orchestrator

Unified interface to apply all enhancements in one call.

Usage:
    from utils.enhancements import enhance_all

    # Enhance everything
    app, player, gpt, settings = enhance_all(
        app=app,
        player=player,
        gpt_handler=gpt,
        settings_manager=settings,
        profile="production"  # or "balanced", "performance", "security"
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar, cast

from core.logging_config import get_logger

from .async_resolution import AsyncResolutionEnhancer
from .connection_pool import ConnectionPoolEnhancer
from .request_tracing import enhance_with_tracing
from .secrets import enhance_with_encryption

logger = get_logger(__name__)

PlayerT = TypeVar("PlayerT")
GptHandlerT = TypeVar("GptHandlerT")
SettingsManagerT = TypeVar("SettingsManagerT")


class _TracingApp(Protocol):
    __dict__: dict[str, object]

    def add_middleware(self, middleware_class: type) -> None: ...


class _HasAsyncCleanup(Protocol):
    async def cleanup(self) -> None: ...


@dataclass
class EnhancementProfile:
    """Configuration profile for enhancements."""

    name: str
    connection_pooling: bool = True
    secrets_encryption: bool = True
    request_tracing: bool = True
    async_resolution: bool = True

    # Tuning parameters
    max_connections: int = 100
    max_keepalive: int = 20
    resolution_workers: int = 4


# Predefined profiles
PROFILES: dict[str, EnhancementProfile] = {
    "production": EnhancementProfile(
        name="production",
        connection_pooling=True,
        secrets_encryption=True,
        request_tracing=True,
        async_resolution=True,
        max_connections=100,
        max_keepalive=20,
        resolution_workers=4,
    ),
    "balanced": EnhancementProfile(
        name="balanced",
        connection_pooling=True,
        secrets_encryption=True,
        request_tracing=True,
        async_resolution=True,
        max_connections=50,
        max_keepalive=10,
        resolution_workers=2,
    ),
    "performance": EnhancementProfile(
        name="performance",
        connection_pooling=True,
        secrets_encryption=False,  # Skip for max speed
        request_tracing=False,  # Skip logging overhead
        async_resolution=True,
        max_connections=200,
        max_keepalive=50,
        resolution_workers=8,
    ),
    "security": EnhancementProfile(
        name="security",
        connection_pooling=True,
        secrets_encryption=True,
        request_tracing=True,  # Full audit trail
        async_resolution=False,  # More predictable/auditable
        max_connections=50,
        max_keepalive=10,
        resolution_workers=2,
    ),
    "minimal": EnhancementProfile(
        name="minimal",
        connection_pooling=False,
        secrets_encryption=False,
        request_tracing=False,
        async_resolution=False,
    ),
}


class EnhancementOrchestrator:
    """
    Orchestrates application of all enhancements.

    Usage:
        orchestrator = EnhancementOrchestrator(profile="production")
        orchestrator.enhance_app(app)
        orchestrator.enhance_player(player)
        orchestrator.enhance_gpt(gpt_handler)
    """

    def __init__(self, profile: str = "balanced"):
        """
        Initialize orchestrator with profile.

        Args:
            profile: Enhancement profile name (production, balanced, etc.)
        """
        if profile not in PROFILES:
            logger.warning("Unknown profile '%s', using 'balanced'", profile)
            profile = "balanced"

        self.profile = PROFILES[profile]
        self.enhancers: dict[str, object] = {}
        self._applied: list[str] = []

        logger.info("🚀 Enhancement Orchestrator initialized with profile: %s", profile)

    def enhance_app(self, app: _TracingApp) -> _TracingApp:
        """Enhance FastAPI application."""
        if app is None:
            return app

        enhancements = []

        # Request tracing
        if self.profile.request_tracing:
            try:
                app = enhance_with_tracing(app)
                enhancements.append("request_tracing")
            except Exception as e:
                logger.error("Failed to enhance app with tracing: %s", e)

        if enhancements:
            self._applied.extend([f"app:{e}" for e in enhancements])
            logger.info("✅ App enhanced with: %s", ", ".join(enhancements))

        return app

    def enhance_player(self, player: PlayerT) -> PlayerT:
        """Enhance music player."""
        if player is None:
            return player

        enhancements = []

        # Async resolution
        if self.profile.async_resolution:
            try:
                enhancer = AsyncResolutionEnhancer(max_workers=self.profile.resolution_workers)
                player = enhancer.enhance(player)
                self.enhancers["async_resolution"] = enhancer
                enhancements.append("async_resolution")
            except Exception as e:
                logger.error("Failed to enhance player with async resolution: %s", e)

        if enhancements:
            self._applied.extend([f"player:{e}" for e in enhancements])
            logger.info("✅ Player enhanced with: %s", ", ".join(enhancements))

        return player

    def enhance_gpt(self, gpt_handler: GptHandlerT) -> GptHandlerT:
        """Enhance GPT handler."""
        if gpt_handler is None:
            return gpt_handler

        enhancements = []

        # Connection pooling
        if self.profile.connection_pooling:
            try:
                enhancer = ConnectionPoolEnhancer(
                    max_connections=self.profile.max_connections,
                    max_keepalive=self.profile.max_keepalive,
                )
                gpt_handler = enhancer.enhance(gpt_handler)
                self.enhancers["connection_pool"] = enhancer
                enhancements.append("connection_pooling")
            except Exception as e:
                logger.error("Failed to enhance GPT with connection pooling: %s", e)

        if enhancements:
            self._applied.extend([f"gpt:{e}" for e in enhancements])
            logger.info("✅ GPT enhanced with: %s", ", ".join(enhancements))

        return gpt_handler

    def enhance_settings(self, settings_manager: SettingsManagerT) -> SettingsManagerT:
        """Enhance settings manager."""
        if settings_manager is None:
            return settings_manager

        enhancements = []

        # Secrets encryption
        if self.profile.secrets_encryption:
            try:
                settings_manager = cast(SettingsManagerT, enhance_with_encryption(settings_manager))
                enhancements.append("encryption")
            except Exception as e:
                logger.error("Failed to enhance settings with encryption: %s", e)

        if enhancements:
            self._applied.extend([f"settings:{e}" for e in enhancements])
            logger.info("✅ Settings enhanced with: %s", ", ".join(enhancements))

        return settings_manager

    def get_status(self) -> dict[str, object]:
        """Get status of applied enhancements."""
        return {
            "profile": self.profile.name,
            "applied": self._applied,
            "enhancers": list(self.enhancers.keys()),
        }

    async def cleanup(self) -> None:
        """Cleanup all enhancers."""
        for name, enhancer in self.enhancers.items():
            if hasattr(enhancer, "cleanup"):
                try:
                    await cast(_HasAsyncCleanup, enhancer).cleanup()
                    logger.info("🧹 Cleaned up %s", name)
                except Exception as e:
                    logger.error("Failed to cleanup %s: %s", name, e)


def enhance_all(
    app: _TracingApp | None = None,
    player: PlayerT | None = None,
    gpt_handler: GptHandlerT | None = None,
    settings_manager: SettingsManagerT | None = None,
    profile: str = "balanced",
) -> tuple[_TracingApp | None, PlayerT | None, GptHandlerT | None, SettingsManagerT | None]:
    """
    Enhance all components with one call.

    Args:
        app: FastAPI application
        player: Music player
        gpt_handler: GPT handler
        settings_manager: Settings manager
        profile: Enhancement profile (production, balanced, performance, security)

    Returns:
        Tuple of (app, player, gpt_handler, settings_manager)

    Example:
        from utils.enhancements import enhance_all

        app, player, gpt, settings = enhance_all(
            app=app,
            player=player,
            gpt_handler=gpt,
            settings_manager=settings,
            profile="production"
        )
    """
    orchestrator = EnhancementOrchestrator(profile=profile)

    # Enhance each component
    if app is not None:
        app = orchestrator.enhance_app(app)
    if player is not None:
        player = orchestrator.enhance_player(player)
    if gpt_handler is not None:
        gpt_handler = orchestrator.enhance_gpt(gpt_handler)
    if settings_manager is not None:
        settings_manager = orchestrator.enhance_settings(settings_manager)

    # Log summary
    status = orchestrator.get_status()
    logger.info(
        "🎉 Enhancement complete! Profile: %s, Applied: %s enhancements",
        status.get("profile"),
        len(orchestrator._applied),
    )

    return app, player, gpt_handler, settings_manager


def get_available_profiles() -> list[str]:
    """Get list of available enhancement profiles."""
    return list(PROFILES.keys())


def get_profile_info(profile_name: str) -> dict[str, object] | None:
    """Get information about an enhancement profile."""
    if profile_name not in PROFILES:
        return None

    profile = PROFILES[profile_name]
    return {
        "name": profile.name,
        "features": {
            "connection_pooling": profile.connection_pooling,
            "secrets_encryption": profile.secrets_encryption,
            "request_tracing": profile.request_tracing,
            "async_resolution": profile.async_resolution,
        },
        "tuning": {
            "max_connections": profile.max_connections,
            "max_keepalive": profile.max_keepalive,
            "resolution_workers": profile.resolution_workers,
        },
    }
