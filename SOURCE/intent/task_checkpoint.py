"""Task checkpoint persistence for agent crash recovery."""

from __future__ import annotations

import json
import shutil
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.constants import TIMEOUT_10_MINUTES
from core.logging_config import get_logger
from core.platform import get_data_dir
from intent.log_redaction import redact_card_data

logger = get_logger(__name__)


def _get_checkpoint_encryption():
    """Return the shared _MemoryEncryption singleton for checkpoint content."""
    from services.memory.store import _get_memory_encryption

    enc = _get_memory_encryption()
    if enc is None or not getattr(enc, "encryption_available", False):
        raise RuntimeError("checkpoint encryption is unavailable")
    return enc


CHECKPOINT_DIR = get_data_dir() / "tasks"
_CHECKPOINT_ENCRYPTED_PREFIX = "viola-checkpoint-fernet-v1:"
_LEGACY_CHECKPOINT_DIR = Path.home().joinpath(".viola", "tasks")
_LEGACY_CHECKPOINT_MIGRATION_DONE = False
_MIN_STEPS_FOR_CHECKPOINT = 3
_MAX_TOOL_RESULT_LEN = 500
_CHECKPOINT_MAX_AGE_HOURS = 24
_CHECKPOINT_KEEP_MIN = 10
WAITING_GATE_CONTEXT_TTL_SECONDS = TIMEOUT_10_MINUTES
RESUMABLE_CHECKPOINT_CONTEXT_TTL_SECONDS = TIMEOUT_10_MINUTES
_WAITING_GATE_EXPIRED_STATUS = "expired"
_WAITING_GATE_EXPIRED_OUTCOME = "waiting_gate_context_expired"


def _migrate_legacy_checkpoint_dir() -> None:
    global _LEGACY_CHECKPOINT_MIGRATION_DONE
    if _LEGACY_CHECKPOINT_MIGRATION_DONE:
        return
    _LEGACY_CHECKPOINT_MIGRATION_DONE = True
    if not _LEGACY_CHECKPOINT_DIR.exists():
        return
    try:
        if not CHECKPOINT_DIR.exists():
            shutil.copytree(_LEGACY_CHECKPOINT_DIR, CHECKPOINT_DIR)
            logger.info(
                "Migrated legacy task checkpoints from %s to %s",
                _LEGACY_CHECKPOINT_DIR,
                CHECKPOINT_DIR,
            )
            return
        for child in _LEGACY_CHECKPOINT_DIR.iterdir():
            target = CHECKPOINT_DIR / child.name
            if target.exists():
                continue
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
        logger.info(
            "Merged legacy task checkpoints from %s into %s",
            _LEGACY_CHECKPOINT_DIR,
            CHECKPOINT_DIR,
        )
    except OSError as exc:
        logger.warning(
            "Could not migrate legacy task checkpoints from %s to %s: %s",
            _LEGACY_CHECKPOINT_DIR,
            CHECKPOINT_DIR,
            exc,
        )


