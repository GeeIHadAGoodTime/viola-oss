"""F-AGENT-02 (2026-04-17): out-of-band append-only agent audit log.

Provides a single place where every agent tool call is recorded, separate
from application logs, with:

  - ``timestamp`` (UTC ISO-8601)
  - ``user_id`` (scope — agent audit entries are always per-user)
  - ``tool_name``
  - ``args_hash`` (SHA-256 of the JSON-serialized, key-sorted args)
  - ``approval_path`` (one of the ``ApprovalBridge`` paths)
  - ``risk`` (SAFE / CONFIRM / DANGEROUS)
  - ``result`` (``ok`` | ``denied`` | ``error``)
  - ``duration_ms``

Storage: ``{data_dir}/agent_audit/{user_id}.jsonl``

Rotation / retention: append-only JSONL; rotated by calendar month (the
writer opens ``{user_id}-YYYYMM.jsonl`` so long-running users don't end
up with an unbounded single file). Callers never delete individual
entries; the whole file can be purged as part of GDPR deletion (see
``auth/gdpr.py``).

Why a separate log? So that a prompt-injection-induced tool call that
also suppresses or edits the application log is still visible in the
audit log, which is written with O_APPEND and not read by the agent.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from core.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    user_id: str
    tool_name: str
    args_hash: str
    approval_path: str
    risk: str
    result: str
    duration_ms: float
    error: str | None = None

    def to_jsonl(self) -> str:
        data: dict[str, Any] = {
            "ts": self.timestamp,
            "user_id": self.user_id,
            "tool": self.tool_name,
            "args_hash": self.args_hash,
            "approval_path": self.approval_path,
            "risk": self.risk,
            "result": self.result,
            "duration_ms": round(self.duration_ms, 3),
        }
        if self.error is not None:
            data["error"] = self.error
        return json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n"


def _hash_args(args: Mapping[str, Any]) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        blob = repr(args)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class AgentAuditLog:
    """Append-only agent audit log, per-user, per-month.

    Thread-safe via a per-user RLock; callers can safely invoke from
    multiple async tasks without external coordination.
    """

    data_dir: Path
    _locks: dict[str, threading.RLock] = field(default_factory=dict, init=False)
    _locks_guard: threading.Lock = field(default_factory=threading.Lock, init=False)

    def _path_for(self, user_id: str) -> Path:
        now = datetime.now(UTC)
        folder = self.data_dir / "agent_audit"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{user_id}-{now.strftime('%Y%m')}.jsonl"

    def _lock_for(self, user_id: str) -> threading.RLock:
        with self._locks_guard:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[user_id] = lock
            return lock

    def record(
        self,
        *,
        user_id: str,
        tool_name: str,
        args: Mapping[str, Any],
        approval_path: str,
        risk: str,
        result: str,
        duration_ms: float,
        error: str | None = None,
    ) -> AuditEntry:
        if not user_id:
            raise ValueError("user_id is required for audit entries")

        entry = AuditEntry(
            timestamp=_now_iso(),
            user_id=user_id,
            tool_name=tool_name,
            args_hash=_hash_args(args),
            approval_path=approval_path,
            risk=risk,
            result=result,
            duration_ms=duration_ms,
            error=error,
        )
        path = self._path_for(user_id)
        with self._lock_for(user_id):
            # Open with O_APPEND so concurrent writers interleave atomically.
            with path.open("a", encoding="utf-8") as f:
                f.write(entry.to_jsonl())
                f.flush()
        return entry

    def read_for_user(self, user_id: str) -> list[dict[str, Any]]:
        """Return all entries for the given user across rotated files.

        Intended for GDPR export and debugging only — not for hot-path
        reads by the agent itself (see module docstring).
        """
        folder = self.data_dir / "agent_audit"
        out: list[dict[str, Any]] = []
        if not folder.exists():
            return out
        for path in sorted(folder.glob(f"{user_id}-*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    logger.warning("Corrupt audit line in %s — skipping", path)
        return out

    def purge_user(self, user_id: str) -> int:
        """Delete all audit files for a user. Called by GDPR deletion."""
        folder = self.data_dir / "agent_audit"
        if not folder.exists():
            return 0
        removed = 0
        with self._lock_for(user_id):
            for path in folder.glob(f"{user_id}-*.jsonl"):
                try:
                    path.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("Failed to remove audit file %s: %s", path, exc)
        return removed
