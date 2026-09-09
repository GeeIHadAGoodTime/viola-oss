"""
OAuth Credential Helper for Google API access.

Retrieves encrypted tokens from the oauth_tokens table and returns
a google.oauth2.credentials.Credentials object suitable for use
with google-api-python-client.

Handles token refresh automatically and persists refreshed tokens
back to the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC

from core.logging_config import get_logger

logger = get_logger("viola.services.oauth.credentials")
_google_refresh_locks: dict[str, asyncio.Lock] = {}


def _get_google_refresh_lock(user_id: str) -> asyncio.Lock:
    # LRU cap: if the dict exceeds 10 000 entries, clear it entirely.
    # Locks are cheap to recreate; this prevents unbounded memory growth
    # from long-running cloud deployments with many transient users.
    if len(_google_refresh_locks) > 10000:
        _google_refresh_locks.clear()
    lock = _google_refresh_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _google_refresh_locks[user_id] = lock
    return lock


def _get_vault_google_token(user_id: str):
    try:
        from music.consent import get_consent_service

        service = get_consent_service()
        bundle = service._vault.get_token("google_calendar", user_id=user_id)
        if bundle and (bundle.access_token or bundle.refresh_token):
            logger.debug("Loaded Google Calendar token from consent vault for user %s", user_id)
            return bundle
    except Exception as exc:
        logger.debug("Consent vault lookup failed for user %s: %s", user_id, exc)
    return None


def _scopes_satisfy(required_scopes: Sequence[str] | None, granted_scopes: Sequence[str] | None) -> bool:
    if not required_scopes:
        return True
    return set(required_scopes).issubset(set(granted_scopes or ()))


async def _get_db_google_scopes(db, user_id: str, provider_value: str) -> list[str]:
    """Best-effort read of stored Google OAuth scopes from the auth DB."""
    try:
        if hasattr(db, "connection"):
            row = db.connection.execute(
                "SELECT scope FROM oauth_tokens WHERE user_id = ? AND provider = ?",
                (user_id, provider_value),
            ).fetchone()
            if row:
                scope_val = row["scope"] if hasattr(row, "keys") else row[0]
                if scope_val:
                    return str(scope_val).split()
        elif hasattr(db, "pool"):
            async with db.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT scope FROM oauth_tokens WHERE user_id = $1 AND provider = $2",
                    user_id,
                    provider_value,
                )
                if row and row.get("scope"):
                    return str(row["scope"]).split()
    except Exception as exc:
        logger.debug("Failed to read stored Google scopes for user %s: %s", user_id, exc)
    return []


async def get_google_credentials(
    user_id: str,
    required_scopes: Sequence[str] | None = None,
) -> google.oauth2.credentials.Credentials | None:
    """Serialize Google credential resolution per user to avoid refresh races."""
    async with _get_google_refresh_lock(user_id):
        return await _get_google_credentials_locked(user_id, required_scopes)


async def _get_google_credentials_locked(
    user_id: str,
    required_scopes: Sequence[str] | None = None,
) -> google.oauth2.credentials.Credentials | None:
    """Build a Google Credentials object from stored OAuth tokens.

    The credentials include access_token, refresh_token, client_id,
    client_secret, token_uri, and scopes.  If the access token is
    expired, this function refreshes it and writes the new token back
    to the database.

    Args:
        user_id: The Viola user ID whose tokens to retrieve.
        required_scopes: Optional scopes that must be present. If the auth DB
            token does not satisfy them, the helper falls through to the
            consent-vault token for that same user.

    Returns:
        A ``google.oauth2.credentials.Credentials`` instance, or ``None``
        if no tokens are stored or required libraries are missing.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        logger.warning("google-auth not installed — pip install google-auth google-api-python-client")
        return None

    from auth.database import get_auth_db
    from auth.models import OAuthProvider
    from services.oauth.google import GOOGLE_TOKEN_URL

    access_token = None
    refresh_token = None
    expires_at = None
    credential_source = "auth_db"
    scopes: list[str] = []
    db = None

    try:
        db = get_auth_db()
        if not db._initialized:
            await db.initialize()
        access_token, refresh_token, expires_at = await db.oauth_tokens.get_tokens(user_id, OAuthProvider.GOOGLE)
        db_scopes = await _get_db_google_scopes(db, user_id, OAuthProvider.GOOGLE.value)
        if db_scopes:
            scopes = db_scopes
        if (access_token or refresh_token) and not _scopes_satisfy(required_scopes, scopes):
            logger.debug(
                "Auth DB Google token for user %s missing required scopes %s; trying consent vault",
                user_id,
                list(required_scopes or ()),
            )
            access_token = None
            refresh_token = None
            expires_at = None
    except Exception as db_exc:
        logger.debug(
            "Auth DB lookup failed for Google tokens (user %s): %s — falling through to vault",
            user_id,
            db_exc,
        )
        access_token = None
        refresh_token = None
        expires_at = None

    if not access_token and not refresh_token:
        vault_bundle = _get_vault_google_token(user_id)
        if vault_bundle is None:
            logger.debug("No Google tokens stored for user %s with required scopes", user_id)
            return None
        else:
            vault_scopes = list(vault_bundle.scopes or ())
            if not _scopes_satisfy(required_scopes, vault_scopes):
                logger.debug(
                    "Consent vault Google token for user %s missing required scopes %s",
                    user_id,
                    list(required_scopes or ()),
                )
                return None
            access_token = vault_bundle.access_token
            refresh_token = vault_bundle.refresh_token
            expires_at = vault_bundle.expires_at
            credential_source = "consent_vault"
            if vault_scopes:
                scopes = vault_scopes

    # Retrieve client_id / client_secret from settings
    from config.settings import get_settings

    settings = get_settings()
    client_id = getattr(settings, "google_client_id", None)
    client_secret = getattr(settings, "google_client_secret", None)
    if not client_id or not client_secret:
        logger.warning("Google OAuth client credentials not configured")
        return None

    # Strip tzinfo for google-auth: Credentials.expiry must be naive UTC
    expiry_naive = expires_at.replace(tzinfo=None) if expires_at else None

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=GOOGLE_TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
        expiry=expiry_naive,
    )

    # Refresh if expired, invalid, or expiry unknown (can't trust unvalidated tokens).
    if creds.expired or not creds.valid or expires_at is None:
        if not creds.refresh_token:
            logger.warning("Google access token expired and no refresh token available")
            return None
        try:
            creds.refresh(Request())
            logger.info("Refreshed Google access token for user %s", user_id)

            # Notify dead-token detector of success (resets failure counter)
            from services.oauth.dead_token_detector import get_dead_token_detector

            get_dead_token_detector().record_success(user_id, "google")

            # Persist the refreshed tokens
            new_expires_at = creds.expiry.replace(tzinfo=UTC) if creds.expiry else None
            if credential_source == "auth_db" and db is not None:
                await db.oauth_tokens.store_tokens(
                    user_id=user_id,
                    provider=OAuthProvider.GOOGLE,
                    access_token=creds.token,
                    refresh_token=creds.refresh_token,
                    expires_at=new_expires_at,
                    scope=" ".join(scopes),
                )
            else:
                from music.consent import get_consent_service
                from music.consent.models import TokenBundle

                get_consent_service()._vault.update_access_token(
                    "google_calendar",
                    TokenBundle(
                        access_token=creds.token,
                        refresh_token=creds.refresh_token,
                        expires_at=new_expires_at,
                        scopes=tuple(scopes),
                    ),
                    user_id=user_id,
                )
        except Exception as exc:
            logger.error(
                "Failed to refresh Google access token for user %s: [%s] %s",
                user_id,
                type(exc).__name__,
                exc,
            )
            # Track consecutive refresh failures for dead token detection
            from services.oauth.dead_token_detector import get_dead_token_detector

            get_dead_token_detector().record_failure(user_id, "google", exc)
            return None

    if not _scopes_satisfy(required_scopes, scopes):
        logger.warning(
            "Resolved Google credentials for user %s do not include required scopes %s",
            user_id,
            list(required_scopes or ()),
        )
        return None

    return creds
