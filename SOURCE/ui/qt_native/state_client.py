"""
State management client for player state polling and caching.
Handles state retrieval, caching, and state change notifications.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from core.constants import TIMEOUT_GRACE, TIMEOUT_SHUTDOWN
from core.logging_config import get_logger

from .api_client_mixin import APIClientMixin

if TYPE_CHECKING:
    from .api_base import PlayerState, StatePollResult

logger = get_logger(__name__)


class StateClient(APIClientMixin):
    """Client for player state management and polling."""

    def __init__(self, session, api_client):
        self.session = session
        self.base_url = None  # Set by parent client
        self.api_client = api_client  # Reference to parent for notifications

        self._player_state_schema = None
        self._last_state_warning_ms = 0
        self._last_state: PlayerState | None = None
        self._last_state_at: float = 0.0

    def get_state(self, *, notify: bool = True) -> PlayerState | None:
        """Get current player state."""
        result = self._perform_state_request(notify=notify)
        return result.state

    def poll_state_async(self, *, on_result=None, on_error=None) -> object | None:
        """Submit a non-blocking state poll and dispatch the result to the UI thread."""

        return self._submit_worker(
            self._perform_state_request,
            kwargs={"notify": False},
            on_result=on_result,
            on_error=on_error,
        )

    def _remember_state(self, state: PlayerState) -> None:
        """Cache the current player state."""
        copy_method = getattr(state, "model_copy", None)
        if callable(copy_method):
            cached = copy_method(deep=True)
        else:
            try:
                import copy

                cached = copy.deepcopy(state)
            except Exception as e:
                logger.exception("Failed to deepcopy state, using reference: %s", e)
                cached = state
        self._last_state = cached
        self._last_state_at = time.time()

    def _get_cached_state(self, max_age: float | None = 5.0) -> PlayerState | None:
        """Retrieve cached state if still valid."""
        if self._last_state is None or not self._last_state_at:
            return None
        if max_age is not None and max_age >= 0.0:
            if time.time() - self._last_state_at > max_age:
                return None
        state = self._last_state
        copy_method = getattr(state, "model_copy", None)
        if callable(copy_method):
            return copy_method(deep=True)
        else:
            try:
                import copy

                return copy.deepcopy(state)
            except Exception as e:
                logger.exception("Failed to deepcopy cached state, using reference: %s", e)
                return state

    def _record_expected_state(self, is_playing: bool, base_state: PlayerState | None = None) -> None:
        """Record expected state change for optimistic updates."""
        source_state = base_state
        if source_state is None:
            source_state = self._get_cached_state(max_age=None)
        if source_state is None:
            return
        copy_method = getattr(source_state, "model_copy", None)
        if callable(copy_method):
            updated = copy_method(update={"is_playing": is_playing}, deep=True)
        else:
            try:
                import copy

                updated = copy.deepcopy(source_state)
                updated.is_playing = is_playing
            except Exception as e:
                logger.exception("Failed to record expected state: %s", e)
                return
        self._remember_state(updated)

    def _perform_state_request(self, *, notify: bool) -> StatePollResult:
        """Perform the state polling request and capture warnings."""
        endpoint = "/v1/state"
        self._emit_api_event(
            "api_request_started",
            {"endpoint": endpoint, "method": "GET"},
        )
        try:
            response = self.session.get(f"{self.base_url}{endpoint}", timeout=TIMEOUT_SHUTDOWN)
            response.raise_for_status()
            payload = response.json()
            state, warnings, error = self._process_state_response(response, payload, endpoint, notify)
            if error:
                from .api_base import StatePollResult

                return StatePollResult(state=None, warnings=tuple(warnings), error=error)
        except Exception as e:
            logger.debug("Failed to get state: %s", e)
            warning = self._notify_state_warning("⚠️ Could not reach player state endpoint.", notify=notify)
            warnings = [warning] if warning else []
            self._emit_api_event(
                "api_request_failed",
                {
                    "endpoint": endpoint,
                    "method": "GET",
                    "error": type(e).__name__,
                },
            )
            from .api_base import StatePollResult

            return StatePollResult(state=None, warnings=tuple(warnings), error=e)
        else:
            if state is not None:
                self._remember_state(state)
            from .api_base import StatePollResult

            return StatePollResult(state=state, warnings=tuple(warnings))

    def _process_state_response(self, response, payload, endpoint: str, notify: bool) -> tuple:
        """Process state API response and return (state, warnings, error)."""
        envelope = self._normalise_envelope(payload)
        if envelope is None:
            logger.warning("Invalid envelope from /v1/state with type=%s", type(payload).__name__)
            warning = self._notify_state_warning(
                "⚠️ Player state unavailable (invalid response).",
                notify=notify,
            )
            warnings = [warning] if warning else []
            self._emit_api_event(
                "api_request_failed",
                {
                    "endpoint": endpoint,
                    "method": "GET",
                    "status": response.status_code,
                    "error": "invalid_envelope",
                },
            )
            return None, warnings, None

        if envelope.get("ok") is False:
            state, warnings = self._handle_state_error_response(response, envelope, endpoint, notify)
            return state, warnings, None
        else:
            return self._handle_state_success_response(response, envelope, payload, endpoint, notify)

    def _handle_state_error_response(self, response, envelope, endpoint: str, notify: bool) -> tuple:
        """Handle backend error response for state endpoint."""
        warnings = []
        error_info = envelope.get("error") or {}
        error_reason = error_info.get("code") if isinstance(error_info, dict) else str(error_info or "unknown_error")
        logger.warning("/v1/state reported error: %s", error_reason)
        data_section = envelope.get("data")
        data_mapping = data_section if isinstance(data_section, dict) else {}
        fallback_state = self._coerce_fallback_state(data_mapping)
        meta = {
            "endpoint": endpoint,
            "method": "GET",
            "status": response.status_code,
            "error": error_reason,
        }
        self._emit_api_event("api_request_failed", meta)
        if fallback_state is None:
            warning = self._notify_state_warning(
                "⚠️ Player state unavailable (backend reported error).",
                notify=notify,
            )
            if warning:
                warnings.append(warning)
            return None, warnings

        warning = self._notify_state_warning(
            "⚠️ Player state degraded. Showing fallback snapshot.",
            notify=notify,
        )
        if warning:
            warnings.append(warning)
        meta["fallback"] = True
        self._emit_api_event("api_request_finished", meta)
        return fallback_state, warnings

    def _handle_state_success_response(
        self, response, envelope, payload, endpoint: str, notify: bool
    ) -> tuple[PlayerState | None, list[str], Exception | None]:
        """Handle successful response for state endpoint."""
        warnings: list[str] = []
        try:
            self._emit_api_event(
                "api_request_finished",
                {
                    "endpoint": endpoint,
                    "method": "GET",
                    "status": response.status_code,
                },
            )
            data_section = envelope.get("data")
            if not isinstance(data_section, dict):
                raise ValueError("Player state payload missing data section")
            from .api_base import validate_player_state_payload

            state = validate_player_state_payload(data_section)
            return state, warnings, None
        except Exception as exc:
            logger.error("Player state schema validation failed: %s", exc)
            payload_fields = sorted(payload.keys()) if isinstance(payload, dict) else []
            logger.debug("Invalid state payload type=%s fields=%s", type(payload).__name__, payload_fields)
            warning = self._notify_state_warning(
                "⚠️ Player state invalid. Some controls may be paused.",
                notify=notify,
            )
            if warning:
                warnings.append(warning)
            self._emit_api_event(
                "api_request_failed",
                {
                    "endpoint": endpoint,
                    "method": "GET",
                    "status": response.status_code,
                    "error": "invalid_payload",
                },
            )
            return None, warnings, exc

    def _coerce_fallback_state(self, payload):
        """Convert fallback payload to PlayerState."""
        from .api_base import validate_player_state_payload

        fallback = payload.get("fallback")
        if not isinstance(fallback, dict):
            fallback = payload.get("data")
        if not isinstance(fallback, dict):
            return None

        normalized = {"ok": True, "error": None}
        normalized.update(dict(fallback))
        try:
            return validate_player_state_payload(normalized)
        except Exception as exc:
            logger.warning("Fallback player state payload invalid: %s", exc)
            logger.debug("Invalid fallback payload: %r", fallback)
            return None

    def _notify_state_warning(self, message: str, *, notify: bool = True) -> str | None:
        """Surface schema validation issues without crashing the UI."""
        try:
            from PySide6.QtCore import QDateTime

            now_ms = int(QDateTime.currentMSecsSinceEpoch())
        except Exception as e:
            logger.exception("Failed to get Qt timestamp, using time.time(): %s", e)
            now_ms = int(time.time() * 1000)
        # Throttle warnings using TIMEOUT_GRACE (3 seconds)
        if self._last_state_warning_ms and (now_ms - self._last_state_warning_ms) < int(TIMEOUT_GRACE * 1000):
            return None

        self._last_state_warning_ms = now_ms

        if not notify:
            return message

        try:
            import importlib

            module = importlib.import_module("ui.qt_native.widgets.notification_widget")
            manager = getattr(module, "NotificationManager", None)
            warning = getattr(manager, "warning", None) if manager is not None else None
            if callable(warning):
                warning(message, duration=4000)
        except Exception as exc:
            logger.debug("Could not show warning notification: %s", exc)
        return message


__all__ = ["StateClient"]
