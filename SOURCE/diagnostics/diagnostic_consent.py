"""Consent + disclosure gates for the anonymized diagnostic minimum.

This module is the single source of truth for *whether* a diagnostic may leave
the device, and *what layer* it may carry. Every send path (crash capture, bug
report attach, the cloud relay) reads its decision from here -- never from an
inline settings read -- so the safety invariants live in exactly one place and
are enforced by one ratchet gate (``scripts/check_diagnostic_consent_invariants.py``).

The founder's approved model has THREE independent switches, and the gates below
compose them so the safe state is the default at every layer:

1. MASTER FEATURE FLAG (``diagnostics_baseline_enabled``, default **False**).
   The fleet-level arming for the whole feature. It stays OFF in the committed
   default until the founder signs off on the final disclosure UI. While it is
   off, NOTHING sends -- not a crash, not a bug-report attach -- regardless of
   any per-install switch. This is the "land it gated, don't flip the default
   on" knob.

2. DISCLOSURE SHOWN (``diagnostics_disclosure_shown``, per-install, default
   **False**). The first-run/post-update disclosure card must have been shown
   once before the *crash* baseline may send. Until then crash payloads queue
   locally (see ``diagnostic_spool``) and flush only after disclosure. A bug
   report is user-initiated -- the user is deliberately sending us a report and
   the bug-report form itself discloses the attached minimum -- so it does not
   require the separate card, only that the user has not opted out.

3. OPT-OUT (``diagnostics_baseline_opted_out``, per-install, default **False**
   = participating). The user may turn the anonymized baseline off entirely.
   When set, both the crash send and the bug-report attach stop. This read
   fails **toward opted-out** (a settings read failure suppresses the send).

Separately, the IDENTIFIABLE EXTRA (contact, verbatim body with identifiers, a
stable install id, screenshots, free-text context) is a distinct OPT-IN
(``consent_diagnostics_identifiable`` / ``VIOLA_CONSENT_DIAGNOSTICS_IDENTIFIABLE``,
default **False**, revocable). It never rides unless the user has explicitly
turned it on, and it can only ride on top of a baseline send that is already
permitted.

All gate reads fail closed: any error resolves to "not armed / not consented",
never to a send.
"""

from __future__ import annotations

import os

from core.logging_config import get_logger

log = get_logger(__name__)

_MISSING = object()

# Setting keys (SettingsManager / settings.json). Kept as constants so the gate
# and the settings-panel endpoints reference the exact same strings.
SETTING_DISCLOSURE_SHOWN = "diagnostics_disclosure_shown"
SETTING_BASELINE_OPTED_OUT = "diagnostics_baseline_opted_out"
SETTING_IDENTIFIABLE_CONSENT = "consent_diagnostics_identifiable"

# Env overrides (mirror the AppConfig / privacy_consent env pattern).
ENV_MASTER_ENABLED = "VIOLA_DIAGNOSTICS_BASELINE_ENABLED"
ENV_IDENTIFIABLE_CONSENT = "VIOLA_CONSENT_DIAGNOSTICS_IDENTIFIABLE"

_TRUTHY = ("1", "true", "yes", "on", "y")


