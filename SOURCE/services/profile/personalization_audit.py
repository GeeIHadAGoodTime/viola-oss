"""Append-only personalization audit log for user-visible sensitive access."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir

logger = get_logger(__name__)
_AUDIT_LOCK = threading.Lock()


class PersonalizationAuditError(RuntimeError):
    """Raised when a sensitive personalization operation cannot be audited."""


def _safe_user_segment(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in user_id)


def _audit_dir() -> Path:
    return get_data_dir() / "personalization_audit"


def _audit_path(user_id: str, *, ts: datetime | None = None) -> Path:
    stamp = ts or datetime.now(UTC)
    return _audit_dir() / ("%s-%s.jsonl" % (_safe_user_segment(user_id), stamp.strftime("%Y%m")))


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def record_personalization_event(
    user_id: str | None,
    event_type: str,
    *,
    details: dict[str, Any] | None = None,
) -> bool:
    """Record a sensitive personalization operation without raw PII values."""
    uid = str(user_id or "").strip()
    event = str(event_type or "").strip()
    if not uid or not event:
        return False

    ts = datetime.now(UTC)
    payload = {
        "ts": ts.isoformat(),
        "user_id": uid,
        "event_type": event,
        "details": _json_safe(details or {}),
    }
    try:
        path = _audit_path(uid, ts=ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with _AUDIT_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("Failed to write personalization audit event %s: %s", event, exc)
        return False


def require_personalization_event(
    user_id: str | None,
    event_type: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    """Persist a required personalization audit row or fail closed."""
    if record_personalization_event(user_id, event_type, details=details):
        return
    event = str(event_type or "").strip()
    uid = str(user_id or "").strip()
    logger.warning(
        "Required personalization audit event was not persisted: user_id=%s event_type=%s",
        uid or "<missing>",
        event or "<missing>",
    )
    raise PersonalizationAuditError("personalization audit write failed for %s" % (event or "<missing>"))


def read_personalization_audit_for_user(
    user_id: str,
    *,
    since: float | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    uid = str(user_id or "").strip()
    if not uid:
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(_audit_dir().glob("%s-*.jsonl" % _safe_user_segment(uid))):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            logger.debug("Could not read personalization audit file %s: %s", path, exc)
            continue
        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Corrupt personalization audit line in %s", path)
                continue
            if not isinstance(payload, dict) or str(payload.get("user_id")) != uid:
                continue
            ts = str(payload.get("ts") or "")
            if since is not None:
                try:
                    ts_seconds = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if ts_seconds < since:
                    continue
            rows.append(payload)
            if len(rows) >= limit:
                return rows
    return rows


def purge_personalization_audit_for_user(user_id: str) -> int:
    uid = str(user_id or "").strip()
    if not uid:
        return 0
    deleted = 0
    for path in sorted(_audit_dir().glob("%s-*.jsonl" % _safe_user_segment(uid))):
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            logger.debug("Could not delete personalization audit file %s: %s", path, exc)
    return deleted
