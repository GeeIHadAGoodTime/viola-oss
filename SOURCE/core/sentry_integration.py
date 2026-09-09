"""Sentry integration for production error monitoring.

This module provides centralized Sentry SDK initialization with:
- Environment-aware configuration
- Sensitive data scrubbing before sending events
- Filtering of expected errors (cancellation, rate limits, client disconnects)
- FastAPI integration for automatic request tracing

Usage:
    from core.sentry_integration import configure_sentry

    # Call early in application startup
    configure_sentry()
"""

from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from core.constants import VIOLA_VERSION
from core.exceptions import ConfigurationError
from core.logging_config import get_logger

if TYPE_CHECKING:
    from config.settings import AppConfig

logger = get_logger(__name__)
_QT_EXCEPTION_HOOK_INSTALLED = False
_PREVIOUS_THREADING_EXCEPTHOOK = threading.excepthook
_INITIALIZED = False
_SENTRY_RELEASE_SESSION_ACTIVE = False
_NONFATAL_RELEASE_HEALTH_ERRORS = (
    AttributeError,
    ImportError,
    OSError,
    RuntimeError,
    TypeError,
    ValueError,
)

# Fields to scrub from Sentry events (case-insensitive substring matching)
# The scrubbing logic checks if ANY of these patterns appear in the field name.
# Pattern matching is case-insensitive substring match (e.g., "password" matches
# "smtp_password", "icloud_password", "user_password", etc.)
SENTRY_SCRUB_FIELDS = frozenset(
    [
        # Generic secret patterns
        "password",
        "secret",  # Catches *_secret (jwt_secret, client_secret, webhook_secret)
        "api_key",  # Catches all *_api_key fields
        "private_key",  # Catches *_private_key_path (Apple OAuth)
        "credentials",  # Catches *_credentials_path (Google Calendar)
        # Raw user-content/body patterns
        "body",
        "request_body",
        "email_body",
        "message_body",
        "command_text",
        "prompt",
        "file_contents",
        "screenshot",
        "payment_details",
        "confirmation_token",
        "payment_confirmation_token",
        # Token patterns
        "token",  # Catches *_token (access_token, refresh_token, developer_token)
        "access_token",
        "refresh_token",
        "oauth_token",
        # Auth patterns
        "authorization",
        "bearer",
        "jwt",
        "session",
        "cookie",
        # Webhook patterns
        "webhook",  # Catches *_webhook_secret (btcpay, stripe)
        # Provider-specific patterns (explicit for clarity)
        "openai_api_key",
        "anthropic_api_key",
        "google_api_key",
        "stripe_key",  # Catches stripe_secret_key
        "publishable_key",  # Catches stripe_publishable_key (Stripe public key)
        "btcpay_api_key",
        "resend_api_key",
        "sentry_dsn",
        "youtube_api_key",
        "client_id",  # OAuth client IDs (not strictly secret but sensitive)
        "client_secret",
        "team_id",  # Apple team IDs
        "key_id",  # Apple key IDs
        # Payment-card patterns
        "card_number",
        "cardnumber",
        "card_cvc",
        "card_cvv",
        "card_security_code",
        "cvc",
        "cvv",
        "local_payment_cvc",
        "payment_cvc",
        "primary_account_number",
        "security_code",
    ]
)

# Exception types that should not be sent to Sentry
# These are expected errors, not bugs
FILTERED_EXCEPTIONS = (
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
)

_MAX_SENTRY_MESSAGE_LENGTH = 1200


@dataclass(frozen=True)
class SentryInitResult:
    """Outcome of one process entry-point Sentry init attempt."""

    initialized: bool
    entry_point: str
    release: str
    environment: str
    reason: str | None = None


