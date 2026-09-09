"""Configuration resolution for the desktop companion client.

The client needs three things before it can pair the desktop with the
user's cloud account:

1. **enabled** -- the user has not opted out (``companion_enabled`` setting).
2. **cloud_url** -- where the cloud bridge lives (``settings.cloud_url`` /
   ``VIOLA_CLOUD_URL``).
3. **a cloud account credential** -- a bearer token the cloud's
   ``AuthMiddleware`` accepts for ``POST /api/v1/companion/register`` and
   the ``/ws/companion`` upgrade.

Where the credential comes from
-------------------------------
It comes from **the desktop's own signed-in GoTrue session**, via
:func:`auth.desktop_session.desktop_access_token_for_active_session` -- the
same seam ``services/llm/providers/cloud_managed_provider.py`` and
``intent/tools/phone_call.py`` already use for their cloud bearers. Signing
in on the desktop IS the pairing act: there is no code to type, no QR to
scan, and no second credential to manage or store.

That indirection is load-bearing rather than stylistic. Production GoTrue
runs ``GOTRUE_JWT_EXP=300``, so an access token is dead five minutes after
it is minted. ``desktop_access_token_for_active_session`` returns the token
for the newest live session and **refreshes it through GoTrue's own rotation
machinery when it is within the expiry skew**, serialized process-wide so
concurrent consumers cannot trip GoTrue's refresh-token replay detection. So
the credential must be re-resolved through this seam on every use; a value
read once and held is guaranteed to be stale before the user's second
interaction. See :meth:`CompanionClientConfig.current_credential`.

``companion_cloud_token`` survives as an explicit override for a surface with
no desktop sign-in (a headless rig, a test harness). It is consulted only
when there is no live desktop session, because a hand-planted token cannot
refresh itself and must never shadow one that can.

Nothing here is a secret store: the credential is read on demand and never
persisted by this module. The GoTrue token pair stays in the desktop session
store's encrypted device-local cache (Tier 3 -- never replicated to cloud);
only the post-registration device token is persisted, by :mod:`identity_store`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from core.logging_config import get_logger

logger = get_logger(__name__)

# Setting keys (see ui/settings_manager.py defaults). Kept as module
# constants so tests and the startup wiring agree on the names.
SETTING_ENABLED = "companion_enabled"
SETTING_CLOUD_TOKEN = "companion_cloud_token"
SETTING_DEVICE_NAME = "companion_device_name"

# A credential provider returns the current cloud-account bearer token, or
# ``None`` / "" when the user is not signed in. It may be sync or async: the
# real one performs a GoTrue refresh round-trip, and a sync-only signature
# would have forced that network call onto a blocking call path.
CredentialProvider = Callable[[], "str | None | Awaitable[str | None]"]


@dataclass(slots=True, frozen=True)
class CompanionClientConfig:
    """Resolved, validated configuration for one client run.

    ``credential`` is a point-in-time SNAPSHOT, useful only for the
    signed-in/signed-out summary and for callers that supplied a fixed token.
    Never present it to the cloud directly -- a GoTrue access token lives
    300 seconds. Use :meth:`current_credential`, which re-resolves through
    the provider (refreshing the session when needed) on every call.
    """

    enabled: bool
    cloud_url: str
    device_name: str
    platform: str
    # The cloud-account bearer credential AS OF CONFIG LOAD. Empty when not
    # signed in, and empty by design when a provider will resolve it later.
    credential: str
    # The GoTrue account this desktop is signed into, when known. Stable
    # across token refreshes, which is exactly why the identity-store
    # partition key is derived from it -- see ``account_key``.
    account_id: str = ""
    # Re-resolves a fresh credential on demand. ``None`` means the snapshot
    # in ``credential`` is all there will ever be.
    credential_provider: CredentialProvider | None = None

    @property
    def has_credential_source(self) -> bool:
        """Whether ANY credential can ever be resolved for this config."""
        return bool(self.credential) or self.credential_provider is not None

    @property
    def is_runnable(self) -> bool:
        """Whether the client has everything it needs to start.

        A provider counts as "has a credential": the user may sign in after
        the app is already running, and the supervisor waits for that rather
        than forcing an app restart. Only a config with neither a snapshot
        nor a provider is genuinely unrunnable.
        """
        return bool(self.enabled and self.cloud_url) and self.has_credential_source

    @property
    def account_key(self) -> str:
        """A stable, non-reversible fingerprint of the cloud ACCOUNT.

        Used as the identity-store partition key so the same desktop can be
        paired with more than one cloud account. Never the credential itself.
        """
        return companion_account_key(account_id=self.account_id, credential=self.credential)

    async def current_credential(self) -> str:
        """Re-resolve the account bearer right now, refreshing if it is stale.

        This is the ONLY correct way to obtain the credential for a network
        call. Returns "" when the user is signed out, which callers must
        treat as "wait for sign-in", never as an error.
        """
        if self.credential_provider is None:
            return self.credential
        return await resolve_credential(self.credential_provider)


async def resolve_credential(provider: CredentialProvider | None) -> str:
    """Call a sync-or-async credential provider and normalize the result.

    Fails closed to "" (signed out) rather than raising: a credential lookup
    that blows up must idle the companion, never crash the supervisor that
    is the desktop's only path back online.
    """
    if provider is None:
        return ""
    try:
        value = provider()
        if inspect.isawaitable(value):
            value = await value
        return str(value or "").strip()
    except Exception:
        logger.exception("Companion credential provider failed; treating as signed out")
        return ""


async def desktop_session_credential() -> str:
    """Return the bearer for the desktop's signed-in Viola account.

    The live GoTrue session wins over the ``companion_cloud_token`` override
    because only the session can refresh itself past the 300 s access-token
    expiry; a stale planted token would otherwise permanently shadow a
    working sign-in.
    """
    try:
        from auth.desktop_session import desktop_access_token_for_active_session

        token = str(await desktop_access_token_for_active_session() or "").strip()
        if token:
            return token
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Desktop session companion credential lookup failed", exc_info=True)

    return str(_read_setting(SETTING_CLOUD_TOKEN, "") or "").strip()


def companion_account_key(*, account_id: str, credential: str) -> str:
    """Derive the identity-store partition key for one cloud account.

    Keyed on the ACCOUNT, not the credential. Deriving it from the access
    token (as this once did) silently re-partitioned the store every time
    GoTrue minted a new token -- i.e. every five minutes -- so the stored
    device identity could never be found again and the desktop registered
    itself as a brand-new companion device on every refresh.

    Resolution order, most to least authoritative:

    1. the account id the local desktop session reports,
    2. the ``sub`` claim of the credential itself (covers the override path,
       which has no desktop session to ask),
    3. a hash of the credential (last resort: an opaque, non-JWT token).
    """
    resolved = str(account_id or "").strip() or _account_id_from_jwt(credential)
    if resolved:
        return hashlib.sha256(("account:%s" % resolved).encode("utf-8")).hexdigest()[:32]
    return hashlib.sha256(str(credential or "").encode("utf-8")).hexdigest()[:32]


def _account_id_from_jwt(token: str) -> str:
    """Return a JWT's ``sub`` claim, or "" if it is not a readable JWT.

    Deliberately unverified: this value is never a security decision, only a
    local partition key, and the desktop has no business verifying a token
    the cloud issues and checks. Any parse failure just falls through.
    """
    parts = str(token or "").strip().split(".")
    if len(parts) != 3:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, binascii.Error):
        return ""
    if not isinstance(claims, dict):
        return ""
    return str(claims.get("sub") or "").strip()


def _default_platform() -> str:
    """Return a short platform tag (``windows`` / ``macos`` / ``linux``)."""
    try:
        from core.platform import Platform, get_platform

        mapping = {
            Platform.WINDOWS: "windows",
            Platform.MACOS: "macos",
            Platform.LINUX: "linux",
        }
        return mapping.get(get_platform(), "desktop")
    except Exception:
        return "desktop"


def _default_device_name() -> str:
    """Return a friendly default device name (``Viola Desktop (HOST)``)."""
    try:
        import socket

        host = socket.gethostname().strip()
        if host:
            return "Viola Desktop (%s)" % host
    except Exception:
        pass
    return "Viola Desktop"


def _resolve_cloud_url() -> str:
    """Resolve the cloud base URL from env override or app settings."""
    import os

    env_url = (os.environ.get("VIOLA_CLOUD_URL", "") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    try:
        from config.settings import settings

        return str(getattr(settings, "cloud_url", "") or "").strip().rstrip("/")
    except Exception:
        return ""


def _read_setting(key: str, default: object) -> object:
    try:
        from ui.settings_manager import get_settings_manager

        return get_settings_manager().get(key, default)
    except Exception:
        logger.debug("Could not read companion setting %s; using default", key)
        return default


def _desktop_account_id() -> str:
    """Return the GoTrue user id this desktop is signed in as, or "".

    A local-only read (no network, no refresh), cheap enough to do at config
    load. Empty when signed out, which is fine: the partition key falls back
    to the credential's own ``sub`` claim.
    """
    try:
        from auth.desktop_session import get_desktop_account_identity

        identity = get_desktop_account_identity()
        if getattr(identity, "signed_in", False):
            return str(getattr(identity, "user_id", "") or "").strip()
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("Desktop account identity lookup failed", exc_info=True)
    return ""


def load_companion_client_config(
    *,
    credential_provider: CredentialProvider | None = None,
) -> CompanionClientConfig:
    """Build a :class:`CompanionClientConfig` from settings + credential source.

    Args:
        credential_provider: Optional callable (sync or async) returning the
            current cloud account bearer token. When omitted, the desktop's
            own signed-in GoTrue session is used -- see the module docstring.

    The returned config carries the PROVIDER, not a resolved token: the
    caller must await :meth:`CompanionClientConfig.current_credential` at the
    moment it needs a bearer, because any earlier value expires in 300 s.
    """
    from config.defaults import COMPANION_ENABLED_DEFAULT

    enabled = bool(_read_setting(SETTING_ENABLED, COMPANION_ENABLED_DEFAULT))
    device_name = str(_read_setting(SETTING_DEVICE_NAME, "") or "").strip() or _default_device_name()
    provider = credential_provider if credential_provider is not None else desktop_session_credential

    return CompanionClientConfig(
        enabled=enabled,
        cloud_url=_resolve_cloud_url(),
        device_name=device_name,
        platform=_default_platform(),
        # Resolved lazily and repeatedly through the provider; a snapshot
        # taken here would be expired long before the first reconnect.
        credential="",
        account_id=_desktop_account_id(),
        credential_provider=provider,
    )
