"""F-AUTH-03 (2026-04-17): first-run passkey offer.

After a successful registration, the frontend asks this endpoint whether
a passkey offer should be shown. The offer is presented once; the user
can accept (go through the WebAuthn registration flow) or skip.

Decoupled from ``/auth/register`` so the critical path does not change.
If the frontend ever fails to render the offer, registration succeeds
anyway.

Endpoint:
    GET  /auth/first-run-offer
        Returns: {"offer_passkey": bool, "has_passkey": bool,
                  "reason": str, "webauthn_register_url": str}

The offer is shown when:
  - the user has no passkeys registered, AND
  - the user has not already dismissed the offer (``first_run_passkey_dismissed``
    flag on the user record), AND
  - the server's WebAuthn / passkey feature is actually wired (so we
    don't send a dead link).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from auth.dependencies import get_current_user, require_real_user
from auth.models import User
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, status

logger = get_logger(__name__)

first_run_router = APIRouter()


class FirstRunOfferResponse(BaseModel):
    offer_passkey: bool
    has_passkey: bool
    reason: str
    webauthn_register_url: str = "/auth/webauthn/register/options"


def _has_passkey(user: User) -> bool:
    """True iff user has at least one WebAuthn credential on record."""
    credentials = getattr(user, "webauthn_credentials", None)
    if not credentials:
        return False
    try:
        return len(credentials) > 0
    except TypeError:
        return bool(credentials)


def _webauthn_feature_ready() -> bool:
    """True iff the WebAuthn route is actually wired on this server."""
    try:
        from auth import webauthn
    except Exception:
        return False
    return webauthn is not None


@first_run_router.get(
    "/first-run-offer",
    response_model=FirstRunOfferResponse,
    summary="First-run passkey offer",
    description=(
        "Called by the web UI immediately after signup. Tells the frontend "
        "whether to render the passkey-create prompt."
    ),
)
async def first_run_offer(
    user: User = Depends(require_real_user),
) -> FirstRunOfferResponse:
    has = _has_passkey(user)
    if has:
        return FirstRunOfferResponse(offer_passkey=False, has_passkey=True, reason="already_registered")

    dismissed = bool(getattr(user, "first_run_passkey_dismissed", False))
    if dismissed:
        return FirstRunOfferResponse(offer_passkey=False, has_passkey=False, reason="user_dismissed")

    if not _webauthn_feature_ready():
        return FirstRunOfferResponse(offer_passkey=False, has_passkey=False, reason="feature_unavailable")

    return FirstRunOfferResponse(
        offer_passkey=True,
        has_passkey=False,
        reason="eligible",
    )


class DismissRequest(BaseModel):
    reason: str | None = None


@first_run_router.post(
    "/first-run-offer/dismiss",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Dismiss first-run passkey offer",
)
async def first_run_offer_dismiss(
    body: DismissRequest,
    user: User = Depends(require_real_user),
    db: Any = Depends(_get_db := lambda: None),  # type: ignore[misc] # VIOLA-000 lazy DB dependency shim
) -> None:
    # Import db factory lazily to avoid a circular import on router load.
    try:
        from auth.dependencies import get_auth_db

        actual_db = db if db is not None else await get_auth_db()
    except Exception as exc:
        logger.warning("first_run_offer_dismiss: auth DB unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "auth_database_unavailable",
                "message": "Account setup is temporarily unavailable. Please try again.",
            },
        ) from exc

    try:
        await actual_db.users.set_first_run_passkey_dismissed(user.id, True)
    except AttributeError:
        # DB layer hasn't added the column yet — swallow quietly so the
        # offer simply re-appears next time; this is non-critical.
        logger.info(
            "first_run_passkey_dismissed column not present on users; offer will re-appear",
        )
    except Exception as exc:
        logger.exception("Failed to persist first-run dismissal: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "first_run_offer_dismiss_failed",
                "message": "We could not save that preference. Please try again.",
            },
        ) from exc
