"""Contribution-program exclusion filter (legal review L10, 2026-05-22).

The LOCKED L10 data posture (``docs/legal/review_2026_05_22/01_DECISIONS.md``)
authorizes an opt-in "Viola Data Contributions" program — off by default,
separate from the Terms, granular, revocable — with **hard exclusions enforced
in code, not merely promised**. The legal posture (``F_industry_and_posture.md``
§2.6) is explicit that "a promised exclusion that the pipeline can violate is an
FTC §5 exposure": the exclusion must be enforced by the contribution pipeline.

Launch posture
==============
As of launch the broad Data Contributions program is **not wired** — no settings
key, route, or collection path exports user conversations / files / transcripts
into a contribution sink. The only data-collection-to-contribution pipeline that
exists is the narrow wake-word audio clip pipeline (``voice/wake_detector/...``),
which is its own contribution category and is itself **launch-excluded** via
``ui/settings_api._WAKE_DATA_CONTRIBUTION_PUBLIC_LAUNCH_ENABLED = False`` (the
"not offered in public launch" carve-out the legal posture says to keep).

So today this filter has nothing live to filter. It exists so the L10 invariant
is enforced **structurally the moment the program ships**:

* Any code that collects/exports user data into a contribution sink MUST route
  the payload through :func:`filter_contribution_payload` (or
  :func:`assert_contribution_safe`) before it crosses the collection/export
  boundary. The filter rejects every excluded data class.
* The companion gate ``scripts/check_contribution_exclusion.py`` fails the build
  if a new contribution sink is added without routing through this filter — the
  requirement is enforced by-gate, inert until the feature lands.

This module is the single source of truth for the excluded-class list. Do not
duplicate the list inside a future contribution pipeline; import it from here so
the gate and the runtime stay in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from core.logging_config import get_logger

logger = get_logger(__name__)


class ExcludedDataClass(str, Enum):
    """Data classes that may NEVER enter the Viola Data Contributions program.

    Sourced verbatim from the L10 LOCKED decision and the F_industry_and_posture
    §2.6 exclusion list. Each member's value is a stable, machine-checkable key.
    """

    PAYMENT_DATA = "payment_data"  # card numbers, security codes, payment vault (PAY-11 / Tier 3)
    AUTH_CREDENTIALS = "auth_credentials"  # OAuth tokens, API keys, BYOK keys, session tokens
    THIRD_PARTY_CONTENT = (
        "third_party_content"  # call transcripts/audio, email/message bodies + recipients of OTHER people
    )
    GOOGLE_USER_DATA = (
        "google_user_data"  # Gmail/Calendar/Drive content governed by Google API Services User Data Policy
    )
    CHILDRENS_DATA = "childrens_data"  # data of a child / known-under-13 user (COPPA)
    PRECISE_LOCATION = "precise_location"  # precise geolocation
    SENSITIVE_CATEGORY = "sensitive_category"  # health, biometric/voiceprint, race, religion, sexual-orientation, etc.
    RAW_USER_FILES = "raw_user_files"  # raw screenshots / files unless explicitly user-selected for contribution


# Frozen tuple form for the gate detector and external callers that want the
# bare list without importing the enum.
EXCLUDED_DATA_CLASSES: tuple[str, ...] = tuple(member.value for member in ExcludedDataClass)


class ContributionExclusionError(Exception):
    """Raised when a contribution payload contains an excluded data class.

    Carries the offending classes so the caller can log/drop precisely. The
    contribution pipeline MUST treat this as fail-closed: drop the payload, never
    "best-effort" strip-and-continue.
    """

    def __init__(self, excluded: Iterable[ExcludedDataClass], detail: str = "") -> None:
        self.excluded = tuple(excluded)
        names = ", ".join(c.value for c in self.excluded)
        msg = f"contribution payload contains excluded data class(es): {names}"
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


@dataclass(frozen=True)
class ContributionDecision:
    """Result of screening a candidate contribution payload."""

    allowed: bool
    excluded: tuple[ExcludedDataClass, ...] = field(default_factory=tuple)
    reasons: tuple[str, ...] = field(default_factory=tuple)


# Field-name substrings that unambiguously mark an excluded class. The match is
# substring-on-lowercased-key so a future pipeline can't smuggle an excluded
# field past the filter by nesting or renaming around the core token. This is a
# DENY list at the boundary, not a query classifier — it screens the structure
# of an export payload, it does not read or branch on user content.
_FIELD_DENY_TOKENS: dict[str, ExcludedDataClass] = {
    # Payment
    "card_number": ExcludedDataClass.PAYMENT_DATA,
    "cardnumber": ExcludedDataClass.PAYMENT_DATA,
    "pan": ExcludedDataClass.PAYMENT_DATA,
    "cvc": ExcludedDataClass.PAYMENT_DATA,
    "cvv": ExcludedDataClass.PAYMENT_DATA,
    "security_code": ExcludedDataClass.PAYMENT_DATA,
    "payment_vault": ExcludedDataClass.PAYMENT_DATA,
    "payment_method": ExcludedDataClass.PAYMENT_DATA,
    "stripe_pm": ExcludedDataClass.PAYMENT_DATA,
    # Credentials
    "oauth_token": ExcludedDataClass.AUTH_CREDENTIALS,
    "access_token": ExcludedDataClass.AUTH_CREDENTIALS,
    "refresh_token": ExcludedDataClass.AUTH_CREDENTIALS,
    "api_key": ExcludedDataClass.AUTH_CREDENTIALS,
    "apikey": ExcludedDataClass.AUTH_CREDENTIALS,
    "byok": ExcludedDataClass.AUTH_CREDENTIALS,
    "session_token": ExcludedDataClass.AUTH_CREDENTIALS,
    "bearer_token": ExcludedDataClass.AUTH_CREDENTIALS,
    "client_secret": ExcludedDataClass.AUTH_CREDENTIALS,
    # Third-party content (other people)
    "call_transcript": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "call_audio": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "call_recording": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "recipient": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "called_party": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "email_body": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "message_body": ExcludedDataClass.THIRD_PARTY_CONTENT,
    "third_party": ExcludedDataClass.THIRD_PARTY_CONTENT,
    # Google user data
    "gmail": ExcludedDataClass.GOOGLE_USER_DATA,
    "google_user_data": ExcludedDataClass.GOOGLE_USER_DATA,
    "calendar_event": ExcludedDataClass.GOOGLE_USER_DATA,
    "drive_file": ExcludedDataClass.GOOGLE_USER_DATA,
    # Children's data
    "child_data": ExcludedDataClass.CHILDRENS_DATA,
    "childrens_data": ExcludedDataClass.CHILDRENS_DATA,
    "minor_data": ExcludedDataClass.CHILDRENS_DATA,
    # Precise location
    "precise_location": ExcludedDataClass.PRECISE_LOCATION,
    "gps_coordinates": ExcludedDataClass.PRECISE_LOCATION,
    "lat_lng": ExcludedDataClass.PRECISE_LOCATION,
    # Sensitive category
    "voiceprint": ExcludedDataClass.SENSITIVE_CATEGORY,
    "biometric": ExcludedDataClass.SENSITIVE_CATEGORY,
    "health_data": ExcludedDataClass.SENSITIVE_CATEGORY,
    "medical": ExcludedDataClass.SENSITIVE_CATEGORY,
    # Raw user files (unless explicitly user-selected — see flag below)
    "raw_screenshot": ExcludedDataClass.RAW_USER_FILES,
    "screenshot": ExcludedDataClass.RAW_USER_FILES,
    "raw_file": ExcludedDataClass.RAW_USER_FILES,
}

# A boolean payload flag that, when explicitly True, marks a file as user-selected
# for contribution. Raw files are excluded by default; this is the only escape
# hatch, and it must be an explicit, affirmative per-item user action.
_USER_SELECTED_FLAG = "user_selected_for_contribution"


def _flatten_keys(payload: Mapping[str, Any], _prefix: str = "") -> Iterable[str]:
    """Yield every (dotted) key in a nested mapping/sequence payload."""
    for key, value in payload.items():
        dotted = f"{_prefix}{key}"
        yield dotted
        if isinstance(value, Mapping):
            yield from _flatten_keys(value, f"{dotted}.")
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    yield from _flatten_keys(item, f"{dotted}.")


def screen_contribution_payload(payload: Mapping[str, Any]) -> ContributionDecision:
    """Screen a candidate contribution payload against the excluded-class list.

    Returns a :class:`ContributionDecision`. ``allowed`` is True only when NO
    excluded data class is present. This is a structural DENY filter on the
    payload's field names — it does not classify or branch on user content.

    Raw-file fields are excluded UNLESS the payload carries an explicit
    ``user_selected_for_contribution=True`` flag (the one §2.6 escape hatch for
    user-selected files).
    """
    user_selected = bool(payload.get(_USER_SELECTED_FLAG, False))

    found: dict[ExcludedDataClass, str] = {}
    for key in _flatten_keys(payload):
        leaf = key.rsplit(".", 1)[-1].lower()
        for token, data_class in _FIELD_DENY_TOKENS.items():
            if token in leaf:
                if data_class is ExcludedDataClass.RAW_USER_FILES and user_selected:
                    # User explicitly selected this file for contribution.
                    continue
                found.setdefault(data_class, key)

    if not found:
        return ContributionDecision(allowed=True)

    excluded = tuple(found.keys())
    reasons = tuple(f"{cls.value}: field '{field_key}'" for cls, field_key in found.items())
    return ContributionDecision(allowed=False, excluded=excluded, reasons=reasons)


def filter_contribution_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return ``payload`` unchanged if it is contribution-safe; raise otherwise.

    This is the boundary call a contribution sink uses: it fails closed on any
    excluded class rather than silently stripping fields (silent stripping hides
    pipeline bugs and risks shipping a partial excluded payload).
    """
    decision = screen_contribution_payload(payload)
    if not decision.allowed:
        logger.warning(
            "contribution payload rejected by exclusion filter: %s",
            "; ".join(decision.reasons),
        )
        raise ContributionExclusionError(decision.excluded, detail="; ".join(decision.reasons))
    return payload


def assert_contribution_safe(payload: Mapping[str, Any]) -> None:
    """Raise :class:`ContributionExclusionError` if ``payload`` is not safe.

    Thin wrapper around :func:`filter_contribution_payload` for call sites that
    only want the assertion semantics.
    """
    filter_contribution_payload(payload)


def is_contribution_program_wired() -> bool:
    """Return whether a live Data Contributions sink exists at this version.

    LAUNCH POSTURE: ``False``. The broad program is not wired; the wake-word
    clip pipeline is launch-excluded. This is the single switch a future
    program-enablement change flips, alongside building the actual sink that
    routes through :func:`filter_contribution_payload`.
    """
    return False


__all__ = [
    "EXCLUDED_DATA_CLASSES",
    "ContributionDecision",
    "ContributionExclusionError",
    "ExcludedDataClass",
    "assert_contribution_safe",
    "filter_contribution_payload",
    "is_contribution_program_wired",
    "screen_contribution_payload",
]