def _env_truthy(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return str(raw).strip().lower() in _TRUTHY


def _settings_bool(key: str, *, default: bool, fail_value: bool) -> bool:
    """Read a bool from SettingsManager.

    ``default`` is returned when the key is simply absent; ``fail_value`` is
    returned when the settings layer cannot be read at all (the fail-closed
    choice is per-caller: a privacy gate fails toward "do not send").
    """
    try:
        from ui.settings_manager import get_settings_manager

        mgr = get_settings_manager()
        value = mgr.get(key, _MISSING)
        if value is _MISSING:
            return default
        return bool(value)
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-consent: settings read failed for %s; using fail value", key)
        return fail_value


def master_baseline_enabled() -> bool:
    """Fleet-level master arming for the anonymized baseline. Default OFF.

    Env ``VIOLA_DIAGNOSTICS_BASELINE_ENABLED`` wins; else AppConfig
    ``diagnostics_baseline_enabled``; else False. Committed default is OFF until
    the founder signs off on the disclosure UI -- so the whole feature ships
    dark and nothing sends until this flips.
    """
    env = _env_truthy(ENV_MASTER_ENABLED)
    if env is not None:
        return env
    try:
        from config.settings import settings

        return bool(getattr(settings, "diagnostics_baseline_enabled", False))
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        return False


def is_disclosure_shown() -> bool:
    """True once the disclosure card has been shown on this install."""
    return _settings_bool(SETTING_DISCLOSURE_SHOWN, default=False, fail_value=False)


def mark_disclosure_shown() -> bool:
    """Persist that the disclosure card has been shown. Best-effort; never raises."""
    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().set(SETTING_DISCLOSURE_SHOWN, True))
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-consent: could not persist disclosure-shown flag")
        return False


def is_baseline_opted_out() -> bool:
    """True when the user has turned the anonymized baseline OFF.

    Fails toward opted-out: if settings cannot be read, we treat the user as
    opted out so a read failure can never cause a send.
    """
    return _settings_bool(SETTING_BASELINE_OPTED_OUT, default=False, fail_value=True)


def is_identifiable_extra_consented() -> bool:
    """True when the user has opted IN to the identifiable EXTRA. Default OFF.

    Env ``VIOLA_CONSENT_DIAGNOSTICS_IDENTIFIABLE`` wins; else the per-install
    setting; else False. Fails closed to not-consented.
    """
    env = _env_truthy(ENV_IDENTIFIABLE_CONSENT)
    if env is not None:
        return env
    return _settings_bool(SETTING_IDENTIFIABLE_CONSENT, default=False, fail_value=False)


def is_crash_baseline_armed() -> bool:
    """Whether an automatic crash diagnostic may send right now.

    Requires ALL of: master flag on, disclosure card shown, not opted out.
    Any failing condition (or any read error) means "not armed" -> the crash
    payload is spooled locally instead of sent.
    """
    return master_baseline_enabled() and is_disclosure_shown() and not is_baseline_opted_out()


def is_bug_report_minimum_allowed() -> bool:
    """Whether the anonymized minimum may attach to a user-initiated bug report.

    A bug report is a deliberate user send whose form discloses the attached
    minimum, so it does not gate on the separate first-run card -- only the
    master flag and the opt-out apply.
    """
    return master_baseline_enabled() and not is_baseline_opted_out()


def may_attach_identifiable_extra() -> bool:
    """Whether the identifiable EXTRA may ride a permitted baseline send.

    Only the explicit opt-in unlocks it. (A caller still only reaches this after
    a baseline send is already permitted, so the extra can never travel alone.)
    """
    return is_identifiable_extra_consented()


def consent_snapshot() -> dict[str, bool]:
    """Read-only view of every gate decision, for the Settings > Privacy panel."""
    return {
        "master_baseline_enabled": master_baseline_enabled(),
        "disclosure_shown": is_disclosure_shown(),
        "baseline_opted_out": is_baseline_opted_out(),
        "identifiable_extra_consented": is_identifiable_extra_consented(),
        "crash_baseline_armed": is_crash_baseline_armed(),
        "bug_report_minimum_allowed": is_bug_report_minimum_allowed(),
    }


__all__ = [
    "ENV_IDENTIFIABLE_CONSENT",
    "ENV_MASTER_ENABLED",
    "SETTING_BASELINE_OPTED_OUT",
    "SETTING_DISCLOSURE_SHOWN",
    "SETTING_IDENTIFIABLE_CONSENT",
    "consent_snapshot",
    "is_baseline_opted_out",
    "is_bug_report_minimum_allowed",
    "is_crash_baseline_armed",
    "is_disclosure_shown",
    "is_identifiable_extra_consented",
    "mark_disclosure_shown",
    "master_baseline_enabled",
    "may_attach_identifiable_extra",
]
