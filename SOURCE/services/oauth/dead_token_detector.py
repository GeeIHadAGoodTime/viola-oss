"""Dead external-service OAuth token detection.

Tracks consecutive refresh failures per (user_id, provider) pair.
After configurable thresholds, logs warnings/errors and records
audit events so that dead tokens are surfaced rather than silently
breaking integrations.

Design:
    - In-memory counters (dict) — lightweight, no DB schema changes.
    - Counters reset on successful refresh.
    - Thread-safe via a threading lock.
    - All methods are non-blocking: failures in audit/notification
      never propagate to callers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger

logger = get_logger("viola.services.oauth.dead_token")

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

WARN_THRESHOLD = 3  # Log warning + audit event
ERROR_THRESHOLD = 5  # Log error + mark needs_reauth


# ---------------------------------------------------------------------------
# Failure record
# ---------------------------------------------------------------------------


@dataclass
class _RefreshFailureRecord:
    """Tracks consecutive refresh failures for one (user_id, provider) pair."""

    count: int = 0
    first_failure_ts: float = 0.0
    last_failure_ts: float = 0.0
    last_error: str = ""
    notified_warn: bool = False
    notified_error: bool = False


# ---------------------------------------------------------------------------
# Detector singleton
# ---------------------------------------------------------------------------


class DeadTokenDetector:
    """Tracks OAuth token refresh failures and escalates appropriately."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key: (user_id, provider) -> _RefreshFailureRecord
        self._failures: dict[tuple[str, str], _RefreshFailureRecord] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_failure(
        self,
        user_id: str,
        provider: str,
        error: Exception | str,
    ) -> int:
        """Record a refresh failure. Returns the new consecutive count.

        Callers should call this from except blocks after a refresh attempt
        fails. This method never raises.
        """
        try:
            return self._record_failure_impl(user_id, provider, error)
        except Exception as exc:
            logger.debug("DeadTokenDetector.record_failure error: %s", exc)
            return 0

    def record_success(self, user_id: str, provider: str) -> None:
        """Reset the failure counter after a successful refresh.

        This method never raises.
        """
        try:
            self._record_success_impl(user_id, provider)
        except Exception as exc:
            logger.debug("DeadTokenDetector.record_success error: %s", exc)

    def get_failure_count(self, user_id: str, provider: str) -> int:
        """Return current consecutive failure count (0 if healthy)."""
        key = (user_id, provider)
        with self._lock:
            rec = self._failures.get(key)
            return rec.count if rec else 0

    def is_dead(self, user_id: str, provider: str) -> bool:
        """Return True if the token has hit the ERROR_THRESHOLD."""
        return self.get_failure_count(user_id, provider) >= ERROR_THRESHOLD

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _record_failure_impl(
        self,
        user_id: str,
        provider: str,
        error: Exception | str,
    ) -> int:
        now = time.time()
        error_str = str(error)
        key = (user_id, provider)

        with self._lock:
            rec = self._failures.get(key)
            if rec is None:
                rec = _RefreshFailureRecord()
                self._failures[key] = rec

            rec.count += 1
            rec.last_failure_ts = now
            rec.last_error = error_str
            if rec.count == 1:
                rec.first_failure_ts = now

            count = rec.count
            should_warn = count >= WARN_THRESHOLD and not rec.notified_warn
            should_error = count >= ERROR_THRESHOLD and not rec.notified_error

            if should_warn:
                rec.notified_warn = True
            if should_error:
                rec.notified_error = True

        # Outside the lock — these do I/O
        if should_warn and not should_error:
            self._on_warn_threshold(user_id, provider, count, error_str)
        if should_error:
            self._on_error_threshold(user_id, provider, count, error_str)

        return count

    def _record_success_impl(self, user_id: str, provider: str) -> None:
        key = (user_id, provider)
        with self._lock:
            prev = self._failures.pop(key, None)

        if prev and prev.count > 0:
            logger.info(
                "OAuth refresh succeeded for user=%s provider=%s after %d consecutive failure(s) — counter reset",
                user_id,
                provider,
                prev.count,
            )

    # ------------------------------------------------------------------
    # Threshold callbacks
    # ------------------------------------------------------------------

    def _on_warn_threshold(
        self,
        user_id: str,
        provider: str,
        count: int,
        last_error: str,
    ) -> None:
        """Fired once when WARN_THRESHOLD is first reached."""
        logger.warning(
            "OAuth refresh token may be dead: user=%s provider=%s consecutive_failures=%d last_error=%s",
            user_id,
            provider,
            count,
            last_error,
        )
        from auth.audit import AuthEventType

        self._log_audit_event(
            user_id=user_id,
            provider=provider,
            event_type=AuthEventType.OAUTH_TOKEN_DYING,
            count=count,
            last_error=last_error,
        )

    def _on_error_threshold(
        self,
        user_id: str,
        provider: str,
        count: int,
        last_error: str,
    ) -> None:
        """Fired once when ERROR_THRESHOLD is first reached."""
        logger.error(
            "OAuth token DEAD — needs reauth: user=%s provider=%s consecutive_failures=%d last_error=%s",
            user_id,
            provider,
            count,
            last_error,
        )
        from auth.audit import AuthEventType

        self._log_audit_event(
            user_id=user_id,
            provider=provider,
            event_type=AuthEventType.OAUTH_TOKEN_DEAD,
            count=count,
            last_error=last_error,
        )
        self._mark_needs_reauth(user_id, provider)
        self._emit_auth_refresh_required(user_id, provider)

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    def _log_audit_event(
        self,
        user_id: str,
        provider: str,
        event_type: str,
        count: int,
        last_error: str,
    ) -> None:
        """Record a dead-token event in the auth audit log."""
        try:
            from auth.audit import get_auth_audit_logger

            audit = get_auth_audit_logger()
            audit.log_event(
                event_type=event_type,
                user_id=user_id,
                outcome="failure",
                details={
                    "provider": provider,
                    "consecutive_failures": count,
                    "last_error": last_error[:500],
                },
            )
        except Exception as exc:
            logger.debug("Failed to log audit event %s: %s", event_type, exc)

    # ------------------------------------------------------------------
    # Mark needs_reauth
    # ------------------------------------------------------------------

    def _mark_needs_reauth(self, user_id: str, provider: str) -> None:
        """Mark the OAuth token record as needing re-authentication.

        Adds a 'needs_reauth' timestamp to the oauth_tokens row so that
        the UI can prompt the user to reconnect.
        """
        try:
            from auth.database import get_auth_db

            db = get_auth_db()
            if hasattr(db, "connection"):
                # SQLite
                conn = db.connection
                conn.execute(
                    "UPDATE oauth_tokens SET updated_at = ?, scope = "
                    "CASE WHEN scope IS NULL THEN 'needs_reauth' "
                    "ELSE scope || ' needs_reauth' END "
                    "WHERE user_id = ? AND provider = ? "
                    "AND (scope IS NULL OR scope NOT LIKE '%needs_reauth%')",
                    (
                        __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
                        user_id,
                        provider,
                    ),
                )
                conn.commit()
                logger.info(
                    "Marked OAuth token as needs_reauth: user=%s provider=%s",
                    user_id,
                    provider,
                )
        except Exception as exc:
            logger.debug(
                "Failed to mark needs_reauth for user=%s provider=%s: %s",
                user_id,
                provider,
                exc,
            )

    # ------------------------------------------------------------------
    # User notification via event bus
    # ------------------------------------------------------------------

    def _emit_auth_refresh_required(self, user_id: str, provider: str) -> None:
        """Emit an AuthRefreshRequired event to notify the UI layer.

        Uses the same event bus pattern as youtube_music_auth.py so the
        UI can prompt the user to re-link their account.
        """
        try:
            from core.events import LocalEventBus
            from core.events.types import AuthRefreshRequired

            bus = LocalEventBus()
            bus.publish(
                AuthRefreshRequired(
                    provider_id=provider,
                    user_id=user_id,
                    reason=(
                        "Your %s connection has expired. "
                        "Please reconnect in Settings > Accounts." % provider.replace("_", " ").title()
                    ),
                    source="dead_token_detector",
                )
            )
        except Exception as exc:
            logger.debug(
                "Failed to emit AuthRefreshRequired for user=%s provider=%s: %s",
                user_id,
                provider,
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_detector: DeadTokenDetector | None = None
_detector_lock = threading.Lock()


def get_dead_token_detector() -> DeadTokenDetector:
    """Get or create the global dead token detector."""
    global _detector
    if _detector is not None:
        return _detector
    with _detector_lock:
        if _detector is not None:
            return _detector
        _detector = DeadTokenDetector()
        return _detector


def reset_dead_token_detector() -> None:
    """Reset the global detector (for tests)."""
    global _detector
    with _detector_lock:
        _detector = None
