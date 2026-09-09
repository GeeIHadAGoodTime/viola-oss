"""Persistent user preference model learned from interactions.

Per-user storage backed by the auth database ``user_models`` table.
``data/user_model.json`` is treated as a **seed file only** — on first
load for a user, if no DB record exists, the seed file is imported
automatically.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.asyncio_safe import run_async_synchronously
from core.logging_config import get_logger
from intent.user_context import normalize_context_text

logger = get_logger(__name__)

_PROFILE_DIR = Path.cwd() / "data"
_SEED_PATH = _PROFILE_DIR / "user_model.json"
_missing_user_model_users_logged: set[str] = set()

SENSITIVE_CATEGORIES = frozenset(
    {
        "password",
        "passwords",
        "financial",
        "banking",
        "credit_card",
        "ssn",
        "social_security",
        "health",
        "medical",
        "diagnosis",
        "medication",
        "insurance",
        "tax",
        "pin",
        "secret",
        "token",
        "api_key",
    }
)

_MAX_PATTERNS = 50
_MAX_PREFS_PER_CATEGORY = 20
_MAX_FACTS = 50
_SUMMARY_MAX_WORDS = 250


def _run_async(coro, *, timeout: float = 3.0):
    """Run *coro* synchronously even from a running loop.

    Bounded wait avoids hanging the cloud request when the asyncpg pool is
    bound to a different event loop. Returns ``None`` on timeout; profile
    callers treat that as "profile not available" and operate without it.

    On the cloud surface, calling this from sync code that is itself running
    on the FastAPI main loop's thread (e.g. agent_executor's post-task
    evaluator → UserModelProfile.__init__ → _load_user_model_payload) trips
    the ASYNC-1 cross-loop guard (``Cannot synchronously wait on the shared
    asyncio worker loop from itself``). The guard raises rather than
    silently corrupting the asyncpg pool. Translate that raise into the
    same "profile not available" graceful degrade the timeout path emits —
    the agent loop already completed by this point, so missing post-task
    learning is recoverable; an unhandled RuntimeError would log noisily
    and confuse error counters. Sync call sites that need real user-model
    persistence on cloud should migrate to the async-native path
    (``_load_user_model_payload_async`` etc., when added in P4 followup).
    """
    try:
        return run_async_synchronously(
            coro,
            timeout=timeout,
            timeout_result=None,
            timeout_log_message=(
                "Profile async call timed out after %.1fs (likely Postgres "
                "pool bound to another loop); returning None."
            ),
            logger=logger,
        )
    except RuntimeError as exc:
        _msg = str(exc)
        # Cloud surface (set_main_loop registered): the cross-loop guard in
        # core.asyncio_safe._run_on_worker_loop raises this exact message.
        # Desktop surface (no set_main_loop): the worker loop's coroutine
        # tries to use a main-loop-bound asyncpg pool and asyncpg's
        # _ensure_current_loop_pool raises a different message. Both are
        # the same class of bug — graceful degrade is appropriate.
        if (
            "Cannot synchronously wait on the shared asyncio worker loop from itself" in _msg
            or "pool is bound to a different event loop" in _msg
        ):
            logger.warning(
                "Profile async dispatch blocked by ASYNC-1 cross-loop guard; "
                "returning None (post-task user-model update skipped). Caller "
                "should migrate to async-native profile API.",
            )
            try:
                coro.close()
            except Exception:
                pass
            return None
        raise


def _get_auth_db():
    from auth.database import get_auth_db

    db = get_auth_db()
    if not db._initialized:
        _run_async(db.initialize())
    return db


def _ensure_user_models_table() -> None:
    db = _get_auth_db()
    if hasattr(db, "pool"):

        async def _assert() -> None:
            async with db.connection() as conn:
                from core.db_backend import assert_pg_relations

                await assert_pg_relations(conn, ("public.sync_user_models",), owner="user_model.profile")

        _run_async(_assert())
        return

    ddl = (
        "CREATE TABLE IF NOT EXISTS user_models ("
        "user_id TEXT PRIMARY KEY,"
        "model_json TEXT NOT NULL DEFAULT '{}',"
        "created_at TEXT NOT NULL,"
        "updated_at TEXT NOT NULL"
        ")"
    )
    db.connection.execute(ddl)
    db.connection.commit()


def _load_user_model_payload(user_id: str) -> dict[str, Any] | None:
    _ensure_user_models_table()
    db = _get_auth_db()

    if hasattr(db, "pool"):
        from services.sync_surfaces import user_models as models_surface

        async def _read():
            async with db.connection() as conn:
                from services.sync.consent import (
                    has_cloud_sync_consent,
                )  # consent-read-only-ok

                if not await has_cloud_sync_consent(conn, user_id):  # consent-read-only-ok
                    return None
                return await models_surface.get_user_model(conn, user_id)

        row = _run_async(_read())
    else:
        row = db.connection.execute(
            "SELECT model_json FROM user_models WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if row is None:
        return None

    model_json = row["model_json"] if hasattr(row, "keys") else row[0]
    if isinstance(model_json, dict):
        return model_json
    try:
        payload = json.loads(model_json)
    except Exception:
        logger.exception("Failed to decode stored user model for user %s", user_id)
        return {}
    return payload if isinstance(payload, dict) else {}


def _log_missing_user_model_user(user_id: str) -> None:
    if user_id in _missing_user_model_users_logged:
        logger.debug("Skipping user model persistence for missing auth user %s", user_id)
        return
    _missing_user_model_users_logged.add(user_id)
    logger.warning("Skipping user model persistence for missing auth user %s", user_id)


def _save_user_model_payload(user_id: str, payload: dict[str, Any]) -> bool:
    _ensure_user_models_table()
    db = _get_auth_db()
    now = datetime.now(UTC).isoformat()
    model_json = json.dumps(payload, ensure_ascii=False)

    if hasattr(db, "pool"):
        import asyncpg as _asyncpg

        from services.sync_surfaces import user_models as models_surface
        from services.sync_surfaces.base import SyncSurfaceError

        async def _write() -> bool:
            try:
                async with db.connection() as conn:
                    from services.sync.consent import has_cloud_sync_consent_locked

                    if not await has_cloud_sync_consent_locked(conn, user_id):
                        logger.info(
                            "Skipping cloud user-model write for user %s without cloud-sync consent",
                            user_id,
                        )
                        return False
                    user_row = await conn.fetchrow(
                        "SELECT user_id FROM public.app_user_profiles WHERE user_id = $1::uuid",
                        user_id,
                    )
                    if user_row is None:
                        _log_missing_user_model_user(user_id)
                        return False
                    await models_surface.upsert_user_model(conn, user_id, {"model_json": payload})
                    return True
            except (_asyncpg.PostgresError, SyncSurfaceError):
                logger.warning(
                    "User model persistence failed for user %s due to PostgreSQL schema issue",
                    user_id,
                )
                return False

        return bool(_run_async(_write()))

    user_row = db.connection.execute(
        "SELECT id FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if user_row is None:
        _log_missing_user_model_user(user_id)
        return False

    existing = db.connection.execute(
        "SELECT user_id FROM user_models WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if existing is None:
        db.connection.execute(
            "INSERT INTO user_models (user_id, model_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, model_json, now, now),
        )
    else:
        db.connection.execute(
            "UPDATE user_models SET model_json = ?, updated_at = ? WHERE user_id = ?",
            (model_json, now, user_id),
        )
    db.connection.commit()
    return True


def _load_seed_data(seed_path: Path | None = None) -> dict[str, Any] | None:
    """Load the JSON seed file (``data/user_model.json``) if it exists.

    Returns the parsed dict or ``None`` on failure / missing file.
    """
    path = seed_path or _SEED_PATH
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        logger.exception("Corrupt seed user model at %s", path)
    return None


class UserModelProfile:
    """Persistent user preference model with thread-safe I/O.

    ``user_id`` is required for DB-backed storage.  When ``user_id`` is
    ``None`` a file-only fallback is used (test environments).
    """

    def __init__(self, user_id: str | None = None, path: Path | None = None) -> None:
        self._user_id = user_id
        self._path = path or _SEED_PATH
        self._lock = threading.RLock()
        self._data: dict[str, Any] = self._empty_profile()
        self._load()

    @staticmethod
    def _empty_profile() -> dict[str, Any]:
        return {
            "preferences": {},
            "facts": {},
            "interaction_patterns": {},
            "pending_suggestions": [],
            "last_updated": "",
        }

    def _load(self) -> None:
        payload: dict[str, Any] | None = None
        if self._user_id:
            # DB is the primary source
            payload = _load_user_model_payload(self._user_id)
            if payload is None:
                # First run for this user — seed from the JSON file
                seed = _load_seed_data(self._path)
                if seed is not None:
                    payload = seed
                    # Best-effort persist.  On desktop/local mode the injected
                    # desktop pseudo-identity has no row in ``users`` yet, so the FK
                    # constraint on ``user_models`` would raise IntegrityError.
                    # We still surface the seed in memory — persistence is
                    # retried on the next ``_save`` once the user row exists.
                    try:
                        if _save_user_model_payload(self._user_id, seed):
                            logger.info(
                                "Seeded user model for %s from %s",
                                self._user_id,
                                self._path,
                            )
                    except Exception:
                        logger.warning(
                            "Could not persist seed user model for %s (will use in-memory seed)",
                            self._user_id,
                        )
        elif self._path.exists():
            # File-only mode (tests, no user_id)
            try:
                raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    payload = data
            except Exception:
                logger.exception("Corrupt legacy user model at %s -- starting fresh", self._path)

        if payload is None:
            logger.debug("No user model found for user_id=%s", self._user_id)
            return

        for key in self._data:
            if key in payload and isinstance(payload[key], type(self._data[key])):
                self._data[key] = payload[key]

    def _save(self) -> None:
        self._data["last_updated"] = datetime.now(UTC).isoformat()
        if self._user_id:
            try:
                _save_user_model_payload(self._user_id, self._data)
            except Exception:
                logger.exception("Failed to save user model for user %s", self._user_id)
            return

        # File-only fallback (tests)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            logger.exception("Failed to save user model to %s", self._path)

    @property
    def preferences(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._data.get("preferences", {}))

    @property
    def facts(self) -> dict[str, str]:
        with self._lock:
            return dict(self._data.get("facts", {}))

    @property
    def interaction_patterns(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._data.get("interaction_patterns", {}))

    @property
    def pending_suggestions(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._data.get("pending_suggestions", []))

    @property
    def last_updated(self) -> str:
        with self._lock:
            return self._data.get("last_updated", "")

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update_preference(self, category: str, key: str, value: Any) -> bool:
        category_lower = category.lower()
        if category_lower in SENSITIVE_CATEGORIES:
            logger.warning("Refusing to store preference in sensitive category %r", category)
            return False

        with self._lock:
            prefs = self._data.setdefault("preferences", {})
            cat = prefs.setdefault(category, {})
            if len(cat) >= _MAX_PREFS_PER_CATEGORY and key not in cat:
                logger.warning(
                    "Preference limit reached for category %r (max %d)",
                    category,
                    _MAX_PREFS_PER_CATEGORY,
                )
                return False
            cat[key] = value
            self._save()
            logger.info("Preference updated: %s.%s", category, key)
        return True

    def add_fact(self, key: str, value: str) -> bool:
        key_lower = key.lower()
        if key_lower in SENSITIVE_CATEGORIES:
            logger.warning("Refusing to store sensitive fact %r", key)
            return False

        try:
            from services.memory.store import SENSITIVE_CONTENT_RE

            combined = "%s %s" % (key, value)
            if SENSITIVE_CONTENT_RE.search(combined):
                logger.warning(
                    "Refusing to store fact %r: sensitive content detected in key or value",
                    key,
                )
                return False
        except ImportError:
            pass

        with self._lock:
            facts = self._data.setdefault("facts", {})
            if len(facts) >= _MAX_FACTS and key not in facts:
                logger.warning("Fact limit reached (max %d)", _MAX_FACTS)
                return False
            facts[key] = value
            self._save()
            logger.info("Fact updated: %s", key)
        return True

    def remove_fact(self, key: str) -> bool:
        with self._lock:
            facts = self._data.get("facts", {})
            if key in facts:
                del facts[key]
                self._save()
                logger.info("Fact removed: %s", key)
                return True
        return False

    def update_pattern(self, action: str, timestamp: str | None = None) -> None:
        ts = timestamp or datetime.now(UTC).isoformat()
        hour = ts[11:16] if len(ts) >= 16 else ""

        with self._lock:
            patterns = self._data.setdefault("interaction_patterns", {})
            if len(patterns) >= _MAX_PATTERNS and action not in patterns:
                least = min(patterns, key=lambda k: patterns[k].get("count", 0))
                del patterns[least]

            entry = patterns.setdefault(
                action,
                {"frequency": "occasional", "typical_time": "", "count": 0},
            )
            entry["count"] = entry.get("count", 0) + 1
            if hour:
                entry["typical_time"] = hour

            count = entry["count"]
            if count >= 14:
                entry["frequency"] = "daily"
            elif count >= 4:
                entry["frequency"] = "weekly"
            else:
                entry["frequency"] = "occasional"

            self._save()

    def add_suggestion(self, suggestion: dict[str, Any]) -> None:
        with self._lock:
            queue = self._data.setdefault("pending_suggestions", [])
            if len(queue) >= 20:
                queue.pop(0)
            queue.append(suggestion)
            self._save()

    def pop_suggestion(self) -> dict[str, Any] | None:
        with self._lock:
            queue = self._data.get("pending_suggestions", [])
            if not queue:
                return None
            suggestion = queue.pop(0)
            self._save()
            return suggestion

    def clear_suggestions(self) -> int:
        with self._lock:
            queue = self._data.get("pending_suggestions", [])
            count = len(queue)
            self._data["pending_suggestions"] = []
            if count:
                self._save()
            return count

    def get_profile_summary(
        self,
        task_text: str | None = None,
        *,
        canonical_name: str = "",
        canonical_address: dict[str, Any] | None = None,
    ) -> str:
        """Render learned facts, preferences, and patterns for the model.

        ``task_text`` is accepted for backwards compatibility but is NOT used
        to filter lines. R5-P0-D (2026-05-30) deleted the
        ``is_forms_or_legal_task`` / ``line_matches_task`` keyword
        classifiers that gated this summary by a brittle wordlist. The
        summary is short (capped at ~250 words) — emit it whole every turn
        and let the model decide what to use.

        ``canonical_name`` and ``canonical_address`` are still honored so
        learned facts that just repeat the ACCOUNT OWNER block are dropped;
        that's data uniqueness, not query classification.
        """
        del task_text  # accepted for compat; no longer drives filtering

        with self._lock:
            prefs = self._data.get("preferences", {})
            facts = self._data.get("facts", {})
            patterns = self._data.get("interaction_patterns", {})
            suggestions = self._data.get("pending_suggestions", [])

        if not prefs and not facts and not patterns:
            return ""

        canonical_name_norm = normalize_context_text(canonical_name)
        canonical_address_norms = {
            normalize_context_text(str(value)) for value in (canonical_address or {}).values() if str(value).strip()
        }

        lines: list[str] = []
        seen: set[str] = set()
        word_count = 0

        def _append(line: str) -> None:
            nonlocal word_count
            signature = normalize_context_text(line)
            if signature and signature in seen:
                return
            if signature:
                seen.add(signature)
            word_count += len(line.split())
            if word_count <= _SUMMARY_MAX_WORDS:
                lines.append(line)

        if facts:
            _append("USER LEARNED FACTS:")
            for k, v in list(facts.items())[:15]:
                key_text = str(k).strip()
                value_text = str(v).strip()
                if not value_text:
                    continue
                value_norm = normalize_context_text(value_text)
                if canonical_name_norm and value_norm == canonical_name_norm:
                    continue
                if canonical_address_norms and value_norm in canonical_address_norms:
                    continue
                line = "- %s: %s" % (key_text.replace("_", " ").title(), value_text)
                _append(line)
                if word_count > _SUMMARY_MAX_WORDS:
                    break

        if prefs and word_count < _SUMMARY_MAX_WORDS:
            _append("USER PREFERENCES:")
            for cat, items in list(prefs.items())[:8]:
                if not isinstance(items, dict):
                    continue
                parts = ["%s=%s" % (k, v) for k, v in list(items.items())[:5]]
                line = "- %s: %s" % (cat.title(), ", ".join(parts))
                _append(line)
                if word_count > _SUMMARY_MAX_WORDS:
                    break

        if patterns and word_count < _SUMMARY_MAX_WORDS:
            frequent = [(k, v) for k, v in patterns.items() if v.get("frequency") in ("daily", "weekly")]
            if frequent:
                _append("RECURRING PATTERNS:")
                for action, info in frequent[:5]:
                    time_str = info.get("typical_time", "")
                    time_part = " around %s" % time_str if time_str else ""
                    line = "- %s: %s%s" % (
                        action.replace("_", " "),
                        info.get("frequency", ""),
                        time_part,
                    )
                    _append(line)
                    if word_count > _SUMMARY_MAX_WORDS:
                        break

        if suggestions:
            _append(
                "PENDING INSIGHTS: %d suggestion(s) from weekly review. Surface naturally when contextually relevant."
                % len(suggestions)
            )

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)


_model_lock = threading.Lock()
_model_singletons: dict[str, UserModelProfile] = {}


def load_profile(user_id: str) -> dict[str, Any]:
    return UserModelProfile(user_id=user_id).to_dict()


def save_profile(user_id: str, data: dict[str, Any]) -> dict[str, Any]:
    profile = UserModelProfile(user_id=user_id)
    with profile._lock:
        profile._data = profile._empty_profile()
        for key in profile._data:
            if key in data and isinstance(data[key], type(profile._data[key])):
                profile._data[key] = data[key]
        profile._save()
    _model_singletons[user_id] = profile
    return profile.to_dict()


def get_user_model(user_id: str, path: Path | None = None) -> UserModelProfile:
    """Return the cached ``UserModelProfile`` for the given user.

    ``user_id`` is required.  The profile is loaded from the auth DB
    (with automatic seed-from-JSON on first run).  Instances are cached
    per ``user_id`` for the lifetime of the process.
    """
    model = _model_singletons.get(user_id)
    if model is None:
        with _model_lock:
            model = _model_singletons.get(user_id)
            if model is None:
                model = UserModelProfile(user_id=user_id, path=path)
                _model_singletons[user_id] = model
    return model


def reload_user_model(user_id: str, path: Path | None = None) -> UserModelProfile:
    """Force-reload the user model from the DB (or seed file)."""
    with _model_lock:
        model = UserModelProfile(user_id=user_id, path=path)
        _model_singletons[user_id] = model
    return model


def migrate_json_to_db(user_id: str, seed_path: Path | None = None) -> bool:
    """Import the legacy ``user_model.json`` file into the DB for *user_id*.

    If the user already has a DB record this is a no-op (returns False).
    Returns True if the migration actually ran.
    """
    existing = _load_user_model_payload(user_id)
    if existing is not None:
        logger.debug("User %s already has a DB model record; skipping migration", user_id)
        return False

    seed = _load_seed_data(seed_path)
    if seed is None:
        logger.debug("No seed file to migrate for user %s", user_id)
        return False

    if not _save_user_model_payload(user_id, seed):
        return False
    logger.info("Migrated seed user model into DB for user %s", user_id)
    return True


def reset_user_model_for_tests() -> None:
    with _model_lock:
        _model_singletons.clear()
