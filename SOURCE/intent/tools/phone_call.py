"""Phone call tool for the agent.

Allows Viola to make outbound phone calls on behalf of users —
booking appointments, placing orders, checking availability, etc.

Hosted installations use the company cloud API and fail closed on cloud errors.
Independent public installations can explicitly select local mode and supply
their own provider credentials, signed callback endpoint and media ingress.
Local mode is never a fallback after a hosted call failure.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import time
from typing import Any

import httpx
from telephony.caller_validation import is_assistant_caller_name, validate_caller_name

from config.defaults import DEFAULT_PHONE_MODEL
from config.settings import settings
from core.constants import TIMEOUT_VERY_LONG
from core.logging_config import get_logger
from core.product import plan_family_for_id
from core.request_context import resolve_runtime_plan_id
from intent.tool_types import ToolResult
from intent.tools.toll_fraud_prefixes import match_toll_fraud_prefix

logger = get_logger(__name__)

_SESSION_TOKEN_ENV_CANDIDATES = (
    "VIOLA_GOTRUE_SESSION_TOKEN",
    "VIOLA_PREFLIGHT_GOTRUE_ACCESS_TOKEN",
)


def _normalize_session_token(value: object) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


# A GoTrue access token must have at least this many seconds of life left before
# we will forward it to the cloud as a bearer. A token that has already expired
# (or expires within the skew) is rejected so the cloud never sees a dead bearer
# and answers 401 -> the desktop proxy then 500s on the missing ``ok`` key.
_BEARER_FRESHNESS_SKEW_SECONDS = 60


def _jwt_exp_epoch(token: str) -> int | None:
    """Return a JWT's ``exp`` claim (epoch seconds), or ``None`` if not decodable.

    Decodes the unverified payload segment only — signature verification is the
    cloud's job. We need the expiry purely to avoid forwarding a stale bearer.
    """
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    segment += "=" * (-len(segment) % 4)
    try:
        decoded = base64.urlsafe_b64decode(segment.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    return exp if isinstance(exp, int) and exp > 0 else None


def _bearer_is_fresh(token: str) -> bool:
    """True when ``token`` is a GoTrue JWT that is still comfortably valid.

    This is the PREFERENCE gate: it decides whether to keep looking for a
    source that can mint something newer. It is deliberately conservative and
    is NOT the floor below which a bearer is unusable — see
    :func:`_bearer_is_live`.

    Fail closed: a token whose ``exp`` cannot be decoded is treated as NOT fresh,
    because we cannot prove it is live and the cloud bearer is always a GoTrue JWT.
    """
    if not token:
        return False
    exp = _jwt_exp_epoch(token)
    if exp is None:
        return False
    return exp > (time.time() + _BEARER_FRESHNESS_SKEW_SECONDS)


# The floor below which a bearer is genuinely dead. A usable bearer only has to
# survive the outbound hop, which on the cloud surface is a loopback POST to our
# own API — milliseconds, not minutes.
_BEARER_MINIMUM_LIFE_SECONDS = 2


def _bearer_is_live(token: str) -> bool:
    """True when ``token`` is a GoTrue JWT that has not expired yet.

    Strictly weaker than :func:`_bearer_is_fresh`. Freshness answers *"should I
    go look for a newer token?"*; liveness answers *"would sending this one
    actually authenticate?"*. Keeping them separate is the whole point: on the
    cloud surface there IS no newer token to go find, so treating the freshness
    preference as a hard veto turned a live credential into no credential.

    Fail closed on an undecodable ``exp``, exactly as freshness does.
    """
    if not token:
        return False
    exp = _jwt_exp_epoch(token)
    if exp is None:
        return False
    return exp > (time.time() + _BEARER_MINIMUM_LIFE_SECONDS)


# ---------------------------------------------------------------------------
# Emergency and premium-rate number blocklists
# ---------------------------------------------------------------------------

# Emergency numbers (must NEVER be called)
_EMERGENCY_NUMBERS = {
    "911",
    "988",
    "112",
    "999",
    "000",
    "110",
    "119",
    "122",  # Global emergency
    "+1911",
    "+1988",
    "+44999",
    "+61000",
    "+49110",
    "+81119",
}

# Emergency short patterns (catch with/without country code)
_EMERGENCY_PATTERNS = [
    re.compile(r"^\+?1?911$"),  # US/Canada 911
    re.compile(r"^\+?1?988$"),  # US Suicide and Crisis Lifeline
    re.compile(r"^\+?44999$"),  # UK 999
    re.compile(r"^\+?61000$"),  # Australia 000
    re.compile(r"^\+?49110$"),  # Germany 110
    re.compile(r"^\+?33112$"),  # France 112
    re.compile(r"^112$"),  # EU universal
]

# Premium-rate / satellite / IRSF destinations (toll-fraud risk) are matched via
# the structured, sourced prefix dataset in intent/tools/toll_fraud_prefixes.py
# (longest-prefix match) — covering satellite networks and national premium
# ranges that the old 6-entry list (SEC-060) let through.

# Short codes and directory services
_BLOCKED_SHORT_CODES = {"411", "611", "711", "511", "311", "211"}

_CRISIS_LIFELINE_ADVICE = (
    "Do not place this call. Encourage the user to dial or text 988 directly. "
    "Offer to stay present, talk through what is going on, help reach a friend or family member, "
    "or guide a calming exercise."
)
_EMERGENCY_NUMBER_ADVICE = (
    "Do not place this call. Encourage the user to dial emergency services directly. "
    "For immediate danger in the US, tell them to dial 911. Offer to stay present, "
    "help reach a friend or family member, or talk through immediate next steps."
)


def _is_blocked_number(number: str) -> str | None:
    """Check if a number is blocked. Returns reason string or None."""
    cleaned = re.sub(r"[\s\-\(\)]", "", number)
    cleaned_digits = cleaned.lstrip("+")

    # Check emergency numbers
    for pattern in _EMERGENCY_PATTERNS:
        if pattern.match(cleaned):
            if cleaned_digits.lstrip("1") == "988":
                return "crisis_lifeline_redirect"
            return "emergency_number_blocked"
    if cleaned_digits in {n.lstrip("+") for n in _EMERGENCY_NUMBERS}:
        if cleaned_digits.lstrip("1") == "988":
            return "crisis_lifeline_redirect"
        return "emergency_number_blocked"

    # Check premium-rate / satellite / IRSF ranges (structured dataset).
    if match_toll_fraud_prefix(cleaned) is not None:
        return "premium_rate_blocked"

    # Check short codes
    if cleaned in _BLOCKED_SHORT_CODES:
        return "short_code_blocked"

    return None


def _blocked_number_payload(reason: str) -> dict[str, object]:
    advice = "Do not place this call. Explain briefly that this number cannot be called for safety reasons."
    if reason == "crisis_lifeline_redirect":
        advice = _CRISIS_LIFELINE_ADVICE
    elif reason == "emergency_number_blocked":
        advice = _EMERGENCY_NUMBER_ADVICE
    return {
        "blocked": True,
        "reason": reason,
        "advice_for_assistant": advice,
    }


def blocked_call_payload_for_number(number: str) -> dict[str, object] | None:
    """Return structured blocked-call guidance for unsafe destination numbers."""
    reason = _is_blocked_number(number)
    if reason is None:
        return None
    return _blocked_number_payload(reason)


# Cloud mode has its own server-side per-user rate limiting (see
# telephony/cloud_routes.py PerUserRateLimiter, scope "phone.call"). The
# desktop tool no longer runs a local pipeline, so there is no local-mode
# rate-limit ledger here — the cloud is the single rate-limit authority.


# ---------------------------------------------------------------------------
# Mode resolution — CLOUD ONLY (PHONE-CLOUD-ONLY, 2026-06-18)
# ---------------------------------------------------------------------------
#
# The production phone-call tool is cloud-only and fails closed. There is no
# desktop "local mode" dispatch and therefore no mode-resolution branch: a
# call request ALWAYS takes the cloud path, and if the cloud cannot be used it
# returns a clear error rather than running a Telnyx/Pipecat pipeline on the
# desktop. Running that pipeline locally drops Telnyx control webhooks (they
# land in the cloud), so answering-machine detection / hangup events are lost
# (calls never hang up after voicemail -> credit burn), and single-GIL
# contention garbles real-time audio. ``VIOLA_PHONE_MODE`` / ``phone_mode``
# no longer steer the user-facing tool toward a local pipeline.


def _resolve_cloud_url() -> str:
    """Return the cloud backend URL, or empty string if unset."""
    return (getattr(settings, "cloud_url", "") or "").rstrip("/")


def _resolve_session_token() -> str:
    """Return the configured desktop session token for cloud API authentication.

    The stored desktop token may be a local opaque desktop session token; callers
    must pass it through ``_resolve_cloud_bearer_token`` before sending it to the
    cloud API.
    """
    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        token = _normalize_session_token(sm.get("session_token", ""))
        if token:
            return token
    except Exception:
        logger.debug("Phone auth token setting lookup failed")

    for env_name in _SESSION_TOKEN_ENV_CANDIDATES:
        token = _normalize_session_token(os.environ.get(env_name, ""))
        if token:
            return token

    return ""


def _is_desktop_local_session_token(token: str) -> bool:
    try:
        from auth.desktop_session import is_desktop_local_session_token
    except ImportError:
        logger.debug("Desktop local session token shape lookup unavailable")
        return token.startswith("vls_")

    return is_desktop_local_session_token(token)


async def _desktop_access_token_for_session_token(session_token: str) -> str | None:
    from auth.desktop_session import desktop_access_token_for_session_token

    return await desktop_access_token_for_session_token(session_token)


async def _desktop_access_token_for_active_session() -> str | None:
    from auth.desktop_session import desktop_access_token_for_active_session

    return await desktop_access_token_for_active_session()


def _active_desktop_session_lookup_allowed() -> bool:
    try:
        from config.settings import settings

        return str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() != "cloud"
    except (AttributeError, ImportError, TypeError, ValueError):
        return False


def _request_scoped_cloud_bearer() -> str:
    """Return the bearer the auth middleware bound for THIS request, if any.

    Read as its own step because it is needed twice: once as the preferred
    source, and once as the last resort in :func:`_resolve_cloud_bearer_token`.
    """
    try:
        from core.user_context import get_current_cloud_access_token

        return _normalize_session_token(get_current_cloud_access_token())
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.debug("Phone request-scoped cloud bearer lookup failed", exc_info=True)
        return ""


def _last_resort_request_bearer(request_bearer: str) -> str:
    """Return *request_bearer* when no source could beat it and it is still live.

    GitHub #584. The freshness skew exists so a source that CAN mint a newer
    token gets the chance to. On the cloud surface no such source exists — the
    desktop session store is disabled there and the ``session_token`` setting is
    absent from the cloud image — so the request-scoped bearer is the only one
    there is. Discarding it inside the skew therefore does not "fall through to
    something better", it sends no credential at all, and the phone route answers
    401. The user then reads
    ``"Cloud authentication failed. Please log in again."`` while signed in with
    a token the middleware accepted moments earlier on this very request.

    Live-proven on prod 2026-07-29 (``_diag/2026-07-29/c335_phone_confirm_live_proof.md``
    run 2): prod's GoTrue access-token TTL is 300 s against a 60 s skew, so the
    last 20% of every token's life is a guaranteed dial failure for a correctly
    signed-in user.

    Liveness, not freshness, is the real floor: a bearer only has to outlive the
    outbound hop. An expired bearer is still refused, so the documented "the
    cloud never sees a dead bearer" invariant is intact.
    """
    if not _bearer_is_live(request_bearer):
        return ""
    logger.info(
        "Phone cloud bearer: no source supplied a comfortably-fresh token; "
        "forwarding this request's own still-valid bearer (#584)"
    )
    return request_bearer


async def _resolve_cloud_bearer_token() -> str:
    """Return a usable GoTrue bearer token for the cloud phone API.

    Sources are tried in preference order and each is freshness-gated, so a
    source that can mint a newer token gets the chance to before an older one is
    used. When NO source clears that bar, the request's own bearer is used
    anyway provided it has not actually expired — see
    :func:`_last_resort_request_bearer` for why that last step exists.

    A genuinely dead bearer is still never forwarded (it would 401, and the
    desktop proxy would then rewrite the cloud's non-enveloped body into a 500);
    with nothing live to send we return "" so the caller fails closed with a
    clear auth error.
    """

    # 1) Request-scoped token bound by the auth middleware. It was validated at
    #    bind time, but guard against a stale bind (e.g. a raw token attached by
    #    a non-refreshing path) before forwarding it onward.
    request_bearer = _request_scoped_cloud_bearer()
    if request_bearer and _bearer_is_fresh(request_bearer):
        return request_bearer

    # 2) Newest desktop session — ``active_access_token`` refreshes on expiry, so
    #    this is the path that mints a fresh token when one is stale.
    if _active_desktop_session_lookup_allowed():
        try:
            active_access_token = _normalize_session_token(await _desktop_access_token_for_active_session())
            if active_access_token and _bearer_is_fresh(active_access_token):
                return active_access_token
        except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
            logger.debug("Phone active desktop session bearer lookup failed", exc_info=True)

    # 3) The configured ``session_token`` setting / env fallback.
    token = _resolve_session_token()
    if not token:
        return _last_resort_request_bearer(request_bearer)

    if _is_desktop_local_session_token(token):
        # 3a) An opaque ``vls_`` handle resolves (and refreshes) through the store.
        try:
            from auth.desktop_session import DesktopSessionError
        except ImportError:
            logger.debug("Desktop local session token translation unavailable")
            return _last_resort_request_bearer(request_bearer)

        try:
            access_token = _normalize_session_token(await _desktop_access_token_for_session_token(token))
            if access_token and _bearer_is_fresh(access_token):
                return access_token
        except DesktopSessionError:
            logger.debug("Desktop local phone session token translation failed", exc_info=True)
        return _last_resort_request_bearer(request_bearer)

    # 3b) A raw GoTrue JWT stored directly. There is no refresh token bound to it,
    #     so it is preferred only while still fresh; a stale one falls through to
    #     the last-resort step rather than being forwarded as a dead bearer.
    if _bearer_is_fresh(token):
        return token
    logger.debug("Phone cloud bearer: stored raw GoTrue token is expired/near-expiry; skipping")
    return _last_resort_request_bearer(request_bearer)


# ---------------------------------------------------------------------------
# Server-side / desktop-control CallManager singleton
# ---------------------------------------------------------------------------
# The independent local tool also uses this manager. ``_get_manager`` is
# the in-process CallManager used by the cloud server's own control surfaces
# (``telephony/routes.py``, ``telephony/listen_ws.py``, ``phone_transmit``).
# Hosted tools continue to use the company cloud API.
_manager = None


def _get_manager():
    """Get or create the in-process CallManager singleton.

    Used by local control/listen routes and the independent local phone tool.
    """
    global _manager
    if _manager is not None:
        return _manager

    api_key = getattr(settings, "telnyx_api_key", "") or ""
    phone_number = getattr(settings, "telnyx_phone_number", "") or ""
    sip_id = getattr(settings, "telnyx_sip_connection_id", "") or ""
    openai_key = getattr(settings, "openai_api_key", "") or ""

    if not all([api_key, phone_number, sip_id]):
        return None

    # Reading empty queue/active state must not import the audio/ML pipeline
    # when this installation has never configured a carrier.
    from telephony.call_manager import CallManager, preload_phone_stt
    from telephony.config import TelnyxConfig

    # Local mode + Cloudflare tunnel for Telnyx media streaming.
    public_ws = getattr(settings, "telnyx_public_ws_url", "") or ""

    config = TelnyxConfig(
        api_key=api_key,
        phone_number=phone_number,
        sip_connection_id=sip_id,
        openai_api_key=openai_key,
        llm_model=DEFAULT_PHONE_MODEL,
        mode="local",
        public_ws_url=public_ws,
        public_webhook_url=getattr(settings, "telnyx_public_webhook_url", "") or "",
        stream_shared_secret=getattr(settings, "telnyx_stream_shared_secret", "") or "",
        # A local transport owns one listening port. Additional calls queue.
        max_concurrent_calls=1,
        # ws_port/STT model/compute type come from TelnyxConfig defaults.
        # tts_voice intentionally omitted: TelnyxConfig resolves it per-provider
        # (Piper -> en_US-lessac-medium) so the Piper path never gets a Kokoro voice.
        audio_recording_enabled=True,
    )

    _manager = CallManager(config)
    from telephony.usage import company_phone_billing_available

    if company_phone_billing_available():
        preload_phone_stt(config)
    return _manager


def _uses_independent_local_phone() -> bool:
    """Select only an explicitly configured independent local installation."""
    from telephony.phone_mode import phone_mode_is_cloud
    from telephony.usage import company_phone_billing_available

    return not phone_mode_is_cloud() and not company_phone_billing_available()


def _local_phone_manager():
    """Require secure ingress before starting an independently funded call."""
    from urllib.parse import urlparse

    for field, scheme in (("telnyx_public_ws_url", "wss"), ("telnyx_public_webhook_url", "https")):
        value = urlparse(getattr(settings, field, "") or "")
        if value.scheme != scheme or not value.hostname or value.username or value.password or value.fragment:
            raise ValueError("Configure %s with a public %s URL before calling." % (field, scheme))
    for field in ("telnyx_stream_shared_secret", "telnyx_webhook_public_key", "openai_api_key"):
        if not (getattr(settings, field, "") or "").strip():
            raise ValueError("Configure %s before calling." % field)
    manager = _get_manager()
    if manager is None:
        raise ValueError("Configure the Telnyx API key, outbound number, and connection ID before calling.")
    return manager


async def _make_phone_call_local(**kwargs) -> ToolResult:
    """Use the same owner, consent, safety, queue and accounting gates locally."""
    user_id = _resolve_user_id()
    if not user_id:
        return ToolResult(ok=False, error="Authentication required for phone calling.")
    wait_for_completion = kwargs.pop("wait_for_completion")
    try:
        manager = _local_phone_manager()
        record = await manager.make_call(**kwargs, user_id=user_id, issuer_channel=_resolve_issuer_channel())
    except (ValueError, RuntimeError) as exc:
        return ToolResult(ok=False, error=str(exc), retryable=False)
    if record.status.value == "queued":
        return ToolResult(
            ok=True,
            data={
                "call_id": record.call_id,
                "queue_id": record.queue_id,
                "status": "queued",
                "position": record.queue_position,
            },
        )
    status = manager.get_status(record.call_id, user_id=user_id)
    if not wait_for_completion:
        return ToolResult(ok=True, data=status)
    deadline = asyncio.get_running_loop().time() + manager.config.max_call_duration + 120
    while status.get("status") not in _CLOUD_TERMINAL_STATUSES:
        if asyncio.get_running_loop().time() >= deadline:
            return ToolResult(
                ok=False,
                error="Call outcome is not yet known. Check this call ID before retrying.",
                retryable=False,
                unverified=True,
                data=status,
            )
        await asyncio.sleep(1)
        status = manager.get_status(record.call_id, user_id=user_id)
    transcript = manager.get_transcript(record.call_id, user_id=user_id)
    reached = status.get("status") in {"completed", "voicemail"}
    return ToolResult(
        ok=reached,
        error=None if reached else "The call ended without reaching the person.",
        retryable=False,
        data={**status, **transcript},
    )


def _resolve_user_id() -> str:
    """Resolve the authenticated user id for phone-call ownership."""
    try:
        from core.user_context import get_current_user_id
    except ImportError:
        logger.debug("Phone call: user_context unavailable", exc_info=True)
        return ""

    try:
        return (get_current_user_id() or "").strip()
    except LookupError:
        return ""


def _paid_phone_call_account_gate_result() -> ToolResult | None:
    user_id = _resolve_user_id()
    try:
        from core.account_gate import (
            paid_action_login_required,
            paid_action_login_required_data,
        )

        if not paid_action_login_required(user_id):
            return None
        data = paid_action_login_required_data(
            action="phone_call",
            message="Sign in to make phone calls.",
        )
        data["user_id"] = user_id
        return ToolResult(
            ok=False,
            error=data["message"],
            data=data,
            error_category="LOGIN_REQUIRED",
        )
    except Exception:
        logger.exception("Phone call account gate lookup failed closed")
        return ToolResult(
            ok=False,
            error="Sign in to make phone calls.",
            data={
                "error_code": "login_required_for_paid_action",
                "message": "Sign in to make phone calls.",
                "action": "phone_call",
                "user_id": user_id,
            },
            error_category="LOGIN_REQUIRED",
        )


def _phone_tos_required_result(message: str) -> ToolResult:
    from core.account_gate import PHONE_TOS_REQUIRED

    return ToolResult(
        ok=False,
        error=message,
        data={
            "error_code": PHONE_TOS_REQUIRED,
            "message": message,
            "action": "phone_call",
            "tos_status_url": "/v1/phone/tos-status",
            "accept_tos_url": "/v1/phone/accept-tos",
        },
        error_category="PHONE_TOS_REQUIRED",
    )


def _cloud_tos_required_result(payload: dict[str, Any]) -> ToolResult | None:
    error = payload.get("error")
    code = ""
    message = ""
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    elif isinstance(error, str):
        message = error
    code = code or str(payload.get("error_code") or payload.get("code") or "")
    message = message or str(payload.get("message") or "")

    if code == "phone_tos_required" or "Phone Calling Terms of Service" in message:
        return _phone_tos_required_result(
            message or "Please accept the Phone Calling Terms of Service to start making calls."
        )
    return None


def _cloud_error_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_user_tier(user_id: str | None = None) -> str:
    """Resolve the canonical plan family for phone-call billing gates.

    Missing user identity is handled by the phone tool boundary as an auth
    failure; this helper only maps a concrete user to a billing family.
    """
    user_id = (user_id or _resolve_user_id()).strip()
    if not user_id:
        return "free"

    try:
        plan_id = resolve_runtime_plan_id(user_id)
        return plan_family_for_id(plan_id).value
    except Exception:
        logger.debug("Falling back to free phone-call plan family")
        return "free"


def _resolve_outbound_caller_name(caller_name: str) -> str:
    """Resolve the owner/on-behalf-of display name before cloud dispatch."""

    candidate = validate_caller_name(caller_name)
    if candidate and not is_assistant_caller_name(candidate):
        return candidate

    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        for key in ("user_name", "full_name"):
            owner_name = validate_caller_name(str(sm.get(key, "") or ""))
            if owner_name and not is_assistant_caller_name(owner_name):
                return owner_name
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError):
        logger.debug("Phone caller-name setting lookup failed")

    return "the user"


def _resolve_issuer_channel() -> object | None:
    try:
        from messaging.channel import get_request_channel

        channel = get_request_channel()
        if channel is not None:
            return channel
    except Exception:
        logger.debug("Phone call: request channel unavailable")

    try:
        from core.user_context import get_current_user_id
    except Exception:
        return None

    try:
        user_id = get_current_user_id()
    except LookupError:
        logger.debug("Phone call: no ambient user; refusing cross-tenant channel fallback")
        return None
    except Exception:
        return None

    try:
        from intent.agent_executor import get_active_executor

        executor = get_active_executor(user_id=user_id)
        if executor is not None:
            return getattr(executor, "_channel", None)
    except Exception:
        logger.debug("Phone call: active executor channel unavailable")
    return None


def _issuer_channel_info(channel: object | None) -> dict[str, Any]:
    if channel is None:
        return {}

    info: dict[str, Any] = {
        "channel_type": str(getattr(channel, "channel_type", "") or "").strip(),
        "channel_class": type(channel).__name__,
    }
    for attr in (
        "session_id",
        "chat_id",
        "_chat_id",
        "room_id",
        "_room_id",
        "user_id",
        "_user_id",
        "phone_number",
        "caller_number",
        "email",
        "email_address",
    ):
        value = getattr(channel, attr, None)
        if value not in (None, ""):
            info[attr.lstrip("_")] = str(value)
    active_delivery = getattr(channel, "active_delivery", None)
    if active_delivery is not None:
        info["active_delivery"] = bool(active_delivery)
    return {key: value for key, value in info.items() if value not in (None, "")}


# ---------------------------------------------------------------------------
# Cloud-mode helpers
# ---------------------------------------------------------------------------

_CLOUD_POLL_INTERVAL = 3.0  # seconds between status polls
_CLOUD_POLL_TIMEOUT = 660.0  # max wait (10 min call + 1 min grace)

# Terminal call statuses the cloud /api/phone/status endpoint can report. This
# MUST cover every CallStatus the server treats as terminal
# (telephony.call_manager._TERMINAL_CALL_STATUSES ->
# completed/failed/timeout/no_answer/voicemail/cancelled). A drifted set here is
# a real mis-report: when this list omitted no_answer and voicemail, a cloud
# call that got no answer, or that Viola successfully left a voicemail on, was
# never recognized as terminal — the poll loop spun for the full
# _CLOUD_POLL_TIMEOUT and then reported a synthetic "Polling timed out" to the
# user for a call that had actually reached a legitimate terminal state. "error"
# and "hangup" are kept as defensive extras for any non-CallStatus error
# envelope the endpoint might surface. Anchored to the server's enum values, not
# hand-maintained ad hoc.
_CLOUD_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "timeout",
        "no_answer",
        "voicemail",
        "cancelled",
        "error",
        "hangup",
    }
)

# Terminal statuses for which fetching the transcript/summary is worthwhile — the
# recipient was reached (completed), Viola left a message (voicemail), or the
# desktop stopped waiting while the call was still live (timeout). no_answer is
# included so a genuinely empty transcript is fetched rather than silently
# skipped; the endpoint simply returns nothing when there's no conversation.
_CLOUD_TRANSCRIPT_STATUSES = frozenset({"completed", "timeout", "voicemail", "no_answer"})


def _cloud_headers(token: str) -> dict[str, str]:
    """Build HTTP headers for cloud API requests."""
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    return headers


def _unwrap_cloud_response_payload(payload: Any) -> dict[str, Any]:
    """Flatten canonical cloud response envelopes into phone-tool payloads."""
    if not isinstance(payload, dict):
        return {}

    data = payload.get("data")
    if payload.get("ok") is True and isinstance(data, dict):
        return {"ok": True, **data}

    if payload.get("ok") is False:
        unwrapped: dict[str, Any] = {"ok": False}
        if isinstance(data, dict):
            unwrapped.update(data)
        error = payload.get("error")
        if isinstance(error, dict):
            unwrapped["error"] = str(error.get("message") or error.get("code") or "Cloud request failed.")
        elif error:
            unwrapped["error"] = str(error)
        else:
            unwrapped["error"] = "Cloud request failed."
        return unwrapped

    return payload


async def _cloud_make_call(
    cloud_url: str,
    token: str,
    phone_number: str,
    task: str,
    caller_name: str,
    extra_context: str,
    issuer_channel_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST to cloud API to initiate a phone call.

    Returns the parsed JSON response body.

    Raises:
        httpx.HTTPStatusError: on 4xx/5xx responses.
        httpx.ConnectError: cloud unreachable.
    """
    headers = _cloud_headers(token)
    # Test-induction knob: VIOLA_PHONE_DIAL_TIMEOUT_OVERRIDE lowers ONLY this
    # dial POST's timeout so a live proof run can force the
    # httpx.TimeoutException -> "call_placed_connecting" branch in
    # _make_phone_call_cloud (item-5 grader-taxonomy proof). Harmless in
    # production: unset (None) keeps TIMEOUT_VERY_LONG byte-identical, and the
    # poll/status/transcript/hangup paths are never affected.
    dial_timeout = settings.phone_dial_timeout_override
    if dial_timeout is None:
        dial_timeout = TIMEOUT_VERY_LONG
    async with httpx.AsyncClient(timeout=dial_timeout) as client:
        resp = await client.post(
            "%s/api/phone/call" % cloud_url,
            headers=headers,
            json={
                "phone_number": phone_number,
                "task": task,
                "caller_name": caller_name,
                "extra_context": extra_context,
                "issuer_channel_info": issuer_channel_info or {},
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _cloud_poll_status(
    cloud_url: str,
    token: str,
    call_id: str,
) -> dict[str, Any]:
    """Poll cloud API until the call reaches a terminal status.

    Terminal statuses: see ``_CLOUD_TERMINAL_STATUSES`` (kept in sync with the
    server's ``CallStatus`` terminal set — completed, failed, timeout,
    no_answer, voicemail, cancelled).

    Returns the final status response body.
    """
    terminal = _CLOUD_TERMINAL_STATUSES
    headers = _cloud_headers(token)
    start = time.monotonic()

    async with httpx.AsyncClient(timeout=TIMEOUT_VERY_LONG) as client:
        while time.monotonic() - start < _CLOUD_POLL_TIMEOUT:
            resp = await client.get(
                "%s/api/phone/status" % cloud_url,
                headers=headers,
                params={"call_id": call_id},
            )
            resp.raise_for_status()
            data = _unwrap_cloud_response_payload(resp.json())
            status = data.get("status", "")
            if status in terminal:
                return data
            await asyncio.sleep(_CLOUD_POLL_INTERVAL)

    # Timed out waiting for the call to complete
    return {
        "ok": False,
        "call_id": call_id,
        "status": "timeout",
        "error": "Polling timed out",
    }


async def _cloud_get_transcript(
    cloud_url: str,
    token: str,
    call_id: str,
) -> dict[str, Any]:
    """Fetch transcript and summary from the cloud API."""
    headers = _cloud_headers(token)
    async with httpx.AsyncClient(timeout=TIMEOUT_VERY_LONG) as client:
        resp = await client.get(
            "%s/api/phone/transcript" % cloud_url,
            headers=headers,
            params={"call_id": call_id},
        )
        resp.raise_for_status()
        return _unwrap_cloud_response_payload(resp.json())


async def _cloud_hangup(
    cloud_url: str,
    token: str,
    call_id: str,
) -> dict[str, Any]:
    """Tell the cloud to hang up a call."""
    headers = _cloud_headers(token)
    async with httpx.AsyncClient(timeout=TIMEOUT_VERY_LONG) as client:
        resp = await client.post(
            "%s/api/phone/hangup" % cloud_url,
            headers=headers,
            json={"call_id": call_id},
        )
        resp.raise_for_status()
        return resp.json()


# --- E.164 plausibility (no external deps) ---

# Country codes that don't exist or are reserved/unassigned.
_INVALID_CC_PREFIXES = frozenset({"0", "00", "999", "998", "997", "996", "995"})

# Valid single-digit country codes: 1 (NANP), 7 (Russia/Kazakhstan).
# Two-digit: 20-69. Three-digit: 200-999. We just block obviously impossible ones.
_ASCENDING = "01234567890123456789"  # doubled for wrap-around substring matching
_DESCENDING = "98765432109876543210"


def _is_plausible_e164(digits: str) -> bool:
    """Check whether *digits* (the part after '+') look like a real phone number.

    Rejects: all-same digits, ascending/descending runs, repeating short
    patterns (e.g. 123123123), and impossible country code prefixes.
    No external library required.
    """
    if not digits or not digits.isdigit() or not 8 <= len(digits) <= 15:
        return False

    # Block impossible country code prefixes (leading 0, reserved 99x, etc.)
    if digits[0] == "0":
        return False
    if digits[:3] in _INVALID_CC_PREFIXES:
        return False

    # All identical digits: +11111111111
    if len(set(digits)) == 1:
        return False

    # Too few unique digits for the length (e.g. 12121212121 — only 2 unique in 11)
    if len(set(digits)) <= 2 and len(digits) >= 8:
        return False

    # Ascending or descending run (check against doubled strings for wrap)
    if digits in _ASCENDING or digits in _DESCENDING:
        return False

    # Repeating short pattern: 123412341234, 121212121212, etc.
    for period in range(1, len(digits) // 2 + 1):
        pattern = digits[:period]
        if pattern * (len(digits) // period) == digits[: period * (len(digits) // period)]:
            # The entire number (minus trailing partial) is just repeats of `pattern`
            remainder = digits[period * (len(digits) // period) :]
            if not remainder or pattern.startswith(remainder):
                if period <= max(len(digits) // 3, 4):
                    return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def make_phone_call(
    phone_number: str,
    task: str,
    caller_name: str = "",
    extra_context: str = "",
    wait_for_completion: bool = True,
) -> ToolResult:
    """Make a phone call on behalf of the user.

    Hosted installs use the cloud API. Independent installs may explicitly
    configure local providers and ingress; cloud failures never select local.

    Args:
        phone_number: Number to call, including the country code (e.g. +13125551234).
        task: What to accomplish (e.g., "Book a haircut for Tuesday at 2pm").
        caller_name: Name to use ("calling on behalf of...").
            Defaults to the user's name from settings.
        extra_context: Additional instructions or preferences.
        wait_for_completion: If True, wait for the call to finish and
            return the full transcript. If False, return immediately
            with a call_id for polling.

    Returns:
        ToolResult with transcript, summary, and outcome.
    """
    # --- Block emergency, premium-rate, and short-code numbers ---
    blocked_payload = blocked_call_payload_for_number(phone_number)
    if blocked_payload:
        block_reason = str(blocked_payload["reason"])
        logger.warning("Blocked call attempt to %s: %s", phone_number[:4] + "***", block_reason)
        return ToolResult(
            ok=False,
            error=block_reason,
            data=blocked_payload,
        )

    # --- Resolve caller name ---
    caller_name = _resolve_outbound_caller_name(caller_name)

    # --- Validate phone number ---
    # Strip dashes, spaces, and parentheses so the LLM doesn't need perfect formatting.
    if phone_number:
        phone_number = re.sub(r"[\s\-\(\)]", "", phone_number)
    digits = phone_number[1:] if phone_number else ""
    if not phone_number or not phone_number.startswith("+") or not digits.isdigit() or not 8 <= len(digits) <= 15:
        return ToolResult(
            ok=False,
            error=(
                "Phone number must start with '+' then the country code, with 8-15 digits total "
                "(e.g., +15551234567). Got: %s" % phone_number
            ),
        )
    if not _is_plausible_e164(digits):
        return ToolResult(
            ok=False,
            error="Phone number appears invalid or fake. Got: %s" % phone_number,
        )

    if _uses_independent_local_phone():
        return await _make_phone_call_local(
            phone_number=phone_number,
            task=task,
            caller_name=caller_name,
            extra_context=extra_context,
            wait_for_completion=wait_for_completion,
        )

    # Hosted installations retain their existing cloud-only dispatch.
    return await _make_phone_call_cloud(
        phone_number=phone_number,
        task=task,
        caller_name=caller_name,
        extra_context=extra_context,
        wait_for_completion=wait_for_completion,
    )


async def _arm_cloud_event_relay_for_call() -> bool:
    """Bring up the desktop's cloud phone-event relay before a call is placed.

    Mirrors the guards on ``POST /v1/phone/cloud-events/start``: the relay is a
    DESKTOP construct that bridges the cloud ``/ws/events`` hub into the local
    one, so on the cloud surface itself (same-origin web client) the events are
    already local and there is nothing to relay.

    Wholly best-effort. Every failure path returns ``False`` and the call still
    goes out — a live transcript is worth less than the call itself.
    """
    try:
        if str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower() == "cloud":
            return False

        from telephony.phone_mode import phone_mode_is_cloud

        if not phone_mode_is_cloud():
            return False

        from telephony.phone_cloud_event_relay import arm_phone_cloud_event_relay_for_call

        armed = await arm_phone_cloud_event_relay_for_call()
        if not armed:
            logger.info(
                "Phone cloud-event relay did not confirm a live socket before the dial; "
                "the phone panel falls back to its active-call recovery"
            )
        return armed
    except Exception:
        logger.exception("Phone cloud-event relay arming failed; placing the call anyway")
        return False


async def _make_phone_call_cloud(
    phone_number: str,
    task: str,
    caller_name: str,
    extra_context: str,
    wait_for_completion: bool,
) -> ToolResult:
    """Cloud phone call: POST to cloud API and poll for results.

    This is the sole production dispatch. It fails closed when the cloud is
    not configured/reachable rather than running any local pipeline.
    """
    # Fail-closed account gate: an anonymous device user must not be able to
    # initiate a billable call even before reaching the network boundary.
    account_gate_result = _paid_phone_call_account_gate_result()
    if account_gate_result is not None:
        return account_gate_result

    cloud_url = _resolve_cloud_url()
    if not cloud_url:
        return ToolResult(
            ok=False,
            error=(
                "Phone calling requires the cloud backend, but no cloud_url is configured. "
                "Phone calls run cloud-only and cannot fall back to this device."
            ),
        )

    token = await _resolve_cloud_bearer_token()
    issuer_channel = _resolve_issuer_channel()

    # Arm the desktop's cloud->local phone-event relay BEFORE the dial. The cloud
    # broadcasts call_started while it is still handling this POST, and the relay
    # is the only way that frame reaches the desktop, so arming afterwards races
    # the event the phone panel needs to render anything at all. Placing a call
    # is the one signal the desktop has that does NOT depend on the relay already
    # being up. Best-effort: a relay that cannot come up never blocks a call.
    await _arm_cloud_event_relay_for_call()

    try:
        resp = await _cloud_make_call(
            cloud_url=cloud_url,
            token=token,
            phone_number=phone_number,
            task=task,
            caller_name=caller_name,
            extra_context=extra_context,
            issuer_channel_info=_issuer_channel_info(issuer_channel),
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        tos_result = _cloud_tos_required_result(_cloud_error_payload(exc.response))
        if tos_result is not None:
            return tos_result
        if status_code == 401:
            return ToolResult(
                ok=False,
                error="Cloud authentication failed. Please log in again.",
            )
        if status_code == 429:
            return ToolResult(
                ok=False,
                error="Cloud rate limit exceeded. Try again later.",
            )
        if status_code == 503:
            return ToolResult(
                ok=False,
                error="Cloud phone service is not available.",
            )
        logger.error(
            "Cloud phone call HTTP %d: %s",
            status_code,
            exc.response.text[:200],
        )
        return ToolResult(
            ok=False,
            error="Cloud phone call failed (HTTP %d)." % status_code,
        )
    except httpx.ConnectError:
        logger.warning("Cloud phone call: cannot reach %s", cloud_url)
        return ToolResult(
            ok=False,
            error="Cannot reach cloud backend at %s." % cloud_url,
        )
    except (httpx.ConnectTimeout, httpx.PoolTimeout):
        # Connect- and pool-phase timeouts mean no request ever left this
        # machine: no socket was established (or none was free), so the cloud
        # never saw a dial and NO call was placed. These subclass
        # httpx.TimeoutException and NOT httpx.ConnectError, so they used to
        # fall through to the optimistic handler below and tell the user their
        # call was connecting -- with no_retry set and an empty call_id, so the
        # user could neither retry nor reference the call that did not exist.
        logger.warning("Cloud phone call: connect timed out reaching %s", cloud_url)
        return ToolResult(
            ok=False,
            error="Could not reach the cloud backend to place the call. Nothing was dialled.",
            error_category="cloud_unreachable",
            retryable=True,
            data={
                "call_id": "",
                "status": "not_placed",
                "phone_number": phone_number,
                "call_placed": False,
            },
        )
    except httpx.TimeoutException:
        # Read/write timeout: the dial request DID reach the cloud, so a call
        # may well be ringing right now -- but we never got the reply, so the
        # outcome is genuinely unknown. Both confident answers are wrong here.
        # Saying it failed made Viola tell the user a live, ringing call had
        # failed (the earlier "duplicate prevention closed this request" wording);
        # saying it is connecting asserts a call we never saw confirmed. Report
        # it as unverified, with retryable False so a duplicate call is not
        # dialled on a guess. There is no cloud endpoint that lists a user's
        # in-flight calls, so without a call_id this cannot be reconciled here.
        logger.warning("Cloud phone call: no reply from %s before timeout; dial outcome unknown", cloud_url)
        return ToolResult(
            ok=False,
            error="The dial request reached the cloud but no reply came back in time, so whether the call connected is unknown.",
            error_category="dial_outcome_unknown",
            retryable=False,
            unverified=True,
            data={
                "call_id": "",
                "status": "dial_outcome_unknown",
                "phone_number": phone_number,
                "call_placed": None,
                "outcome_known": False,
            },
        )
    except Exception as exc:
        logger.exception("Cloud phone call failed")
        return ToolResult(
            ok=False,
            error="Cloud phone call failed: %s" % exc,
        )

    if not resp.get("ok"):
        tos_result = _cloud_tos_required_result(resp)
        if tos_result is not None:
            return tos_result
        return ToolResult(
            ok=False,
            error=resp.get("error", "Cloud rejected the call request."),
        )
    if isinstance(resp.get("data"), dict):
        resp = {"ok": True, **resp["data"]}

    call_id = resp.get("call_id", "")
    if resp.get("status") == "queued":
        return ToolResult(
            ok=True,
            data={
                "call_id": call_id,
                "queue_id": resp.get("queue_id", call_id),
                "status": "queued",
                "position": resp.get("position", 0),
                "message": "Call queued. It will dial automatically when the current call ends.",
            },
        )
    if not wait_for_completion:
        return ToolResult(
            ok=True,
            data={
                "call_id": call_id,
                "status": resp.get("status", "pending"),
                "message": "Call initiated via cloud. Use check_call_status to monitor.",
            },
        )

    # --- Poll for completion ---
    try:
        status_data = await _cloud_poll_status(cloud_url, token, call_id)
    except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException):
        # The dial itself succeeded (we hold a call_id), but contact was lost
        # before any outcome came back. The call was asked to be placed AND
        # reported on; only the first half is established, so this cannot ride
        # an ok=True envelope -- that told the model the tool had done its job
        # and let Viola speak about a call whose fate nobody knew.
        logger.exception("Cloud phone status polling failed for call %s", call_id)
        return ToolResult(
            ok=False,
            error="The call was placed, but contact with the cloud was lost before its outcome was known.",
            error_category="call_outcome_unknown",
            retryable=False,
            unverified=True,
            data={
                "call_id": call_id,
                "status": "status_unknown",
                "phone_number": phone_number,
                "call_placed": True,
                "outcome_known": False,
            },
        )

    final_status = status_data.get("status", "unknown")

    # --- Fetch transcript ---
    transcript_data: dict[str, Any] = {}
    if final_status in _CLOUD_TRANSCRIPT_STATUSES:
        try:
            transcript_data = await _cloud_get_transcript(cloud_url, token, call_id)
        except Exception:
            logger.warning("Failed to fetch transcript for call %s", call_id)

    # A terminal status of failed / no_answer / cancelled / error / timeout means
    # the conversation the user asked for did NOT happen. Reporting every one of
    # those on an ok=True envelope told the model the call had succeeded, and the
    # already-computed `completed` field was the honest answer sitting unused.
    # voicemail is a real, delivered outcome (Viola left a message), so it counts
    # as the tool having done its job.
    reached = final_status in {"completed", "voicemail"}
    return ToolResult(
        ok=reached,
        error=None if reached else "The call ended without reaching the person (%s)." % final_status,
        error_category=None if reached else "call_not_completed",
        retryable=False,
        data={
            "call_id": call_id,
            "status": final_status,
            "phone_number": phone_number,
            "summary": transcript_data.get("summary", ""),
            "transcript": transcript_data.get("transcript", []),
            "outcome": status_data.get("outcome", ""),
            "duration_seconds": status_data.get("duration_seconds", 0),
            "completed": final_status == "completed",
            "no_retry": True,
            "retryable": False,
        },
    )


async def check_call_status(call_id: str) -> ToolResult:
    """Check the status of an in-progress phone call.

    Uses the configured installation, with owner-scoped local access.

    Args:
        call_id: The call ID returned by make_phone_call.
    """
    if _uses_independent_local_phone():
        manager, user_id = _get_manager(), _resolve_user_id()
        data = manager.get_status(call_id, user_id=user_id) if manager and user_id else {}
        if manager and user_id and data.get("status") == "not_found":
            queued = await manager.list_call_queue(user_id=user_id)
            data = next(({**item, "call_id": call_id} for item in queued if item["queue_id"] == call_id), data)
        if not data or data.get("status") == "not_found":
            return ToolResult(ok=False, error="Call not found.")
        return ToolResult(ok=True, data=data)
    cloud_url = _resolve_cloud_url()
    if not cloud_url:
        return ToolResult(
            ok=False,
            error="Phone calling requires the cloud backend (no cloud_url configured).",
        )
    token = await _resolve_cloud_bearer_token()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_VERY_LONG) as client:
            resp = await client.get(
                "%s/api/phone/status" % cloud_url,
                headers=_cloud_headers(token),
                params={"call_id": call_id},
            )
            resp.raise_for_status()
            # The cloud wraps the status in a canonical envelope
            # {"ok": True, "data": {"status": ...}}. Without unwrapping, the
            # 'status' field sits under data["data"] and every inspection here
            # reads the wrong shape (the not_found check never matches and the
            # model receives a doubly-nested payload). Mirror the sibling
            # _cloud_poll_status / _cloud_get_transcript helpers.
            data = _unwrap_cloud_response_payload(resp.json())
        if data.get("status") == "not_found":
            return ToolResult(ok=False, error="Call %s not found." % call_id)
        return ToolResult(ok=True, data=data)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return ToolResult(ok=False, error="Call %s not found." % call_id)
        return ToolResult(
            ok=False,
            error="Cloud status check failed (HTTP %d)." % exc.response.status_code,
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return ToolResult(
            ok=False,
            error="Cannot reach cloud backend.",
        )


async def end_phone_call(call_id: str) -> ToolResult:
    """End an active phone call.

    Uses the configured installation, with owner-scoped local access.

    Args:
        call_id: The call ID to hang up.
    """
    if _uses_independent_local_phone():
        manager, user_id = _get_manager(), _resolve_user_id()
        ended = await manager.end_call(call_id, user_id=user_id) if manager and user_id else False
        return ToolResult(
            ok=bool(ended), error=None if ended else "Call not found or already ended.", data={"call_id": call_id}
        )
    cloud_url = _resolve_cloud_url()
    if not cloud_url:
        return ToolResult(
            ok=False,
            error="Phone calling requires the cloud backend (no cloud_url configured).",
        )
    token = await _resolve_cloud_bearer_token()
    try:
        data = await _cloud_hangup(cloud_url, token, call_id)
        if data.get("ok"):
            return ToolResult(
                ok=True,
                data={"call_id": call_id, "message": "Call ended."},
            )
        return ToolResult(
            ok=False,
            error=data.get("error", "Call not found or already ended."),
        )
    except httpx.HTTPStatusError as exc:
        return ToolResult(
            ok=False,
            error="Cloud hangup failed (HTTP %d)." % exc.response.status_code,
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return ToolResult(
            ok=False,
            error="Cannot reach cloud backend.",
        )


async def get_call_transcript(call_id: str) -> ToolResult:
    """Get the full transcript and summary of a phone call.

    Uses the configured installation, with owner-scoped local access.

    Args:
        call_id: The call ID to get transcript for.
    """
    if _uses_independent_local_phone():
        manager, user_id = _get_manager(), _resolve_user_id()
        data = manager.get_transcript(call_id, user_id=user_id) if manager and user_id else {}
        if not data or data.get("error") or data.get("status") == "not_found":
            return ToolResult(ok=False, error="Call not found.")
        return ToolResult(ok=True, data=data)
    cloud_url = _resolve_cloud_url()
    if not cloud_url:
        return ToolResult(
            ok=False,
            error="Phone calling requires the cloud backend (no cloud_url configured).",
        )
    token = await _resolve_cloud_bearer_token()
    try:
        data = await _cloud_get_transcript(cloud_url, token, call_id)
        if data.get("error"):
            return ToolResult(ok=False, error=data["error"])
        return ToolResult(ok=True, data=data)
    except httpx.HTTPStatusError as exc:
        return ToolResult(
            ok=False,
            error="Cloud transcript fetch failed (HTTP %d)." % exc.response.status_code,
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return ToolResult(
            ok=False,
            error="Cannot reach cloud backend.",
        )