def configure_sentry(
    entry_point: str = "unknown",
    *,
    require_config: bool = False,
) -> SentryInitResult:
    """Initialize Sentry SDK if DSN is configured.

    This function should be called early in application startup, before
    request handling begins. It's safe to call even if Sentry is not
    configured unless ``require_config`` is true.

    The SDK is configured with:
    - Environment detection from settings
    - Release tagging from core.constants.VIOLA_VERSION
    - FastAPI integration for automatic request tracing
    - asyncio integration for unhandled task failures when available
    - Logging integration (custom event levels only)
    - Sensitive data scrubbing via before_send hook
    - Error filtering for expected exceptions
    """
    global _INITIALIZED

    from config.settings import settings

    environment = _determine_environment(settings)

    if _INITIALIZED:
        try:
            import sentry_sdk

            _apply_runtime_tags(sentry_sdk, entry_point)
        except ImportError:
            return _skip_or_raise(
                "sentry-sdk not installed after prior initialization",
                entry_point=entry_point,
                environment=environment,
                require_config=require_config,
            )
        return SentryInitResult(
            initialized=True,
            entry_point=entry_point,
            release=VIOLA_VERSION,
            environment=environment,
        )

    if not bool(getattr(settings, "sentry_enabled", True)):
        return _skip_or_raise(
            "disabled by settings.sentry_enabled",
            entry_point=entry_point,
            environment=environment,
            require_config=require_config,
        )

    dsn = str(getattr(settings, "sentry_dsn", "") or "").strip()
    if not dsn:
        return _skip_or_raise(
            "settings.sentry_dsn is empty",
            entry_point=entry_point,
            environment=environment,
            require_config=require_config,
        )

    # Privacy consent gate: do not initialize Sentry unless user has opted in.
    # The _before_send hook also checks consent as defense-in-depth, but
    # skipping init entirely avoids collecting/processing any data at all.
    if _sentry_requires_consent(settings):
        from core.privacy_consent import is_error_reporting_consented

        if not is_error_reporting_consented():
            return _skip_or_raise(
                "error reporting consent not given",
                entry_point=entry_point,
                environment=environment,
                require_config=require_config,
            )

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError as exc:
        return _skip_or_raise(
            "sentry-sdk not installed",
            entry_point=entry_point,
            environment=environment,
            require_config=require_config,
            exc=exc,
        )

    integrations: list[Any] = [
        # Automatic FastAPI request tracing with endpoint-based transaction names
        FastApiIntegration(transaction_style="endpoint"),
        # Logging integration - only forward explicitly tagged events
        # Set level=None to disable breadcrumb collection
        # Set event_level=None to disable automatic error log forwarding
        LoggingIntegration(level=None, event_level=None),
    ]

    try:
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
    except ImportError as exc:
        if require_config:
            raise ConfigurationError("Sentry asyncio integration unavailable for %s" % entry_point) from exc
        logger.debug("Sentry asyncio integration unavailable for %s", entry_point)
    else:
        integrations.append(AsyncioIntegration())

    traces_sample_rate = _coerce_traces_sample_rate(settings, require_config=require_config)

    sentry_sdk.init(
        dsn=dsn,
        release=_determine_release(settings),
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        before_send=_before_send,
        integrations=integrations,
        # Don't send default PII (email, user IP, etc.)
        send_default_pii=False,
        # Never attach request bodies — they may contain user voice data,
        # conversation content, or other PII that should not leave the device.
        max_request_body_size="never",
    )
    _INITIALIZED = True
    _apply_runtime_tags(sentry_sdk, entry_point)

    logger.info(
        "Sentry initialized: environment=%s, traces_sample_rate=%s, release=%s, entry_point=%s",
        environment,
        traces_sample_rate,
        VIOLA_VERSION,
        entry_point,
    )
    return SentryInitResult(
        initialized=True,
        entry_point=entry_point,
        release=VIOLA_VERSION,
        environment=environment,
    )


def _determine_environment(settings: AppConfig) -> str:
    """Determine the Sentry environment from settings.

    Priority:
    1. Explicit sentry_environment setting
    2. Infer from settings.env ("dev", "test", "prod")
    3. Default to "unknown"

    Args:
        settings: Application configuration

    Returns:
        Environment string for Sentry
    """
    # Explicit override takes precedence
    sentry_environment = str(getattr(settings, "sentry_environment", "") or "").strip()
    if sentry_environment:
        return sentry_environment

    # Map settings.env to Sentry environment
    env_mapping = {
        "dev": "development",
        "test": "testing",
        "prod": "production",
    }

    env_name = str(getattr(settings, "env", "") or "").strip().lower()
    return env_mapping.get(env_name, env_name or "unknown")


def _sentry_requires_consent(settings: AppConfig) -> bool:
    """Return whether this process must gate Sentry behind explicit user consent.

    Desktop surface: consent is required — Sentry must not initialize unless
    the end-user has explicitly opted in via the privacy settings.

    Cloud surface: consent is operator/SaaS-implicit.  On the cloud surface,
    signing up IS the consent (the same doctrine as cloud-LLM consent — see
    docker-compose.cloud.yml and core/privacy_consent._cloud_llm_consent_service_value).
    The per-user opt-out lives at /v1/consent.  Error reporting of our own
    cloud backend is operator policy, not end-user telemetry.

    The ``sentry_require_consent`` AppConfig field (env: VIOLA_SENTRY_REQUIRE_CONSENT)
    is the explicit operator override and always takes precedence when set.
    On the cloud surface, docker-compose.cloud.yml sets it to ``false`` so that
    containers never block at boot waiting for a desktop privacy gate that can
    never be satisfied inside a server container.
    """
    # Explicit operator override via VIOLA_SENTRY_REQUIRE_CONSENT env var
    # (wired through _apply_sentry_environment_overrides in config/settings.py).
    # When absent, the field defaults to True (fail-closed for desktop).
    # On the cloud surface the compose file sets it to false explicitly.
    explicit_val = getattr(settings, "sentry_require_consent", None)
    if explicit_val is not None:
        # If the field is False (operator explicitly disabled consent gate), honour it.
        # If True, still check the surface — cloud surface is implicitly no-consent-required.
        if not bool(explicit_val):
            return False

    # Surface-aware fallback: cloud backend never requires desktop-style consent.
    surface = str(getattr(settings, "app_surface", None) or "desktop").strip().lower()
    if surface == "cloud":
        return False

    return True


def _coerce_traces_sample_rate(settings: AppConfig, *, require_config: bool) -> float:
    raw_rate = getattr(settings, "sentry_traces_sample_rate", 0.0)
    try:
        rate = float(raw_rate)
    except (TypeError, ValueError) as exc:
        if require_config:
            raise ConfigurationError("settings.sentry_traces_sample_rate must be a number") from exc
        logger.debug("Invalid Sentry traces sample rate %r; using 0.0", raw_rate)
        return 0.0
    if 0.0 <= rate <= 1.0:
        return rate
    if require_config:
        raise ConfigurationError("settings.sentry_traces_sample_rate must be between 0.0 and 1.0")
    logger.debug("Out-of-range Sentry traces sample rate %r; using 0.0", raw_rate)
    return 0.0


