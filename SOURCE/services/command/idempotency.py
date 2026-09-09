"""Idempotency tracking for command execution.

Provides the ``IdempotencyLedger`` to persist completed command results and
serve them back via lookup/record/reconcile workflows. Idempotency prevents
client retries or duplicate request IDs from re-running command side effects.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


class IdempotencyLedger:
    """
    Persistent ledger tracking processed command IDs.

    Stores serialized command results on disk so that API restarts can avoid
    re-executing already completed requests.
    """

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 2048,
    ) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._load()

    # ------------------------------------------------------------------ public
    def lookup(self, *, user_id: str, request_id: str) -> dict[str, Any] | None:
        """Return the serialized result for a known idempotent request."""

        ledger_key = self._scoped_key(user_id=user_id, request_id=request_id)
        with self._lock:
            entry = self._entries.get(ledger_key)
            if not entry:
                return None
            if self._is_expired(entry["timestamp"]):
                self._entries.pop(ledger_key, None)
                self._persist()
                return None
            return entry.get("result")

    def record(self, *, user_id: str, request_id: str, result_payload: dict[str, Any]) -> None:
        """Persist the result for a completed request."""

        ledger_key = self._scoped_key(user_id=user_id, request_id=request_id)
        now = time.time()
        with self._lock:
            self._entries[ledger_key] = {"timestamp": now, "result": result_payload}
            self._enforce_limits()
            self._persist()

    def reconcile(self) -> None:
        """Prune expired entries. Invoked during API startup."""

        with self._lock:
            stale = [key for (key, value) in self._entries.items() if self._is_expired(value["timestamp"])]
            for key in stale:
                self._entries.pop(key, None)
            if stale:
                self._persist()

    # ----------------------------------------------------------------- helpers
    def _load(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text("utf-8"))
            if isinstance(data, dict):
                entries: dict[str, dict[str, Any]] = {}
                legacy_count = 0
                for raw_key, value in data.items():
                    key = str(raw_key)
                    if ":" not in key:
                        legacy_count += 1
                        continue
                    if isinstance(value, dict) and "timestamp" in value and "result" in value:
                        entries[key] = value
                self._entries = entries
                if legacy_count:
                    logger.warning("Discarded %d legacy unscoped idempotency ledger entries", legacy_count)
                    self._persist()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to load idempotency ledger: %s", exc)
            self._entries = {}

    def _persist(self) -> None:
        try:
            payload = json.dumps(self._entries, indent=2, sort_keys=True)
            self._path.write_text(payload, encoding="utf-8")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to persist idempotency ledger: %s", exc)

    def _enforce_limits(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        # Drop oldest entries first
        sorted_ids = sorted(self._entries.items(), key=lambda item: item[1]["timestamp"])
        for key, _ in sorted_ids[: len(self._entries) - self._max_entries]:
            self._entries.pop(key, None)

    def _is_expired(self, timestamp: float) -> bool:
        return (time.time() - timestamp) > self._ttl

    def _scoped_key(self, *, user_id: str, request_id: str) -> str:
        scope = str(user_id or "").strip()
        key = str(request_id or "").strip()
        if not scope:
            raise ValueError("user_id is required for idempotency ledger")
        if not key:
            raise ValueError("request_id is required for idempotency ledger")
        return f"{scope}:{key}"


__all__ = ["IdempotencyLedger"]
