"""Resolve local versus hosted calling without loading hosted transports."""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger(__name__)


def phone_mode_is_cloud() -> bool:
    """Return True when phone calls run on the cloud (the SaaS default).

    Read from SettingsManager first (runtime user-preference truth, per the
    Settings Resolution rule), falling back to AppConfig. ``"cloud"`` is the
    default on both. Any non-"local" value is treated as cloud so a missing /
    malformed value fails toward the cloud path (where the SaaS calls live)
    rather than silently reading the empty local store.
    """
    mode = ""
    try:
        from ui.settings_manager import get_settings_manager

        mode = str(get_settings_manager().get("phone_mode", "") or "").strip().lower()
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        logger.debug("phone_mode SettingsManager lookup failed; trying AppConfig", exc_info=True)

    if not mode:
        try:
            from config.settings import settings

            mode = str(getattr(settings, "phone_mode", "") or "").strip().lower()
        except (ImportError, AttributeError, TypeError, ValueError):
            logger.debug("phone_mode AppConfig lookup failed", exc_info=True)

    return mode != "local"


def local_phone_carrier_configured() -> bool:
    """Return True when this install carries its own Telnyx carrier settings."""
    try:
        from config.settings import settings

        return all(
            str(getattr(settings, field, "") or "").strip()
            for field in ("telnyx_api_key", "telnyx_phone_number", "telnyx_sip_connection_id")
        )
    except (ImportError, AttributeError, TypeError, ValueError):
        logger.debug("Local phone carrier settings lookup failed", exc_info=True)
        return False


def uses_independent_local_phone() -> bool:
    """Return True only for an explicitly configured independent local install.

    This is the same routing decision ``intent.tools.phone_call.make_phone_call``
    makes: a hosted install (cloud phone mode, or company phone accounting
    present) always dials through the cloud CallManager.
    """
    from telephony.usage import company_phone_billing_available

    return not phone_mode_is_cloud() and not company_phone_billing_available()


def phone_calling_unavailable_reason() -> str | None:
    """Explain why no phone-call path can work in this runtime, or return None.

    Installed desktop builds call through the hosted cloud CallManager and
    never carry local Telnyx settings, so a missing local carrier must not hide
    the phone tool there. The tool is unusable only when neither path exists:

    * independent local install: the local Telnyx carrier settings are missing;
    * hosted install: no cloud URL is configured to reach the cloud CallManager.

    Per-user readiness on the hosted path (sign-in, phone terms, cloud consents,
    plan spend limits) is decided by the cloud at dial time and comes back to the
    model as a clear tool result with a sign-in or terms card, which is far
    better than the model believing it has no phone at all.
    """
    try:
        independent_local = uses_independent_local_phone()
    except Exception:
        # A broken company service install fails the call itself loudly; for
        # tool visibility, fall back to the hosted path the SaaS default uses.
        logger.debug("Phone route lookup failed; assuming the hosted cloud path", exc_info=True)
        independent_local = False

    if independent_local:
        if local_phone_carrier_configured():
            return None
        return "local_carrier_not_configured"

    try:
        from config.settings import settings

        cloud_url = str(getattr(settings, "cloud_url", "") or "").strip()
    except (ImportError, AttributeError, TypeError, ValueError):
        logger.debug("cloud_url lookup failed for phone availability", exc_info=True)
        cloud_url = ""
    if cloud_url:
        return None
    if local_phone_carrier_configured():
        # A hosted install without a cloud URL cannot dial, but a carrier-backed
        # install that set its own settings keeps the historical behavior.
        return None
    return "cloud_url_not_configured"