def _skip_or_raise(
    reason: str,
    *,
    entry_point: str,
    environment: str,
    require_config: bool,
    exc: BaseException | None = None,
) -> SentryInitResult:
    if require_config:
        raise ConfigurationError("Sentry required for %s but %s" % (entry_point, reason)) from exc
    logger.debug("Sentry initialization skipped for %s: %s", entry_point, reason)
    return SentryInitResult(
        initialized=False,
        entry_point=entry_point,
        release=VIOLA_VERSION,
        environment=environment,
        reason=reason,
    )


def _apply_runtime_tags(sentry_sdk: Any, entry_point: str) -> None:
    try:
        sentry_sdk.set_tag("entry_point", entry_point)
        sentry_sdk.set_tag("viola_version", VIOLA_VERSION)
    except Exception:
        logger.debug("Could not apply Sentry runtime tags", exc_info=True)


def sentry_initialized() -> bool:
    """Return whether this process has already initialized the Sentry SDK."""
    return _INITIALIZED


def _determine_release(settings: AppConfig) -> str:
    """Return the release tag shared by backend and desktop Sentry events."""
    configured = str(getattr(settings, "sentry_release", "") or "").strip()
    return configured or VIOLA_VERSION


def _truncate_sentry_text(value: Any, limit: int = _MAX_SENTRY_MESSAGE_LENGTH) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _safe_sentry_url(value: Any, limit: int = 500) -> str:
    text = _truncate_sentry_text(value, limit)
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except ValueError:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    safe = "%s://%s%s" % (parsed.scheme, parsed.netloc, parsed.path)
    return _truncate_sentry_text(safe, limit)


def _is_sentry_capture_allowed(settings: AppConfig) -> bool:
    if not str(getattr(settings, "sentry_dsn", "") or "").strip():
        return False
    try:
        from core.privacy_consent import is_error_reporting_consented

        return is_error_reporting_consented()
    except Exception:
        return False


def _capture_event(event: dict[str, Any]) -> str | None:
    try:
        import sentry_sdk
    except ImportError:
        logger.debug("sentry-sdk not installed, dropping explicit Sentry event")
        return None
    try:
        return sentry_sdk.capture_event(event)
    except Exception:
        logger.exception("Explicit Sentry event capture failed")
        return None


_FRONTEND_ERROR_DEDUPE_WINDOW_SECONDS = 30.0
_FRONTEND_ERROR_DEDUPE_MAX_KEYS = 128
_frontend_error_dedupe: dict[str, float] = {}


def _frontend_event_is_duplicate(event: dict[str, Any]) -> bool:
    import time

    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    first = values[0] if isinstance(values, list) and values else {}
    if not isinstance(first, dict):
        first = {}
    key = "|".join(
        [
            str(first.get("type") or ""),
            str(first.get("value") or ""),
            str(event.get("message") or ""),
        ]
    )
    now = time.monotonic()
    previous = _frontend_error_dedupe.get(key)
    _frontend_error_dedupe[key] = now
    if len(_frontend_error_dedupe) > _FRONTEND_ERROR_DEDUPE_MAX_KEYS:
        oldest_key = min(_frontend_error_dedupe, key=_frontend_error_dedupe.get)
        _frontend_error_dedupe.pop(oldest_key, None)
    return previous is not None and now - previous < _FRONTEND_ERROR_DEDUPE_WINDOW_SECONDS


def _ambient_sentry_user() -> dict[str, str] | None:
    try:
        from core.user_context import get_current_user_id, user_id_or_none

        user_id = user_id_or_none(get_current_user_id())
    except Exception:
        return None
    if not user_id:
        return None
    return {"id": user_id}


def capture_qt_frontend_error(payload: dict[str, Any]) -> str | None:
    """Capture a JavaScript error observed inside the Qt WebEngine surface."""
    from config.settings import settings

    if not bool(getattr(settings, "sentry_qt_bridge_enabled", False)):
        return None
    if not _is_sentry_capture_allowed(settings):
        return None

    kind = _truncate_sentry_text(payload.get("kind"), 80) or "javascript_error"
    message = _truncate_sentry_text(payload.get("message") or payload.get("error_message"))
    error_type = _truncate_sentry_text(payload.get("error_type"), 120) or "JavaScriptError"
    source = _safe_sentry_url(payload.get("source") or payload.get("url"), 500)

    exception_value: dict[str, Any] = {
        "type": error_type,
        "value": message or kind,
        "mechanism": {
            "type": "qt_webengine",
            "handled": False,
            "data": {"kind": kind},
        },
    }
    stack = _truncate_sentry_text(payload.get("stack"), 4000)
    if stack:
        exception_value["stacktrace"] = {"frames": [{"function": "<javascript>", "context_line": stack}]}

    event: dict[str, Any] = {
        "level": "error",
        "platform": "javascript",
        "logger": "viola.qt.webview",
        "release": _determine_release(settings),
        "message": message or kind,
        "exception": {"values": [exception_value]},
        "tags": {
            "surface": "qt_desktop",
            "ui_entrypoint": "qwebengineview",
            "js_error_kind": kind,
        },
        "contexts": {
            "qt_webview": {
                "source": source,
                "line": payload.get("line"),
                "column": payload.get("column"),
            }
        },
    }
    user = _ambient_sentry_user()
    if user is not None:
        event["user"] = user
    if _frontend_event_is_duplicate(event):
        return None
    return _capture_event(event)


