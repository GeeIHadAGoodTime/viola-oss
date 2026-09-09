"""One-shot opt-in ``first_run`` funnel ping — the download→install bridge.

Converts "installer bytes served" into "a real install actually launched":
on the first telemetry-eligible run, the desktop app sends ONE count-only
event to the first-party funnel endpoint, carrying nothing but the
pseudonymous random install id (hashed server-side before storage), the app
version, and the OS family. Modeled on the .NET SDK's single
successful-install telemetry entry and Firefox's first-run ping.

Privacy contract (identical to the aggregate reporter, enforced by reusing
its gates): the ping is sent ONLY when every telemetry send condition holds —
user opt-in, explicit privacy consent, configured server URL, kill-switch
enabled — and the transport must be https (loopback http allowed for
self-hosted dev). No PII, no voice, no command text, no campaign fingerprint
beyond what the user chose to self-report. Sent at most once per install,
tracked by a settings flag that flips only after a confirmed 2xx.
"""

from __future__ import annotations

import platform
from urllib.parse import urlparse, urlunparse

import httpx

from core.constants import TIMEOUT_LONG, VIOLA_VERSION
from core.logging_config import get_logger

logger = get_logger(__name__)

_SENT_FLAG_KEY = "telemetry_first_run_ping_sent"
_FUNNEL_PATH = "/api/public/funnel/event"


def _funnel_endpoint_from_server_url(server_url: str) -> str:
    """Derive the funnel endpoint from the configured telemetry base URL.

    ``telemetry_server_url`` is the API origin (the reporter appends
    ``/api/telemetry/ingest`` to it); the funnel endpoint lives on the same
    origin, so strip any path and append the funnel route.
    """
    parsed = urlparse(server_url.strip().rstrip("/"))
    return urlunparse((parsed.scheme, parsed.netloc, _FUNNEL_PATH, "", "", ""))


def _already_sent() -> bool:
    from ui.settings_manager import get_settings_manager

    return bool(get_settings_manager().get(_SENT_FLAG_KEY, False))


def _mark_sent() -> None:
    from ui.settings_manager import get_settings_manager

    get_settings_manager().set(_SENT_FLAG_KEY, True, save_immediately=True)


def _self_reported_attribution() -> str:
    """Return the user's optional "how did you hear about us?" answer.

    Populated by onboarding/settings when the user chooses to answer;
    empty string when they haven't. Normalized to a closed enum server-side.
    """
    try:
        from ui.settings_manager import get_settings_manager

        value = get_settings_manager().get("attribution_self_report", "")
        return value.strip() if isinstance(value, str) else ""
    except Exception:  # noqa: BLE001, RUF100 - optional self-report; absence must never block the ping
        return ""


async def send_first_run_ping() -> bool:
    """Send the one-shot first_run funnel event if all opt-in gates pass.

    Returns True when the ping was delivered (or had already been delivered
    earlier); False when gating or transport prevented it. Never raises.
    """
    try:
        if _already_sent():
            return True

        from telemetry.reporter import (
            TelemetryReporter,
            _is_telemetry_transport_secure,
            resolve_telemetry_server_url,
        )

        # Full opt-in gate set: user toggle, consent, server URL, kill-switch.
        if not TelemetryReporter.should_send():
            logger.debug("first_run ping skipped: telemetry send conditions not met")
            return False

        from config.settings import settings

        # Resolve the destination the same way should_send() does: an explicit
        # telemetry_server_url if the operator set one, else the cloud API origin
        # (api_base_url). Without this fallback the shipped desktop build left the
        # URL empty and the ping had nowhere to go — the funnel first_run=0 bug.
        server_url = resolve_telemetry_server_url(settings)
        if not server_url or not _is_telemetry_transport_secure(server_url):
            logger.debug("first_run ping skipped: no secure telemetry endpoint configured")
            return False

        from telemetry.install_id import get_or_create_install_id

        payload = {
            "event": "first_run",
            "install_id": get_or_create_install_id(),
            "attribution": _self_reported_attribution(),
            "app_version": VIOLA_VERSION,
            "os": platform.system().lower(),
            "website": "",
        }
        url = _funnel_endpoint_from_server_url(server_url)
        async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

        _mark_sent()
        logger.info("first_run funnel ping sent")
        return True
    except Exception:  # noqa: BLE001, RUF100 - best-effort one-shot ping; retried on next start, deduped server-side
        # Best effort: the flag stays unset, so the next scheduler start
        # retries. The server dedupes per hashed install id, so a retry after
        # a delivered-but-unacked ping can never double-count.
        logger.debug("first_run funnel ping failed; will retry on next start", exc_info=True)
        return False
