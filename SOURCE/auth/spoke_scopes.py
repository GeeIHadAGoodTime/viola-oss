from __future__ import annotations

_READ_ONLY_METHODS = frozenset({"GET", "HEAD"})

# Narrow, read-only allowlist for UNTRUSTED (cloud) spokes — a remote browser
# authenticated only by a GoTrue JWT marked as a spoke principal. These have no
# physical proximity to the hub, so they stay tightly scoped.
SPOKE_ALLOWED_PREFIXES = (
    ("/api/v1/cloud/conversations", _READ_ONLY_METHODS),
    ("/api/v1/cloud/rooms", _READ_ONLY_METHODS),
    ("/api/v1/rooms", _READ_ONLY_METHODS),
    ("/api/v1/room_groups", _READ_ONLY_METHODS),
    ("/api/v1/devices", _READ_ONLY_METHODS),
    ("/api/v1/multiroom", _READ_ONLY_METHODS),
    ("/api/v1/sync_calibration", _READ_ONLY_METHODS),
    ("/api/v1/clock", _READ_ONLY_METHODS),
    ("/api/v1/network/local-address", _READ_ONLY_METHODS),
    ("/api/v1/audio_stream", _READ_ONLY_METHODS),
    ("/api/v1/aec_telemetry", _READ_ONLY_METHODS),
)

# Genuinely sensitive surfaces — denied for EVERY spoke, paired LAN or cloud.
# A paired LAN spoke is a full hub WINDOW for display + control, but managing
# the account/identity, money, admin, and secret material stays on the hub
# itself. (Paid actions reachable via /v1/command are ALSO independently gated
# by core.account_gate.requires_account_for_command, so allowing command below
# does not widen the money/account blast radius.)
#
# /v1/onboarding is included even though it isn't billing/account/admin/byok
# by name: POST /v1/onboarding/save writes a Tier-3 secret (the user's BYOK
# llm_api_key) straight to SettingsManager/keyring
# (ui/api/routes/onboarding.py:121) with NO secret-key rejection guard —
# unlike PUT /v1/settings, which explicitly 403s any write to a
# `_api_key`/`_token`-suffixed key via `is_user_facing_secret_key`
# (ui/settings_api.py:766). The one-time hub setup wizard has no legitimate
# spoke use case, so it stays hub-owner-only for every spoke, trusted or not.
SPOKE_SENSITIVE_DENIED_PREFIXES = (
    "/auth/",
    "/v1/auth/",
    "/billing",
    "/v1/billing",
    "/api/v1/billing",
    "/admin",
    "/api/v1/admin",
    "/v1/account",
    "/api/v1/account",
    "/api/v1/byok",
    "/api/v1/payment",
    "/v1/connectors",
    "/api/v1/cloud/settings",
    "/v1/onboarding",
    # --- #568 full route-by-route audit: Tier-3 secret + money surfaces that
    # were reachable by a trusted paired LAN spoke because no denied prefix
    # covered them. All are desktop-registered (backend/fastapi_app.py) and
    # each already carries a route-level guard (localhost-only / token /
    # session-cookie), so these are defense-in-depth default-deny gaps, not
    # open holes — but the spoke-scope layer is the STRUCTURAL default-deny
    # enforcement (same reasoning as /v1/onboarding above and the Tier-3 rule
    # in memory/reference_local_only_storage_tier.md: payment vault, OAuth
    # tokens, and browser profiles are desktop-only, NEVER a spoke surface).
    #
    # Payment vault management — ui/api/routes/payment_cards.py mounts
    # /api/payments/cards (save/delete card, change spend ceiling). Writes the
    # Tier-3 encrypted card vault; router-level Depends(_localhost_only) already
    # blocks a LAN peer, but the scope layer must also default-deny it.
    "/api/payments",
    # Payment confirmation / approval gate — ui/api/routes/payment_confirm.py
    # mounts bare /confirm/{token}/... (approve, reject, submit card PAN via
    # /cards + /one-shot-card, mark-reentry, DELETE). Approving a payment or
    # entering a card PAN from a secondary paired device defeats the hub's
    # payment-confirmation control. The card-PAN-entry and secret-bearing
    # approve routes carry the localhost gate, but approve/reject are only
    # token+session-cookie bound, so the scope layer is the structural deny.
    # The /confirm-* siblings below do not match the "/confirm/" prefix (no
    # trailing-slash boundary) so they are listed explicitly.
    "/confirm/",
    "/confirm-test-session",
    "/confirm-preview",
    # Browser-profile provider auth — ui/api/routes/browser_auth.py mounts
    # /v1/browser/auth (login/refresh/set-default a stored provider session).
    # Guarded ONLY by Depends(require_auth), which a trusted spoke satisfies for
    # any method, so before this the scope layer was the ONLY thing that could
    # deny it — and it did not. Browser profiles are Tier-3 desktop-only secrets.
    "/v1/browser/auth",
    # OAuth consent handshake — ui/consent_api.py mounts /v1/consent and
    # /api/v1/consent (create/revoke an OAuth session that grants a provider
    # connector). Guarded ONLY by Depends(require_auth) (trusted-spoke-passable).
    # The connector surface it feeds (/v1/connectors) is already denied above,
    # so allowing a spoke to mint the OAuth session behind it was inconsistent.
    # The auth-EXEMPT OAuth callback (/v1/consent/callback,
    # /api/v1/consent/callback) bypasses this middleware entirely (auth_exempt_paths
    # in ui/security/config.py) and is unaffected; provider *status* reads live
    # under /v1/providers, not here.
    "/v1/consent",
    "/api/v1/consent",
)

