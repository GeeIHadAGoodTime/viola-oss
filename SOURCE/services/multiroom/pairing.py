"""Spoke Device Pairing for Multi-Room Network.

Provides PIN-based pairing for spoke devices connecting to the hub.
When a spoke device requests pairing, a 6-digit PIN is displayed on
the hub. The spoke must submit the correct PIN to complete pairing
and receive a per-device authentication token.

Security model:
    - PINs are 6-digit random numeric codes (1,000,000 possibilities)
    - PINs expire after 5 minutes
    - Max 3 attempts per pairing session
    - Per-device tokens stored in SQLite for selective revocation
    - Pairing is OFF by default for LAN simplicity; enable in settings

Usage:
    >>> from services.multiroom.pairing import get_pairing_service
    >>> service = get_pairing_service()
    >>> session_id, code = service.create_pairing_session("192.168.1.50")
    >>> # Display code on hub, spoke submits it:
    >>> token = service.confirm_pairing(session_id, code)
    >>> # Verify spoke on subsequent requests:
    >>> device = service.verify_device_token(token)
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PIN_LENGTH = 6
_SESSION_EXPIRY_SECONDS = 300  # 5 minutes
_MAX_ATTEMPTS = 3
_TOKEN_LENGTH = 32  # bytes, URL-safe base64 encoded


# ---------------------------------------------------------------------------
# Data Structures
# ---------------------------------------------------------------------------


@dataclass
class PairingSession:
    """An ephemeral pairing session between hub and spoke."""

    session_id: str
    pin: str
    client_ip: str
    created_at: float
    attempts: int = 0
    completed: bool = False


@dataclass(frozen=True)
class PairedDevice:
    """A paired spoke device with its authentication token."""

    device_id: str
    device_name: str
    token_hash: str
    ip_address: str
    paired_at: float
    last_seen: float
    revoked: bool = False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_PAIRED_DEVICES_SCHEMA = """
