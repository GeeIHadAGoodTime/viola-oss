"""Email tools for the agent.

Provides SMTP send capabilities with graceful
"not configured" fallback when credentials are absent.

For Gmail specifically, the Google Workspace MCP server (MCP-4) provides
richer read/search functionality. This module only sends email.
"""

from __future__ import annotations

import base64
import html
import smtplib
import time
from email.message import EmailMessage
from email.mime.text import MIMEText

import httpx

from core.constants import TIMEOUT_LONG
from core.logging_config import get_logger
from intent.tool_types import ToolResult

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Send-rate limiting: max 5 emails per hour per bucket per process.
# Applies in BOTH cloud and desktop mode (SEC-058 desktop parity) so that a
# prompt-injection-driven exfiltration burst is capped even on a single-user
# install where the irreversible-action confirmation gate is the primary
# backstop. The rate limit + recipient allowlist are defense-in-depth.
# ---------------------------------------------------------------------------
_SEND_RATE_WINDOW = 3600  # 1 hour in seconds
_SEND_RATE_MAX = 5
# Stable bucket for desktop sends where no scoped user_id is supplied
# (one-user-per-install: every send belongs to the install owner).
_DESKTOP_RATE_BUCKET = "desktop-owner"
_send_timestamps_by_user: dict[str, list[float]] = {}
_GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"


async def _external_channels_denial(user_id: str | None, *, action: str) -> ToolResult | None:
    try:
        from services.operator_controls import require_enabled_async

        decision = await require_enabled_async("external_channels", user_id=user_id, action=action)
    except Exception:
        logger.exception("email owner safety control check failed closed")
        return ToolResult(
            ok=False,
            error="This capability is temporarily paused while safety controls recover.",
        )
    if decision.allowed:
        return None
    return ToolResult(
        ok=False,
        error=decision.public_message or "This capability is temporarily paused for safety.",
    )


def _get_email_settings() -> dict[str, object]:
    """Load email settings from config."""
    from config.settings import settings

    return {
        "imap_host": getattr(settings, "email_imap_host", None),
        "imap_port": getattr(settings, "email_imap_port", 993),
        "smtp_host": getattr(settings, "email_smtp_host", None) or getattr(settings, "smtp_host", None),
        "smtp_port": getattr(settings, "email_smtp_port", 587) or getattr(settings, "smtp_port", 587),
        "address": getattr(settings, "email_address", None),
        "password": getattr(settings, "email_password", None) or getattr(settings, "smtp_password", None),
    }


async def _resolve_account_email(user_id: str | None) -> str:
    """Best-effort canonical account email lookup for OAuth sends."""
    if not user_id:
        return ""
    # GoTrue on cloud, legacy SQLite users on desktop, local profile last --
    # never a bare public.users query on Postgres (dropped by migration 066, #1233).
    try:
        from auth.account_lookup import resolve_account_email

        email = str(await resolve_account_email(user_id) or "").strip()
        if "@" in email:
            return email
    except Exception:
        logger.debug("Could not resolve account email for user %s", user_id)
    return ""


def _system_email_from_address() -> str:
    from config.settings import settings

    return str(
        getattr(settings, "email_from_address", "")
        or getattr(settings, "ops_email_from", "")
        or "Viola <noreply@useviola.com>"
    )


def _plain_text_html(body: str) -> str:
    escaped = html.escape(body or "", quote=False)
    return '<pre style="white-space: pre-wrap; font-family: inherit;">%s</pre>' % escaped


def _email_service_provider_name(service: object) -> str:
    provider = getattr(service, "provider", None)
    if provider is None:
        return "system"
    return type(provider).__name__.replace("Provider", "").lower() or "system"


def _email_trace_metadata(to: str, subject: str) -> dict[str, object]:
    return {
        "to": to,
        "subject_redacted": True,
        "subject_chars": len(subject or ""),
    }


def _cloud_rate_limit_bucket(user_id: str | None) -> str | None:
    if not user_id:
        return None
    return str(user_id).strip() or None


def _rate_limit_bucket(user_id: str | None, *, is_cloud: bool) -> str | None:
    """Resolve the send-rate bucket for both cloud and desktop modes.

    Cloud requires a scoped user_id (None signals "refuse, unauthenticated").
    Desktop is one-user-per-install, so an absent user_id falls back to a
    stable owner bucket — the limit still applies (SEC-058 desktop parity).
    """
    scoped = str(user_id).strip() if user_id else ""
    if is_cloud:
        return scoped or None
    return scoped or _DESKTOP_RATE_BUCKET


