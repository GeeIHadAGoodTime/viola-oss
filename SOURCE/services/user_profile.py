"""Structured user profile for personal info used in forms, orders, and personalization."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.asyncio_safe import run_async_synchronously
from core.logging_config import get_logger
from core.platform import get_data_dir
from intent.user_context import normalize_context_text

logger = get_logger(__name__)

PROFILE_PATH = get_data_dir() / "user_profile.json"
_LEGACY_PROFILE_PATH = Path.home().joinpath(".viola", "user_profile.json")
_PROFILE_CACHE: dict[str, UserProfile] = {}
PLACEHOLDER_EMAIL_RE = re.compile(r"@(example\.(com|org|net)|test\.com|invalid|localhost)$", re.IGNORECASE)
_PLACEHOLDER_PROFILE_EMAIL_MIGRATION_DONE = False
_LEGACY_PROFILE_FILE_MIGRATION_DONE = False

_ENV_MAP = {
    "full_name": "VIOLA_USER_NAME",
    "address": "VIOLA_USER_ADDRESS",
    "city": "VIOLA_USER_CITY",
    "state": "VIOLA_USER_STATE",
    "zip_code": "VIOLA_USER_ZIP",
    "phone": "VIOLA_USER_PHONE",
    "email": "VIOLA_USER_EMAIL",
}


def _ensure_legacy_profile_file_migrated() -> None:
    global _LEGACY_PROFILE_FILE_MIGRATION_DONE
    if _LEGACY_PROFILE_FILE_MIGRATION_DONE:
        return
    _LEGACY_PROFILE_FILE_MIGRATION_DONE = True
    if PROFILE_PATH.exists() or not _LEGACY_PROFILE_PATH.exists():
        return
    try:
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_LEGACY_PROFILE_PATH, PROFILE_PATH)
        logger.info(
            "Migrated legacy user profile from %s to %s",
            _LEGACY_PROFILE_PATH,
            PROFILE_PATH,
        )
    except OSError as exc:
        logger.warning(
            "Could not migrate legacy user profile from %s to %s: %s",
            _LEGACY_PROFILE_PATH,
            PROFILE_PATH,
            exc,
        )


def _is_non_auth_identity(user_id: str | None) -> bool:
    """Return True when the identity is desktop/session scoped, not an auth user row."""
    if not user_id:
        return False
    from core.user_context import is_desktop_local_principal

    return is_desktop_local_principal(user_id)


def is_placeholder_email(value: Any) -> bool:
    email = str(value or "").strip()
    return bool(email and PLACEHOLDER_EMAIL_RE.search(email))


def valid_user_email_or_empty(value: Any) -> str:
    email = str(value or "").strip()
    if "@" not in email or is_placeholder_email(email):
        return ""
    return email


def _sanitize_profile_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = dict(payload)
    if "email" in clean:
        clean["email"] = valid_user_email_or_empty(clean.get("email"))
    return clean


def _run_async(coro):
    """Sync→async bridge with cross-loop graceful degrade.

    On the cloud surface, this can be invoked from sync code that's running
    on the FastAPI main loop's thread (context_builder._build_profile_context,
    etc.). The ASYNC-1 cross-loop guard raises in that case rather than
    silently corrupting the asyncpg pool. Catching it here yields ``None``
    to the caller — same shape as a transient DB miss; profile lookups
    surface as "no profile" and the rest of the request continues.

    Async call sites should prefer the ``_load_profile_payload_async`` /
    ``_save_profile_payload_async`` paths added 2026-04-30 to skip this
    bridge entirely.
    """
    try:
        return run_async_synchronously(coro)
    except RuntimeError as exc:
        _msg = str(exc)
        # Cloud (set_main_loop registered) raises one message; desktop
        # (worker loop hits asyncpg pool bound to main loop) raises a
        # different one. Both are the same class of cross-loop dispatch
        # failure — graceful degrade is appropriate either way.
        if (
            "Cannot synchronously wait on the shared asyncio worker loop from itself" in _msg
            or "pool is bound to a different event loop" in _msg
        ):
            logger.warning(
                "user_profile sync dispatch blocked by ASYNC-1 cross-loop guard; "
                "returning None (profile not loaded). Caller should use the "
                "async-native variant.",
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


_PROFILE_TABLE_ENSURED = False


def _ensure_profile_table() -> None:
    """Idempotent CREATE TABLE IF NOT EXISTS guarded by a module-level flag.

    The DDL is itself idempotent at the SQL level, but every invocation
    burns a ``run_async_synchronously`` round-trip in cloud Postgres mode.
    Caching the success eliminates that cost on the hot path
    (``_load_profile_payload`` → ``_ensure_profile_table`` is called from
    every request handler that touches a profile).
    """
    global _PROFILE_TABLE_ENSURED
    if _PROFILE_TABLE_ENSURED:
        return
    db = _get_auth_db()
    if hasattr(db, "pool"):

        async def _assert() -> None:
            async with db.connection() as conn:
                from core.db_backend import assert_pg_relations

                await assert_pg_relations(conn, ("public.sync_user_profiles",), owner="services.user_profile")

        _run_async(_assert())
    else:
        ddl = (
            "CREATE TABLE IF NOT EXISTS user_profiles ("
            "user_id TEXT PRIMARY KEY,"
            "profile_json TEXT NOT NULL DEFAULT '{}',"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL"
            ")"
        )
        db.connection.execute(ddl)
        db.connection.commit()

    _PROFILE_TABLE_ENSURED = True


def _profile_email_cleared_json(profile_json: str) -> tuple[str, bool]:
    try:
        payload = json.loads(profile_json or "{}")
    except Exception:
        logger.debug("Skipping invalid stored user profile JSON during placeholder email cleanup")
        return profile_json, False
    if not isinstance(payload, dict) or not is_placeholder_email(payload.get("email")):
        return profile_json, False
    payload = dict(payload)
    payload["email"] = ""
    return json.dumps(payload, ensure_ascii=False), True


def clear_placeholder_profile_emails(*, force: bool = False) -> int:
    """Clear placeholder emails from legacy local/device profile rows."""
    global _PLACEHOLDER_PROFILE_EMAIL_MIGRATION_DONE
    if _PLACEHOLDER_PROFILE_EMAIL_MIGRATION_DONE and not force:
        return 0

    _ensure_profile_table()
    db = _get_auth_db()
    if hasattr(db, "pool"):
        _PLACEHOLDER_PROFILE_EMAIL_MIGRATION_DONE = True
        return 0

    now = datetime.now(UTC).isoformat()
    from core.user_context import legacy_local_user_id

    params = (legacy_local_user_id(), "device-%", "session:%", "transient:%")

    cleared = 0
    rows = db.connection.execute(
        """
        SELECT user_id, profile_json FROM user_profiles
        WHERE user_id = ? OR user_id LIKE ? OR user_id LIKE ? OR user_id LIKE ?
        """,
        params,
    ).fetchall()
    for row in rows:
        user_id = row["user_id"] if hasattr(row, "keys") else row[0]
        raw_json = row["profile_json"] if hasattr(row, "keys") else row[1]
        profile_json, changed = _profile_email_cleared_json(str(raw_json or "{}"))
        if not changed:
            continue
        db.connection.execute(
            "UPDATE user_profiles SET profile_json = ?, updated_at = ? WHERE user_id = ?",
            (profile_json, now, user_id),
        )
        cleared += 1
    if cleared:
        db.connection.commit()

    if cleared:
        logger.info("Cleared %d placeholder email(s) from local profile rows", cleared)
    _PLACEHOLDER_PROFILE_EMAIL_MIGRATION_DONE = True
    return cleared


async def _load_profile_payload_async(user_id: str) -> dict[str, Any] | None:
    """Async-native load of the user_profiles row for ``user_id``.

    Async callers (FastAPI route handlers, MCP servers running on the
    request loop) should ``await`` this directly to avoid the
    ``run_async_synchronously`` thread-blocking detour. The sync wrapper
    ``_load_profile_payload`` continues to work for legacy sync callers
    via main-loop dispatch (see ASYNC-1).
    """
    _ensure_profile_table()
    clear_placeholder_profile_emails()
    db = _get_auth_db()

    if hasattr(db, "pool"):
        from services.sync_surfaces import user_profiles as profiles_surface

        async with db.connection() as conn:
            from services.sync.consent import (
                has_cloud_sync_consent,
            )  # consent-read-only-ok

            if not await has_cloud_sync_consent(conn, user_id):  # consent-read-only-ok
                return None
            row_dict = await profiles_surface.get_user_profile(conn, user_id)
    else:
        row_obj = db.connection.execute(
            "SELECT profile_json FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        row_dict = dict(row_obj) if row_obj else None

    if row_dict is None:
        return None

    profile_json = row_dict.get("profile_json")
    if isinstance(profile_json, dict):
        return _sanitize_profile_payload(profile_json)
    try:
        payload = json.loads(profile_json) if profile_json else {}
    except Exception:
        logger.exception("Failed to decode stored user profile for user %s", user_id)
        return {}
    return _sanitize_profile_payload(payload) if isinstance(payload, dict) else {}


def _load_profile_payload(user_id: str) -> dict[str, Any] | None:
    """Sync wrapper around ``_load_profile_payload_async`` for legacy callers.

    New async-native callers should switch to ``_load_profile_payload_async``
    to avoid blocking a threadpool thread on every read.
    """
    return _run_async(_load_profile_payload_async(user_id))


async def _save_profile_payload_async(user_id: str, payload: dict[str, Any]) -> None:
    """Async-native upsert of the user_profiles row for ``user_id``.

    See ``_load_profile_payload_async`` for the design rationale (Path
    away from threadpool-blocking sync wrappers).
    """
    payload = _sanitize_profile_payload(payload)
    if _is_non_auth_identity(user_id):
        return

    _ensure_profile_table()
    db = _get_auth_db()

    if hasattr(db, "pool"):
        from services.sync_surfaces import user_profiles as profiles_surface

        async with db.connection() as conn:
            from services.sync.consent import has_cloud_sync_consent_locked

            if not await has_cloud_sync_consent_locked(conn, user_id):
                logger.info(
                    "Skipping cloud user-profile write for user %s without cloud-sync consent",
                    user_id,
                )
                return
            await profiles_surface.upsert_user_profile(conn, user_id, {"profile_json": payload})
        return
    # SQLite path falls through to the sync code below
    await asyncio.sleep(0)  # pragma: no cover — yield once for symmetry


def _save_profile_payload(user_id: str, payload: dict[str, Any]) -> None:
    """Sync wrapper around ``_save_profile_payload_async``.

    Falls through to the SQLite sync path inline because SQLite needs
    no event loop.
    """
    payload = _sanitize_profile_payload(payload)
    if _is_non_auth_identity(user_id):
        return

    _ensure_profile_table()
    db = _get_auth_db()
    now = datetime.now(UTC).isoformat()
    profile_json = json.dumps(payload, ensure_ascii=False)

    if hasattr(db, "pool"):
        _run_async(_save_profile_payload_async(user_id, payload))
        return

    existing = db.connection.execute(
        "SELECT user_id FROM user_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if existing is None:
        db.connection.execute(
            "INSERT INTO user_profiles (user_id, profile_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, profile_json, now, now),
        )
    else:
        db.connection.execute(
            "UPDATE user_profiles SET profile_json = ?, updated_at = ? WHERE user_id = ?",
            (profile_json, now, user_id),
        )
    db.connection.commit()


@dataclass
class UserProfile:
    """Structured personal information for the user."""

    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    email: str = ""
    preferred_state_of_incorporation: str = ""

    def __post_init__(self) -> None:
        self.email = valid_user_email_or_empty(self.email)

    @classmethod
    def load(cls, user_id: str | None = None) -> UserProfile:
        """Load profile from auth DB. For desktop/local identities OR when no
        user_id is given, fall back to legacy JSON + env vars (the "this is the
        founder's desktop" defaults). For NAMED AUTH USERS, never fall back to
        those defaults — that would let one user's data answer for another.

        Origin incident (2026-05-20): every test user_id ('user-1', 'jay',
        'test-user', etc.) was resolving to the founder's email and profile
        because legacy JSON + env vars applied unconditionally. In dev that
        spammed the founder; in production multi-tenant SaaS, any cache miss
        or replication lag on the auth DB lookup would have leaked one user's
        data to another, or sent a real user's confirmation link to the
        founder's address.
        """
        _ensure_legacy_profile_file_migrated()
        profile = cls()
        # Named auth users only get DB data — no env vars, no legacy file.
        is_named_auth_user = bool(user_id) and not _is_non_auth_identity(user_id)
        if user_id:
            data = _load_profile_payload(user_id)
            if data is None and PROFILE_PATH.exists() and not is_named_auth_user:
                # Legacy JSON is the desktop founder's profile — only valid as
                # a seed for local-desktop identities.
                try:
                    data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and not _is_non_auth_identity(user_id):
                        _save_profile_payload(user_id, data)
                except Exception:
                    logger.exception("Failed to import legacy user profile for user %s", user_id)
                    data = {}
            if isinstance(data, dict):
                for key, value in data.items():
                    if key in cls.__dataclass_fields__ and value:
                        setattr(profile, key, str(value))
        elif PROFILE_PATH.exists():
            try:
                data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for key, value in data.items():
                        if key in cls.__dataclass_fields__ and value:
                            setattr(profile, key, str(value))
            except Exception:
                logger.debug("Failed to load legacy user profile from %s", PROFILE_PATH)

        if not is_named_auth_user:
            for field_name, env_var in _ENV_MAP.items():
                if not getattr(profile, field_name, ""):
                    env_val = os.environ.get(env_var, "")
                    if env_val:
                        setattr(profile, field_name, env_val)

        profile.email = valid_user_email_or_empty(profile.email)

        if profile.full_name and not profile.first_name:
            parts = profile.full_name.strip().split()
            if parts:
                profile.first_name = parts[0]
            if len(parts) > 1:
                profile.last_name = parts[-1]

        return profile

    def save(self, user_id: str | None = None) -> None:
        """Persist profile to auth DB when a user ID is available."""
        _ensure_legacy_profile_file_migrated()
        self.email = valid_user_email_or_empty(self.email)
        if user_id:
            _save_profile_payload(user_id, asdict(self))
            return

        try:
            PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PROFILE_PATH.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save user profile to %s", PROFILE_PATH)

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in self.__dataclass_fields__)

    def get_profile_context(
        self,
        task_text: str | None = None,
        *,
        canonical_name: str = "",
        canonical_address: dict[str, Any] | None = None,
    ) -> str:
        """Render the structured user profile as a context block for the model.

        ``task_text`` is accepted for backwards compatibility but is NOT used
        to filter which fields appear. R5-P0-D (2026-05-30) deleted the
        ``is_forms_or_legal_task`` / ``is_profile_prefill_task`` /
        ``line_matches_task`` keyword classifiers that gated this block by a
        brittle wordlist (matched "filing" / "food", missed "paperwork" /
        "lunch order"). The profile is short — emit every populated line
        every turn, let the model decide what to use.

        ``canonical_name`` and ``canonical_address`` are still honored to
        dedupe lines that would simply repeat the ACCOUNT OWNER block;
        that's data uniqueness, not query classification.
        """
        del task_text  # accepted for compat; no longer drives filtering

        if self.is_empty():
            return ""

        field_labels = {
            "full_name": "Name",
            "address": "Address",
            "city": "City",
            "state": "State",
            "zip_code": "ZIP",
            "phone": "Phone",
            "email": "Email",
            "preferred_state_of_incorporation": "Preferred state for business filings",
        }

        canonical_name_norm = normalize_context_text(canonical_name)
        canonical_address_norms = {
            normalize_context_text(str(value)) for value in (canonical_address or {}).values() if str(value).strip()
        }

        lines = ["USER PROFILE (use this to pre-fill forms and personalize responses):"]
        for field_name, label in field_labels.items():
            value_text = str(getattr(self, field_name, "")).strip()
            if not value_text:
                continue

            value_norm = normalize_context_text(value_text)
            # Skip data that would just repeat the ACCOUNT OWNER block.
            if field_name == "full_name" and canonical_name_norm and value_norm == canonical_name_norm:
                continue
            if (
                field_name in {"address", "city", "state", "zip_code"}
                and canonical_address_norms
                and value_norm in canonical_address_norms
            ):
                continue

            lines.append("  %s: %s" % (label, value_text))

        if len(lines) <= 1:
            return ""
        return "\n".join(lines)


def load_profile(user_id: str) -> UserProfile:
    return UserProfile.load(_require_profile_user_id(user_id))


def save_profile(user_id: str, data: UserProfile | dict[str, Any]) -> UserProfile:
    cache_key = _require_profile_user_id(user_id)
    profile = data if isinstance(data, UserProfile) else UserProfile(**data)
    profile.save(cache_key)
    _PROFILE_CACHE[cache_key] = profile
    return profile


def _require_profile_user_id(user_id: str | None) -> str:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id is required for user profile access")
    return user_id.strip()


def get_user_profile(user_id: str | None = None) -> UserProfile:
    cache_key = _require_profile_user_id(user_id)
    profile = _PROFILE_CACHE.get(cache_key)
    if profile is None:
        profile = UserProfile.load(cache_key)
        _PROFILE_CACHE[cache_key] = profile
    return profile


def reload_user_profile(user_id: str | None = None) -> UserProfile:
    cache_key = _require_profile_user_id(user_id)
    profile = UserProfile.load(cache_key)
    _PROFILE_CACHE[cache_key] = profile
    return profile