def capture_diagnostic_minimum(payload: dict[str, Any]) -> str | None:
    """Forward an anonymized diagnostic MINIMUM to GlitchTip as a Sentry event.

    This is the cloud-side sink of the relay: the desktop builds the allowlist-only
    minimum (``diagnostics.diagnostic_minimum.build_diagnostic_minimum``), the
    authenticated ingest endpoint validates its shape, and this turns it into a
    GlitchTip issue. It is DELIBERATELY identity-free: no ``set_user`` /
    ``_ambient_sentry_user`` call, because the baseline's whole promise is that it
    carries no user identity. The per-device consent gate does NOT apply here --
    consent for the anonymized baseline is enforced upstream on the desktop (via
    ``diagnostics.diagnostic_consent``) before anything is ever transmitted; by the
    time a payload reaches this cloud forwarder it has already cleared consent.
    The only gate here is whether GlitchTip is reachable (a configured DSN).
    """
    from config.settings import settings

    if not str(getattr(settings, "sentry_dsn", "") or "").strip():
        return None
    if not isinstance(payload, dict):
        return None

    report_kind = _truncate_sentry_text(payload.get("report_kind"), 40) or "crash"
    error_type = _truncate_sentry_text(payload.get("error_type"), 120) or "DiagnosticMinimum"
    error_value = _truncate_sentry_text(payload.get("error_value"), 300) or report_kind

    frames: list[dict[str, Any]] = []
    raw_stack = payload.get("stack")
    if isinstance(raw_stack, list):
        for frame in raw_stack:
            if not isinstance(frame, dict):
                continue
            frames.append(
                {
                    "module": _truncate_sentry_text(frame.get("module"), 200),
                    "function": _truncate_sentry_text(frame.get("function"), 120),
                    "lineno": frame.get("lineno") if isinstance(frame.get("lineno"), int) else None,
                }
            )

    exception_value: dict[str, Any] = {
        "type": error_type,
        "value": error_value,
        "mechanism": {"type": "anonymized_diagnostic", "handled": report_kind != "crash"},
    }
    if frames:
        # Sentry renders innermost-last; our stack is outermost-first.
        exception_value["stacktrace"] = {"frames": list(reversed(frames))}

    app_state = payload.get("app_state") if isinstance(payload.get("app_state"), dict) else {}
    event: dict[str, Any] = {
        "level": "error" if report_kind == "crash" else "info",
        "platform": "python",
        "logger": "viola.diagnostics.minimum",
        "release": _truncate_sentry_text(payload.get("app_version"), 60) or _determine_release(settings),
        "message": "%s: %s" % (error_type, error_value),
        "exception": {"values": [exception_value]},
        "tags": {
            "diagnostic_source": "anonymized_minimum",
            "report_kind": report_kind,
            "surface": _truncate_sentry_text(payload.get("surface"), 40) or "unknown",
            "os_family": _truncate_sentry_text(payload.get("os_family"), 40) or "unknown",
        },
        "contexts": {
            "os": {
                "name": _truncate_sentry_text(payload.get("os_family"), 40),
                "version": _truncate_sentry_text(payload.get("os_version"), 60),
            },
            "runtime": {"name": "python", "version": _truncate_sentry_text(payload.get("python_version"), 40)},
            "viola_app_state": dict(app_state),
        },
    }
    # Identity rides ONLY when the caller attached the opt-in identifiable extra
    # (built solely by diagnostics.diagnostic_dispatch.build_identifiable_extra,
    # which returns None without the separate consent). When absent -- the default
    # anonymized baseline -- no user block and no identity context is emitted.
    extra = payload.get("identifiable_extra")
    if isinstance(extra, dict) and extra:
        event["tags"]["has_identifiable_extra"] = "true"
        event["contexts"]["viola_identifiable_extra"] = dict(extra)
        contact = extra.get("install_id") or extra.get("contact")
        if contact:
            event["user"] = {"id": _truncate_sentry_text(contact, 200)}
    return _capture_event(event)


def capture_user_bug_report(
    *,
    user_id: str,
    feedback_id: str,
    message: str,
    context: dict[str, Any] | None,
    route: str,
    bug_ticket_id: int | None = None,
) -> str | None:
    """Mirror an authenticated user bug report into Sentry as a best-effort event."""
    from config.settings import settings

    if not bool(getattr(settings, "sentry_user_bug_report_enabled", False)):
        return None
    if not _is_sentry_capture_allowed(settings):
        return None

    context_data = context if isinstance(context, dict) else {}
    # The user-authored body is mirrored VERBATIM: a bug report is a deliberate
    # message the user typed to our own (founder-only GlitchTip) support channel, so
    # redacting it destroys exactly what they chose to send. Only bound its length.
    # The auto-captured context payload stays redacted.
    verbatim_message = _truncate_sentry_text(message) or ""
    try:
        from diagnostics.support_redaction import redact_support_payload

        redacted_context = redact_support_payload(context_data)
    except Exception:
        redacted_context = context_data
    if not isinstance(redacted_context, dict):
        redacted_context = {}
    surface = _truncate_sentry_text(redacted_context.get("surface"), 80) or "unknown"
    entrypoint = _truncate_sentry_text(redacted_context.get("ui_entrypoint"), 80) or "unknown"
    severity = _truncate_sentry_text(redacted_context.get("severity"), 20).lower() or "medium"
    if severity not in {"low", "medium", "high", "critical"}:
        severity = "medium"

    event = {
        "level": "info",
        "platform": "python",
        "logger": "viola.user_bug_report",
        "release": _determine_release(settings),
        "message": "User submitted bug report",
        "exception": {
            "values": [
                {
                    "type": "UserBugReport",
                    "value": verbatim_message,
                    "mechanism": {"type": "user_bug_report", "handled": True},
                }
            ]
        },
        "user": {"id": user_id},
        "tags": {
            "surface": surface,
            "ui_entrypoint": entrypoint,
            "route": _truncate_sentry_text(route, 120),
            "severity": severity,
        },
        "contexts": {
            "bug_report": {
                "feedback_id": feedback_id,
                "bug_ticket_id": bug_ticket_id,
                "source": _truncate_sentry_text(redacted_context.get("source"), 120),
                "current_trace_id": _truncate_sentry_text(redacted_context.get("current_trace_id"), 120),
                "screen_capture_metadata": redacted_context.get("screen_capture_metadata"),
            }
        },
    }
    return _capture_event(event)


