"""
Visual Feedback System - Modular, Plugin-Friendly
Provides loading states, animations, and progress indicators for better UX

Features:
- Loading state management
- Progress tracking
- Toast notifications
- Skeleton screens
- Micro-interactions
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from core.constants import TIMEOUT_GRACE
from core.logging_config import get_logger

logger = get_logger(__name__)

# UI feedback duration constant (in milliseconds)
UI_FEEDBACK_DURATION_MS = int(TIMEOUT_GRACE * 1000)  # 3000ms = 3 seconds


class FeedbackType(Enum):
    """Types of feedback that can be shown"""

    LOADING = "loading"
    SUCCESS = "success"
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    PROGRESS = "progress"


class LoadingStage(Enum):
    """Stages for multi-step operations"""

    SEARCHING = "searching"
    FETCHING = "fetching"
    PROCESSING = "processing"
    FINALIZING = "finalizing"
    COMPLETE = "complete"


@dataclass
class FeedbackConfig:
    """Configuration for feedback display"""

    type: FeedbackType
    message: str
    duration: int = UI_FEEDBACK_DURATION_MS  # milliseconds
    action_label: str | None = None
    action_callback: str | None = None  # JavaScript function name
    icon: str | None = None
    progress: float | None = None  # 0.0 to 1.0
    stage: LoadingStage | None = None
    dismissible: bool = True
    show_spinner: bool = False


@dataclass
class OperationState:
    """Tracks state of a long-running operation"""

    id: str
    name: str
    stage: LoadingStage
    progress: float  # 0.0 to 1.0
    message: str
    start_time: float
    estimated_duration: float | None = None  # seconds

    @property
    def elapsed_time(self) -> float:
        """Time elapsed since operation started"""
        return time.time() - self.start_time

    @property
    def estimated_remaining(self) -> float | None:
        """Estimated time remaining in seconds"""
        if self.estimated_duration and self.progress > 0:
            total_est = self.estimated_duration
            elapsed = self.elapsed_time
            if self.progress < 1.0:
                return max(0, (total_est - elapsed))
        return None


class FeedbackSystem:
    """
    Centralized feedback system for user notifications and loading states.
    Works across web and native Qt interfaces.
    """

    def __init__(self):
        self._operations: dict[str, OperationState] = {}
        self._feedback_queue: list[FeedbackConfig] = []
        self._subscribers: list[Callable] = []

    def subscribe(self, callback: Callable[[FeedbackConfig], None]):
        """Subscribe to feedback events"""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        """Unsubscribe from feedback events"""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _notify_subscribers(self, config: FeedbackConfig):
        """Notify all subscribers of new feedback"""
        for callback in self._subscribers:
            try:
                callback(config)
            except Exception as e:
                logger.error("Feedback subscriber error: %s", e)

    def show_loading(
        self,
        operation_id: str,
        message: str,
        stage: LoadingStage = LoadingStage.SEARCHING,
        estimated_duration: float | None = None,
    ) -> str:
        """
        Start showing loading feedback for an operation

        Returns: operation_id for tracking
        """
        operation = OperationState(
            id=operation_id,
            name=message,
            stage=stage,
            progress=0.0,
            message=self._get_stage_message(stage, message),
            start_time=time.time(),
            estimated_duration=estimated_duration,
        )

        self._operations[operation_id] = operation

        # Show feedback
        config = FeedbackConfig(
            type=FeedbackType.LOADING,
            message=operation.message,
            icon=self._get_stage_icon(stage),
            show_spinner=True,
            dismissible=False,
            stage=stage,
        )

        self._notify_subscribers(config)
        return operation_id

    def update_progress(
        self,
        operation_id: str,
        progress: float,
        stage: LoadingStage | None = None,
        message: str | None = None,
    ):
        """Update progress for an ongoing operation"""
        if operation_id not in self._operations:
            logger.warning("Unknown operation: %s", operation_id)
            return

        operation = self._operations[operation_id]
        operation.progress = min(1.0, max(0.0, progress))

        if stage:
            operation.stage = stage

        if message:
            operation.message = message
        else:
            operation.message = self._get_stage_message(operation.stage, operation.name)

        # Notify subscribers with progress
        config = FeedbackConfig(
            type=FeedbackType.PROGRESS,
            message=operation.message,
            progress=operation.progress,
            stage=operation.stage,
            icon=self._get_stage_icon(operation.stage),
            show_spinner=operation.progress < 1.0,
            dismissible=False,
        )

        self._notify_subscribers(config)

    def complete_operation(self, operation_id: str, success: bool = True, message: str | None = None):
        """Mark an operation as complete"""
        if operation_id not in self._operations:
            return

        operation = self._operations.pop(operation_id)

        if not message:
            message = f"✓ {operation.name} complete" if success else f"✗ {operation.name} failed"

        config = FeedbackConfig(
            type=FeedbackType.SUCCESS if success else FeedbackType.ERROR,
            message=message,
            icon="✓" if success else "✗",
            duration=2000,
            dismissible=True,
            show_spinner=False,
        )

        self._notify_subscribers(config)

    def show_toast(
        self,
        message: str,
        type: FeedbackType = FeedbackType.INFO,
        duration: int = UI_FEEDBACK_DURATION_MS,
        action_label: str | None = None,
        action_callback: str | None = None,
        icon: str | None = None,
    ):
        """Show a toast notification"""
        if not icon:
            icon = self._get_type_icon(type)

        config = FeedbackConfig(
            type=type,
            message=message,
            duration=duration,
            action_label=action_label,
            action_callback=action_callback,
            icon=icon,
            dismissible=True,
            show_spinner=False,
        )

        self._notify_subscribers(config)

    def show_success(self, message: str, duration: int = 2000):
        """Shortcut for success toast"""
        self.show_toast(message, FeedbackType.SUCCESS, duration)

    def show_error(
        self,
        message: str,
        duration: int = 4000,
        action_label: str | None = None,
        action_callback: str | None = None,
    ):
        """Shortcut for error toast"""
        self.show_toast(message, FeedbackType.ERROR, duration, action_label, action_callback)

    def show_warning(self, message: str, duration: int = UI_FEEDBACK_DURATION_MS):
        """Shortcut for warning toast"""
        self.show_toast(message, FeedbackType.WARNING, duration)

    def get_operation_state(self, operation_id: str) -> OperationState | None:
        """Get current state of an operation"""
        return self._operations.get(operation_id)

    def _get_stage_icon(self, stage: LoadingStage) -> str:
        """Get icon for loading stage"""
        icons = {
            LoadingStage.SEARCHING: "🔍",
            LoadingStage.FETCHING: "📥",
            LoadingStage.PROCESSING: "⚙️",
            LoadingStage.FINALIZING: "✨",
            LoadingStage.COMPLETE: "✓",
        }
        return icons.get(stage, "⏳")

    def _get_stage_message(self, stage: LoadingStage, base_message: str) -> str:
        """Get descriptive message for stage"""
        stage_verbs = {
            LoadingStage.SEARCHING: "Searching for",
            LoadingStage.FETCHING: "Loading",
            LoadingStage.PROCESSING: "Processing",
            LoadingStage.FINALIZING: "Finishing",
            LoadingStage.COMPLETE: "Completed",
        }
        verb = stage_verbs.get(stage, "Working on")
        return f"{verb} {base_message}..."

    def _get_type_icon(self, type: FeedbackType) -> str:
        """Get icon for feedback type"""
        icons = {
            FeedbackType.LOADING: "⏳",
            FeedbackType.SUCCESS: "✓",
            FeedbackType.ERROR: "✗",
            FeedbackType.WARNING: "⚠",
            FeedbackType.INFO: "ℹ",
            FeedbackType.PROGRESS: "⏳",
        }
        return icons.get(type, "•")

    def to_dict(self, config: FeedbackConfig) -> dict[str, Any]:
        """Convert feedback config to dict for JSON serialization"""
        return {
            "type": config.type.value,
            "message": config.message,
            "duration": config.duration,
            "action_label": config.action_label,
            "action_callback": config.action_callback,
            "icon": config.icon,
            "progress": config.progress,
            "stage": config.stage.value if config.stage else None,
            "dismissible": config.dismissible,
            "show_spinner": config.show_spinner,
        }


# Global singleton instance
_feedback_system: FeedbackSystem | None = None


def get_feedback_system() -> FeedbackSystem:
    """Get or create global feedback system instance"""
    global _feedback_system
    if _feedback_system is None:
        _feedback_system = FeedbackSystem()
    return _feedback_system


# Convenience functions for common operations
def show_music_search_loading(query: str) -> str:
    """Show loading state for music search"""
    system = get_feedback_system()
    return system.show_loading(
        f"music_search_{time.time()}",
        f'"{query}"',
        LoadingStage.SEARCHING,
        estimated_duration=3.0,  # Usually takes 2-4 seconds
    )


def show_ai_thinking() -> str:
    """Show loading state for AI processing"""
    system = get_feedback_system()
    return system.show_loading(
        f"ai_thinking_{time.time()}",
        "your request",
        LoadingStage.PROCESSING,
        estimated_duration=2.0,
    )


def show_transcription_progress() -> str:
    """Show loading state for voice transcription"""
    system = get_feedback_system()
    return system.show_loading(
        f"transcription_{time.time()}",
        "your voice",
        LoadingStage.PROCESSING,
        estimated_duration=1.5,
    )