# Additional denials that apply ONLY to untrusted (cloud) spokes. A paired LAN
# spoke — which had to read the 6-digit code off the hub screen to pair — is a
# trusted hub window and may drive the agent and change local settings, exactly
# like the desktop. A cloud spoke may not.
_CLOUD_ONLY_DENIED_PREFIXES = ("/v1/command",)

# Backwards-compatible full cloud denylist (sensitive + cloud-only).
SPOKE_DENIED_PREFIXES = SPOKE_SENSITIVE_DENIED_PREFIXES + _CLOUD_ONLY_DENIED_PREFIXES


def _matches_prefix(path: str, prefix: str) -> bool:
    clean_path = path or "/"
    clean_prefix = prefix or "/"
    if clean_prefix.endswith("/"):
        return clean_path == clean_prefix[:-1] or clean_path.startswith(clean_prefix)
    return clean_path == clean_prefix or clean_path.startswith("%s/" % clean_prefix)


def is_spoke_denied(path: str, *, trusted: bool = False) -> bool:
    """True when *path* is off-limits to a spoke.

    ``trusted=True`` is a paired LAN/desktop spoke (a window into the hub); it
    is denied only the genuinely sensitive surfaces. ``trusted=False`` is an
    untrusted cloud spoke; it is additionally denied the agent command path.
    """
    prefixes = SPOKE_SENSITIVE_DENIED_PREFIXES
    if not trusted:
        prefixes = prefixes + _CLOUD_ONLY_DENIED_PREFIXES
    return any(_matches_prefix(path, prefix) for prefix in prefixes)


def is_spoke_path_allowed(path: str, *, trusted: bool = False) -> bool:
    if is_spoke_denied(path, trusted=trusted):
        return False
    if trusted:
        # A paired LAN spoke is 1:1 with the hub: full surface minus the
        # sensitive denylist above.
        return True
    return any(_matches_prefix(path, prefix) for prefix, _methods in SPOKE_ALLOWED_PREFIXES)


def is_spoke_allowed(path: str, method: str, *, trusted: bool = False) -> bool:
    if is_spoke_denied(path, trusted=trusted):
        return False
    if trusted:
        # Paired LAN spoke: same methods as the hub (GET/POST/PUT/...).
        return True
    normalized_method = (method or "").upper()
    return any(
        _matches_prefix(path, prefix) and normalized_method in allowed_methods
        for prefix, allowed_methods in SPOKE_ALLOWED_PREFIXES
    )


__all__ = [
    "SPOKE_ALLOWED_PREFIXES",
    "SPOKE_DENIED_PREFIXES",
    "SPOKE_SENSITIVE_DENIED_PREFIXES",
    "is_spoke_allowed",
    "is_spoke_denied",
    "is_spoke_path_allowed",
]