def capture_web_bug_report(
    *,
    message: str,
    page: str | None = None,
    client_ip: str | None = None,
    test: bool = False,
    context: dict[str, Any] | None = None,
) -> str | None:
    """Capture an anonymous website bug report into Sentry.

    Returns the Sentry event ID on success, or None if Sentry is not initialized
    (caller is responsible for deciding how to handle the None case).  Never
    raises — any internal Sentry failure is logged and swallowed here so the
    HTTP layer can decide the response policy.

    ``test=True`` marks the report machine-readably with the ``viola_test`` tag
    (the same marker convention as ``viola_test_account`` used for test accounts).
    A founder/dev test submission carries this tag so downstream operator tooling
    (the ops-ticket bridge) can exclude it on the marker alone — never on a
    hand-maintained denylist of messages.

    ``context`` carries the user-consented actionable fields (app version, OS,
    origin surface, an optional contact address, and prompted repro steps) that
    the reporter saw in the submit form. These are what make a report
    followupable, so they are surfaced as GlitchTip tags (app_version / os /
    origin_surface — searchable) and a ``report_context`` context block
    (contact + repro — readable but not broadly indexed). A desktop report that
    reaches here via the notify upload carries its OWN app version this way, so
    the tag no longer misreports the forwarding cloud's release. ``context`` is
    optional; a bare/legacy submission (None) keeps the original event shape.
    """
    from config.settings import settings

    try:
        import sentry_sdk
    except ImportError:
        logger.debug("sentry-sdk not installed, dropping web bug report")
        return None

    # _INITIALIZED is the canonical "Sentry was configured" flag.  Don't
    # attempt to capture if init was never called successfully — the SDK
    # would just no-op with noise, and on desktop (no DSN) it would spam
    # debug logs.
    if not _INITIALIZED:
        return None

    safe_message = _truncate_sentry_text(message)
    safe_page = _truncate_sentry_text(page or "", 200) or None

    ctx = context if isinstance(context, dict) else {}
    app_version = _truncate_sentry_text(ctx.get("app_version"), 40)
    os_name = _truncate_sentry_text(ctx.get("os"), 80)
    origin_surface = _truncate_sentry_text(ctx.get("surface"), 40)
    contact = _truncate_sentry_text(ctx.get("contact"), 200)
    # Prompted repro fields the user typed into labelled boxes; verbatim like
    # the body (they are deliberate user messages), only length-bounded.
    report_context: dict[str, Any] = {
        "contact": contact or None,
        "steps": _truncate_sentry_text(ctx.get("steps"), 1000) or None,
        "expected": _truncate_sentry_text(ctx.get("expected"), 500) or None,
        "actual": _truncate_sentry_text(ctx.get("actual"), 500) or None,
    }
    report_context = {key: value for key, value in report_context.items() if value is not None}

    tags: dict[str, str] = {
        "surface": "website",
        "source": "web_bug_report",
        # Machine-readable test marker (matches the viola_test_account
        # convention). Only set on founder/dev test submissions so the
        # operator queue can exclude them on the tag alone.
        **({"viola_test": "true"} if test else {}),
    }
    # Actionable searchable dimensions -- only when the reporter supplied them.
    if app_version:
        tags["app_version"] = app_version
    if os_name:
        tags["os"] = os_name
    if origin_surface:
        tags["origin_surface"] = origin_surface

    contexts: dict[str, Any] = {"web_bug_report": {"page": safe_page}}
    if report_context:
        contexts["report_context"] = report_context

    event: dict[str, Any] = {
        "level": "info",
        "platform": "python",
        "logger": "viola.web_bug_report",
        "release": _determine_release(settings),
        "message": safe_message,
        "tags": tags,
        "contexts": contexts,
        # Fingerprint by source so website reports group into one issue
        # family in Sentry, separate from backend exceptions.
        "fingerprint": ["web_bug_report", safe_page or "unknown_page"],
    }
    try:
        event_id = sentry_sdk.capture_event(event)
    except Exception:
        logger.exception("Web bug report Sentry capture failed")
        return None
    return event_id


