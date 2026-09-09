"""Desktop write path for the cloud-sync consent toggle (#4789).

The Cloud Sync switch in the desktop Settings panel used to write
``consent_cloud_sync`` into the local ``settings.json`` and nothing else. That
made it a consent control that lied in both directions:

* Turning it **on** did not grant anything. Every cloud Tier-2 gate reads the
  ``sync_user_preferences`` row where ``key='consent_cloud_sync'``
  (``services/sync/consent.py::has_cloud_sync_consent``), not a desktop file, so
  the user's own cloud features kept answering ``consent_required``.
* Turning it **off** did not withdraw anything, which is the worse half. A
  revoking write to the authoritative row runs
  ``tombstone_cloud_sync_data_after_withdrawal`` in the same transaction — it
  tombstones the user's synced Tier-2 data and bumps the consent era so queued
  pre-withdrawal pushes are rejected as stale. A local-only "off" skipped all of
  that and left the cloud copy readable.

So this module makes the desktop toggle write the row the server actually reads.
It is deliberately narrow: it mirrors ONE key through the shipped
``/v1/sync/user-preferences/{key}`` route using the existing
:class:`~services.sync.http_transport.HttpSyncTransport` (same bearer handling,
same envelope parsing, same fail-closed no-token behaviour) rather than opening a
second HTTP client. It is NOT the desktop Tier-2 data-sync loop —
:class:`~services.sync.client.Tier2SyncClient` still has no persistent cache,
outbox or scheduler, so desktop-authored settings and playlists do not travel
yet, and the Settings copy says so.

Fail-closed contract, relied on by ``ui/settings_api.py``: the caller asks this
module FIRST and only persists locally when the answer is ok. A cloud write that
did not land therefore cannot leave the local file claiming a consent state the
cloud never agreed to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from core.logging_config import get_logger
from services.sync.client import CONSENT_KEY, SyncConsentRequired, SyncTransportError

logger = get_logger(__name__)

# Re-exported so a caller (``ui/settings_api.py``) names the consent key exactly
# once, from the same constant the sync client and the server share. A second
# hand-typed "consent_cloud_sync" literal in a write path is the drift this whole
# module exists to remove.
CLOUD_SYNC_CONSENT_KEY = CONSENT_KEY

# Short on purpose: this runs inside a Settings save, so the user is watching a
# spinner. A slow cloud is reported as "not recorded", never waited out.
CONSENT_MIRROR_TIMEOUT_SECONDS = 10.0

_SIGNED_OUT_MESSAGE = "Sign in to your Viola account before changing cloud sync."
_UNREACHABLE_MESSAGE = "Cloud sync was not changed: Viola could not reach your account. Nothing was saved. Try again."


class ConsentPreferenceWriter(Protocol):
    """The seam this module writes through. ``HttpSyncTransport`` satisfies it."""

    async def put_user_preference(self, *, key: str, value: Any) -> dict[str, Any]: ...


class ConsentMirrorOutcome(str, Enum):
    """What happened to the authoritative cloud consent row."""

    MIRRORED = "mirrored"
    """The cloud row now holds the requested value."""

    NOT_DESKTOP = "not_desktop"
    """Not the desktop surface, so there is no desktop consent to mirror. The
    cloud surface writes the same row directly through its own settings route."""

    NO_ACCOUNT = "no_account"
    """Nobody is signed in, so no cloud row exists to consent on."""

    FAILED = "failed"
    """The cloud write was attempted and did not land."""


@dataclass(frozen=True)
class ConsentMirrorResult:
    """Outcome of one mirror attempt, plus the sentence to show the user."""

    outcome: ConsentMirrorOutcome
    message: str = ""
    enabled: bool | None = None

    @property
    def ok(self) -> bool:
        """True only when the authoritative row is known to match the request."""
        return self.outcome in (ConsentMirrorOutcome.MIRRORED, ConsentMirrorOutcome.NOT_DESKTOP)

    def as_dict(self) -> dict[str, object]:
        return {"outcome": self.outcome.value, "message": self.message, "enabled": self.enabled}


def _is_desktop_surface() -> bool:
    try:
        from ui.core.security import is_desktop_surface

        return bool(is_desktop_surface())
    except Exception:  # noqa: BLE001, RUF100 - surface detection must never break a settings write
        logger.debug("Could not read the app surface; treating it as desktop", exc_info=True)
        return True


def _cloud_base_url() -> str:
    from config.settings import settings

    return str(getattr(settings, "cloud_url", "") or "https://api.useviola.com").rstrip("/")


def _device_id() -> str:
    """A stable per-install identifier for the ``X-Device-Id`` header.

    The consent row is per-user, not per-device, so this only labels which
    install authored the change. A fallback keeps the consent write possible on
    an install whose device identity cannot be read.
    """
    try:
        from core.user_context import get_device_user_id

        # mt-ok: device-scoped label on a per-user consent write, not a user identity
        device_id = str(get_device_user_id() or "").strip()
    except Exception:  # noqa: BLE001, RUF100 - a missing device label must not block a consent write
        logger.debug("Could not read the device identifier for the consent mirror", exc_info=True)
        device_id = ""
    return device_id or "desktop"


async def desktop_access_token() -> str | None:
    """Return the signed-in desktop account's GoTrue access token, refreshed."""
    try:
        from auth.desktop_session import desktop_access_token_for_active_session

        return await desktop_access_token_for_active_session()
    except Exception:  # noqa: BLE001, RUF100 - a token lookup failure is "not signed in", not a crash
        logger.debug("Desktop access-token lookup for the consent mirror failed", exc_info=True)
        return None


