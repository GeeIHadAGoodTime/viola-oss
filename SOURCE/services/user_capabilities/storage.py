"""Per-user JSON storage for user-authored routines."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from core.logging_config import get_logger
from services.memory.dir import account_root, safe_account_id

from .schema import UserCapability, slugify_capability_id, validate_capability_id

logger = get_logger(__name__)

CAPABILITIES_DIR = "capabilities"
AUDIT_LOG = "audit.log"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class CapabilityStore:
    """Manage one user's routine files under ``users/<account>/capabilities``."""

    def __init__(self, user_id: str, root: Path | None = None, *, create: bool = True) -> None:
        self.account_id = (user_id or "").strip()
        self.user_id = safe_account_id(user_id)
        self.account_dir = account_root(user_id, root)
        self.root = self.account_dir / CAPABILITIES_DIR
        self.audit_path = self.root / AUDIT_LOG
        if create:
            self._ensure()

    def _ensure(self) -> None:
        self.account_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self.audit_path.touch(exist_ok=True)
        self._chmod_private()

    def _chmod_private(self) -> None:
        if os.name == "nt":
            return
        try:
            self.account_dir.chmod(0o700)
            self.root.chmod(0o700)
        except OSError:
            logger.debug("Could not set private mode on capability directory")

    def _path_for_id(self, capability_id: str) -> Path:
        return self.root / ("%s.json" % validate_capability_id(capability_id))

    def _append_audit_entry(self, entry: dict[str, Any]) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return str(entry["id"])

    def _audit(
        self,
        action: str,
        capability_id: str,
        detail: str,
        *,
        before: Any | None = None,
        after: Any | None = None,
    ) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        audit_id = "%s-%s" % (datetime.now(UTC).strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
        entry: dict[str, Any] = {
            "id": audit_id,
            "when": _now_iso(),
            "action": action,
            "capability_id": capability_id,
            "detail": detail[:4000],
            "source": "agent",
        }
        if before is not None:
            entry["before"] = before
        if after is not None:
            entry["after"] = after
        return self._append_audit_entry(entry)

    def _audit_run(
        self,
        *,
        action: str,
        source: str,
        capability_id: str,
        detail: str,
        extra: dict[str, Any],
    ) -> str:
        audit_id = "%s-%s" % (datetime.now(UTC).strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
        entry: dict[str, Any] = {
            "id": audit_id,
            "when": _now_iso(),
            "action": action,
            "capability_id": capability_id,
            "detail": detail[:4000],
            "source": source,
            **extra,
        }
        return self._append_audit_entry(entry)

    def audit_run_start(
        self,
        capability_id: str,
        *,
        plan_steps_count: int,
        started_at: str,
    ) -> str:
        """Append a run_start audit entry for a successfully-built routine plan."""
        return self._audit_run(
            action="run",
            source="run_start",
            capability_id=capability_id,
            detail="plan_steps=%d" % plan_steps_count,
            extra={
                "plan_steps_count": plan_steps_count,
                "started_at": started_at,
            },
        )

    def audit_run_complete(
        self,
        capability_id: str,
        *,
        started_at: str,
        plan_steps_count: int,
        status: str,
        executed_steps: list[dict[str, Any]],
        task_id: str | None = None,
    ) -> str:
        """Append a task-level completion audit entry for a routine run."""
        completed_at = _now_iso()
        extra: dict[str, Any] = {
            "started_at": started_at,
            "completed_at": completed_at,
            "plan_steps_count": plan_steps_count,
            "status": status,
            "executed_steps": executed_steps,
            "executed_tools": [step["tool"] for step in executed_steps if step.get("tool")],
        }
        if task_id:
            extra["task_id"] = task_id
        return self._audit_run(
            action="run_complete",
            source="run_complete",
            capability_id=capability_id,
            detail="status=%s executed_tools=%d" % (status, len(extra["executed_tools"])),
            extra=extra,
        )

    def next_available_id(self, name: str) -> str:
        """Return a name-derived id that does not collide in this user store."""
        base = slugify_capability_id(name)
        candidate = base
        counter = 2
        while self._path_for_id(candidate).exists():
            suffix = "_%d" % counter
            candidate = "%s%s" % (base[: 80 - len(suffix)].rstrip("_"), suffix)
            counter += 1
        return candidate

    def write(
        self,
        capability: UserCapability,
        *,
        audit_action: str = "write",
        detail: str | None = None,
        before: Any | None = None,
        after: Any | None = None,
    ) -> dict[str, Any]:
        """Atomically write one saved routine JSON file."""
        self._ensure()
        path = self._path_for_id(capability.id)
        payload = json.dumps(capability.model_dump(mode="json"), indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        tmp_path = self.root / (".%s.%s.tmp" % (capability.id, uuid.uuid4().hex))
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                logger.debug("Could not remove temporary capability file %s", tmp_path)
        audit_detail = detail or "bytes=%d" % len(payload.encode("utf-8"))
        audit_id = self._audit(
            audit_action,
            capability.id,
            audit_detail,
            before=before,
            after=after,
        )
        return {"ok": True, "audit_id": audit_id, "path": path.name}

    def read(self, capability_id: str) -> UserCapability | None:
        """Read one routine by id, returning None when it does not exist."""
        if not self.root.exists():
            return None
        path = self._path_for_id(capability_id)
        if not path.exists():
            return None
        return UserCapability.model_validate_json(path.read_text(encoding="utf-8"))

    def list(self) -> list[UserCapability]:
        """List routines sorted by newest created_at first."""
        if not self.root.exists():
            return []
        capabilities: list[UserCapability] = []
        for path in self.root.glob("*.json"):
            try:
                capabilities.append(UserCapability.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValidationError, ValueError) as exc:
                logger.warning("Skipping invalid capability file %s: %s", path, exc)
        return sorted(capabilities, key=lambda item: _parse_timestamp(item.created_at), reverse=True)

    def delete(self, capability_id: str) -> dict[str, Any]:
        """Delete a routine by id. Missing ids are treated as an idempotent success."""
        normalized_id = validate_capability_id(capability_id)
        if not self.root.exists():
            return {"ok": True, "id": normalized_id, "removed": False}
        path = self._path_for_id(normalized_id)
        if not path.exists():
            return {"ok": True, "id": normalized_id, "removed": False}
        before: dict[str, Any] | None = None
        try:
            before = UserCapability.model_validate_json(path.read_text(encoding="utf-8")).model_dump(mode="json")
        except (OSError, ValidationError, ValueError):
            before = None
        path.unlink()
        audit_id = self._audit("delete", normalized_id, "removed %s" % path.name, before=before)
        return {"ok": True, "id": normalized_id, "removed": True, "audit_id": audit_id}
