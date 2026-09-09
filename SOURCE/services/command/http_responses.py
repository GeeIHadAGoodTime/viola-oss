from __future__ import annotations

import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from contracts.api_response import failure_response
from contracts.fastapi_helpers import SafeJSONResponse
from core.logging_config import get_logger

if TYPE_CHECKING:
    from services.command.types import CommandResult

logger = get_logger(__name__)


def seconds_until_midnight_utc() -> int:
    """Return the number of seconds until the next UTC midnight."""
    now = datetime.datetime.now(datetime.UTC)
    midnight = (now + datetime.timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return max(1, int((midnight - now).total_seconds()))


def build_429_response(result: CommandResult) -> SafeJSONResponse:
    """Build the HTTP response for a rate-limited command result."""
    message = result.data.get("message") or "Daily usage limit reached. Upgrade to Pro for higher limits."
    envelope: dict[str, Any] = failure_response(
        "rate_limited",
        message,
        data={
            "intent": result.intent,
            "policy_flags": result.policy_flags,
        },
    )
    retry_after = seconds_until_midnight_utc()
    logger.warning(
        "Returning HTTP 429 for rate-limited request (retry_after=%s)",
        retry_after,
    )
    return SafeJSONResponse(
        status_code=HTTPStatus.TOO_MANY_REQUESTS,
        content=envelope,
        headers={"Retry-After": str(retry_after)},
    )