CREATE TABLE IF NOT EXISTS paired_devices (
    device_id TEXT PRIMARY KEY,
    device_name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    ip_address TEXT,
    paired_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paired_devices_token ON paired_devices(token_hash);
"""


# ---------------------------------------------------------------------------
# Pairing Service
# ---------------------------------------------------------------------------


class SpokePairingService:
    """Manages spoke device pairing with PIN verification.

    Thread-safe. Pairing sessions are stored in-memory (ephemeral).
    Paired device tokens are persisted in SQLite for durability.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._sessions: dict[str, PairingSession] = {}
        self._lock = threading.Lock()
        self._db_initialized = False

    def _ensure_db(self) -> None:
        """Create paired_devices table if it does not exist."""
        if self._db_initialized:
            return
        with self._lock:
            if self._db_initialized:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self._db_path))
                conn.executescript(_PAIRED_DEVICES_SCHEMA)
                conn.commit()
                conn.close()
                self._db_initialized = True
            except Exception as exc:
                logger.error("Failed to initialize paired_devices table: %s", exc)

    # ------------------------------------------------------------------
    # Pairing Session Management
    # ------------------------------------------------------------------

    def create_pairing_session(self, client_ip: str) -> tuple[str, str]:
        """Create a new pairing session and return (session_id, pin).

        The PIN should be displayed on the hub device. The spoke must
        submit it via confirm_pairing() to complete pairing.

        Args:
            client_ip: IP address of the requesting spoke device.

        Returns:
            Tuple of (session_id, pin).
        """
        self._cleanup_expired_sessions()

        session_id = secrets.token_urlsafe(16)
        pin = self._generate_pin()

        session = PairingSession(
            session_id=session_id,
            pin=pin,
            client_ip=client_ip,
            created_at=time.time(),
        )

        with self._lock:
            self._sessions[session_id] = session

        logger.info(
            "Pairing session created for spoke at %s (session %s)",
            client_ip,
            session_id[:8],
        )

        # Emit event for UI display
        self._display_pin(pin, client_ip)

        return session_id, pin

    def confirm_pairing(
        self,
        session_id: str,
        submitted_pin: str,
        device_name: str = "",
    ) -> str | None:
        """Confirm a pairing session with the submitted PIN.

        On success, creates a paired device entry and returns the
        authentication token. On failure, returns None.

        Args:
            session_id: The pairing session ID.
            submitted_pin: The PIN submitted by the spoke device.
            device_name: Optional human-readable name for the device.

        Returns:
            Device authentication token on success, None on failure.

        Raises:
            ValueError: If session not found, expired, or exhausted.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise ValueError("Pairing session not found or expired")

            # Check expiry
            if time.time() - session.created_at > _SESSION_EXPIRY_SECONDS:
                del self._sessions[session_id]
                raise ValueError("Pairing session has expired")

            # Check attempts
            session.attempts += 1
            if session.attempts > _MAX_ATTEMPTS:
                del self._sessions[session_id]
                logger.warning(
                    "Pairing session %s exhausted attempts from %s",
                    session_id[:8],
                    session.client_ip,
                )
                raise ValueError("Too many failed pairing attempts")

            # Constant-time PIN comparison
            if not secrets.compare_digest(submitted_pin.strip(), session.pin):
                remaining = _MAX_ATTEMPTS - session.attempts
                logger.info(
                    "Pairing PIN mismatch from %s (attempt %d, %d remaining)",
                    session.client_ip,
                    session.attempts,
                    remaining,
                )
                return None

            # Success -- mark session completed
            session.completed = True
            del self._sessions[session_id]

        # Generate device token and persist
        token = secrets.token_urlsafe(_TOKEN_LENGTH)
        token_hash = self._hash_token(token)
        device_id = secrets.token_urlsafe(12)
        now = time.time()

        if not device_name:
            device_name = "Spoke-%s" % session.client_ip

        self._store_paired_device(
            device_id=device_id,
            device_name=device_name,
            token_hash=token_hash,
            ip_address=session.client_ip,
            paired_at=now,
        )

        logger.info(
            "Spoke device paired: %s from %s (device_id=%s)",
            device_name,
            session.client_ip,
            device_id[:8],
        )

        return token

    # ------------------------------------------------------------------
    # Device Token Verification
    # ------------------------------------------------------------------

    def verify_device_token(self, token: str) -> PairedDevice | None:
        """Verify a device authentication token.

        Updates last_seen timestamp on successful verification.

        Args:
            token: The device authentication token.

        Returns:
            PairedDevice if valid and not revoked, None otherwise.
        """
        self._ensure_db()
        token_hash = self._hash_token(token)

        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM paired_devices WHERE token_hash = ? AND revoked = 0",
                (token_hash,),
            ).fetchone()

            if row is None:
                conn.close()
                return None

            # Update last_seen
            now = time.time()
            conn.execute(
                "UPDATE paired_devices SET last_seen = ? WHERE device_id = ?",
                (now, row["device_id"]),
            )
            conn.commit()
            conn.close()

            return PairedDevice(
                device_id=row["device_id"],
                device_name=row["device_name"],
                token_hash=row["token_hash"],
                ip_address=row["ip_address"],
                paired_at=row["paired_at"],
                last_seen=now,
                revoked=False,
            )
        except Exception as exc:
            logger.error("Failed to verify device token: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Device Management
    # ------------------------------------------------------------------

    def revoke_device(self, device_id: str) -> bool:
        """Revoke a paired device token.

        Args:
            device_id: The device ID to revoke.

        Returns:
            True if the device was found and revoked.
        """
        self._ensure_db()
        try:
            conn = sqlite3.connect(str(self._db_path))
            cursor = conn.execute(
                "UPDATE paired_devices SET revoked = 1 WHERE device_id = ?",
                (device_id,),
            )
            affected = cursor.rowcount
            conn.commit()
            conn.close()

            if affected > 0:
                logger.info("Revoked device token for device_id=%s", device_id[:8])
                return True
            return False
        except Exception as exc:
            logger.error("Failed to revoke device %s: %s", device_id[:8], exc)
            return False

    def list_paired_devices(self) -> list[dict[str, Any]]:
        """List all paired devices (including revoked).

        Returns:
            List of device info dicts (token_hash excluded).
        """
        self._ensure_db()
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT device_id, device_name, ip_address, paired_at, "
                "last_seen, revoked FROM paired_devices ORDER BY paired_at DESC"
            ).fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as exc:
            logger.error("Failed to list paired devices: %s", exc)
            return []

    def cleanup_revoked(self) -> int:
        """Delete revoked device entries from the database.

        Returns:
            Number of entries deleted.
        """
        self._ensure_db()
        try:
            conn = sqlite3.connect(str(self._db_path))
            cursor = conn.execute("DELETE FROM paired_devices WHERE revoked = 1")
            count = cursor.rowcount
            conn.commit()
            conn.close()
            if count > 0:
                logger.info("Cleaned up %d revoked device entries", count)
            return count
        except Exception as exc:
            logger.error("Failed to cleanup revoked devices: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _generate_pin(self) -> str:
        """Generate a random numeric PIN."""
        return "".join(str(secrets.randbelow(10)) for _ in range(_PIN_LENGTH))

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash a device token for storage using SHA-256."""
        import hashlib

        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _store_paired_device(
        self,
        device_id: str,
        device_name: str,
        token_hash: str,
        ip_address: str,
        paired_at: float,
    ) -> None:
        """Persist a paired device to SQLite."""
        self._ensure_db()
        try:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute(
                "INSERT OR REPLACE INTO paired_devices "
                "(device_id, device_name, token_hash, ip_address, paired_at, last_seen, revoked) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (device_id, device_name, token_hash, ip_address, paired_at, paired_at),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.error("Failed to store paired device: %s", exc)

    def _display_pin(self, pin: str, client_ip: str) -> None:
        """Display the pairing PIN on the hub device."""
        logger.info(
            "PAIRING PIN for spoke %s: [ %s ]  (expires in %d seconds)",
            client_ip,
            pin,
            _SESSION_EXPIRY_SECONDS,
        )

        try:
            from core.events import EventBus

            EventBus.emit(
                "spoke_pairing_pin",
                {
                    "pin": pin,
                    "client_ip": client_ip,
                    "expires_in": _SESSION_EXPIRY_SECONDS,
                },
            )
        except Exception:
            logger.debug("EventBus not available for pairing PIN display")

    def _cleanup_expired_sessions(self) -> None:
        """Remove expired pairing sessions."""
        now = time.time()
        with self._lock:
            expired = [
                sid for sid, s in self._sessions.items() if now - s.created_at > _SESSION_EXPIRY_SECONDS or s.completed
            ]
            for sid in expired:
                del self._sessions[sid]


# ---------------------------------------------------------------------------
# Module-level Singleton
# ---------------------------------------------------------------------------

_pairing_service: SpokePairingService | None = None
_pairing_lock = threading.Lock()


def get_pairing_service() -> SpokePairingService:
    """Get or create the global spoke pairing service.

    Uses the multiroom data directory for the paired devices database.
    """
    global _pairing_service
    if _pairing_service is not None:
        return _pairing_service

    with _pairing_lock:
        if _pairing_service is not None:
            return _pairing_service

        db_path = Path(settings.data_dir) / "multiroom" / "paired_devices.db"
        _pairing_service = SpokePairingService(db_path)
        return _pairing_service


def reset_pairing_service() -> None:
    """Reset global instance (for tests)."""
    global _pairing_service
    with _pairing_lock:
        _pairing_service = None