def _email_recipient_allowlist() -> list[str]:
    """Optional recipient allowlist from SettingsManager (desktop defense-in-depth).

    When ``email_recipient_allowlist`` is set and non-empty, ``send_email`` only
    delivers to addresses (or domains, entries beginning with ``@``) on the list.
    An empty/unset list means no allowlist restriction (the confirmation gate
    remains the primary backstop). Returns normalized lower-cased entries.
    """
    try:
        from ui.settings_manager import get_settings_manager

        raw = get_settings_manager().get("email_recipient_allowlist", [])
    except (ImportError, OSError, RuntimeError, AttributeError, KeyError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    return [str(entry).strip().lower() for entry in raw if str(entry).strip()]


def _recipient_allowed(to: str, allowlist: list[str]) -> bool:
    """True if *to* matches the allowlist (exact address or ``@domain`` entry)."""
    if not allowlist:
        return True
    target = (to or "").strip().lower()
    if not target:
        return False
    domain = target.rsplit("@", 1)[-1]
    for entry in allowlist:
        if entry.startswith("@"):
            if domain == entry[1:]:
                return True
        elif target == entry:
            return True
    return False


def _prune_send_timestamps(bucket: str, now: float) -> list[float]:
    timestamps = _send_timestamps_by_user.setdefault(bucket, [])
    timestamps[:] = [ts for ts in timestamps if now - ts < _SEND_RATE_WINDOW]
    return timestamps


def _record_send(bucket: str | None) -> None:
    """Record a successful send against the rate bucket (cloud and desktop)."""
    if bucket:
        _send_timestamps_by_user.setdefault(bucket, []).append(time.monotonic())


def _configured_system_email_backends() -> tuple[list, list[str]]:
    """System email services, plus backends that are configured but broken.

    The two cases used to collapse into one silent ``except``: a provider
    module that is simply absent (expected, not a failure) and a provider that
    IS configured and then failed to initialise. Both left ``services`` empty,
    so ``send_email`` told the user "email sending is not configured" and named
    the very environment variables they had already set. Splitting the import
    from the construction keeps the absent case quiet and surfaces the broken
    one as what it is.
    """
    from viola_email.service import EmailService, get_email_service

    services: list = []
    errors: list[str] = []

    def add_service(service: object | None) -> None:
        provider = getattr(service, "provider", None) if service is not None else None
        if service is not None and provider is not None and getattr(service, "is_configured", False):
            services.append(service)

    service = get_email_service()
    add_service(service)

    try:
        from viola_email.providers.resend import ResendProvider, is_resend_configured
    except ImportError as exc:
        logger.debug("Resend email backend module unavailable: %s", exc)
    else:
        try:
            if is_resend_configured():
                add_service(
                    EmailService(
                        provider=ResendProvider.from_settings(),
                        from_address=_system_email_from_address(),
                    )
                )
        except Exception as exc:
            logger.exception("Resend email backend is configured but could not be initialised")
            errors.append("resend: %s: %s" % (type(exc).__name__, exc))

    try:
        from viola_email.providers.smtp import SMTPProvider, is_smtp_configured
    except ImportError as exc:
        logger.debug("SMTP email backend module unavailable: %s", exc)
    else:
        try:
            if is_smtp_configured():
                add_service(
                    EmailService(
                        provider=SMTPProvider.from_settings(),
                        from_address=_system_email_from_address(),
                    )
                )
        except Exception as exc:
            logger.exception("SMTP email backend is configured but could not be initialised")
            errors.append("smtp: %s: %s" % (type(exc).__name__, exc))

    return services, errors


def _get_configured_system_email_services():
    services, _errors = _configured_system_email_backends()
    return services


def _get_configured_system_email_service():
    services = _get_configured_system_email_services()
    return services[0] if services else None


async def _send_email_via_gmail_oauth(
    to: str,
    subject: str,
    body: str,
    *,
    user_id: str | None,
) -> ToolResult | None:
    """Send through the user's Google OAuth token when Gmail send is available."""
    if not user_id:
        return None

    try:
        from services.oauth.google import get_enabled_gmail_send_scopes

        gmail_send_scopes = get_enabled_gmail_send_scopes()
        if not gmail_send_scopes:
            return None

        from services.oauth.credentials import get_google_credentials

        creds = await get_google_credentials(user_id, required_scopes=gmail_send_scopes)
    except Exception as exc:
        logger.debug("Gmail OAuth credential lookup failed for user %s: %s", user_id, exc)
        return None

    token = str(getattr(creds, "token", "") or "").strip() if creds else ""
    if not token:
        return None

    sender = await _resolve_account_email(user_id)
    if not sender:
        logger.debug("Gmail OAuth send skipped because account email is unavailable for user %s", user_id)
        return None

    message = EmailMessage()
    message["To"] = to
    message["From"] = sender
    message["Subject"] = subject
    message.set_content(body or "")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_LONG * 6) as client:
            response = await client.post(
                _GMAIL_SEND_URL,
                headers={"Authorization": "Bearer %s" % token},
                json={"raw": raw},
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        # Nothing left this machine: no socket was opened (or none was free),
        # so Gmail never saw the message. Safe to fall through to another
        # backend, and safe to retry.
        logger.warning("Gmail OAuth send never reached Google: %s", exc)
        return ToolResult(
            ok=False,
            error="Could not reach Gmail to send the message. Nothing was sent.",
            error_category="EMAIL_NOT_SENT",
            retryable=True,
            data={"configured": True, "sent": False, "provider": "gmail_oauth"},
        )
    except httpx.RequestError as exc:
        # The request was already on the wire when this failed (read/write
        # phase), so Gmail may well have accepted and delivered the message --
        # only the reply was lost. Both confident answers are wrong: claiming
        # failure is what made send_email fall through to the system backend
        # and put a SECOND copy of the same message in the recipient's inbox.
        logger.warning("Gmail OAuth send outcome unknown after the request was sent: %s", exc)
        return ToolResult(
            ok=False,
            error="The message was sent to Gmail but no reply came back, so whether it went out is unknown.",
            error_category="email_outcome_unknown",
            retryable=False,
            unverified=True,
            data={
                "configured": True,
                "provider": "gmail_oauth",
                "outcome_known": False,
            },
        )

    if response.status_code in {200, 201}:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return ToolResult(
            ok=True,
            data={
                "configured": True,
                "sent": True,
                **_email_trace_metadata(to, subject),
                "provider": "gmail_oauth",
                "message_id": payload.get("id"),
            },
        )

    if response.status_code in {401, 403}:
        logger.warning("Gmail OAuth send denied with status %d; trying configured system backend", response.status_code)
        return None

    logger.warning("Gmail OAuth send failed with status %d", response.status_code)
    return ToolResult(
        ok=False,
        error="Gmail OAuth send failed with status %d: %s" % (response.status_code, response.text[:300]),
    )


async def _send_email_via_system_backend(to: str, subject: str, body: str) -> ToolResult | None:
    services, backend_errors = _configured_system_email_backends()
    if not services:
        if backend_errors:
            # A configured backend that would not start is a broken send path,
            # not an unconfigured one. Reporting it as "not configured" sent the
            # user to set credentials that were already set.
            return ToolResult(
                ok=False,
                error="An email backend is configured but could not be initialised: %s" % "; ".join(backend_errors),
                error_category="EMAIL_BACKEND_UNAVAILABLE",
                data={
                    "configured": True,
                    "sent": False,
                    "backend_errors": backend_errors,
                },
            )
        return None

    from viola_email.service import Email

    attempted = []
    for service in services:
        provider_name = _email_service_provider_name(service)
        attempted.append(provider_name)
        sent = await service.send(
            Email(
                to=to,
                subject=subject,
                html=_plain_text_html(body),
                text=body,
            )
        )
        if sent:
            return ToolResult(
                ok=True,
                data={
                    "configured": True,
                    "sent": True,
                    **_email_trace_metadata(to, subject),
                    "provider": provider_name,
                    "providers_attempted": attempted,
                },
            )

    return ToolResult(
        ok=False,
        error="System email backend failed to send.",
        data={
            "configured": True,
            "sent": False,
            "provider": attempted[-1] if attempted else "system",
            "providers_attempted": attempted,
        },
    )


async def send_email(to: str, subject: str, body: str, *, user_id: str | None = None) -> ToolResult:
    """Send an email via Gmail OAuth, user SMTP, or the system email backend.

    Args:
        to: Recipient email address
        subject: Email subject line
        body: Email body text
        user_id: Optional account owner for Google OAuth send.
    """
    if not to or "@" not in to:
        return ToolResult(ok=False, error="Invalid recipient email address: %s" % to)

    owner_denial = await _external_channels_denial(user_id, action="email_send")
    if owner_denial is not None:
        return owner_denial

    # --- Exfiltration protections (defense-in-depth, cloud AND desktop) ---
    from config.settings import settings as _app_config

    _is_cloud = getattr(_app_config, "deployment_mode", "user") != "user"

    # Recipient allowlist (desktop opt-in via settings; the confirmation gate
    # is the primary backstop, this is SEC-058 defense-in-depth parity).
    allowlist = _email_recipient_allowlist()
    if not _recipient_allowed(to, allowlist):
        from auth.utils import mask_email as _mask_email

        logger.warning("Email send blocked: recipient %s not in configured allowlist", _mask_email(to))
        return ToolResult(
            ok=False,
            error="Recipient is not on the configured email allowlist.",
        )

    # Rate limiting: max 5 sends/hour per bucket to cap a burst exfiltration.
    # Cloud requires a scoped user_id; desktop falls back to the owner bucket.
    rate_bucket = _rate_limit_bucket(user_id, is_cloud=_is_cloud)
    if _is_cloud and rate_bucket is None:
        logger.warning("Cloud email send refused without a scoped user id")
        return ToolResult(ok=False, error="Email sending requires an authenticated user in cloud mode.")

    if rate_bucket is not None:
        now = time.monotonic()
        timestamps = _prune_send_timestamps(rate_bucket, now)
        if len(timestamps) >= _SEND_RATE_MAX:
            logger.warning(
                "Email rate limit exceeded for bucket %s: %d sends in the last hour",
                rate_bucket,
                len(timestamps),
            )
            return ToolResult(
                ok=False,
                error="Rate limit exceeded: maximum %d emails per hour." % _SEND_RATE_MAX,
            )

    # Audit log every send attempt (cloud and desktop).
    # SEC-R3: mask recipient so DEBUG logs don't leak the full address.
    from auth.utils import mask_email as _mask_email

    logger.info("Email send: to=%s subject_chars=%d", _mask_email(to), len(subject or ""))

    gmail_result = await _send_email_via_gmail_oauth(to, subject, body, user_id=user_id)
    if gmail_result is not None and gmail_result.ok:
        _record_send(rate_bucket)
        return gmail_result
    if gmail_result is not None and gmail_result.unverified:
        # An attempt whose outcome is unknown may already have delivered. Every
        # other failure falls through to the next backend, which is right when
        # nothing was sent and wrong here: it is how one request becomes two
        # copies in the recipient's inbox. Stop, and say the outcome is unknown.
        # The attempt still costs rate-limit budget, so a forced timeout cannot
        # be used to send past the hourly cap.
        _record_send(rate_bucket)
        return gmail_result

    cfg = _get_email_settings()
    smtp_host = cfg.get("smtp_host")
    has_user_smtp = bool(smtp_host and cfg.get("address") and cfg.get("password"))
    if not has_user_smtp:
        system_result = await _send_email_via_system_backend(to, subject, body)
        if system_result is not None:
            if system_result.ok:
                _record_send(rate_bucket)
            if gmail_result is not None and not system_result.ok:
                return gmail_result
            return system_result
        if gmail_result is not None:
            return gmail_result
        # No OAuth token, no user SMTP, no system backend: no send was even
        # attempted. ``sent: False`` alone left the envelope saying the tool
        # succeeded, and every consumer that reads ``ToolResult.ok`` (the
        # native tool_result is_error flag, the PostToolUse hook split, the
        # tool-call metrics) recorded a delivered email that never existed.
        return ToolResult(
            ok=False,
            error=(
                "Email sending is not configured, so nothing was sent. Set VIOLA_RESEND_API_KEY for the "
                "system backend, or VIOLA_SMTP_HOST, VIOLA_SMTP_USERNAME, and VIOLA_SMTP_PASSWORD for SMTP."
            ),
            error_category="EMAIL_NOT_CONFIGURED",
            data={
                "configured": False,
                "sent": False,
                "send_attempted": False,
            },
        )

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = str(cfg["address"])
        msg["To"] = to

        smtp_port = int(cfg.get("smtp_port", 587))
        with smtplib.SMTP(str(smtp_host), smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(str(cfg["address"]), str(cfg["password"]))
            # sendmail returns the recipients the server refused while still
            # accepting the message for the others. A total refusal raises
            # SMTPRecipientsRefused (handled below); a partial one comes back
            # here quietly, and reporting that as a clean send would tell the
            # user an address was delivered to that the server rejected.
            refused = server.sendmail(str(cfg["address"]), [to], msg.as_string())

        if refused:
            logger.warning("SMTP server refused %d recipient(s) for this send", len(refused))
            return ToolResult(
                ok=False,
                error="The SMTP server refused the recipient: %s" % ", ".join(sorted(refused)),
                error_category="SMTP_RECIPIENT_REFUSED",
                data={
                    "configured": True,
                    "sent": False,
                    "provider": "smtp",
                    "refused_recipient_count": len(refused),
                },
            )

        # Record successful send for rate limiting (cloud and desktop)
        _record_send(rate_bucket)

        return ToolResult(
            ok=True,
            data={
                "configured": True,
                "sent": True,
                "provider": "smtp",
                **_email_trace_metadata(to, subject),
            },
        )
    except smtplib.SMTPException as exc:
        logger.warning("SMTP error sending email: %s", exc)
        return ToolResult(ok=False, error="SMTP error: %s" % exc)
    except OSError as exc:
        logger.warning("Connection error sending email: %s", exc)
        return ToolResult(ok=False, error="Connection error: %s" % exc)