def capture_qt_python_exception(exc: BaseException) -> str | None:
    """Capture a native desktop Python exception with Qt surface tags."""
    _record_release_health_unhandled_exception(exc)

    # Anonymized diagnostic minimum on the opt-out baseline. This is INDEPENDENT
    # of the legacy Sentry consent gate below: it has its own consent + disclosure
    # model (diagnostics.diagnostic_consent) and, while the master flag is off (the
    # committed default), it merely queues locally and sends nothing. Best-effort;
    # a diagnostics failure must never turn a crash handler into a second crash.
    try:
        from diagnostics.diagnostic_dispatch import dispatch_crash_diagnostic

        dispatch_crash_diagnostic(exc=exc, surface="qt_desktop")
    except Exception:  # noqa: BLE001, RUF100 - crash handler must never raise
        logger.debug("Anonymized crash diagnostic dispatch failed", exc_info=True)

    from config.settings import settings

    if not _is_sentry_capture_allowed(settings):
        return None
    try:
        import sentry_sdk
    except ImportError:
        logger.debug("sentry-sdk not installed, dropping Qt Python exception")
        return None
    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("surface", "qt_desktop")
            scope.set_tag("ui_entrypoint", "viola_qt")
            scope.set_context("qt", {"exception_bridge": "QApplication.notify"})
            user = _ambient_sentry_user()
            if user is not None:
                scope.set_user(user)
            return sentry_sdk.capture_exception(exc)
    except Exception:
        logger.exception("Qt Python exception Sentry capture failed")
        return None


def _record_release_health_unhandled_exception(exc: BaseException) -> None:
    try:
        from telemetry.release_health_session import record_unhandled_exception

        record_unhandled_exception(exc)
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        logger.debug("Release-health unhandled exception counter unavailable")


def start_sentry_release_session() -> None:
    """Start a Sentry release-health session if the SDK is initialized."""
    global _SENTRY_RELEASE_SESSION_ACTIVE
    if not _INITIALIZED or _SENTRY_RELEASE_SESSION_ACTIVE:
        return
    try:
        import sentry_sdk
    except ImportError:
        return
    start_session = getattr(sentry_sdk, "start_session", None)
    if not callable(start_session):
        logger.debug("sentry_sdk.start_session unavailable")
        return
    try:
        start_session()
        _SENTRY_RELEASE_SESSION_ACTIVE = True
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        logger.debug("Sentry release session start failed")


def end_sentry_release_session(*, status: str = "exited") -> None:
    """End the active Sentry release-health session."""
    global _SENTRY_RELEASE_SESSION_ACTIVE
    if not _SENTRY_RELEASE_SESSION_ACTIVE:
        return
    try:
        import sentry_sdk
    except ImportError:
        _SENTRY_RELEASE_SESSION_ACTIVE = False
        return
    end_session = getattr(sentry_sdk, "end_session", None)
    if not callable(end_session):
        _SENTRY_RELEASE_SESSION_ACTIVE = False
        return
    try:
        end_session(status=status)
    except TypeError:
        try:
            end_session()
        except _NONFATAL_RELEASE_HEALTH_ERRORS:
            logger.debug("Sentry release session end failed")
    except _NONFATAL_RELEASE_HEALTH_ERRORS:
        logger.debug("Sentry release session end failed")
    finally:
        _SENTRY_RELEASE_SESSION_ACTIVE = False


def _flush_diagnostic_spool_at_startup() -> None:
    """Best-effort: relay any diagnostics spooled before the baseline was armed.

    ``diagnostics.diagnostic_dispatch.flush_spool``'s own docstring says it runs
    "at startup", but nothing actually called it there (#2600 finding) -- a crash
    that queued locally (baseline not armed yet) sat in the spool until the NEXT
    crash happened to arrive after arming, which could be days or never on a
    quiet install. Calling it once, here, from each hook-install entrypoint
    (Qt + non-GUI) closes that gap: every process boot gets a chance to flush
    whatever a previous session queued, the moment the current session's
    consent/master-flag state allows it. Never raises -- a diagnostics failure
    must never affect process startup.
    """
    try:
        from diagnostics.diagnostic_dispatch import flush_spool

        flush_spool()
    except Exception:  # noqa: BLE001, RUF100 - startup flush must never block boot
        logger.debug("Startup diagnostic spool flush failed", exc_info=True)