def _parse_checkpoint_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _waiting_checkpoint_is_stale(
    data: dict[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> bool:
    gate_updated_at = _parse_checkpoint_time(data.get("updated_at") or data.get("created_at"))
    if gate_updated_at is None:
        return True
    return (now - gate_updated_at).total_seconds() > max_age_seconds


def _write_checkpoint_data(path: Path, data: dict[str, Any], enc: Any) -> None:
    content = json.dumps(redact_card_data(data), indent=2, default=str)
    content = "%s%s" % (_CHECKPOINT_ENCRYPTED_PREFIX, enc.encrypt(content))
    path.write_text(content, encoding="utf-8")


def _read_checkpoint_data(path: Path, enc: Any) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if raw.startswith(_CHECKPOINT_ENCRYPTED_PREFIX):
        raw = enc.decrypt(raw[len(_CHECKPOINT_ENCRYPTED_PREFIX) :])
    elif raw.startswith("gAAAAA"):
        raw = enc.decrypt(raw)
    else:
        raise ValueError("plaintext checkpoint rejected")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("checkpoint payload is not an object")
    return data


def _expire_stale_waiting_checkpoint(
    path: Path,
    data: dict[str, Any],
    enc: Any,
    *,
    now: datetime,
) -> None:
    data["status"] = _WAITING_GATE_EXPIRED_STATUS
    data["outcome"] = _WAITING_GATE_EXPIRED_OUTCOME
    data["updated_at"] = now.isoformat()
    try:
        _write_checkpoint_data(path, data, enc)
        logger.info("Expired stale waiting gate checkpoint %s", data.get("task_id") or path.stem)
    except Exception:
        logger.exception("Failed to encrypt expired waiting gate checkpoint %s; deleting it", path)
        try:
            path.unlink()
        except OSError:
            logger.debug("Failed to delete unrewritable checkpoint %s", path, exc_info=True)


def _resolve_checkpoint_dir(user_id: str | None = None) -> Path:
    """Return the checkpoint directory for a user.

    Per-user isolation: each user's checkpoints are stored in a
    subdirectory named by their user_id.
    """
    if not user_id:
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except Exception:
            from core.user_context import get_device_user_id

            user_id = get_device_user_id()
    _migrate_legacy_checkpoint_dir()
    return CHECKPOINT_DIR / user_id


def _cleanup_stale_checkpoints() -> None:
    """Delete checkpoint files older than *_CHECKPOINT_MAX_AGE_HOURS*.

    Keeps the *_CHECKPOINT_KEEP_MIN* most-recently-modified files regardless
    of age so a productive session is never wiped.  Files modified within
    the last 60 seconds are always kept (they may belong to a task that is
    still running).

    Called once at module import — best-effort, never raises.
    """
    _migrate_legacy_checkpoint_dir()
    if not CHECKPOINT_DIR.exists():
        return

    try:
        # Collect (mtime, path) for every checkpoint file.
        entries: list[tuple[float, Path]] = []
        for p in CHECKPOINT_DIR.rglob("*.json"):
            try:
                entries.append((p.stat().st_mtime, p))
            except OSError:
                continue

        if not entries:
            return

        # Sort newest-first so the first N are the ones we keep.
        entries.sort(key=lambda e: e[0], reverse=True)

        now = datetime.now(UTC).timestamp()
        max_age_secs = _CHECKPOINT_MAX_AGE_HOURS * 3600
        active_grace_secs = 60  # never touch files < 60s old

        removed = 0
        for idx, (mtime, path) in enumerate(entries):
            # Always keep the N most recent files.
            if idx < _CHECKPOINT_KEEP_MIN:
                continue
            age = now - mtime
            # Never delete a file that may be actively written.
            if age < active_grace_secs:
                continue
            if age > max_age_secs:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass

        if removed:
            logger.info(
                "Cleaned up %d stale checkpoint file(s) (kept %d)",
                removed,
                len(entries) - removed,
            )
    except Exception:
        # Best-effort cleanup — never prevent module loading.
        logger.debug("Checkpoint cleanup skipped due to error", exc_info=True)


@dataclass
class CheckpointStep:
    """A single tool invocation within a task."""

    index: int
    tool: str
    input: dict[str, Any]
    output: str = ""
    duration_ms: int = 0
    timestamp: str = ""
    verified: bool = False

    def __post_init__(self) -> None:
        self.input = redact_card_data(self.input)
        self.output = redact_card_data(self.output)
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat()


@dataclass
class TaskCheckpoint:
    """Full state snapshot of an agent task."""

    task_id: str
    task_description: str
    status: str = "in_progress"
    step_index: int = 0
    steps: list[CheckpointStep] = field(default_factory=list)
    llm_messages: list[dict[str, Any]] = field(default_factory=list)
    llm_continuity: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    parent_task_id: str | None = None
    child_task_ids: list[str] = field(default_factory=list)
    outcome: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        now = datetime.now(UTC).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now


def generate_task_id() -> str:
    """Return a short unique task identifier."""
    return uuid.uuid4().hex[:12]


def should_checkpoint(step_count: int) -> bool:
    """Return True if the task has enough steps to warrant persistence."""
    return step_count >= _MIN_STEPS_FOR_CHECKPOINT


def save_checkpoint(checkpoint: TaskCheckpoint, user_id: str | None = None) -> None:
    """Write checkpoint JSON to disk (encrypted at rest). Best-effort — logs on failure."""
    try:
        cp_dir = _resolve_checkpoint_dir(user_id)
        cp_dir.mkdir(parents=True, exist_ok=True)
        data = redact_card_data(asdict(checkpoint))
        enc = _get_checkpoint_encryption()
        path = cp_dir / f"{checkpoint.task_id}.json"
        _write_checkpoint_data(path, data, enc)
    except Exception:
        logger.exception("Failed to save checkpoint %s", checkpoint.task_id)
        raise


def _word_overlap_score(a: str, b: str) -> float:
    """Return fraction of shared words between two strings."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / max(len(words_a), len(words_b))


def _checkpoint_from_data(data: dict[str, Any]) -> TaskCheckpoint:
    steps = [CheckpointStep(**s) for s in data.pop("steps", [])]
    return TaskCheckpoint(**data, steps=steps)


def _load_encrypted_checkpoint_path(path: Path) -> dict[str, Any] | None:
    try:
        return _read_checkpoint_data(path, _get_checkpoint_encryption())
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError):
        logger.warning(
            "Checkpoint skipped because it is missing a valid encrypted envelope: %s",
            path,
        )
        return None


def load_checkpoint(task_id: str, user_id: str | None = None) -> TaskCheckpoint | None:
    """Load an encrypted checkpoint. Plaintext checkpoints fail closed."""
    cp_dir = _resolve_checkpoint_dir(user_id)
    candidates = [cp_dir / f"{task_id}.json", CHECKPOINT_DIR / f"{task_id}.json"]
    for path in candidates:
        if not path.exists():
            continue
        data = _load_encrypted_checkpoint_path(path)
        if data is None:
            return None
        try:
            return _checkpoint_from_data(data)
        except (TypeError, KeyError):
            logger.warning("Corrupt checkpoint file: %s", task_id)
            return None
    logger.warning("Checkpoint file not found: %s", task_id)
    return None


def find_resumable(task_description: str, user_id: str | None = None) -> TaskCheckpoint | None:
    """Find the most recent encrypted in-progress checkpoint matching the description."""
    cp_dir = _resolve_checkpoint_dir(user_id)
    if not cp_dir.exists():
        return None
    best: TaskCheckpoint | None = None
    best_time = ""
    for path in cp_dir.glob("*.json"):
        data = _load_encrypted_checkpoint_path(path)
        if data is None or data.get("status") not in ("in_progress", "interrupted"):
            continue
        if _word_overlap_score(task_description, str(data.get("task_description") or "")) < 0.5:
            continue
        updated = str(data.get("updated_at") or "")
        if best is None or updated > best_time:
            try:
                best = _checkpoint_from_data(data)
                best_time = updated
            except (TypeError, KeyError):
                continue
    return best


def get_latest_resumable(
    user_id: str | None = None,
    *,
    max_age_seconds: float | None = None,
) -> TaskCheckpoint | None:
    """Return the most recently updated encrypted resumable checkpoint."""
    cp_dir = _resolve_checkpoint_dir(user_id)
    if not cp_dir.exists():
        return None
    best: TaskCheckpoint | None = None
    best_time = ""
    now = datetime.now(UTC)
    for path in cp_dir.glob("*.json"):
        data = _load_encrypted_checkpoint_path(path)
        if data is None:
            continue
        if data.get("status", "") not in (
            "in_progress",
            "interrupted",
            "error",
            "failed",
            "timed_out",
            "waiting_for_user",
        ):
            continue
        updated_at = _parse_checkpoint_time(data.get("updated_at") or data.get("created_at"))
        if max_age_seconds is not None:
            if updated_at is None:
                continue
            if (now - updated_at).total_seconds() > max_age_seconds:
                continue
        updated = updated_at.isoformat() if updated_at is not None else str(data.get("updated_at") or "")
        if best is None or updated > best_time:
            try:
                best = _checkpoint_from_data(data)
                best_time = updated
            except (TypeError, KeyError):
                continue
    return best


def get_latest_waiting_checkpoint(
    user_id: str | None = None,
    *,
    gate_type: str | None = None,
    max_age_seconds: float | None = None,
) -> TaskCheckpoint | None:
    """Return the latest encrypted waiting-for-user checkpoint."""
    cp_dir = _resolve_checkpoint_dir(user_id)
    if not cp_dir.exists():
        return None
    try:
        enc = _get_checkpoint_encryption()
    except Exception:
        logger.warning("Checkpoint encryption unavailable; waiting-checkpoint lookup failed closed")
        return None
    best: TaskCheckpoint | None = None
    best_time = ""
    now = datetime.now(UTC)
    for path in cp_dir.glob("*.json"):
        try:
            data = _read_checkpoint_data(path, enc)
        except (json.JSONDecodeError, OSError, ValueError):
            logger.warning(
                "Checkpoint skipped because it is missing a valid encrypted envelope: %s",
                path,
            )
            continue
        if data.get("status") != "waiting_for_user":
            continue
        context = data.get("context") or {}
        if gate_type and str(context.get("pending_gate_type") or "").strip().lower() != gate_type.lower():
            continue
        if max_age_seconds is not None and _waiting_checkpoint_is_stale(data, now=now, max_age_seconds=max_age_seconds):
            _expire_stale_waiting_checkpoint(path, data, enc, now=now)
            continue
        updated = str(data.get("updated_at") or "")
        if best is None or updated > best_time:
            try:
                best = _checkpoint_from_data(data)
                best_time = updated
            except (TypeError, KeyError):
                continue
    return best


def append_step(checkpoint: TaskCheckpoint, step: CheckpointStep) -> None:
    """Add a step, bump index and timestamp, then persist."""
    checkpoint.steps.append(step)
    checkpoint.step_index = len(checkpoint.steps)
    checkpoint.updated_at = datetime.now(UTC).isoformat()
    if should_checkpoint(len(checkpoint.steps)):
        save_checkpoint(checkpoint)


def _normalize_block(block: Any) -> dict[str, Any]:
    """Convert a single content block to a plain dict.

    Handles Anthropic SDK Pydantic objects (ToolUseBlock, TextBlock, etc.)
    that may be present in ``_native_messages`` at checkpoint time.  Without
    this, ``json.dumps(default=str)`` would mangle them into opaque strings,
    producing corrupt checkpoints that crash on resume (Appendix B5).
    """
    if isinstance(block, dict):
        return block
    if hasattr(block, "model_dump"):
        return block.model_dump()
    if hasattr(block, "to_dict"):
        return block.to_dict()
    # Manual fallback for known SDK block types
    block_type = getattr(block, "type", None)
    if block_type == "tool_use":
        return {
            "type": "tool_use",
            "id": getattr(block, "id", ""),
            "name": getattr(block, "name", ""),
            "input": getattr(block, "input", {}),
        }
    if block_type == "text":
        return {
            "type": "text",
            "text": getattr(block, "text", ""),
        }
    return {"type": "text", "text": str(block)}


def _normalize_message_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ensure every content-block list contains only plain dicts.

    This is the checkpoint-side counterpart of
    ``agent_executor._normalize_content_blocks``.  It must live here to
    avoid a circular import (agent_executor already imports from this
    module).
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [_normalize_block(b) for b in content]
    return messages


def compress_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a deep copy of messages with long tool results truncated.

    Also normalises SDK content-block objects to plain dicts so they
    survive JSON round-tripping through checkpoint save/load.
    """
    compressed = deepcopy(messages)
    # Normalise SDK objects → plain dicts before truncation.
    _normalize_message_content(compressed)
    for msg in compressed:
        content = msg.get("content")
        if isinstance(content, str) and msg.get("role") == "tool":
            if len(content) > _MAX_TOOL_RESULT_LEN:
                msg["content"] = content[:_MAX_TOOL_RESULT_LEN] + "..."
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                text = block.get("text", "")
                if isinstance(text, str) and len(text) > _MAX_TOOL_RESULT_LEN:
                    block["text"] = text[:_MAX_TOOL_RESULT_LEN] + "..."
    return compressed


def mark_complete(
    checkpoint: TaskCheckpoint,
    status: str,
    outcome: str | None = None,
) -> None:
    """Set final status and outcome, then persist."""
    checkpoint.status = status
    checkpoint.outcome = outcome
    checkpoint.updated_at = datetime.now(UTC).isoformat()
    save_checkpoint(checkpoint)


# ---------------------------------------------------------------------------
# Startup cleanup — runs once when the module is first imported.
# ---------------------------------------------------------------------------
_cleanup_stale_checkpoints()
