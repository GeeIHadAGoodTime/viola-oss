"""Policy layer: decide whether/what diagnostic to send, then send it.

This is the ONE place that composes the pieces:

    build the anonymized minimum  (diagnostic_minimum)
    check the consent + disclosure gates  (diagnostic_consent)
    queue-or-send  (diagnostic_spool / diagnostic_relay)
    only-if-opted-in: attach the identifiable EXTRA

Every send decision here flows through ``diagnostics.diagnostic_consent`` -- no
inline settings read -- so the safety invariants are enforced in exactly one
place and proven by ``scripts/check_diagnostic_consent_invariants.py``.

The three invariants this module guarantees:

* The crash baseline never sends before the disclosure card has been shown
  (``is_crash_baseline_armed`` requires it); until then the anonymized payload is
  spooled locally and flushed only once armed.
* The identifiable EXTRA never attaches without the separate opt-in
  (``may_attach_identifiable_extra`` gates ``build_identifiable_extra``, which
  returns ``None`` otherwise).
* Opt-out (and the master flag being off) stops ALL sends and ALL attaches.

Nothing here raises: it runs off crash handlers and request paths where a
diagnostics failure must never break the surrounding operation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.logging_config import get_logger
from diagnostics import diagnostic_consent, diagnostic_spool
from diagnostics.diagnostic_minimum import build_browser_diagnostic_minimum, build_diagnostic_minimum

log = get_logger(__name__)

_DEFAULT_SURFACE = "desktop_qt"


def dispatch_crash_diagnostic(
    exc: BaseException | None = None,
    *,
    exc_info: tuple[type[BaseException], BaseException, Any] | None = None,
    app_state: Mapping[str, Any] | None = None,
    surface: str | None = None,
) -> str:
    """Handle an automatic crash diagnostic on the opt-out baseline.

    Returns one of: ``"sent"`` (relayed now), ``"queued"`` (spooled locally
    because the baseline is not armed yet), or ``"suppressed"`` (opted out /
    build failed). Never raises.
    """
    try:
        payload = build_diagnostic_minimum(
            report_kind="crash",
            exc=exc,
            exc_info=exc_info,
            app_state=app_state,
            surface=surface or _DEFAULT_SURFACE,
        )
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-dispatch: minimum build failed for crash", exc_info=True)
        return "suppressed"

    return _dispatch_built_crash(payload, app_state=app_state)


def _dispatch_built_crash(
    payload: dict[str, Any],
    *,
    app_state: Mapping[str, Any] | None = None,
) -> str:
    """Apply the crash-baseline gates to an already-built minimum.

    Shared by the Python and browser crash paths so there is exactly ONE
    consent sequence to reason about and to ratchet. Never raises.
    """
    # Opted out (or master off + opted-out fail path): send nothing, queue nothing.
    if diagnostic_consent.is_baseline_opted_out():
        return "suppressed"

    if not diagnostic_consent.is_crash_baseline_armed():
        # Master off, or disclosure not shown yet: queue locally, flush after arm.
        diagnostic_spool.enqueue(payload)
        return "queued"

    # Armed: flush anything queued from before arming, then send this one.
    _flush_spool_locked()
    sent = _relay(payload, allow_identifiable=True, app_state=app_state)
    return "sent" if sent else "queued_after_failure" if diagnostic_spool.enqueue(payload) else "suppressed"


def dispatch_browser_crash_diagnostic(
    *,
    error_type: Any,
    error_value: Any,
    frames: Any = None,
    app_state: Mapping[str, Any] | None = None,
    surface: str | None = None,
) -> str:
    """Handle a crash raised in the desktop's React UI, on the same baseline.

    Identical consent behaviour to :func:`dispatch_crash_diagnostic` -- and
    deliberately routed through the SAME gates rather than growing a second
    policy path, because a browser error is no less sensitive than a Python
    one. Returns ``"sent"`` / ``"queued"`` / ``"suppressed"``. Never raises.
    """
    try:
        payload = build_browser_diagnostic_minimum(
            error_type=error_type,
            error_value=error_value,
            frames=frames,
            app_state=app_state,
            surface=surface,
        )
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-dispatch: browser minimum build failed", exc_info=True)
        return "suppressed"

    return _dispatch_built_crash(payload, app_state=app_state)


def dispatch_bug_report_minimum(
    *,
    app_state: Mapping[str, Any] | None = None,
    surface: str | None = None,
) -> dict[str, Any] | None:
    """Return the anonymized minimum to ATTACH to a user-initiated bug report.

    A bug report is a deliberate user send whose form discloses the attached
    minimum, so it gates only on the master flag + opt-out (not the first-run
    card). Returns the payload dict when allowed, else ``None``. Never raises.
    """
    try:
        if not diagnostic_consent.is_bug_report_minimum_allowed():
            return None
        return build_diagnostic_minimum(
            report_kind="bug_report",
            app_state=app_state,
            surface=surface or _DEFAULT_SURFACE,
        )
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-dispatch: bug-report minimum build failed", exc_info=True)
        return None


def build_identifiable_extra(
    *,
    contact: str | None = None,
    free_text: str | None = None,
) -> dict[str, Any] | None:
    """Assemble the identifiable EXTRA -- ONLY when the opt-in is on.

    Returns ``None`` whenever the separate identifiable-extra consent is off, so
    the extra can never be assembled (let alone attached) without opt-in. When
    on, it carries the correlation id + any contact/free-text the caller passes.
    This is the ONLY place identity enters a diagnostic.
    """
    if not diagnostic_consent.may_attach_identifiable_extra():
        return None
    extra: dict[str, Any] = {"consent": "identifiable_extra_opt_in"}
    install_id = _stable_install_id()
    if install_id:
        extra["install_id"] = install_id
    if contact:
        extra["contact"] = str(contact)[:200]
    if free_text:
        extra["context"] = str(free_text)[:2000]
    return extra


def flush_spool() -> int:
    """Send any spooled crash payloads if the baseline is now armed.

    Called at startup (after the disclosure card is shown) and whenever a fresh
    send happens. Returns the number of payloads relayed. Never raises.
    """
    if not diagnostic_consent.is_crash_baseline_armed():
        return 0
    return _flush_spool_locked()


def _flush_spool_locked() -> int:
    """Relay every queued payload, re-queueing the ones that did not send.

    A failed relay must never cost the payload (#4227): the outbox hands them
    over on loan (``claim``) and takes back whatever did not go out
    (``release``). The direct-send path in ``dispatch_crash_diagnostic`` does
    the same thing via ``enqueue``, so both routes out of this module are
    loss-free.
    """
    sent = 0
    unsent: set[int] = set()
    try:
        from diagnostics.diagnostic_relay import can_relay

        if not can_relay():
            # No ingest credential / no cloud URL: the transport is dark, not
            # failing. Leave the queue untouched rather than claiming payloads
            # only to spend a delivery attempt each on a send that cannot even
            # be attempted -- the queue has to survive until it IS provisioned.
            log.debug("diagnostic-dispatch: relay transport unavailable; leaving the spool untouched")
            return 0

        claim_id, payloads = diagnostic_spool.claim()
        # Everything is unsent until a relay positively confirms otherwise, so a
        # KeyboardInterrupt/SystemExit part-way through the loop re-queues the
        # payloads it never reached instead of dropping them with the sent ones.
        unsent = set(range(len(payloads)))
        try:
            for index, payload in enumerate(payloads):
                if _relay(payload, allow_identifiable=True, app_state=None):
                    sent += 1
                    unsent.discard(index)
        finally:
            # The claim id keeps this release pinned to THIS flush's own sidecar,
            # so an overlapping flush can never take back our payloads (or lose
            # its own to us).
            diagnostic_spool.release(claim_id, sorted(unsent))
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-dispatch: spool flush failed", exc_info=True)
    return sent


def _relay(
    payload: dict[str, Any],
    *,
    allow_identifiable: bool,
    app_state: Mapping[str, Any] | None,
) -> bool:
    """Attach the identifiable extra iff opted-in, then hand to the relay."""
    try:
        outgoing = dict(payload)
        if allow_identifiable:
            extra = build_identifiable_extra()
            if extra is not None:
                outgoing["identifiable_extra"] = extra
        from diagnostics.diagnostic_relay import relay_diagnostic

        return bool(relay_diagnostic(outgoing))
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        log.debug("diagnostic-dispatch: relay hand-off failed", exc_info=True)
        return False


def _stable_install_id() -> str:
    try:
        from ui.settings_manager import get_settings_manager

        return str(get_settings_manager().get("telemetry_install_id", "") or "")
    except Exception:  # noqa: BLE001, RUF100 - fail closed; must never raise
        return ""


__all__ = [
    "build_identifiable_extra",
    "dispatch_browser_crash_diagnostic",
    "dispatch_bug_report_minimum",
    "dispatch_crash_diagnostic",
    "flush_spool",
]
