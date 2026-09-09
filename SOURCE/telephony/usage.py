"""Select company phone accounting or independent local call safety and usage."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings
from core.platform import get_data_dir
from services.company_service_boundary import company_service_module_available

# Existing phone anti-harassment policy applies to self-hosted calls too.
_PER_NUMBER_HOURLY = 3
_PER_NUMBER_DAILY = 10
_STALE_HEARTBEAT_SECONDS = 120


def company_phone_billing_available() -> bool:
    """Do not silently replace broken or required company services."""
    return company_service_module_available("billing.models", component="Phone accounting")


@dataclass(frozen=True)
class LocalCallDecision:
    allowed: bool
    reason: str = ""


class LocalPhoneUsage:
    """Owner-scoped admission and usage; provider bills stay with the owner.

    SQLite serializes admission across processes. Recheck inside the insert
    transaction so simultaneous callers cannot race through a stale precheck.
    No subscriptions, company balances, or card information live here.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS local_phone_usage (
                call_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                number_hash TEXT NOT NULL, started REAL NOT NULL,
                heartbeat REAL NOT NULL, ended REAL, duration REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active')""")
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _owner(user_id: str) -> None:
        if not user_id or not user_id.strip():
            raise ValueError("A call owner is required")

    def concurrent_limit_for_tier(self, tier: str | None) -> int:
        return max(1, int(settings.phone_global_max_concurrent))

    def _check(self, db, user_id: str, phone_number: str) -> LocalCallDecision:
        self._owner(user_id)
        if not phone_number.startswith("+1"):
            return LocalCallDecision(False, "Only US phone numbers are supported.")
        now = time.time()
        # An expired heartbeat remains in the rate counters and usage history.
        active = db.execute(
            "SELECT COUNT(*) FROM local_phone_usage WHERE ended IS NULL AND heartbeat >= ?",
            (now - _STALE_HEARTBEAT_SECONDS,),
        ).fetchone()[0]
        if active >= self.concurrent_limit_for_tier(None):
            return LocalCallDecision(False, "Maximum concurrent phone calls reached.")
        number_hash = hashlib.sha256(phone_number.encode()).hexdigest()
        for seconds, cap, number_cap in (
            (3600, settings.phone_call_hourly_limit, _PER_NUMBER_HOURLY),
            (86400, settings.phone_call_daily_limit, _PER_NUMBER_DAILY),
        ):
            count, number_count = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(number_hash = ?), 0) "
                "FROM local_phone_usage WHERE user_id = ? AND started >= ?",
                (number_hash, user_id, now - seconds),
            ).fetchone()
            if count >= max(0, int(cap)) or number_count >= number_cap:
                return LocalCallDecision(False, "Phone call rate limit reached. Try again later.")
        return LocalCallDecision(True)

    async def check_can_make_call(self, user_id: str, phone_number: str, tier: str = "free") -> LocalCallDecision:
        with self._transaction() as db:
            return self._check(db, user_id, phone_number)

    async def record_call_start(
        self, user_id: str, call_id: str, phone_number: str, tier: str = "free", *, estimated_cents: int = 1
    ) -> None:
        with self._transaction() as db:
            decision = self._check(db, user_id, phone_number)
            if not decision.allowed:
                raise ValueError(decision.reason)
            now = time.time()
            db.execute(
                "INSERT INTO local_phone_usage(call_id,user_id,number_hash,started,heartbeat) VALUES(?,?,?,?,?)",
                (call_id, user_id, hashlib.sha256(phone_number.encode()).hexdigest(), now, now),
            )

    async def heartbeat(self, user_id: str, call_id: str) -> None:
        self._owner(user_id)
        with self._transaction() as db:
            db.execute(
                "UPDATE local_phone_usage SET heartbeat=? WHERE user_id=? AND call_id=? AND ended IS NULL",
                (time.time(), user_id, call_id),
            )

    async def record_call_end(
        self, user_id: str, call_id: str, duration_seconds: float, cost_usd: float, status: str = "completed"
    ) -> None:
        self._owner(user_id)
        with self._transaction() as db:
            db.execute(
                "UPDATE local_phone_usage SET ended=?,duration=?,cost=?,status=? WHERE user_id=? AND call_id=?",
                (time.time(), max(0, duration_seconds), max(0, cost_usd), status, user_id, call_id),
            )

    async def correct_settled_call_status(self, user_id: str, call_id: str, status: str) -> bool:
        self._owner(user_id)
        with self._transaction() as db:
            return bool(
                db.execute(
                    "UPDATE local_phone_usage SET status=? WHERE user_id=? AND call_id=? AND ended IS NOT NULL",
                    (status, user_id, call_id),
                ).rowcount
            )

    async def get_usage_summary(self, user_id: str, tier: str = "free") -> dict:
        self._owner(user_id)
        with self._transaction() as db:
            calls, cost, duration = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(cost),0),COALESCE(SUM(duration),0) "
                "FROM local_phone_usage WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return {
            "total_calls": calls,
            "total_cost_usd": round(cost, 4),
            "minutes_used": round(duration / 60, 1),
            "minutes_limit": -1,
        }

    async def cleanup_old_records(self, days: int = 365) -> int:
        with self._transaction() as db:
            return db.execute("DELETE FROM local_phone_usage WHERE ended < ?", (time.time() - days * 86400,)).rowcount


def get_phone_billing():
    """Retain the private API for existing consumers; select it explicitly."""
    if company_phone_billing_available():
        from telephony.phone_billing import get_phone_billing as company_gate

        return company_gate()
    return LocalPhoneUsage(get_data_dir() / "phone" / "local_usage.sqlite3")
