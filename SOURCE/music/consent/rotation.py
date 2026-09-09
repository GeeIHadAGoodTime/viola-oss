"""Token rotation helpers for Batch B."""

from __future__ import annotations

from datetime import timedelta

from core.logging_config import get_logger
from music.consent.models import RotationOutcome
from music.consent.service import ConsentService

logger = get_logger("viola.music.consent.rotation")


class TokenRotationJob:
    """
    Background-friendly helper that refreshes provider tokens when they are
    close to expiring.  The job can be scheduled via cron or invoked manually
    through the provided script.
    """

    def __init__(
        self,
        service: ConsentService,
        *,
        user_id: str | None = None,
        min_ttl: timedelta = timedelta(hours=12),
    ) -> None:
        self._service = service
        self._user_id = user_id
        self._min_ttl = min_ttl

    def run_once(self) -> list[RotationOutcome]:
        outcomes = self._service.rotate_tokens(user_id=self._user_id, min_ttl=self._min_ttl)
        refreshed = sum(1 for outcome in outcomes if outcome.refreshed)
        logger.info(
            "Token rotation finished for user %s: %s refreshed, %s checked",
            self._user_id,
            refreshed,
            len(outcomes),
        )
        for outcome in outcomes:
            logger.debug("Rotation result %s", outcome.to_dict())
        return outcomes
