"""
Unified UX Enhancement Manager - Plugin-Friendly Architecture
Coordinates all UX enhancement systems in a modular, extensible way

Features:
- Loading states & progress feedback
- First-run onboarding
- Smart error recovery
- Voice feedback visualizations
- Queue discovery & recommendations
- Event-driven architecture
- WebSocket integration
- Easy plugin registration
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import cast

from core.json_types import JSONObject, JSONValue, to_json_object
from core.logging_config import get_logger

logger = get_logger(__name__)


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        logger.error("Background task failed: %s", exc)


def _resolve_broadcast_user_id() -> str | None:
    """Return the active user for broadcast, using the desktop principal outside requests."""
    try:
        from core.user_context import get_current_or_device_user_id

        return get_current_or_device_user_id()
    except LookupError:
        return None


# Import enhancement subsystems
from ui.error_recovery import ErrorInfo, get_error_recovery_system
from ui.feedback_system import (
    FeedbackConfig,
    FeedbackType,
    LoadingStage,
    get_feedback_system,
)
from ui.onboarding import get_onboarding_system
from ui.queue_discovery import get_queue_discovery_system


class UXEventType(Enum):
    """Types of UX events that can be broadcast"""

    # Loading events
    LOADING_START = "loading_start"
    LOADING_PROGRESS = "loading_progress"
    LOADING_COMPLETE = "loading_complete"

    # Feedback events
    TOAST_SUCCESS = "toast_success"
    TOAST_ERROR = "toast_error"
    TOAST_WARNING = "toast_warning"
    TOAST_INFO = "toast_info"

    # Error events
    ERROR_OCCURRED = "error_occurred"
    ERROR_RECOVERED = "error_recovered"

    # Voice events
    VOICE_LISTENING = "voice_listening"
    VOICE_TRANSCRIBING = "voice_transcribing"
    VOICE_PROCESSING = "voice_processing"
    VOICE_SPEAKING = "voice_speaking"
    VOICE_COMPLETE = "voice_complete"

    # Music events
    MUSIC_SEARCHING = "music_searching"
    MUSIC_FOUND = "music_found"
    MUSIC_PLAYING = "music_playing"

    # Queue events
    QUEUE_UPDATED = "queue_updated"
    QUEUE_RECOMMENDATIONS = "queue_recommendations"

    # Onboarding events
    ONBOARDING_SHOW = "onboarding_show"
    ONBOARDING_STEP = "onboarding_step"
    ONBOARDING_COMPLETE = "onboarding_complete"


@dataclass
class UXEvent:
    """Unified UX event structure"""

    type: UXEventType
    data: JSONObject
    timestamp: float | None = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = time.time()

    def to_dict(self) -> JSONObject:
        """Convert to dict for JSON serialization"""
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
        }


class UXPlugin:
    """Base class for UX plugins"""

    def __init__(self, manager: UXEnhancementManager):
        self.manager = manager
        self.enabled = True

    def on_event(self, event: UXEvent):
        """Handle incoming UX event"""
        pass

    def enable(self):
        """Enable this plugin"""
        self.enabled = True

    def disable(self):
        """Disable this plugin"""
        self.enabled = False


class UXEnhancementManager:
    """
    Unified UX Enhancement Manager

    Coordinates all UX systems in a modular, plugin-friendly way.
    Acts as central hub for UX events and state management.
    """

    def __init__(self, settings_manager, music_player=None, websocket_hub=None):
        """
        Initialize UX enhancement manager

        Args:
            settings_manager: Settings manager instance
            music_player: Optional music player for queue discovery
            websocket_hub: Optional WebSocket hub for real-time updates
        """
        self.settings_manager = settings_manager
        self.music_player = music_player
        self.websocket_hub = websocket_hub

        # Initialize subsystems
        self.feedback = get_feedback_system()
        self.error_recovery = get_error_recovery_system()
        self.onboarding = get_onboarding_system(settings_manager)
        self.queue_discovery = get_queue_discovery_system(music_player, settings_manager) if music_player else None

        # Plugin registry
        self._plugins: dict[str, UXPlugin] = {}

        # Event subscribers
        self._event_subscribers: dict[UXEventType, list[Callable]] = {}

        # Active operations tracking
        self._active_operations: dict[str, JSONObject] = {}

        # Connect feedback system to our event bus
        self.feedback.subscribe(self._on_feedback_event)

        logger.info("UXEnhancementManager initialized")

    # ==================== Plugin Management ====================

    def register_plugin(self, name: str, plugin: UXPlugin):
        """Register a UX plugin"""
        self._plugins[name] = plugin
        logger.info("Registered UX plugin: %s", name)

    def unregister_plugin(self, name: str):
        """Unregister a UX plugin"""
        if name in self._plugins:
            del self._plugins[name]
            logger.info("Unregistered UX plugin: %s", name)

    def get_plugin(self, name: str) -> UXPlugin | None:
        """Get a registered plugin"""
        return self._plugins.get(name)

    def list_plugins(self) -> list[str]:
        """List all registered plugins"""
        return list(self._plugins.keys())

    # ==================== Event System ====================

    def subscribe(self, event_type: UXEventType, callback: Callable):
        """Subscribe to UX events"""
        if event_type not in self._event_subscribers:
            self._event_subscribers[event_type] = []
        self._event_subscribers[event_type].append(callback)

    def unsubscribe(self, event_type: UXEventType, callback: Callable):
        """Unsubscribe from UX events"""
        if event_type in self._event_subscribers:
            if callback in self._event_subscribers[event_type]:
                self._event_subscribers[event_type].remove(callback)

    async def emit_event(self, event: UXEvent):
        """
        Emit a UX event to all subscribers and plugins

        Also broadcasts to WebSocket clients if hub is available
        """
        # Notify subscribers
        if event.type in self._event_subscribers:
            for callback in self._event_subscribers[event.type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event)
                    else:
                        callback(event)
                except Exception as e:
                    logger.error("Event subscriber error: %s", e)

        # Notify plugins
        for plugin in self._plugins.values():
            if plugin.enabled:
                try:
                    plugin.on_event(event)
                except Exception as e:
                    logger.error("Plugin event error: %s", e)

        # Broadcast to WebSocket clients
        if self.websocket_hub:
            try:
                await self.websocket_hub.broadcast(
                    "ux_event",
                    event.to_dict(),
                    user_id=_resolve_broadcast_user_id(),
                )
            except Exception as e:
                logger.debug("WebSocket broadcast error: %s", e)

    def _on_feedback_event(self, config: FeedbackConfig):
        """Handle feedback system events"""
        # Convert feedback to UX event
        event_type_map = {
            FeedbackType.SUCCESS: UXEventType.TOAST_SUCCESS,
            FeedbackType.ERROR: UXEventType.TOAST_ERROR,
            FeedbackType.WARNING: UXEventType.TOAST_WARNING,
            FeedbackType.INFO: UXEventType.TOAST_INFO,
            FeedbackType.LOADING: UXEventType.LOADING_START,
            FeedbackType.PROGRESS: UXEventType.LOADING_PROGRESS,
        }

        event_type = event_type_map.get(config.type, UXEventType.TOAST_INFO)
        event = UXEvent(type=event_type, data=to_json_object(self.feedback.to_dict(config)))

        # Emit event asynchronously
        task = asyncio.create_task(self.emit_event(event))
        task.add_done_callback(_log_task_exception)

    # ==================== Loading States ====================

    async def show_loading(
        self,
        operation_id: str,
        message: str,
        stage: LoadingStage = LoadingStage.SEARCHING,
        estimated_duration: float | None = None,
    ) -> str:
        """
        Show loading state for an operation

        Returns: operation_id for tracking
        """
        self.feedback.show_loading(operation_id, message, stage, estimated_duration)

        # Track operation
        self._active_operations[operation_id] = {
            "message": message,
            "stage": stage.value,
            "start_time": time.time(),
        }

        # Emit event
        await self.emit_event(
            UXEvent(
                type=UXEventType.LOADING_START,
                data={
                    "operation_id": operation_id,
                    "message": message,
                    "stage": stage.value,
                    "estimated_duration": estimated_duration,
                },
            )
        )

        return operation_id

    async def update_progress(
        self,
        operation_id: str,
        progress: float,
        stage: LoadingStage | None = None,
        message: str | None = None,
    ):
        """Update progress for an operation"""
        self.feedback.update_progress(operation_id, progress, stage, message)

        # Update tracking
        if operation_id in self._active_operations:
            if stage:
                self._active_operations[operation_id]["stage"] = stage.value
            if message:
                self._active_operations[operation_id]["message"] = message

        # Emit event
        await self.emit_event(
            UXEvent(
                type=UXEventType.LOADING_PROGRESS,
                data={
                    "operation_id": operation_id,
                    "progress": progress,
                    "stage": stage.value if stage else None,
                    "message": message,
                },
            )
        )

    async def complete_operation(self, operation_id: str, success: bool = True, message: str | None = None):
        """Mark an operation as complete"""
        self.feedback.complete_operation(operation_id, success, message)

        # Remove from tracking
        if operation_id in self._active_operations:
            del self._active_operations[operation_id]

        # Emit event
        await self.emit_event(
            UXEvent(
                type=UXEventType.LOADING_COMPLETE,
                data={
                    "operation_id": operation_id,
                    "success": success,
                    "message": message,
                },
            )
        )

    # ==================== Music Operations ====================

    async def show_music_search(self, query: str) -> str:
        """Show music search loading state"""
        operation_id = f"music_search_{time.time()}"
        await self.show_loading(operation_id, f'"{query}"', LoadingStage.SEARCHING, estimated_duration=3.0)

        await self.emit_event(
            UXEvent(
                type=UXEventType.MUSIC_SEARCHING,
                data={"query": query, "operation_id": operation_id},
            )
        )

        return operation_id

    async def show_music_found(self, operation_id: str, track_info: JSONObject) -> None:
        """Show music found state"""
        await self.update_progress(operation_id, 0.7, LoadingStage.FETCHING)

        await self.emit_event(UXEvent(type=UXEventType.MUSIC_FOUND, data={"track_info": track_info}))

    async def show_music_playing(self, operation_id: str, track_info: JSONObject) -> None:
        """Show music playing state"""
        await self.complete_operation(
            operation_id,
            success=True,
            message=f"Now playing: {track_info.get('title') or 'Track'} by {track_info.get('artist') or 'Unknown'}",
        )

        await self.emit_event(UXEvent(type=UXEventType.MUSIC_PLAYING, data={"track_info": track_info}))

    # ==================== Voice Operations ====================

    async def show_voice_listening(self) -> None:
        """Show voice listening state"""
        await self.emit_event(UXEvent(type=UXEventType.VOICE_LISTENING, data={"message": "Listening..."}))

    async def show_voice_transcribing(self) -> None:
        """Show voice transcribing state"""
        await self.emit_event(
            UXEvent(
                type=UXEventType.VOICE_TRANSCRIBING,
                data={"message": "Processing audio..."},
            )
        )

    async def show_voice_transcript(self, transcript: str):
        """Show transcription result"""
        await self.emit_event(
            UXEvent(
                type=UXEventType.VOICE_PROCESSING,
                data={"message": f'You said: "{transcript}"', "transcript": transcript},
            )
        )

    async def show_voice_speaking(self, message: str):
        """Show TTS speaking state"""
        await self.emit_event(UXEvent(type=UXEventType.VOICE_SPEAKING, data={"message": message}))

    async def show_voice_complete(self):
        """Voice interaction complete"""
        await self.emit_event(UXEvent(type=UXEventType.VOICE_COMPLETE, data={"message": "Complete"}))

    # ==================== Error Handling ====================

    async def show_error(
        self,
        error_code: str,
        technical_details: str | None = None,
        context: JSONObject | None = None,
    ) -> ErrorInfo:
        """
        Show smart error with recovery options

        Returns: ErrorInfo with actionable suggestions
        """
        error_info = self.error_recovery.get_error_info(error_code, technical_details, context)

        # Show error feedback
        self.feedback.show_error(
            error_info.message,
            duration=5000,
            action_label=error_info.actions[0].label if error_info.actions else None,
            action_callback=(error_info.actions[0].callback if error_info.actions else None),
        )

        # Emit event
        await self.emit_event(
            UXEvent(
                type=UXEventType.ERROR_OCCURRED,
                data=to_json_object(error_info.to_dict()),
            )
        )

        return error_info

    async def attempt_recovery(self, error_code: str, context: JSONObject | None = None) -> bool:
        """Attempt automatic error recovery"""
        success = await self.error_recovery.attempt_auto_recovery(error_code, context)

        if success:
            await self.emit_event(UXEvent(type=UXEventType.ERROR_RECOVERED, data={"error_code": error_code}))

        return bool(success)

    # ==================== Onboarding ====================

    def should_show_onboarding(self) -> bool:
        """Check if onboarding should be shown"""
        return not self.onboarding.is_onboarding_complete()

    async def show_onboarding(self) -> None:
        """Show onboarding wizard"""
        steps = [self.onboarding.to_dict(step) for step in self.onboarding._steps]
        progress = self.onboarding.get_progress()
        await self.emit_event(
            UXEvent(
                type=UXEventType.ONBOARDING_SHOW,
                data=to_json_object({"steps": steps, "progress": progress}),
            )
        )

    async def onboarding_next_step(self) -> JSONObject | None:
        """Move to next onboarding step"""
        # Worker-thread hop: onboarding persistence can spawn a subprocess —
        # keep it off the event loop.
        next_step = await asyncio.to_thread(self.onboarding.next_step)

        if next_step:
            next_step_dict = to_json_object(self.onboarding.to_dict(next_step))
            await self.emit_event(UXEvent(type=UXEventType.ONBOARDING_STEP, data=next_step_dict))
            return next_step_dict
        else:
            await self.emit_event(UXEvent(type=UXEventType.ONBOARDING_COMPLETE, data={"completed": True}))
            return None

    def get_onboarding_progress(self) -> JSONObject:
        """Get onboarding progress"""
        return to_json_object(self.onboarding.get_progress())

    # ==================== Queue Discovery ====================

    async def get_queue_preview(self, max_items: int = 3) -> JSONObject:
        """Get mini queue preview"""
        if not self.queue_discovery:
            return {"items": [], "total_items": 0, "has_more": False}

        preview = self.queue_discovery.get_mini_queue_preview(max_items)

        preview_json = to_json_object(preview)
        await self.emit_event(UXEvent(type=UXEventType.QUEUE_UPDATED, data=preview_json))

        return preview_json

    async def get_recommendations(
        self,
        current_song: dict[str, object] | None = None,
        max_items: int = 5,
    ) -> list[JSONObject]:
        """Get smart music recommendations"""
        if not self.queue_discovery:
            return []

        recommendations = await self.queue_discovery.get_recommendations(current_song, max_items)
        recommendations_list = [to_json_object(rec.to_dict()) for rec in recommendations]

        # Cast required: list[JSONObject] is structurally compatible with list[JSONValue]
        # but mypy doesn't recognize this for dict entries
        data: JSONObject = {
            "recommendations": cast(JSONValue, recommendations_list),
            "count": len(recommendations),
        }
        await self.emit_event(UXEvent(type=UXEventType.QUEUE_RECOMMENDATIONS, data=data))

        return recommendations_list

    # ==================== Status & Debug ====================

    def get_status(self) -> JSONObject:
        """Get comprehensive UX system status"""
        return {
            "enabled": True,
            "plugins": {name: plugin.enabled for name, plugin in self._plugins.items()},
            "active_operations": len(self._active_operations),
            "onboarding_complete": self.onboarding.is_onboarding_complete(),
            "subsystems": {
                "feedback": self.feedback is not None,
                "error_recovery": self.error_recovery is not None,
                "onboarding": self.onboarding is not None,
                "queue_discovery": self.queue_discovery is not None,
            },
        }

    def get_active_operations(self) -> dict[str, JSONObject]:
        """Get all active operations"""
        return self._active_operations.copy()


# ==================== Global Instance ====================

_ux_manager: UXEnhancementManager | None = None


def get_ux_manager(settings_manager=None, music_player=None, websocket_hub=None) -> UXEnhancementManager:
    """Get or create global UX manager instance"""
    global _ux_manager

    if _ux_manager is None:
        if settings_manager is None:
            raise ValueError("settings_manager required for first initialization")
        _ux_manager = UXEnhancementManager(settings_manager, music_player, websocket_hub)

    return _ux_manager


def reset_ux_manager():
    """Reset global UX manager (for testing)"""
    global _ux_manager
    _ux_manager = None
