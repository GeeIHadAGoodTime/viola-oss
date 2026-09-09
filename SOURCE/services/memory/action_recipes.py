"""Recent action recipe storage for repeat-action requests.

This module records compact summaries of successful multi-step actions so
follow-up turns like "my usual" or "do it like last time" can be grounded in
recent user-specific history instead of forcing the model to rebuild from
scratch.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from services.persistence.state_store import STATE_DB_SCHEMA_LOCK

logger = get_logger(__name__)

_DB_FILENAME = "state.sqlite3"
_MAX_SUMMARY_CHARS = 500
_MAX_TEXT_CHARS = 1000
_MAX_RECIPES_PER_USER = 100

_SENSITIVE_PARAM_RE = re.compile(
    r"(?:password|passcode|secret|token|api[_-]?key|card|cvv|cvc|ssn|social_security)",
    re.IGNORECASE,
)
_RECIPE_ENCRYPTED_PREFIX = "viola-action-recipe-fernet-v1:"


@dataclass(frozen=True)
class ActionRecipe:
    """A compact record of a prior action that can be repeated later."""

    id: str
    user_id: str
    intent: str
    summary: str
    user_text: str
    assistant_text: str
    params: dict[str, Any]
    steps: list[dict[str, Any]]
    source: str
    created_at: float
    updated_at: float
    last_used_at: float | None
    use_count: int


def _shorten(text: str, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def _scrub_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_PARAM_RE.search(key_text):
                continue
            cleaned[key_text] = _scrub_sensitive(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_scrub_sensitive(item) for item in value]
    return value


def _loads_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _loads_json_steps(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    steps: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            steps.append({str(key): val for key, val in item.items()})
    return steps


def _recipe_encryption(db_path: Path) -> Any:
    from services.memory.store import _get_memory_encryption

    return _get_memory_encryption(db_path=db_path)


def _encrypt_recipe_field(db_path: Path, plaintext: str) -> str:
    return "%s%s" % (_RECIPE_ENCRYPTED_PREFIX, _recipe_encryption(db_path).encrypt(plaintext))


def _decrypt_recipe_field(db_path: Path, value: str | None) -> str:
    raw = str(value or "")
    if not raw.startswith(_RECIPE_ENCRYPTED_PREFIX):
        return raw
    return _recipe_encryption(db_path).decrypt(raw[len(_RECIPE_ENCRYPTED_PREFIX) :])


def _intent_token(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    normalized = "".join(ch if ch.isalnum() else "." for ch in text)
    token = ".".join(part for part in normalized.split(".") if part)
    return token[:80]


def infer_action_intent(
    text: str | None = None,
    *,
    command: str | None = None,
    tools_called: Iterable[str] | None = None,
) -> str:
    """Return a stable recipe bucket from structural runtime signals only.

    ``text`` remains accepted for compatibility, but user wording is not
    classified here. The model sees raw recent recipes and decides relevance.
    """

    _ = text
    command_token = _intent_token(command)
    if command_token:
        return "command.%s" % command_token
    tool_tokens = [_intent_token(str(tool)) for tool in (tools_called or [])]
    tool_tokens = [token for token in tool_tokens if token]
    if tool_tokens:
        return "tools.%s" % "+".join(tool_tokens[:3])
    return "general.action"


def build_recipe_summary(
    *,
    intent: str,
    user_text: str,
    assistant_text: str = "",
    params: dict[str, Any] | None = None,
) -> str:
    """Build a concise human-readable recipe summary."""

    cleaned_params = _scrub_sensitive(params or {})
    bits = [intent]
    if user_text:
        bits.append(_shorten(user_text, 220))
    if cleaned_params:
        try:
            bits.append(json.dumps(cleaned_params, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        except TypeError:
            bits.append(str(cleaned_params))
    elif assistant_text:
        bits.append(_shorten(assistant_text, 220))
    return _shorten(" | ".join(bits), _MAX_SUMMARY_CHARS)


class ActionRecipeStore:
    """SQLite-backed recent action recipe store."""

    def __init__(self, root: Path | None = None) -> None:
        if root is not None:
            base_path = Path(root)
        else:
            try:
                from config.settings import settings as _cfg

                base_path = Path(_cfg.data_dir)
            except Exception:
                base_path = Path.cwd()

        self._db_path = base_path / "data" / "persistence" / _DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with STATE_DB_SCHEMA_LOCK, self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS action_recipes (
                    id             TEXT PRIMARY KEY,
                    user_id        TEXT NOT NULL,
                    intent         TEXT NOT NULL,
                    summary        TEXT NOT NULL,
                    user_text      TEXT NOT NULL,
                    assistant_text TEXT NOT NULL,
                    params_json    TEXT NOT NULL,
                    steps_json     TEXT NOT NULL,
                    source         TEXT NOT NULL,
                    created_at     REAL NOT NULL,
                    updated_at     REAL NOT NULL,
                    last_used_at   REAL,
                    use_count      INTEGER NOT NULL DEFAULT 0
                )
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_action_recipes_user_intent_updated
                ON action_recipes(user_id, intent, updated_at DESC)
                """)
            self._conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_action_recipes_user_updated
                ON action_recipes(user_id, updated_at DESC)
                """)

    def record(
        self,
        *,
        user_id: str,
        intent: str,
        user_text: str,
        assistant_text: str = "",
        params: dict[str, Any] | None = None,
        steps: list[dict[str, Any]] | None = None,
        summary: str | None = None,
        source: str = "agent",
    ) -> ActionRecipe:
        """Record a recent action recipe and return the stored record."""

        now = time.time()
        recipe_id = str(uuid.uuid4())
        cleaned_params = _scrub_sensitive(params or {})
        cleaned_steps = _scrub_sensitive(steps or [])
        if not isinstance(cleaned_steps, list):
            cleaned_steps = []
        summary_text = summary or build_recipe_summary(
            intent=intent,
            user_text=user_text,
            assistant_text=assistant_text,
            params=cleaned_params,
        )
        summary_text = _shorten(summary_text, _MAX_SUMMARY_CHARS)
        user_text_clean = _shorten(user_text, _MAX_TEXT_CHARS)
        assistant_text_clean = _shorten(assistant_text, _MAX_TEXT_CHARS)
        params_json = json.dumps(cleaned_params, ensure_ascii=False, sort_keys=True)
        steps_json = json.dumps(cleaned_steps, ensure_ascii=False, sort_keys=True)
        stored_summary = _encrypt_recipe_field(self._db_path, summary_text)
        stored_user_text = _encrypt_recipe_field(self._db_path, user_text_clean)
        stored_assistant_text = _encrypt_recipe_field(self._db_path, assistant_text_clean)
        stored_params_json = _encrypt_recipe_field(self._db_path, params_json)
        stored_steps_json = _encrypt_recipe_field(self._db_path, steps_json)

        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO action_recipes (
                    id, user_id, intent, summary, user_text, assistant_text,
                    params_json, steps_json, source, created_at, updated_at,
                    last_used_at, use_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)
                """,
                (
                    recipe_id,
                    user_id,
                    intent,
                    stored_summary,
                    stored_user_text,
                    stored_assistant_text,
                    stored_params_json,
                    stored_steps_json,
                    source,
                    now,
                    now,
                ),
            )
            self._prune_locked(user_id)

        return ActionRecipe(
            id=recipe_id,
            user_id=user_id,
            intent=intent,
            summary=summary_text,
            user_text=user_text_clean,
            assistant_text=assistant_text_clean,
            params=cleaned_params,
            steps=cleaned_steps,
            source=source,
            created_at=now,
            updated_at=now,
            last_used_at=None,
            use_count=0,
        )

    def recent(self, *, user_id: str, intent: str | None = None, limit: int = 3) -> list[ActionRecipe]:
        """Return the user's recent action recipes, newest first."""

        limit = max(1, min(int(limit or 1), 20))
        with self._lock:
            if intent:
                rows = self._conn.execute(
                    """
                    SELECT *
                    FROM action_recipes
                    WHERE user_id = ? AND intent = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, intent, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT *
                    FROM action_recipes
                    WHERE user_id = ?
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
        return [self._row_to_recipe(row) for row in rows]

    def find_for_text(self, *, user_id: str, text: str, limit: int = 3) -> list[ActionRecipe]:
        """Return recent recipes as neutral context for the model to judge."""

        _ = text
        return self.recent(user_id=user_id, limit=limit)

    def touch(self, recipe_id: str, *, user_id: str) -> bool:
        """Mark a recipe as used."""

        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                UPDATE action_recipes
                SET last_used_at = ?, use_count = use_count + 1, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, now, recipe_id, user_id),
            )
            return cursor.rowcount > 0

    def delete_all_for_user(self, user_id: str) -> int:
        """Delete all recipes for one user. Intended for tests/privacy reset."""

        with self._lock, self._conn:
            cursor = self._conn.execute("DELETE FROM action_recipes WHERE user_id = ?", (user_id,))
            return int(cursor.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _prune_locked(self, user_id: str) -> None:
        rows = self._conn.execute(
            """
            SELECT id
            FROM action_recipes
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT -1 OFFSET ?
            """,
            (user_id, _MAX_RECIPES_PER_USER),
        ).fetchall()
        if not rows:
            return
        stale_ids = [str(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in stale_ids)
        delete_sql = f"DELETE FROM action_recipes WHERE user_id = ? AND id IN ({placeholders})"  # nosec B608
        self._conn.execute(
            delete_sql,
            (user_id, *stale_ids),
        )

    def _row_to_recipe(self, row: sqlite3.Row) -> ActionRecipe:
        summary = _decrypt_recipe_field(self._db_path, row["summary"])
        user_text = _decrypt_recipe_field(self._db_path, row["user_text"])
        assistant_text = _decrypt_recipe_field(self._db_path, row["assistant_text"])
        params_json = _decrypt_recipe_field(self._db_path, row["params_json"])
        steps_json = _decrypt_recipe_field(self._db_path, row["steps_json"])
        return ActionRecipe(
            id=str(row["id"]),
            user_id=str(row["user_id"]),
            intent=str(row["intent"]),
            summary=summary,
            user_text=user_text,
            assistant_text=assistant_text,
            params=_loads_json_dict(params_json),
            steps=_loads_json_steps(steps_json),
            source=str(row["source"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_used_at=float(row["last_used_at"]) if row["last_used_at"] is not None else None,
            use_count=int(row["use_count"]),
        )


_store_lock = threading.RLock()
_store_singleton: ActionRecipeStore | None = None


def get_action_recipe_store(root: Path | None = None) -> ActionRecipeStore:
    """Return the process-wide action recipe store."""

    global _store_singleton
    with _store_lock:
        if root is not None:
            return ActionRecipeStore(root=root)
        if _store_singleton is None:
            _store_singleton = ActionRecipeStore()
        return _store_singleton


def reset_action_recipe_store_for_tests() -> None:
    """Close and clear the singleton so tests can isolate temporary stores."""

    global _store_singleton
    with _store_lock:
        if _store_singleton is not None:
            try:
                _store_singleton.close()
            except Exception:
                logger.debug("Failed to close action recipe store during reset", exc_info=True)
        _store_singleton = None