def install_qt_exception_hooks() -> None:
    """Install desktop process exception hooks for Qt and worker-thread errors."""
    global _PREVIOUS_THREADING_EXCEPTHOOK, _QT_EXCEPTION_HOOK_INSTALLED
    if _QT_EXCEPTION_HOOK_INSTALLED:
        return
    _QT_EXCEPTION_HOOK_INSTALLED = True
    _PREVIOUS_THREADING_EXCEPTHOOK = threading.excepthook
    _flush_diagnostic_spool_at_startup()

    previous_sys_hook = sys.excepthook

    def _sys_hook(exc_type: type[BaseException], exc_value: BaseException, traceback: Any) -> None:
        if not issubclass(exc_type, FILTERED_EXCEPTIONS):
            capture_qt_python_exception(exc_value)
        previous_sys_hook(exc_type, exc_value, traceback)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        exc_type = args.exc_type
        exc_value = args.exc_value
        if exc_type is not None and exc_value is not None and not issubclass(exc_type, FILTERED_EXCEPTIONS):
            capture_qt_python_exception(exc_value)
        _PREVIOUS_THREADING_EXCEPTHOOK(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def capture_process_exception(exc: BaseException, *, surface: str, entry_point: str | None = None) -> str | None:
    """Capture a fatal, unhandled exception in a non-GUI desktop process.

    Generalizes ``capture_qt_python_exception`` for the Qt-less desktop
    surfaces -- the local backend/daemon (``services.daemon.viola_daemon``)
    and the multiroom spoke relay (``viola_spoke``) (#1419). Same diagnostic-
    relay hand-off (``dispatch_crash_diagnostic``) and release-health counter
    as the Qt sink, but tagged with the CALLER's own surface/entry_point
    instead of a hardcoded ``qt_desktop`` tag, so the resulting GlitchTip issue
    carries a process-correct surface instead of every non-GUI crash being
    mislabeled (or simply invisible, which was the prior state).
    """
    _record_release_health_unhandled_exception(exc)

    # Anonymized diagnostic minimum on the opt-out baseline. Independent of the
    # legacy Sentry consent gate below (own consent + disclosure model); best
    # effort -- a diagnostics failure must never turn a crash handler into a
    # second crash. This call MUST stay unconditional (before the
    # _is_sentry_capture_allowed early-return) -- see
    # qt-crash-diagnostic-dispatch-wired for why the Qt sink enforces the same
    # ordering; the nongui sink mirrors it.
    try:
        from diagnostics.diagnostic_dispatch import dispatch_crash_diagnostic

        dispatch_crash_diagnostic(exc=exc, surface=surface)
    except Exception:  # noqa: BLE001, RUF100 - crash handler must never raise
        logger.debug("Anonymized crash diagnostic dispatch failed (surface=%s)", surface, exc_info=True)

    from config.settings import settings

    if not _is_sentry_capture_allowed(settings):
        return None
    try:
        import sentry_sdk
    except ImportError:
        logger.debug("sentry-sdk not installed, dropping %s process exception", surface)
        return None
    try:
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("surface", surface)
            scope.set_tag("process_entry_point", entry_point or surface)
            scope.set_context(
                "process",
                {"exception_bridge": "excepthook", "entry_point": entry_point or surface},
            )
            user = _ambient_sentry_user()
            if user is not None:
                scope.set_user(user)
            return sentry_sdk.capture_exception(exc)
    except Exception:
        logger.exception("%s process exception Sentry capture failed", surface)
        return None


_PROCESS_EXCEPTION_HOOK_INSTALLED = False
_PREVIOUS_PROCESS_THREADING_EXCEPTHOOK = threading.excepthook


def install_process_exception_hooks(surface: str, *, entry_point: str | None = None) -> None:
    """Install ``sys.excepthook`` + ``threading.excepthook`` for a non-GUI desktop process.

    Generalizes ``install_qt_exception_hooks`` for background/daemon/spoke
    desktop processes so an unhandled exception that reaches the top of the
    main thread, or any worker thread, is routed through
    ``capture_process_exception`` -- tagged with the caller's own surface --
    instead of vanishing with no diagnostic (#1419). Uses its own module-level
    install guard (separate from the Qt hook's) so the two are independent:
    a real desktop install only ever runs ONE of these entry points per OS
    process, but keeping the guards separate avoids any cross-surface
    interference in a test harness that imports both.

    Idempotent: a second call is a no-op (matches ``install_qt_exception_hooks``).
    """
    global _PREVIOUS_PROCESS_THREADING_EXCEPTHOOK, _PROCESS_EXCEPTION_HOOK_INSTALLED
    if _PROCESS_EXCEPTION_HOOK_INSTALLED:
        return
    _PROCESS_EXCEPTION_HOOK_INSTALLED = True
    _PREVIOUS_PROCESS_THREADING_EXCEPTHOOK = threading.excepthook
    _flush_diagnostic_spool_at_startup()

    previous_sys_hook = sys.excepthook

    def _sys_hook(exc_type: type[BaseException], exc_value: BaseException, traceback: Any) -> None:
        if not issubclass(exc_type, FILTERED_EXCEPTIONS):
            capture_process_exception(exc_value, surface=surface, entry_point=entry_point)
        previous_sys_hook(exc_type, exc_value, traceback)

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        exc_type = args.exc_type
        exc_value = args.exc_value
        if exc_type is not None and exc_value is not None and not issubclass(exc_type, FILTERED_EXCEPTIONS):
            capture_process_exception(exc_value, surface=surface, entry_point=entry_point)
        _PREVIOUS_PROCESS_THREADING_EXCEPTHOOK(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook


def install_asyncio_exception_handler(
    surface: str,
    *,
    entry_point: str | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Route unhandled asyncio Task/callback exceptions to the same crash sink.

    ``sys.excepthook``/``threading.excepthook`` never fire for an exception
    that only ever lives inside an asyncio ``Task`` nobody awaited -- asyncio's
    default behavior is to log "Task exception was never retrieved" and move
    on, never to reach the interpreter's top-level exception hooks. That is
    exactly the shape of the daemon's and the spoke's own asyncio main loops
    (#1419), so this closes the gap those two hooks cannot: it installs a
    ``loop.set_exception_handler`` that funnels the same class of "this
    process is now silently broken" failure through
    ``capture_process_exception``, chaining to any previously-installed
    handler (or asyncio's own default) afterward.

    Must be called from inside the running loop it should guard (or pass one
    explicitly); a call with no running loop and no ``loop`` argument is a
    no-op (logged at debug), never a raise.
    """
    try:
        target_loop = loop or asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("install_asyncio_exception_handler: no running loop for surface=%s", surface)
        return

    previous_handler = target_loop.get_exception_handler()

    def _handler(handler_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, BaseException) and not isinstance(exc, FILTERED_EXCEPTIONS):
            capture_process_exception(exc, surface=surface, entry_point=entry_point)
        if previous_handler is not None:
            previous_handler(handler_loop, context)
        else:
            handler_loop.default_exception_handler(context)

    target_loop.set_exception_handler(_handler)


def _before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Process events before sending to Sentry.

    This hook:
    1. Checks user consent for error reporting (drops event if not consented)
    2. Filters out expected exceptions (cancellation, keyboard interrupt, etc.)
    3. Filters out rate limit and connection errors (client issues, not bugs)
    4. Scrubs sensitive data from event payloads

    Args:
        event: The Sentry event dictionary
        hint: Additional context including the original exception

    Returns:
        Processed event dict, or None to drop the event
    """
    # Privacy consent gate: drop all events if this deployment requires per-device consent.
    try:
        from config.settings import settings

        consent_required = _sentry_requires_consent(settings)
    except Exception:
        consent_required = True

    if consent_required:
        try:
            from core.privacy_consent import is_error_reporting_consented

            if not is_error_reporting_consented():
                return None
        except Exception:
            # If we can't check consent, fail closed (don't send)
            return None

    # Check if this is an expected/filtered exception
    exc_info = hint.get("exc_info")
    if exc_info is not None:
        exc_type = exc_info[0]
        exc_value = exc_info[1]

        # Filter expected exceptions
        if exc_type and issubclass(exc_type, FILTERED_EXCEPTIONS):
            return None

        # Filter rate limit errors (user/client issue, not a bug)
        if _is_rate_limit_error(exc_type, exc_value):
            return None

        # Filter connection errors (client disconnected, not a bug)
        if _is_connection_error(exc_type, exc_value):
            return None

    # Defense-in-depth: strip request body data even if the SDK body-size
    # setting is somehow overridden. Voice/conversation data must never
    # leave the device via error reports.
    _strip_request_bodies(event)

    try:
        from core.sentry_pii_filter import before_send as _privacy_before_send
    except (ImportError, AttributeError):
        # If the privacy filter cannot load, fail closed.
        return None

    try:
        return _privacy_before_send(event, hint)
    except (
        AttributeError,
        IndexError,
        KeyError,
        RecursionError,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        # If the privacy filter cannot run, fail closed.
        return None


def _is_rate_limit_error(exc_type: type | None, exc_value: BaseException | None) -> bool:
    """Check if exception is a rate limit error."""
    if exc_type is None:
        return False

    # Check exception class name
    type_name = exc_type.__name__
    if "RateLimit" in type_name or "QuotaExceeded" in type_name:
        return True

    # Check exception message
    if exc_value:
        msg = str(exc_value).lower()
        if "rate limit" in msg or "quota exceeded" in msg or "too many requests" in msg:
            return True

    return False


def _is_connection_error(exc_type: type | None, exc_value: BaseException | None) -> bool:
    """Check if exception is a client connection error."""
    if exc_type is None:
        return False

    # Check exception class name
    type_name = exc_type.__name__
    connection_errors = (
        "ConnectionResetError",
        "BrokenPipeError",
        "ConnectionAbortedError",
        "ClientDisconnect",
        "ClientDisconnected",
        "Disconnected",
    )
    if type_name in connection_errors:
        return True

    # Check for starlette/fastapi disconnection
    if exc_value:
        msg = str(exc_value).lower()
        if "disconnect" in msg or "connection reset" in msg or "broken pipe" in msg:
            return True

    return False


def _strip_request_bodies(event: dict[str, Any]) -> None:
    """Remove request body data from Sentry events.

    Defense-in-depth: ensures no user content (voice transcriptions,
    conversation text, command data) is transmitted in error reports,
    regardless of the SDK request-body capture setting.
    """
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("body", None)


def _scrub_event(event: dict[str, Any]) -> None:
    """Scrub sensitive data from a Sentry event in-place.

    Recursively traverses the event dictionary and replaces
    values for sensitive keys with "[Filtered]".

    Args:
        event: The Sentry event dictionary (modified in-place)
    """
    _scrub_dict(event)
    try:
        from intent.log_redaction import redact_diagnostic_payload

        redacted = redact_diagnostic_payload(event)
    except Exception:
        return
    if isinstance(redacted, dict):
        event.clear()
        event.update(redacted)


def _scrub_dict(data: dict[str, Any]) -> None:
    """Recursively scrub sensitive fields from a dictionary."""
    for key, value in list(data.items()):
        key_lower = key.lower()

        # Check if this key should be scrubbed
        if any(scrub_field in key_lower for scrub_field in SENTRY_SCRUB_FIELDS):
            data[key] = "[Filtered]"
        elif isinstance(value, dict):
            _scrub_dict(value)
        elif isinstance(value, list):
            _scrub_list(value)


def _scrub_list(data: list[Any]) -> None:
    """Recursively scrub sensitive fields from a list."""
    for i, item in enumerate(data):
        if isinstance(item, dict):
            _scrub_dict(item)
        elif isinstance(item, list):
            _scrub_list(item)


__all__ = [
    "FILTERED_EXCEPTIONS",
    "SENTRY_SCRUB_FIELDS",
    "SentryInitResult",
    "capture_diagnostic_minimum",
    "capture_process_exception",
    "capture_qt_frontend_error",
    "capture_qt_python_exception",
    "capture_user_bug_report",
    "capture_web_bug_report",
    "configure_sentry",
    "end_sentry_release_session",
    "install_asyncio_exception_handler",
    "install_process_exception_hooks",
    "install_qt_exception_hooks",
    "sentry_initialized",
    "start_sentry_release_session",
]
