"""
Shared mixin for API client classes.

Provides common methods for response envelope normalization,
debug event emission, error envelope building, and async worker submission.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, cast

from core.logging_config import get_logger

if TYPE_CHECKING:
    from contracts.api_response import ResponseEnvelope

logger = get_logger(__name__)


class APIClientMixin:
    """Mixin providing common API client methods."""

    base_url: str | None = None

    def _normalise_envelope(self, payload: object) -> ResponseEnvelope | None:
        """
        Validate and normalize an API response envelope.

        Args:
            payload: Raw response payload from API

        Returns:
            Normalized ResponseEnvelope or None if invalid
        """
        from contracts.api_response import (
            ResponseContractError,
            ResponseError,
            ensure_envelope,
        )
        from core.json_types import JsonValue, to_json_value

        try:
            envelope = ensure_envelope(payload)
        except ResponseContractError as exc:
            logger.error("Invalid API response envelope: %s", exc)
            return None

        error_obj = envelope.get("error")
        if error_obj is not None and isinstance(error_obj, Mapping):
            normalised_error: ResponseError = {
                "code": str(error_obj.get("code", "error")),
                "message": str(error_obj.get("message", "Request failed.")),
            }
            details = error_obj.get("details")
            if isinstance(details, Mapping):
                coerced_details: dict[str, JsonValue] = {str(k): to_json_value(v) for k, v in details.items()}
                normalised_error["details"] = coerced_details
            envelope["error"] = normalised_error
        return envelope

    def _build_error_envelope(self, code: str, message: str, data: dict[str, object] | None = None) -> ResponseEnvelope:
        """
        Build a standardized error response envelope.

        Args:
            code: Error code string
            message: Human-readable error message
            data: Optional additional data

        Returns:
            Error envelope dictionary
        """
        from contracts.api_response import failure_response
        from core.json_types import to_json_value

        return failure_response(code, message, data=to_json_value(data or {}))

    def _emit_api_event(self, signal_name: str, payload: dict[str, object]) -> None:
        """
        Emit a debug event to the Qt debug bus.

        Args:
            signal_name: Name of the signal to emit
            payload: Event payload data
        """
        from .api_base import emit_debug_event, get_debug_bus

        merged_payload: dict[str, object] = {"client": "qt", **payload}

        bus_factory = get_debug_bus if get_debug_bus is not None else None
        if bus_factory is not None:
            try:
                bus = bus_factory()
                signal = getattr(bus, signal_name, None)
                emit = getattr(signal, "emit", None)
                if callable(emit):
                    emit(merged_payload)
                    return
            except Exception as e:
                logger.exception("Failed to emit debug event via bus: %s", e)
                pass

        if callable(emit_debug_event):
            try:
                emit_debug_event(signal_name, merged_payload, source="qt")
            except Exception as e:
                logger.exception("Failed to emit debug event: %s", e)
                pass

    def _submit_worker(
        self,
        fn: Callable[..., object],
        args: tuple[object, ...] | None = None,
        kwargs: dict[str, object] | None = None,
        on_result: Callable[[object], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> object | None:
        """
        Submit a function to run on a Qt worker thread.

        Args:
            fn: Function to execute
            args: Positional arguments for fn
            kwargs: Keyword arguments for fn
            on_result: Callback for successful result
            on_error: Callback for errors

        Returns:
            Worker object if Qt available, None otherwise
        """
        from .api_base import QT_AVAILABLE, _APICallRunnableCls

        call_args = args or ()
        call_kwargs = kwargs or {}

        if not QT_AVAILABLE:
            try:
                result = fn(*call_args, **call_kwargs)
            except Exception as exc:
                if on_error:
                    on_error(exc)
                    return None
                raise
            else:
                if on_result:
                    on_result(result)
                return None

        worker = _APICallRunnableCls(fn, args=call_args, kwargs=call_kwargs)
        if on_result is not None:
            worker.signals.result.connect(on_result)
        if on_error is not None:
            worker.signals.error.connect(on_error)
        else:
            worker.signals.error.connect(lambda exc: logger.debug("Async API call raised: %s", exc))

        from PySide6.QtCore import QRunnable, QThreadPool

        QThreadPool.globalInstance().start(cast(QRunnable, worker))
        return worker


__all__ = ["APIClientMixin"]
