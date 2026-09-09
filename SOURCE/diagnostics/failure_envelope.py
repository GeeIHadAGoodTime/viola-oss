"""
diagnostics/failure_envelope.py
================================

Unified helper for logging structured failure envelopes. Each envelope captures
an error code, component, optional exception details, and contextual metadata.
The helper fan-outs the envelope to runtime metrics, diagnostics bus, and the
standard Loguru logger to avoid silent exception paths.

Error Coalescing:
    Repeated identical failures within a configurable time window (default 30s)
    are coalesced to prevent error spam. The first failure in a window is fully
    emitted (metrics, bus, logs, UI). Subsequent failures within the window:
    - Update metrics and internal counters
    - Suppress user-facing logs and UI messages
    - Update last-seen timestamp for monitoring
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from typing import Any

from core.asyncio_safe import is_event_loop_closed_error
from core.logging_config import get_logger
from diagnostics.bus import get_diagnostics_bus
from diagnostics.coalescing import get_coalescer as _get_coalescer
from diagnostics.runtime_metrics import get_runtime_metrics
from intent.log_redaction import redact_diagnostic_payload

logger = get_logger(__name__)


# Re-export get_coalescer with None-safe wrapper
def get_coalescer():
    """Get coalescer instance (None-safe wrapper)."""
    return _get_coalescer()


# Optional Qt debug event emitter - None in headless mode
# We store the function if available; typed as a union with None for the fallback case
_emit_debug_event_fn: Callable[..., None] | None = None

try:
    from ui.qt_native.debug_events import emit_debug_event as _imported_emit_debug_event

    _emit_debug_event_fn = _imported_emit_debug_event
except Exception:  # pragma: no cover - optional in headless mode
    pass


def _emit_bus_safely(name: str, **payload: Any) -> None:
    """Emit to diagnostics bus without letting closed-loop telemetry fail work."""
    try:
        get_diagnostics_bus().emit(name, **payload)
    except Exception as exc:
        if is_event_loop_closed_error(exc):
            return
        logger.debug("Diagnostics bus emission failed (non-critical): %s", exc)


def _emit_debug_event_safely(event: str, payload: dict[str, Any], *, source: str) -> None:
    if _emit_debug_event_fn is None:
        return
    try:
        _emit_debug_event_fn(event, payload, source=source)
    except Exception as exc:
        if is_event_loop_closed_error(exc):
            return
        logger.debug("Failure-envelope UI debug event failed (non-critical): %s", exc)


def _map_strings(value: Any, transform: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return transform(value)
    if isinstance(value, Mapping):
        return {key: _map_strings(inner, transform) for key, inner in value.items()}
    if isinstance(value, list):
        return [_map_strings(inner, transform) for inner in value]
    if isinstance(value, tuple):
        return tuple(_map_strings(inner, transform) for inner in value)
    if isinstance(value, set):
        return {_map_strings(inner, transform) for inner in value}
    return value


def _scrub_vault_secrets(value: Any) -> Any:
    try:
        from services.api_vault.vault import scrub_secrets
    except ImportError as exc:
        logger.debug("Secret scrubber unavailable, skipping vault secret scrub: %s", exc)
        return value

    def _safe_scrub(text: str) -> str:
        try:
            return scrub_secrets(text)
        except (RuntimeError, TypeError, ValueError):
            return "[REDACTED:SECRET_SCRUB_FAILED]"

    return _map_strings(value, _safe_scrub)


def _redact_failure_payload(value: Any) -> Any:
    scrubbed = _scrub_vault_secrets(value)
    try:
        return redact_diagnostic_payload(scrubbed)
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Failure-envelope diagnostic redaction failed (non-critical): %s", exc)
        return "[REDACTED:DIAGNOSTIC_REDACTION_FAILED]"


def emit_failure(
    code: str,
    component: str,
    *,
    message: str | None = None,
    exc: BaseException | None = None,
    severity: str = "ERROR",
    include_snapshot: bool = False,
    correlation_id: str | None = None,
    tier: str | None = None,
    retryable: bool | None = None,
    coalesce: bool = True,
    **context: Any,
) -> dict[str, Any]:
    """
    Emit a structured failure envelope with optional coalescing.

    Args:
        code: Stable failure code identifier.
        component: Component emitting the failure.
        message: Optional human-readable message.
        exc: Optional exception instance (stack trace will be captured).
        severity: Log severity (default ERROR).
        include_snapshot: When True, embed the latest runtime metrics snapshot.
        correlation_id: Optional correlation ID for tracking requests.
        tier: Optional system tier (e.g., "backend", "music", "ui").
        retryable: Whether the failure is retryable.
        coalesce: Whether to apply coalescing (default True). Set to False to force emission.
        context: Additional metadata (must be JSON serialisable or convertible to str).

    Returns:
        The failure envelope dict for further processing/testing.
        For coalesced failures, returns the first envelope with aggregated count.
    """

    metrics = get_runtime_metrics()
    context_payload: dict[str, Any] = dict(context)

    derived_correlation_id = correlation_id or context_payload.get("correlation_id")
    if derived_correlation_id is None:
        derived_correlation_id = context_payload.get("trace_id") or context_payload.get("request_id")

    derived_tier = tier or context_payload.get("tier")
    if derived_tier is None and component:
        derived_tier = component.split(".", 1)[0]

    derived_retryable = retryable
    if derived_retryable is None:
        derived_retryable = context_payload.get("retryable")
    if derived_retryable is None and exc is not None:
        derived_retryable = getattr(exc, "retryable", None)

    if derived_correlation_id is not None:
        context_payload.setdefault("correlation_id", derived_correlation_id)
    if derived_tier is not None:
        context_payload.setdefault("tier", derived_tier)
    if derived_retryable is not None:
        context_payload.setdefault("retryable", derived_retryable)

    redacted_context = _redact_failure_payload(context_payload)
    context_payload = redacted_context if isinstance(redacted_context, dict) else {}

    # Check coalescing before building full envelope
    should_coalesce_flag = False
    coalesced_entry = None
    coalescer = None
    if coalesce:
        coalescer = get_coalescer()
        if coalescer is not None:
            should_coalesce_flag, coalesced_entry = coalescer.should_coalesce(component, code, context_payload)

    _raw_message = str(_redact_failure_payload(message or (str(exc) if exc else "")))

    envelope: dict[str, Any] = {
        "code": code,
        "component": component,
        "message": _raw_message,
        "severity": severity,
        "tier": derived_tier,
        "correlation_id": derived_correlation_id,
        "retryable": derived_retryable,
        "context": context_payload,
    }

    if exc is not None:
        envelope["exception"] = {
            "type": type(exc).__name__,
            "message": str(_redact_failure_payload(str(exc))),
            "stacktrace": _redact_failure_payload(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }

    if include_snapshot:
        try:
            envelope["snapshot"] = metrics.snapshot()
        except Exception as e:
            logger.debug(
                "Failed to attach runtime snapshot to failure envelope (non-critical): %s",
                e,
                exc_info=True,
            )

    metrics_context = dict(envelope.get("context", {}))
    metrics_context.pop("correlation_id", None)
    metrics_context.pop("tier", None)
    metrics_context.pop("retryable", None)

    # Always update metrics (coalescing doesn't affect metrics)
    metrics.record_failure(
        code,
        component,
        message=envelope.get("message"),
        correlation_id=derived_correlation_id,
        tier=derived_tier,
        retryable=derived_retryable,
        **metrics_context,
    )

    # Record in error registry for pattern detection
    try:
        from diagnostics.error_classification import ErrorCategory, categorize_exception
        from diagnostics.error_registry import get_error_registry

        category = categorize_exception(exc) if exc else ErrorCategory.UNEXPECTED_BUG
        get_error_registry().record_error(
            component=component,
            code=code,
            category=category,
            message=envelope.get("message", ""),
            exception_type=type(exc).__name__ if exc else None,
            context=context_payload,
            correlation_id=derived_correlation_id,
        )
    except Exception as e:
        # Don't let registry errors break failure emission
        logger.debug("Error registry recording failed (non-critical): %s", e)

    # If coalesced, update aggregation and return early (skip user-facing emission)
    if should_coalesce_flag and coalesced_entry is not None and coalescer is not None:
        # Record the coalesced failure
        coalesced_entry = coalescer.record_coalesced(component, code, context_payload, envelope)

        # Update envelope with aggregation info
        envelope["coalesced"] = True
        envelope["coalesced_count"] = coalesced_entry.count
        envelope["coalesced_first_seen"] = coalesced_entry.first_seen
        envelope["coalesced_last_seen"] = coalesced_entry.last_seen

        # Still emit to diagnostics bus (for internal monitoring) but with coalesced flag
        diagnostics_payload = dict(envelope)
        diagnostics_payload.pop("snapshot", None)
        bus_context = dict(diagnostics_payload.get("context", {}))
        bus_context.pop("correlation_id", None)
        bus_context.pop("tier", None)
        bus_context.pop("retryable", None)
        _emit_bus_safely(
            f"failure.{component}",
            severity=severity,
            message=f"{envelope.get('message') or code} (coalesced x{coalesced_entry.count})",
            code=code,
            component=component,
            correlation_id=derived_correlation_id,
            tier=derived_tier,
            retryable=derived_retryable,
            coalesced=True,
            coalesced_count=coalesced_entry.count,
            **bus_context,
        )

        # Log at DEBUG level to avoid spam (metrics and bus already captured it)
        logger.debug(
            "Failure coalesced: %s[%s] x%s",
            component,
            code,
            coalesced_entry.count,
            failure_code=code,
            component=component,
            coalesced_count=coalesced_entry.count,
        )

        # Return the first envelope (with aggregation info)
        if coalesced_entry.first_envelope:
            result = dict(coalesced_entry.first_envelope)
            result.update(
                {
                    "coalesced": True,
                    "coalesced_count": coalesced_entry.count,
                    "coalesced_first_seen": coalesced_entry.first_seen,
                    "coalesced_last_seen": coalesced_entry.last_seen,
                }
            )
            return result

        return envelope

    # Not coalesced - emit fully (first failure in window or outside window)
    # Record this failure for future coalescing
    if coalesce and coalescer is not None:
        coalescer.record_coalesced(component, code, context_payload, envelope)

    # Emit to diagnostics bus
    diagnostics_payload = dict(envelope)
    diagnostics_payload.pop("snapshot", None)  # avoid flooding bus with massive payloads
    bus_context = dict(diagnostics_payload.get("context", {}))
    bus_context.pop("correlation_id", None)
    bus_context.pop("tier", None)
    bus_context.pop("retryable", None)
    _emit_bus_safely(
        f"failure.{component}",
        severity=severity,
        message=envelope.get("message") or code,
        code=code,
        component=component,
        correlation_id=derived_correlation_id,
        tier=derived_tier,
        retryable=derived_retryable,
        **bus_context,
    )

    # Emit user-facing log
    log = logger.bind(
        failure_code=code,
        component=component,
        correlation_id=derived_correlation_id,
        tier=derived_tier,
        retryable=derived_retryable,
        context=context_payload,
    )
    if exc is not None:
        # For .exception(), we need to handle the message parameter differently
        # since it doesn't support lazy formatting with positional args
        final_message = str(envelope.get("message") or "%s failure [%s]" % (component, code))
        try:
            log.exception(final_message)
        except Exception as log_exc:
            if not is_event_loop_closed_error(log_exc):
                raise
    else:
        try:
            if message:
                log.error(str(envelope.get("message") or ""))
            else:
                log.error("%s failure [%s]", component, code)
        except Exception as log_exc:
            if not is_event_loop_closed_error(log_exc):
                raise

    # Emit UI debug event (user-facing)
    if _emit_debug_event_fn is not None:
        event_payload = dict(envelope)
        event_payload["snapshot_included"] = include_snapshot
        _emit_debug_event_safely(
            "failure_envelope",
            event_payload,
            source=component,
        )

    return envelope


__all__ = ["emit_failure"]
