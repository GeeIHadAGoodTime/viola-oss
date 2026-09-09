"""
Audio Service Playback Operations Handler.

This module contains playback operation logic
extracted from the main AudioServiceAPI class to comply with code constraints.
"""

from __future__ import annotations

import asyncio
import builtins
from typing import Any

from core.exceptions import (
    AudioStateError as StateError,
    AudioTimeoutError as TimeoutError,
)
from core.logging_config import get_logger

from .state_machine import PlaybackState, StateTransitionError

logger = get_logger(__name__)


class AudioServicePlaybackOperations:
    """Handles playback operations for the audio service API."""

    def __init__(self, api_instance):
        """
        Initialize playback operations handler.

        Args:
            api_instance: The AudioServiceAPI instance
        """
        self.api = api_instance

    async def play(
        self,
        item: Any | None = None,
        timeout: float | None = None,
    ) -> None:
        """
        Start or resume playback.

        Args:
            item: Item to play (None = current/resume)
            timeout: Operation timeout

        Raises:
            StateError: If state transition fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.wait_for(
                self.api._state_machine.transition_to(PlaybackState.PLAYING, item=item),
                timeout=timeout,
            )

        except StateTransitionError as e:
            raise StateError(f"Failed to start playback: {e}", operation="play") from e
        except builtins.TimeoutError:
            raise TimeoutError("Play operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to start playback: %s", e)
            raise StateError(f"Internal error: {e!s}", operation="play") from e

    async def pause(self, timeout: float | None = None) -> None:
        """
        Pause playback.

        Args:
            timeout: Operation timeout

        Raises:
            StateError: If state transition fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.wait_for(
                self.api._state_machine.transition_to(PlaybackState.PAUSED),
                timeout=timeout,
            )

        except StateTransitionError as e:
            raise StateError(f"Failed to pause playback: {e}", operation="pause") from e
        except builtins.TimeoutError:
            raise TimeoutError("Pause operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to pause playback: %s", e)
            raise StateError(f"Internal error: {e!s}", operation="pause") from e

    async def resume(self, timeout: float | None = None) -> None:
        """
        Resume playback.

        Args:
            timeout: Operation timeout

        Raises:
            StateError: If state transition fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.wait_for(
                self.api._state_machine.transition_to(PlaybackState.PLAYING),
                timeout=timeout,
            )

        except StateTransitionError as e:
            raise StateError(f"Failed to resume playback: {e}", operation="resume") from e
        except builtins.TimeoutError:
            raise TimeoutError("Resume operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to resume playback: %s", e)
            raise StateError(f"Internal error: {e!s}", operation="resume") from e

    async def stop(self, timeout: float | None = None) -> None:
        """
        Stop playback.

        Args:
            timeout: Operation timeout

        Raises:
            StateError: If state transition fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.wait_for(
                self.api._state_machine.transition_to(PlaybackState.IDLE),
                timeout=timeout,
            )

        except StateTransitionError as e:
            raise StateError(f"Failed to stop playback: {e}", operation="stop") from e
        except builtins.TimeoutError:
            raise TimeoutError("Stop operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to stop playback: %s", e)
            raise StateError(f"Internal error: {e!s}", operation="stop") from e

    async def skip(self, timeout: float | None = None) -> None:
        """
        Skip to next item.

        Args:
            timeout: Operation timeout

        Raises:
            StateError: If state transition fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.wait_for(self.api._state_machine.skip_to_next(), timeout=timeout)

        except StateTransitionError as e:
            raise StateError(f"Failed to skip to next item: {e}", operation="skip") from e
        except builtins.TimeoutError:
            raise TimeoutError("Skip operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to skip to next item: %s", e)
            raise StateError(f"Internal error: {e!s}", operation="skip") from e

    async def seek(self, position_ms: int, timeout: float | None = None) -> None:
        """
        Seek to position in current track.

        Args:
            position_ms: Position in milliseconds
            timeout: Operation timeout

        Raises:
            StateError: If seek fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            await asyncio.wait_for(self.api._state_machine.seek_to(position_ms), timeout=timeout)

        except StateTransitionError as e:
            raise StateError(f"Failed to seek to position {position_ms}: {e}", operation="seek") from e
        except builtins.TimeoutError:
            raise TimeoutError("Seek operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to seek to position %s: %s", position_ms, e)
            raise StateError(f"Internal error: {e!s}", operation="seek") from e

    async def set_volume(self, volume: float, timeout: float | None = None) -> None:
        """
        Set playback volume.

        Args:
            volume: Volume level (0.0 to 1.0)
            timeout: Operation timeout

        Raises:
            StateError: If volume change fails
            TimeoutError: If operation times out
        """
        try:
            timeout = timeout or self.api._default_timeout

            # Validate volume range
            if not 0.0 <= volume <= 1.0:
                raise StateError(
                    f"Volume must be between 0.0 and 1.0, got {volume}",
                    operation="set_volume",
                )

            # Get previous volume
            previous_volume = await asyncio.wait_for(self.api.get_volume(), timeout=timeout)

            await asyncio.wait_for(self.api._state_machine.set_volume(volume), timeout=timeout)

            # Emit volume changed event
            if hasattr(self.api, "_event_bus"):
                from .events import VolumeChanged

                event = VolumeChanged(
                    volume=int(volume * 100),
                    previous_volume=previous_volume,
                    source="audio_service_api",
                )
                self.api._event_bus.emit(event)

        except StateTransitionError as e:
            raise StateError(f"Failed to set volume to {volume}: {e}", operation="set_volume") from e
        except builtins.TimeoutError:
            raise TimeoutError("Volume change operation timed out", timeout=timeout or 0.0) from None
        except Exception as e:
            logger.exception("Failed to set volume to %s: %s", volume, e)
            raise StateError(f"Internal error: {e!s}", operation="set_volume") from e

    async def get_status(self) -> dict[str, Any]:
        """
        Get current playback status.

        Returns:
            Status dictionary
        """
        try:
            state = self.api._state_machine.get_current_state()
            current_item = self.api._state_machine.get_current_item()
            position = await self.api._state_machine.get_position()
            duration = await self.api._state_machine.get_duration()

            return {
                "state": state.value if hasattr(state, "value") else str(state),
                "current_item": current_item,
                "position_ms": position,
                "duration_ms": duration,
                "queue_length": await self.api._queue_operations.get_queue_length(),
            }

        except Exception as e:
            logger.exception("Failed to get playback status: %s", e)
            raise StateError(f"Failed to get status: {e!s}", operation="get_status") from e
