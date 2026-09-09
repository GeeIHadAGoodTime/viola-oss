"""Per-device record of paired spokes, so one device can be revoked alone.

Why this exists (#4434 / C-664)
-------------------------------
A spoke credential is a stateless HMAC token, so before this the only way to
stop one was to rotate the signing secret — which unpairs **every** paired
device at once. A user who wanted to kick one phone out of the house had to
re-pair the whole house, so in practice nobody revoked anything.

This registry gives every credential a durable, per-device row on the desktop
(Tier 3, local only — it never leaves the machine). It carries no secret: only
the credential's ``device_id``, when it was first and last seen, an optional
room label, and a revoked flag. That is enough for:

* **per-device revoke** — ``revoke_device`` refuses exactly one device and
  leaves the rest of the house paired;
* **idle expiry** — ``last_seen`` lets the verifier retire a credential that
  has not been used for a long time, instead of trusting it for its whole
  signed lifetime;
* **a list the user can act on** — "Kitchen, last seen 2 hours ago" instead of
  an opaque token.

Writes are throttled: a live spoke re-authenticates often and only a materially
newer ``last_seen`` is worth persisting.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from ui.security import bootstrap as bootstrap_module

logger = get_logger(__name__)

SPOKE_DEVICE_REGISTRY_FILENAME = "spoke_devices.json"

# A live spoke verifies its credential on every socket connect; persisting
# last_seen every time would hammer the disk for no benefit. Idle expiry is
# measured in days, so quarter-hour resolution is plenty.
_LAST_SEEN_PERSIST_INTERVAL_SECONDS = 900

_SCHEMA_VERSION = 1

_lock = threading.RLock()
_cache: dict[str, dict[str, Any]] | None = None
_cache_signature: tuple[int, int] | None = None


@dataclass(frozen=True)
class SpokeDeviceRecord:
    device_id: str
    first_seen: int
    last_seen: int
    revoked: bool
    revoked_at: int | None = None
    room: str | None = None
    legacy: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "revoked": self.revoked,
            "revoked_at": self.revoked_at,
            "room": self.room,
            "legacy": self.legacy,
        }


def _registry_path() -> Path:
    return bootstrap_module.ensure_bootstrap_secret_dir() / SPOKE_DEVICE_REGISTRY_FILENAME


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _load_locked() -> dict[str, dict[str, Any]]:
    """Return the device map, re-reading the file when it changed on disk."""
    global _cache, _cache_signature

    path = _registry_path()
    signature = _file_signature(path)
    if _cache is not None and signature == _cache_signature:
        return _cache

    devices: dict[str, dict[str, Any]] = {}
    if signature is not None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            loaded = raw.get("devices") if isinstance(raw, dict) else None
            if isinstance(loaded, dict):
                devices = {str(key): dict(value) for key, value in loaded.items() if isinstance(value, dict)}
        except (OSError, ValueError):
            logger.exception("Spoke device registry at %s is unreadable; starting a fresh one", path)
            devices = {}

    _cache = devices
    _cache_signature = signature
    return devices


def _persist_locked(devices: dict[str, dict[str, Any]]) -> None:
    global _cache, _cache_signature

    # No per-file permission hardening here on purpose: this file holds no
    # secret (device ids and timestamps only), it inherits the already-locked
    # bootstrap secret directory's ACL, and the hardening call shells out to
    # icacls — which must never run on the audio event loop.
    path = _registry_path()
    payload = json.dumps({"version": _SCHEMA_VERSION, "devices": devices}, indent=2, sort_keys=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError:
        logger.exception("Failed to persist the spoke device registry at %s", path)
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return

    _cache = devices
    _cache_signature = _file_signature(path)


def _remember_locked(devices: dict[str, dict[str, Any]]) -> None:
    """Keep an in-memory-only update (no disk write) as the live view."""
    global _cache
    _cache = devices


def _to_record(device_id: str, entry: dict[str, Any]) -> SpokeDeviceRecord:
    return SpokeDeviceRecord(
        device_id=device_id,
        first_seen=int(entry.get("first_seen") or 0),
        last_seen=int(entry.get("last_seen") or 0),
        revoked=bool(entry.get("revoked")),
        revoked_at=int(entry["revoked_at"]) if entry.get("revoked_at") else None,
        room=str(entry["room"]) if entry.get("room") else None,
        legacy=bool(entry.get("legacy")),
    )


def get_device(device_id: str) -> SpokeDeviceRecord | None:
    """Return the stored record for ``device_id``, if this hub knows it."""
    if not device_id:
        return None
    with _lock:
        entry = _load_locked().get(device_id)
        return _to_record(device_id, entry) if entry is not None else None


def is_device_revoked(device_id: str) -> bool:
    record = get_device(device_id)
    return bool(record and record.revoked)


def record_device_seen(
    device_id: str,
    *,
    issued_at: int | None = None,
    legacy: bool = False,
    room: str | None = None,
) -> SpokeDeviceRecord | None:
    """Register a successful use of ``device_id`` and return its record.

    Creates the row on first sight (which is how already-paired devices join the
    registry after an upgrade) and refreshes ``last_seen``. Returns ``None`` for
    a revoked device — callers must not treat a revoked device as seen.
    """
    if not device_id:
        return None

    now = int(time.time())
    with _lock:
        devices = dict(_load_locked())
        entry = dict(devices.get(device_id) or {})
        if entry.get("revoked"):
            return _to_record(device_id, entry)

        is_new = not entry
        if is_new:
            entry = {
                "first_seen": int(issued_at) if issued_at else now,
                "last_seen": now,
                "revoked": False,
                "legacy": bool(legacy),
            }
        previous_last_seen = int(entry.get("last_seen") or 0)
        if room and entry.get("room") != room:
            entry["room"] = room
            is_new = True  # label change is worth a write
        entry["last_seen"] = max(previous_last_seen, now)

        devices[device_id] = entry
        needs_write = is_new or (now - previous_last_seen) >= _LAST_SEEN_PERSIST_INTERVAL_SECONDS
        if needs_write:
            _persist_locked(devices)
        else:
            # Keep the refreshed timestamp in memory without touching disk.
            _remember_locked(devices)

        return _to_record(device_id, entry)


def revoke_device(device_id: str) -> bool:
    """Refuse this one device from now on. Returns False if already revoked.

    A device this hub has never seen still gets a revoked row, so revoking a
    credential that is currently offline works the moment it comes back.
    """
    if not device_id:
        return False

    now = int(time.time())
    with _lock:
        devices = dict(_load_locked())
        entry = dict(devices.get(device_id) or {})
        if entry.get("revoked"):
            return False
        if not entry:
            entry = {"first_seen": now, "last_seen": now, "legacy": False}
        entry["revoked"] = True
        entry["revoked_at"] = now
        devices[device_id] = entry
        _persist_locked(devices)

    logger.info("Spoke device revoked: %s", device_id[:8])
    return True


def set_device_room(device_id: str, room: str) -> None:
    """Label a device with the room it is serving, for the paired-devices list."""
    if not device_id or not room:
        return
    with _lock:
        devices = dict(_load_locked())
        entry = dict(devices.get(device_id) or {})
        if not entry or entry.get("room") == room:
            return
        entry["room"] = room
        devices[device_id] = entry
        _persist_locked(devices)


def device_ids_for_room(room: str) -> list[str]:
    """Return device ids last known to serve ``room`` (newest use first)."""
    if not room:
        return []
    with _lock:
        devices = _load_locked()
        matches = [
            (device_id, int(entry.get("last_seen") or 0))
            for device_id, entry in devices.items()
            if str(entry.get("room") or "") == room and not entry.get("revoked")
        ]
    matches.sort(key=lambda item: item[1], reverse=True)
    return [device_id for device_id, _ in matches]


def list_devices() -> list[dict[str, Any]]:
    """Return every known device, newest use first, for the desktop UI/API."""
    with _lock:
        devices = _load_locked()
        records = [_to_record(device_id, entry) for device_id, entry in devices.items()]
    records.sort(key=lambda record: record.last_seen, reverse=True)
    return [record.as_dict() for record in records]


def reset_cache_for_tests() -> None:
    """Drop the in-memory cache (test helper only)."""
    global _cache, _cache_signature
    with _lock:
        _cache = None
        _cache_signature = None


__all__ = [
    "SPOKE_DEVICE_REGISTRY_FILENAME",
    "SpokeDeviceRecord",
    "device_ids_for_room",
    "get_device",
    "is_device_revoked",
    "list_devices",
    "record_device_seen",
    "reset_cache_for_tests",
    "revoke_device",
    "set_device_room",
]
