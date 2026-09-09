"""Offered-vs-forced update apply policy (LOCKED founder decision 2026-06-12).

This module is the single, installer-agnostic decision point for *whether* an
available desktop update may apply without explicit user consent. It encodes the
LOCKED policy:

    Updates are OFFERED by default, FORCED only by exception.

- **OFFERED** (the default for every ordinary release): the running app may
  download and signature-verify the update, but it applies ONLY after the user
  explicitly accepts. No silent / background auto-apply.
- **FORCED** (the narrow exception): a release flagged ``mandatory`` auto-applies
  WITHOUT waiting for an accept — and ``mandatory`` is permitted ONLY for the
  closed safety / brick class (CVE / auth-bypass / data-integrity push, or an
  install-breaking brick-fix). Everything else is OFFERED.

The most important negative this module enforces, and the one the Ratchet gate
pins: **a NON-mandatory update must NEVER resolve to auto-apply.** A normal
release cannot smuggle a forced apply by setting an unrelated flag — only the
closed, named class of reasons promotes a release to FORCED, and the
``mandatory`` boolean is the manifest's commitment that the publisher classified
it into that closed set (see ``.claude/skills/publish-update`` step 8).

This is plumbing: it reads structured manifest fields and returns a structured
decision. It does no natural-language classification of anything (no boxing of
Viola's model — this never sees a user query or a model reply).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# The closed set of reasons that may promote a release to FORCED (auto-apply
# without user consent). This mirrors the publish-update skill's force-update
# classification (SKILL.md step 8 "Force-update flag") plus the brick-fix class
# named in the LOCKED policy. It is deliberately a *closed* allowlist: a reason
# outside this set can never force an apply, so a routine release cannot smuggle
# a forced apply by inventing a justification.
MANDATORY_REASON_ALLOWLIST: frozenset[str] = frozenset(
    {
        # --- critical-safety push class (publish-update SKILL.md step 8) ---
        "payment_vault_vulnerability",
        "cve",
        "auth_bypass",
        "session_leak",
        "token_confusion",
        "tier1_boundary_violation",
        "credential_exfiltration",
        "byok_exposure",
        "arbitrary_code_execution",
        "installer_integrity_compromise",
        "data_integrity_loss",
        "billing_gate_bypass",
        "active_exploit",
        # --- brick-fix class (LOCKED policy: install-breaking defect) ---
        "brick_fix",
    }
)


class ApplyDisposition(str, Enum):
    """How an available update is permitted to be applied."""

    #: No update is available; nothing to apply.
    NONE = "none"
    #: Default. Download + verify allowed; apply ONLY on explicit user accept.
    OFFERED = "offered"
    #: Exception. Auto-apply without an accept (closed safety / brick class only).
    FORCED = "forced"


@dataclass(frozen=True)
class ApplyDecision:
    """The resolved apply policy for one update-check result.

    ``may_auto_apply`` is the single load-bearing flag: it is True ONLY for a
    FORCED disposition. The OFFERED and NONE dispositions both leave it False,
    which is what makes "a non-mandatory update must not auto-apply" structural
    rather than a convention.
    """

    disposition: ApplyDisposition
    may_auto_apply: bool
    requires_user_consent: bool
    reason: str = ""
    rejected_reasons: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # Defense-in-depth invariant: auto-apply implies FORCED, and FORCED
        # implies no consent requirement. A constructed instance that violates
        # this is a programming error, not a state we ever want to ship.
        if self.may_auto_apply and self.disposition is not ApplyDisposition.FORCED:
            raise ValueError("may_auto_apply is only valid for a FORCED disposition")
        if self.may_auto_apply and self.requires_user_consent:
            raise ValueError("a FORCED auto-apply cannot also require user consent")
        if self.disposition is ApplyDisposition.OFFERED and not self.requires_user_consent:
            raise ValueError("an OFFERED update must require user consent")


def _normalise_reason(value: object) -> str:
    """Lower-case, collapse separators, strip — for allowlist matching."""
    text = str(value or "").strip().lower()
    return text.replace("-", "_").replace(" ", "_")


def _manifest_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _is_mandatory(manifest: dict[str, object]) -> bool:
    """Read the ``mandatory`` commitment from an update-check / manifest dict.

    Looks at the top level and at a nested ``candidate`` block (the public
    manifest carries it under ``candidate.mandatory``; ``check_for_update``
    surfaces it at the top level once it projects). Any truthy spelling counts,
    but ONLY when a valid closed-class reason is also present (see
    ``classify_apply``) does it actually force an apply.
    """
    if _manifest_truthy(manifest.get("mandatory")):
        return True
    candidate = manifest.get("candidate")
    if isinstance(candidate, dict) and _manifest_truthy(candidate.get("mandatory")):
        return True
    return False


def _mandatory_reasons(manifest: dict[str, object]) -> list[str]:
    """Collect declared mandatory reasons from the manifest (top + candidate)."""
    raw: list[object] = []
    for source in (manifest, manifest.get("candidate")):
        if not isinstance(source, dict):
            continue
        value = source.get("mandatory_reason")
        if value is None:
            value = source.get("mandatory_reasons")
        if isinstance(value, (list, tuple)):
            raw.extend(value)
        elif value is not None:
            raw.append(value)
    return [_normalise_reason(item) for item in raw if str(item or "").strip()]


def classify_apply(manifest: dict[str, object]) -> ApplyDecision:
    """Resolve the OFFERED / FORCED / NONE apply disposition for a check result.

    ``manifest`` is the dict returned by ``utils.update_checker.check_for_update``
    (or the equivalent projected public manifest). The decision is:

    1. No update available -> NONE (nothing to apply). ``available`` is the
       authoritative "is there a newer version for this cohort" signal computed
       upstream (rollout / frozen / max_version / downgrade already applied).
    2. ``mandatory`` truthy AND at least one declared reason is in the closed
       allowlist -> FORCED (auto-apply, no consent).
    3. Everything else -> OFFERED (download + verify, apply only on consent).

    Fail-closed bias: if ``mandatory`` is set but NO valid closed-class reason is
    present, the release is treated as OFFERED, and the unrecognised reasons are
    reported in ``rejected_reasons``. A publisher that flags ``mandatory`` without
    naming a real safety/brick reason therefore CANNOT force an apply — the worst
    a misclassified release can do is fall back to the safe, consent-gated path.
    """
    available = _manifest_truthy(manifest.get("available"))
    if not available:
        return ApplyDecision(
            disposition=ApplyDisposition.NONE,
            may_auto_apply=False,
            requires_user_consent=False,
        )

    if not _is_mandatory(manifest):
        return ApplyDecision(
            disposition=ApplyDisposition.OFFERED,
            may_auto_apply=False,
            requires_user_consent=True,
        )

    declared = _mandatory_reasons(manifest)
    allowed = [r for r in declared if r in MANDATORY_REASON_ALLOWLIST]
    rejected = [r for r in declared if r not in MANDATORY_REASON_ALLOWLIST]

    if not allowed:
        # mandatory flag present but no valid closed-class reason -> fail closed
        # to OFFERED. This is the smuggle guard: a normal release cannot force an
        # apply just by setting mandatory=true with no (or a bogus) reason.
        return ApplyDecision(
            disposition=ApplyDisposition.OFFERED,
            may_auto_apply=False,
            requires_user_consent=True,
            rejected_reasons=tuple(rejected),
        )

    return ApplyDecision(
        disposition=ApplyDisposition.FORCED,
        may_auto_apply=True,
        requires_user_consent=False,
        reason=allowed[0],
        rejected_reasons=tuple(rejected),
    )


def may_auto_apply(manifest: dict[str, object]) -> bool:
    """Convenience: True only when the update is FORCED (closed safety/brick class)."""
    return classify_apply(manifest).may_auto_apply


__all__ = [
    "MANDATORY_REASON_ALLOWLIST",
    "ApplyDecision",
    "ApplyDisposition",
    "classify_apply",
    "may_auto_apply",
]
