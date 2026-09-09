"""Bootstrap API key management and device pairing.

The bootstrap flow allows new LAN devices to pair without ever receiving
the master API key. A confirmed pairing instead mints a scoped spoke
credential that is limited to spoke/browser WebSocket access.

Flow (typed pairing word, no camera):
  1. Device -> POST /bootstrap/request   -> gets pairing_session_id
  2. Viola displays 6-digit code on screen / console / TTS
  3. Device -> POST /bootstrap/confirm   -> sends session_id + code -> gets spoke credential

Flow (scanned QR):
  1. Desktop shows a URL carrying a short-lived, single-use pairing TICKET
     (utils/speaker_pairing_flow.py) — never a credential, so a photo of the
     screen is worthless minutes later (#4434).
  2. Device -> POST /bootstrap/claim     -> exchanges the ticket once -> gets spoke credential

Either way the credential is delivered into the device's cookie jar and is
never displayed. ``GET /bootstrap/spoke-session`` lets a paired device ask
whether it is still paired, and transparently renews an ageing credential so a
speaker in daily use never hits the expiry wall.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import socket
import subprocess  # nosec B404
import threading
import time
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse

from config.constants import DATA_DIR
from config.settings import settings
from core.logging_config import get_logger
from core.subprocess_utils import run_silent
from fastapi import APIRouter, HTTPException, Request

logger = get_logger(__name__)

BOOTSTRAP_DIR = Path(DATA_DIR) / "secrets"
BOOTSTRAP_KEY_FILENAME = "initial_api_key"
BOOTSTRAP_ACK_FILENAME = "initial_api_key.ack"
BOOTSTRAP_OLD_KEY_FILENAME = "initial_api_key.old"
WS_TOKEN_SECRET_FILENAME = "auth_token_secret"  # nosec B105  # pragma: allowlist secret

# Pairing session settings
_PAIRING_CODE_LENGTH = 6
_PAIRING_EXPIRY_SECONDS = 300  # 5 minutes
_PAIRING_MAX_ATTEMPTS = 3

# Key rotation grace period (seconds) — both old and new keys accepted
_ROTATION_GRACE_PERIOD_SECONDS = 60


@dataclass(frozen=True)
class BootstrapKeyInfo:
    api_key: str
    was_created: bool
    path: Path


@dataclass(frozen=True)
class BootstrapSecretInfo:
    secret: str
    was_created: bool
    path: Path


@dataclass
class _PairingSession:
    """An in-memory ephemeral pairing session."""

    session_id: str
    code: str
    client_ip: str
    created_at: float
    attempts: int = 0
    completed: bool = False


# In-memory store for pending pairing sessions (ephemeral by design)
_pairing_sessions: dict[str, _PairingSession] = {}


def _cleanup_expired_sessions() -> None:
    """Remove expired pairing sessions."""
    now = time.time()
    expired = [
        sid
        for sid, session in _pairing_sessions.items()
        if now - session.created_at > _PAIRING_EXPIRY_SECONDS or session.completed
    ]
    for sid in expired:
        del _pairing_sessions[sid]


def _generate_pairing_code() -> str:
    """Generate a random numeric pairing code."""
    return "".join(str(secrets.randbelow(10)) for _ in range(_PAIRING_CODE_LENGTH))


def pending_pairing_codes() -> list[dict[str, Any]]:
    """Return active pairing codes awaiting confirmation, for desktop display.

    The LAN join flow is device-initiated: a joining browser calls
    ``/bootstrap/request``, which mints a short-lived pairing code and stashes
    a session here. The code is the out-of-band secret that gates the spoke
    mint — it is NEVER returned to the joining device. The desktop hub instead
    reads it through this accessor (behind ``require_auth``) and DISPLAYS it on
    its own screen so the user can read it to the joining device. Only
    non-expired, not-yet-confirmed sessions are returned.
    """
    now = time.time()
    pending: list[dict[str, Any]] = []
    for session in _pairing_sessions.values():
        if session.completed:
            continue
        age = now - session.created_at
        if age > _PAIRING_EXPIRY_SECONDS:
            continue
        pending.append(
            {
                "code": session.code,
                "client_ip": session.client_ip,
                "expires_in": int(_PAIRING_EXPIRY_SECONDS - age),
            }
        )
    # Newest first (largest remaining TTL) so the panel shows the latest join.
    pending.sort(key=lambda entry: entry["expires_in"], reverse=True)
    return pending


def _display_pairing_code(code: str, client_ip: str) -> None:
    """Display the pairing code on the main Viola device.

    Uses multiple channels:
    - Console/log output (always available)
    - EventBus notification (picked up by Qt UI and WebSocket clients)
    """
    logger.info(
        "PAIRING CODE for device %s: [ %s ]  (expires in %d seconds)",
        client_ip,
        code,
        _PAIRING_EXPIRY_SECONDS,
    )

    # Emit event for UI display (Qt notification, web dashboard, etc.)
    try:
        from core.events import EventBus

        EventBus.emit(
            "pairing_code_generated",
            {
                "code": code,
                "client_ip": client_ip,
                "expires_in": _PAIRING_EXPIRY_SECONDS,
            },
        )
    except Exception:
        logger.debug("EventBus not available for pairing code display")


def _windows_acl_identity() -> str | None:
    username = os.environ.get("USERNAME", "").strip()
    if not username:
        return None
    domain = os.environ.get("USERDOMAIN", "").strip()
    if domain and "\\" not in username:
        return "%s\\%s" % (domain, username)
    return username


def _is_windows_host() -> bool:
    return os.name == "nt"


def _apply_secret_permissions(path: Path, *, directory: bool) -> None:
    if _is_windows_host():
        identity = _windows_acl_identity()
        if not identity:
            raise RuntimeError("Cannot secure desktop transport secret path without a Windows user identity")
        grant = "(OI)(CI)(F)" if directory else "(R,W)"
        try:
            run_silent(
                [
                    "icacls",
                    str(path),
                    "/inheritance:r",
                    "/remove:g",
                    "*S-1-1-0",
                    "*S-1-5-11",
                    "*S-1-5-32-545",
                    "/grant:r",
                    "%s:%s" % (identity, grant),
                ],
                capture_output=True,
                timeout=10,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Failed to secure desktop transport secret path %s" % path) from exc
        return

    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError as exc:  # pragma: no cover - platform specific
        raise RuntimeError("Failed to secure desktop transport secret path %s" % path) from exc


def _apply_secret_file_permissions(path: Path) -> None:
    _apply_secret_permissions(path, directory=False)


def apply_bootstrap_secret_file_permissions(path: Path) -> None:
    """Restrict a file in the desktop transport-secret directory to the owning OS user."""
    _apply_secret_file_permissions(path)


def ensure_bootstrap_secret_dir() -> Path:
    """Ensure the desktop transport-secret directory is owner-only."""
    return _bootstrap_dir()


# Cache of secret directories whose permission hardening SUCCEEDED, keyed by
# path string, valued by the directory's identity (st_dev, st_ino) captured
# immediately after the successful icacls/chmod run.
#
# Why: _bootstrap_dir() is reached from the auth middleware on EVERY
# spoke-authenticated HTTP request (auth.py _spoke_credential_valid ->
# verify_spoke_credential -> load_spoke_token_secret -> _secret_dir). Pre-fix
# it re-spawned icacls synchronously on the asyncio event loop each time
# (57-91ms idle, p90 226ms, max 4273ms under load), starving multiroom spoke
# audio (_diag/2026-07-01/spoke_ios_foreground_stall_and_video_sync.md).
#
# Security contract (Identity & Access surface — this must not weaken):
# - Only a SUCCESSFUL _apply_secret_permissions run populates the cache, so a
#   cache hit can never mask a directory whose ACLs were never applied.
#   Failures raise (fail-closed, unchanged) and cache nothing -> retried on
#   the next call.
# - The hit is validated against the directory's current (st_dev, st_ino):
#   if the directory was deleted and recreated (new file id) or replaced, the
#   identity differs -> cache miss -> mkdir + re-harden.
# - Keyed by path, so a changed BOOTSTRAP_DIR (tests, config) re-hardens.
# - Filesystems that don't expose stable file ids (st_ino == 0) never cache:
#   behavior is byte-identical to pre-fix (harden every call).
_secured_dir_cache: dict[str, tuple[int, int]] = {}
_secured_dir_cache_lock = threading.Lock()


def _reset_secured_dir_cache() -> None:
    """Test hook: forget hardening results so the next call re-applies ACLs."""
    with _secured_dir_cache_lock:
        _secured_dir_cache.clear()


def _dir_identity(directory: Path) -> tuple[int, int] | None:
    try:
        st = directory.stat()
    except OSError:
        return None
    if not st.st_ino:
        # No stable file id on this filesystem — refuse to cache.
        return None
    return (int(st.st_dev), int(st.st_ino))


def _bootstrap_dir() -> Path:
    directory = BOOTSTRAP_DIR
    key = str(directory)

    identity = _dir_identity(directory)
    if identity is not None:
        with _secured_dir_cache_lock:
            if _secured_dir_cache.get(key) == identity:
                return directory

    directory.mkdir(parents=True, exist_ok=True)
    _apply_secret_permissions(directory, directory=True)

    identity = _dir_identity(directory)
    if identity is not None:
        with _secured_dir_cache_lock:
            _secured_dir_cache[key] = identity
    return directory


def _key_path() -> Path:
    return _bootstrap_dir() / BOOTSTRAP_KEY_FILENAME


def _ack_path() -> Path:
    return _bootstrap_dir() / BOOTSTRAP_ACK_FILENAME


def _ws_token_secret_path() -> Path:
    return _bootstrap_dir() / WS_TOKEN_SECRET_FILENAME


def ensure_bootstrap_api_key() -> BootstrapKeyInfo:
    """
    Ensure a bootstrap API key exists and return its metadata.

    Returns:
        BootstrapKeyInfo: information about the API key state.
    """
    path = _key_path()
    if path.exists():
        api_key = path.read_text(encoding="utf-8").strip()
        if api_key:
            return BootstrapKeyInfo(api_key=api_key, was_created=False, path=path)
        logger.warning("Bootstrap key file at %s was empty; regenerating.", path)

    api_key = secrets.token_urlsafe(32)
    path.write_text(api_key, encoding="utf-8")
    _apply_secret_file_permissions(path)

    return BootstrapKeyInfo(api_key=api_key, was_created=True, path=path)


def load_bootstrap_api_key() -> str | None:
    """Return the bootstrap API key if it exists."""
    path = _key_path()
    if not path.exists():
        return None
    key = path.read_text(encoding="utf-8").strip()
    return key or None


def ensure_ws_token_secret() -> BootstrapSecretInfo:
    """Ensure a persistent local WebSocket token secret exists."""
    path = _ws_token_secret_path()
    if path.exists():
        secret = path.read_text(encoding="utf-8").strip()
        if secret:
            return BootstrapSecretInfo(secret=secret, was_created=False, path=path)
        logger.warning("WS token secret file at %s was empty; regenerating.", path)

    secret = secrets.token_urlsafe(48)
    path.write_text(secret, encoding="utf-8")
    _apply_secret_file_permissions(path)
    return BootstrapSecretInfo(secret=secret, was_created=True, path=path)


def load_ws_token_secret() -> str | None:
    """Return the local WebSocket token secret if it exists."""
    path = _ws_token_secret_path()
    if not path.exists():
        return None
    secret = path.read_text(encoding="utf-8").strip()
    return secret or None


def mark_bootstrap_key_acknowledged() -> None:
    """Record that the bootstrap key has been acknowledged by a human operator."""
    ack_file = _ack_path()
    ack_file.write_text("acknowledged", encoding="utf-8")
    _apply_secret_file_permissions(ack_file)


def is_bootstrap_key_acknowledged() -> bool:
    """Check whether the bootstrap key acknowledgement file exists."""
    ack_file = _ack_path()
    return ack_file.exists()


def rotate_bootstrap_api_key() -> BootstrapKeyInfo:
    """Rotate the bootstrap API key with a grace period.

    During the grace period (60 seconds), both the old and new keys
    are accepted. After the grace period, the old key file is deleted.

    The rotation event is logged to the auth audit log.
    """
    key_path = _key_path()
    ack_path = _ack_path()
    old_key_path = _bootstrap_dir() / BOOTSTRAP_OLD_KEY_FILENAME

    # Preserve old key for grace period
    old_key: str | None = None
    try:
        if key_path.exists():
            old_key = key_path.read_text(encoding="utf-8").strip()
            if old_key:
                # Write old key to grace period file with timestamp
                grace_data = "%s\n%f" % (old_key, time.time())
                old_key_path.write_text(grace_data, encoding="utf-8")
                _apply_secret_file_permissions(old_key_path)
            key_path.unlink()
    except Exception as exc:
        logger.warning("Failed to handle previous API key at %s: %s", key_path, exc)

    try:
        if ack_path.exists():
            ack_path.unlink()
    except Exception as exc:
        logger.debug("Failed to delete bootstrap ack at %s: %s", ack_path, exc)

    new_key_info = ensure_bootstrap_api_key()

    # Log rotation event to auth audit log
    try:
        from auth.audit import AuthEventType, get_auth_audit_logger

        audit = get_auth_audit_logger()
        audit.log_event(
            "api_key_rotation",
            outcome="success",
            details={
                "old_key_prefix": old_key[:4] if old_key else None,
                "new_key_prefix": new_key_info.api_key[:4],
                "grace_period_seconds": _ROTATION_GRACE_PERIOD_SECONDS,
            },
        )
    except Exception as exc:
        logger.debug("Failed to log rotation to audit: %s", exc)

    logger.info(
        "Bootstrap API key rotated. Grace period: %d seconds",
        _ROTATION_GRACE_PERIOD_SECONDS,
    )

    return new_key_info


def is_valid_bootstrap_key(candidate: str) -> bool:
    """Check if a candidate key matches the current or grace-period key.

    During key rotation, both the new key and the old key (within the
    grace period) are accepted. After the grace period expires, only
    the new key is valid.

    Args:
        candidate: The API key to validate.

    Returns:
        True if the key is valid.
    """
    # Check current key
    current = load_bootstrap_api_key()
    if current and secrets.compare_digest(candidate, current):
        return True

    # Check grace period key
    old_key_path = _bootstrap_dir() / BOOTSTRAP_OLD_KEY_FILENAME
    if old_key_path.exists():
        try:
            content = old_key_path.read_text(encoding="utf-8").strip()
            lines = content.split("\n")
            if len(lines) >= 2:
                old_key = lines[0].strip()
                rotation_time = float(lines[1].strip())
                elapsed = time.time() - rotation_time

                if elapsed <= _ROTATION_GRACE_PERIOD_SECONDS:
                    if secrets.compare_digest(candidate, old_key):
                        logger.info(
                            "Grace period key accepted (%.1f seconds remaining)",
                            _ROTATION_GRACE_PERIOD_SECONDS - elapsed,
                        )
                        return True
                else:
                    # Grace period expired — clean up old key file
                    try:
                        old_key_path.unlink()
                        logger.debug("Cleaned up expired grace period key file")
                    except OSError as exc:
                        logger.debug("Failed to delete expired grace period key file: %s", exc)
        except Exception as exc:
            logger.debug("Failed to check grace period key: %s", exc)

    return False


def _is_local_network_client(request: Request) -> bool:
    """Allow loopback and private LAN clients on desktop.

    Uses ``extract_client_ip`` so the locality decision reads the real
    client IP (X-Forwarded-For from trusted proxies), not ``request.client.host``.
    Phase 5 R1 convergence (2026-05-25): when these routes were registered
    on the cloud FastAPI app, ``request.client.host`` was always the Caddy
    Docker-bridge IP (RFC1918), so every public-internet request passed the
    gate. Live probes returned 200 on /bootstrap/request and emitted pairing
    codes to container logs.

    The cloud no longer registers these routes at all (see
    ``backend/cloud_app.py`` near the bootstrap registration block), so this
    helper is defence-in-depth against future re-introduction or accidental
    spillover into a cloud-context app. If a cloud app accidentally registers
    these routes, RFC1918/private addresses are not local; only loopback is.
    """
    from auth.ip_utils import extract_client_ip

    host = extract_client_ip(request)
    if host is None:
        return False

    try:
        addr = ip_address(host)
    except ValueError:
        try:
            resolved = socket.gethostbyname(host)
            addr = ip_address(resolved)
        except Exception:
            logger.exception("Failed to resolve host for network check")
            return False
    if str(getattr(settings, "app_surface", "")).strip().lower() == "cloud":
        return addr.is_loopback
    return addr.is_loopback or addr.is_private


def register_bootstrap_routes(router: APIRouter) -> None:
    """Register secure bootstrap helper routes."""

    # Rate limiting for bootstrap endpoint: max 3 attempts per IP per hour.
    _bootstrap_attempts: dict[str, tuple[int, float]] = {}
    _BOOTSTRAP_MAX_ATTEMPTS = 3
    _BOOTSTRAP_WINDOW_SECONDS = 3600.0  # 1 hour

    def _check_bootstrap_rate_limit(client_ip: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()

        # Clean up expired entries
        expired = [
            ip for ip, (_, first_time) in _bootstrap_attempts.items() if now - first_time > _BOOTSTRAP_WINDOW_SECONDS
        ]
        for ip in expired:
            del _bootstrap_attempts[ip]

        entry = _bootstrap_attempts.get(client_ip)
        if entry is None:
            _bootstrap_attempts[client_ip] = (1, now)
            return True

        count, first_time = entry
        if now - first_time > _BOOTSTRAP_WINDOW_SECONDS:
            _bootstrap_attempts[client_ip] = (1, now)
            return True

        if count >= _BOOTSTRAP_MAX_ATTEMPTS:
            return False

        _bootstrap_attempts[client_ip] = (count + 1, first_time)
        return True

    # -----------------------------------------------------------------
    # Pairing-based bootstrap flow (secure)
    # -----------------------------------------------------------------

    @router.post("/bootstrap/request", include_in_schema=False)
    async def request_pairing(request: Request) -> dict[str, Any]:
        """Request a new device pairing session.

        Returns a session_id. The pairing code is displayed on the
        main Viola device and must be submitted via /bootstrap/confirm.
        """
        client_ip = request.client.host if request.client else "unknown"

        if not _is_local_network_client(request):
            logger.warning(
                "Rejected bootstrap pairing request from non-local client %s",
                client_ip,
            )
            raise HTTPException(
                status_code=403,
                detail="Device pairing is limited to local network clients.",
            )

        if not _check_bootstrap_rate_limit(client_ip):
            logger.warning("Bootstrap rate limit exceeded for IP %s", client_ip)
            raise HTTPException(
                status_code=429,
                detail="Too many bootstrap attempts. Try again later.",
            )

        # Clean up expired sessions before creating a new one
        _cleanup_expired_sessions()

        # Generate pairing session
        session_id = secrets.token_urlsafe(16)
        code = _generate_pairing_code()
        session = _PairingSession(
            session_id=session_id,
            code=code,
            client_ip=client_ip,
            created_at=time.time(),
        )
        _pairing_sessions[session_id] = session

        logger.info("Pairing session created for IP %s (session %s)", client_ip, session_id[:8])

        # Display the code on the main device
        _display_pairing_code(code, client_ip)

        # Wrap in canonical response envelope ({ok, data, error}) — the
        # response middleware rejects raw dicts with response_contract_violation.
        return {
            "ok": True,
            "error": None,
            "data": {
                "pairing_session_id": session_id,
                "message": "Enter the pairing code displayed on your Viola device.",
                "expires_in": _PAIRING_EXPIRY_SECONDS,
            },
        }

    @router.post("/bootstrap/confirm", include_in_schema=False)
    async def confirm_pairing(request: Request) -> dict[str, Any]:
        """Confirm a pairing session with the displayed code.

        On success, returns a scoped spoke credential and sets an HttpOnly
        spoke cookie. On failure, returns an error. After 3 failed attempts,
        the session is invalidated.
        """
        from ui.security.config import get_security_config
        from ui.security.spoke_credentials import (
            SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS,
            SPOKE_TOKEN_COOKIE_NAME,
            issue_spoke_credential,
        )

        client_ip = request.client.host if request.client else "unknown"

        if not _is_local_network_client(request):
            raise HTTPException(
                status_code=403,
                detail="Device pairing is limited to local network clients.",
            )

        body = await request.json()
        session_id = body.get("pairing_session_id", "")
        submitted_code = str(body.get("code", "")).strip()

        if not session_id or not submitted_code:
            raise HTTPException(
                status_code=400,
                detail="Both pairing_session_id and code are required.",
            )

        session = _pairing_sessions.get(session_id)
        if session is None:
            logger.info(
                "Pairing confirm failed: invalid or expired session from IP %s",
                client_ip,
            )
            raise HTTPException(
                status_code=404,
                detail="Pairing session not found or expired. Request a new code.",
            )

        # Check expiry (before cleanup, so we give a specific error)
        if time.time() - session.created_at > _PAIRING_EXPIRY_SECONDS:
            del _pairing_sessions[session_id]
            logger.info("Pairing session expired for IP %s", client_ip)
            raise HTTPException(
                status_code=410,
                detail="Pairing code has expired. Request a new code.",
            )

        # Clean up other expired sessions
        _cleanup_expired_sessions()

        # Check attempts
        session.attempts += 1
        if session.attempts > _PAIRING_MAX_ATTEMPTS:
            del _pairing_sessions[session_id]
            logger.warning(
                "Pairing session %s exhausted attempts from IP %s",
                session_id[:8],
                client_ip,
            )
            raise HTTPException(
                status_code=429,
                detail="Too many failed attempts. Request a new pairing code.",
            )

        # Validate code (constant-time comparison)
        if not secrets.compare_digest(submitted_code, session.code):
            remaining = _PAIRING_MAX_ATTEMPTS - session.attempts
            logger.info(
                "Pairing code mismatch from IP %s (attempt %d, %d remaining)",
                client_ip,
                session.attempts,
                remaining,
            )
            raise HTTPException(
                status_code=403,
                detail="Incorrect pairing code. %d attempts remaining." % remaining,
            )

        # Success - mark session complete and return a scoped spoke credential
        session.completed = True
        del _pairing_sessions[session_id]

        config = get_security_config()

        if not config.auth_enabled:
            logger.info("Pairing succeeded from IP %s (auth disabled)", client_ip)
            return {"ok": True, "error": None, "data": {"auth_required": False}}

        # Worker-thread hop: issuing writes + hardens the secret file (icacls
        # subprocess) — keep it off the event loop. The mint stays inside
        # this PIN-gated block (constant-time compare_digest above); the
        # closure only moves WHERE it executes, not WHEN it is allowed.
        def _mint_paired_spoke_credential():
            return issue_spoke_credential()

        credential = await asyncio.to_thread(_mint_paired_spoke_credential)
        logger.info(
            "Pairing succeeded - scoped spoke credential issued to IP %s (device %s)",
            client_ip,
            credential.device_id,
        )

        # Canonical envelope. The spoke_token is the credential the client
        # uses; auth_required + credential_type + device_id are metadata.
        response = JSONResponse(
            content={
                "ok": True,
                "error": None,
                "data": {
                    "auth_required": True,
                    "credential_type": "spoke",
                    "device_id": credential.device_id,
                    "spoke_token": credential.token,
                },
            }
        )
        response.set_cookie(
            key=SPOKE_TOKEN_COOKIE_NAME,
            value=credential.token,
            max_age=SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    # -----------------------------------------------------------------
    # QR pairing-ticket exchange
    # -----------------------------------------------------------------

    # A scan is a deliberate human act, so the ceiling only needs to stop
    # abuse, not normal re-pairing: a household adding several speakers in one
    # sitting must not be locked out halfway through.
    _claim_attempts: dict[str, tuple[int, float]] = {}
    _CLAIM_MAX_ATTEMPTS = 20
    _CLAIM_WINDOW_SECONDS = 3600.0

    def _check_claim_rate_limit(client_ip: str) -> bool:
        now = time.time()
        expired = [ip for ip, (_, first) in _claim_attempts.items() if now - first > _CLAIM_WINDOW_SECONDS]
        for ip in expired:
            del _claim_attempts[ip]

        entry = _claim_attempts.get(client_ip)
        if entry is None or now - entry[1] > _CLAIM_WINDOW_SECONDS:
            _claim_attempts[client_ip] = (1, now)
            return True
        count, first = entry
        if count >= _CLAIM_MAX_ATTEMPTS:
            return False
        _claim_attempts[client_ip] = (count + 1, first)
        return True

    def _set_spoke_cookie(response: JSONResponse, request: Request, token: str) -> None:
        from ui.security.spoke_credentials import (
            SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS,
            SPOKE_TOKEN_COOKIE_NAME,
        )

        response.set_cookie(
            key=SPOKE_TOKEN_COOKIE_NAME,
            value=token,
            max_age=SPOKE_TOKEN_COOKIE_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )

    @router.post("/bootstrap/claim", include_in_schema=False)
    async def claim_pairing_ticket(request: Request) -> dict[str, Any]:
        """Exchange a scanned pairing ticket for a spoke credential.

        This is the QR half of pairing. The ticket in the scanned URL is
        short-lived and single-use and authorises nothing but this exchange, so
        a photograph of the Add Room screen cannot join the house later. The
        credential minted here is returned to the joining device only — it is
        never rendered on the desktop.
        """
        from ui.security.config import get_security_config
        from ui.security.spoke_credentials import issue_spoke_credential
        from ui.security.spoke_pairing_ticket import PairingTicketError, redeem_pairing_ticket

        client_ip = request.client.host if request.client else "unknown"

        if not _is_local_network_client(request):
            logger.warning("Rejected pairing-ticket claim from non-local client %s", client_ip)
            raise HTTPException(
                status_code=403,
                detail="Device pairing is limited to local network clients.",
            )

        if not _check_claim_rate_limit(client_ip):
            logger.warning("Pairing-ticket claim rate limit exceeded for IP %s", client_ip)
            raise HTTPException(
                status_code=429,
                detail="Too many pairing attempts. Try again later.",
            )

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError, RuntimeError):
            # No body, or not JSON: treated the same as no ticket.
            body = {}
        ticket = str((body or {}).get("ticket", "")).strip()
        if not ticket:
            raise HTTPException(status_code=400, detail="A pairing link code is required.")

        try:
            redeemed = await asyncio.to_thread(redeem_pairing_ticket, ticket)
        except PairingTicketError as exc:
            logger.info(
                "Pairing-ticket claim refused for IP %s (%s)",
                client_ip,
                exc.reason,
            )
            status = 410 if exc.reason in {"expired", "used"} else 403
            raise HTTPException(status_code=status, detail=exc.message) from None

        config = get_security_config()
        if not config.auth_enabled:
            logger.info("Pairing ticket claimed from IP %s (auth disabled)", client_ip)
            return {"ok": True, "error": None, "data": {"auth_required": False}}

        credential = await asyncio.to_thread(issue_spoke_credential)
        logger.info(
            "Pairing ticket %s claimed - scoped spoke credential issued to IP %s (device %s)",
            redeemed.ticket_id[:8],
            client_ip,
            credential.device_id,
        )

        response = JSONResponse(
            content={
                "ok": True,
                "error": None,
                "data": {
                    "auth_required": True,
                    "credential_type": "spoke",
                    "device_id": credential.device_id,
                    "spoke_token": credential.token,
                },
            }
        )
        _set_spoke_cookie(response, request, credential.token)
        return response

    @router.get("/bootstrap/spoke-session", include_in_schema=False)
    async def get_spoke_session(request: Request) -> dict[str, Any]:
        """Report whether the calling device is still paired, and renew it.

        A spoke page asks this on load. Two things depend on it:

        * an unpaired/expired/revoked device learns so immediately and can show
          the pairing gate instead of a silently dead speaker screen;
        * a still-valid credential that is over half way through its life is
          re-issued for the SAME device id, so a speaker in normal use rolls
          forward forever and never meets the expiry wall (revocation still
          applies, because the device id carries across the renewal).
        """
        from ui.security.config import get_security_config
        from ui.security.spoke_credentials import (
            SPOKE_TOKEN_MAX_AGE_SECONDS,
            get_presented_spoke_token,
            issue_spoke_credential,
            verify_spoke_credential,
        )

        if not get_security_config().auth_enabled:
            return {"ok": True, "error": None, "data": {"paired": True, "auth_required": False}}

        token = get_presented_spoke_token(request)
        if not token:
            return {"ok": True, "error": None, "data": {"paired": False, "auth_required": True}}

        verified = await asyncio.to_thread(verify_spoke_credential, token)
        if verified is None:
            return {"ok": True, "error": None, "data": {"paired": False, "auth_required": True}}

        data: dict[str, Any] = {
            "paired": True,
            "auth_required": True,
            "device_id": verified.device_id,
        }

        age = int(time.time()) - int(verified.issued_at or 0)
        if verified.device_id and age > SPOKE_TOKEN_MAX_AGE_SECONDS // 2:
            renewed = await asyncio.to_thread(issue_spoke_credential, verified.device_id)
            logger.info("Renewed spoke credential for device %s (age %ds)", verified.device_id, age)
            data["spoke_token"] = renewed.token
            data["renewed"] = True
            response = JSONResponse(content={"ok": True, "error": None, "data": data})
            _set_spoke_cookie(response, request, renewed.token)
            return response

        return {"ok": True, "error": None, "data": data}

    # -----------------------------------------------------------------
    # Legacy direct bootstrap (DEPRECATED — use pairing flow instead)
    # Kept for backward compat with older clients. Stricter rate limit
    # than the pairing flow: 1 request per 5 minutes per IP.
    # -----------------------------------------------------------------

    # Separate, stricter rate-limit store for the legacy endpoint:
    # 1 request per 5 minutes per IP (vs 3/hour for pairing).
    _legacy_bootstrap_attempts: dict[str, float] = {}
    _LEGACY_BOOTSTRAP_COOLDOWN = 300.0  # 5 minutes

    def _check_legacy_rate_limit(client_ip: str) -> bool:
        """Return True if the legacy bootstrap request is allowed."""
        now = time.time()
        # Purge expired entries
        expired = [ip for ip, ts in _legacy_bootstrap_attempts.items() if now - ts > _LEGACY_BOOTSTRAP_COOLDOWN]
        for ip in expired:
            del _legacy_bootstrap_attempts[ip]

        last_ts = _legacy_bootstrap_attempts.get(client_ip)
        if last_ts is not None and now - last_ts < _LEGACY_BOOTSTRAP_COOLDOWN:
            return False
        _legacy_bootstrap_attempts[client_ip] = now
        return True

    @router.get("/bootstrap/auth", include_in_schema=False, deprecated=True)
    async def get_bootstrap_auth(request: Request) -> dict[str, Any]:
        """REMOVED: Use the pairing flow (/bootstrap/request + /bootstrap/confirm) instead.

        H7 fix: This legacy endpoint previously returned the full API key to
        any LAN client without pairing.  It now returns 410 Gone.
        """
        client_ip = request.client.host if request.client else "unknown"
        logger.warning(
            "REMOVED endpoint /bootstrap/auth called by IP %s — returns 410 Gone",
            client_ip,
        )
        raise HTTPException(
            status_code=410,
            detail="This endpoint has been removed. Use the pairing flow: POST /bootstrap/request + POST /bootstrap/confirm",
        )

    @router.post("/bootstrap/rotate", include_in_schema=False)
    async def rotate_api_key(request: Request) -> dict[str, Any]:
        """
        Rotate the bootstrap API key.

        SECURITY: Requires loopback access and current valid authentication.
        """
        from ui.security.auth import AuthenticationPlugin
        from ui.security.config import get_security_config

        if not _is_local_network_client(request):
            logger.warning(
                "Rejected key rotation request from non-local client %s",
                request.client.host if request.client else "unknown",
            )
            raise HTTPException(
                status_code=403,
                detail="Key rotation is limited to local network clients.",
            )

        config = get_security_config()

        # Require current valid authentication before allowing rotation
        if config.auth_enabled:
            auth_plugin = AuthenticationPlugin(config)
            if not await auth_plugin.verify_request(request):
                raise HTTPException(
                    status_code=401,
                    detail="Valid authentication required to rotate key.",
                    headers={"WWW-Authenticate": "ApiKey"},
                )

        # Rotate the key (worker-thread hop: rotation writes + hardens key
        # files with icacls subprocesses — keep it off the event loop).
        new_key_info = await asyncio.to_thread(rotate_bootstrap_api_key)
        masked_key = new_key_info.api_key[:4] + "*" * (len(new_key_info.api_key) - 8) + new_key_info.api_key[-4:]

        logger.info("API key rotated successfully. New key stored at %s", new_key_info.path)

        return {
            "ok": True,
            "message": "API key rotated successfully.",
            "masked_key": masked_key,
            "key_location": str(new_key_info.path),
        }