async def mirror_cloud_sync_consent(
    enabled: bool,
    *,
    writer: ConsentPreferenceWriter | None = None,
) -> ConsentMirrorResult:
    """Write the desktop user's cloud-sync consent to the authoritative cloud row.

    *writer* exists for tests and for a caller that already holds a transport;
    when it is None this builds a short-lived :class:`HttpSyncTransport` against
    the configured cloud URL and closes it again.
    """
    desired = bool(enabled)

    if writer is None and not _is_desktop_surface():
        return ConsentMirrorResult(
            ConsentMirrorOutcome.NOT_DESKTOP,
            "",
            desired,
        )

    owns_writer = writer is None
    if owns_writer:
        if not await desktop_access_token():
            logger.info("Cloud-sync consent change refused: no signed-in desktop account")
            return ConsentMirrorResult(ConsentMirrorOutcome.NO_ACCOUNT, _SIGNED_OUT_MESSAGE, desired)

        from services.sync.http_transport import HttpSyncTransport

        writer = HttpSyncTransport(
            base_url=_cloud_base_url(),
            device_id=_device_id(),
            token_provider=desktop_access_token,
            timeout_seconds=CONSENT_MIRROR_TIMEOUT_SECONDS,
        )

    try:
        await writer.put_user_preference(key=CONSENT_KEY, value=desired)
    except SyncConsentRequired:
        # The transport raises this when there is no bearer at all; the consent
        # key itself is exempt from the server's consent gate, so a genuine
        # consent_required here also means "no usable account".
        logger.info("Cloud-sync consent change refused: no usable account credential")
        return ConsentMirrorResult(ConsentMirrorOutcome.NO_ACCOUNT, _SIGNED_OUT_MESSAGE, desired)
    except SyncTransportError as exc:
        logger.warning(
            "Cloud-sync consent mirror failed: code=%s status=%s",
            getattr(exc, "code", None),
            getattr(exc, "status_code", None),
        )
        return ConsentMirrorResult(ConsentMirrorOutcome.FAILED, _UNREACHABLE_MESSAGE, desired)
    except Exception:  # noqa: BLE001, RUF100 - report the outcome; never turn a consent write into a 500
        logger.exception("Cloud-sync consent mirror raised unexpectedly")
        return ConsentMirrorResult(ConsentMirrorOutcome.FAILED, _UNREACHABLE_MESSAGE, desired)
    finally:
        if owns_writer:
            close = getattr(writer, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001, RUF100 - a close failure must not mask the write outcome
                    logger.debug("Closing the consent-mirror transport failed", exc_info=True)

    logger.info("Cloud-sync consent mirrored to the account: enabled=%s", desired)
    return ConsentMirrorResult(ConsentMirrorOutcome.MIRRORED, "", desired)


__all__ = [
    "CLOUD_SYNC_CONSENT_KEY",
    "CONSENT_MIRROR_TIMEOUT_SECONDS",
    "ConsentMirrorOutcome",
    "ConsentMirrorResult",
    "ConsentPreferenceWriter",
    "desktop_access_token",
    "mirror_cloud_sync_consent",
]
