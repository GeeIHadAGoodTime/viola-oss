from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from core.logging_config import get_logger

logger = get_logger(__name__)


class _AuditLogger(Protocol):
    def log_security_event(
        self,
        *,
        event_type: str,
        user_id: str,
        severity: str,
        details: dict[str, Any],
    ) -> None: ...


# Phase 5 Intent Market audit logger is opt-in via VIOLA_ENABLE_INTENT_MARKET.
# When disabled, get_audit_logger is None and the music compliance pipeline
# falls back to the structured logger only — no phase5 code is imported.
get_audit_logger: Callable[[], _AuditLogger] | None = None
try:  # pragma: no cover - optional Phase 5+ dependency
    from config import env as _env  # canonical env helper, avoids os.getenv per linter

    if _env.get_bool("VIOLA_ENABLE_INTENT_MARKET", default=False):
        from experimental.phase5_intent_market.intent_market.audit_logger import (
            get_audit_logger as _get_audit_logger,
        )

        get_audit_logger = _get_audit_logger
except ImportError:  # pragma: no cover - fallback if phase5 tree absent
    pass


@dataclass(slots=True)
class AuditEvent:
    """Structured audit event emitted by the music compliance pipeline."""

    provider: str
    event_type: str
    status: str
    subject_hash: str | None
    metadata: dict[str, Any]
    occurred_at: float


class MusicAuditPipeline:
    """
    Normalized audit pipeline for music providers.

    Logs provider actions (playback, search, token rotation) with hashed subjects
    to satisfy compliance requirements without storing raw PII.
    """

    def __init__(self, system_user: str = "music_pipeline") -> None:
        self._system_user = system_user
        self._audit_logger = get_audit_logger() if get_audit_logger else None
        if self._audit_logger is None:
            logger.debug("Audit logger unavailable; falling back to log-only mode")

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def record_resolution(
        self,
        provider: str,
        query: str,
        status: str,
        latency_ms: float | None = None,
        source: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record resolution attempt outcome."""
        query_hash = self._hash(query)
        metadata = {
            "query_hash": query_hash,
            "source": source,
            "latency_ms": latency_ms,
            "error": error,
        }
        self._log_event(
            AuditEvent(
                provider=provider,
                event_type="resolution",
                status=status,
                subject_hash=query_hash,
                metadata=metadata,
                occurred_at=time.time(),
            )
        )

    def record_playback(
        self,
        provider: str,
        track_id: str | None,
        status: str,
        gap_ms: float | None = None,
        bitrate: int | None = None,
        lyrics_available: bool | None = None,
        error: str | None = None,
    ) -> None:
        """Record playback success/failure."""
        metadata = {
            "gap_ms": gap_ms,
            "bitrate": bitrate,
            "lyrics_available": lyrics_available,
            "error": error,
        }
        self._log_event(
            AuditEvent(
                provider=provider,
                event_type="playback",
                status=status,
                subject_hash=self._hash(track_id),
                metadata=metadata,
                occurred_at=time.time(),
            )
        )

    def record_token_rotation(
        self,
        provider: str,
        lease_id: str,
        status: str,
        rotation_type: str,
        error: str | None = None,
    ) -> None:
        """Record token rotation or revocation."""
        metadata = {
            "lease_hash": self._hash(lease_id),
            "rotation_type": rotation_type,
            "error": error,
        }
        self._log_event(
            AuditEvent(
                provider=provider,
                event_type="token_rotation",
                status=status,
                subject_hash=metadata["lease_hash"],
                metadata=metadata,
                occurred_at=time.time(),
            )
        )

    def record_compliance_note(
        self,
        provider: str,
        message: str,
        severity: str = "info",
    ) -> None:
        """Record compliance note (e.g., SLA breach, manual review)."""
        metadata = {"message": message, "severity": severity}
        self._log_event(
            AuditEvent(
                provider=provider,
                event_type="compliance_note",
                status="logged",
                subject_hash=None,
                metadata=metadata,
                occurred_at=time.time(),
            )
        )

    def export_for_legal(self, provider: str | None = None) -> dict[str, Any]:
        """
        Produce sanitized snapshot for legal review.

        Returns a lightweight report summarizing recent activity.
        """
        summary = {
            "provider": provider or "all",
            "generated_at": time.time(),
            "mode": "log_only" if self._audit_logger is None else "audit_logger",
        }
        # The actual detailed export lives in intent_market.audit_logger.
        if self._audit_logger is None:
            summary["note"] = "audit_logger_unavailable"
        else:
            summary["note"] = "use audit_logger.export_user_data or search interface"
        return summary

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _log_event(self, event: AuditEvent) -> None:
        """Persist audit event using structured schema."""
        if self._audit_logger is not None:
            try:
                self._audit_logger.log_security_event(
                    event_type=f"music.{event.event_type}",
                    user_id=self._system_user,
                    severity="high" if event.status == "failed" else "medium",
                    details={
                        "provider": event.provider,
                        "status": event.status,
                        "subject_hash": event.subject_hash,
                        "metadata": {k: v for k, v in event.metadata.items() if v is not None},
                        "occurred_at": event.occurred_at,
                    },
                )
                return
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to persist audit event via audit_logger: %s", exc)
        # Fallback to structured logging
        logger.bind(
            component="music_audit",
            provider=event.provider,
            event_type=event.event_type,
            status=event.status,
        ).info(
            "Audit event (fallback)",
            subject_hash=event.subject_hash,
            metadata={k: v for k, v in event.metadata.items() if v is not None},
        )

    @staticmethod
    def _hash(value: str | None) -> str | None:
        """Hash sensitive identifiers for audit storage."""
        if not value:
            return None
        digest = hashlib.sha256(value.encode("utf-8"))
        return digest.hexdigest()
