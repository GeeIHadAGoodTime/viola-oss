"""Automatic memory consolidation loop."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from core.constants import TIMEOUT_HOUR
from core.logging_config import get_logger
from services.memory.dir import MemoryDir, get_memory_dir
from services.memory.extract_memories import (
    ExtractedMemory,
    _normalize_extracted_memory,
    _parse_extractor_response,
)
from services.memory.store import redact_sensitive_prompt_text

logger = get_logger(__name__)


DreamConsolidator = Callable[[str], Sequence[ExtractedMemory | dict[str, Any] | str] | Awaitable[Sequence[Any]]]


@dataclass(frozen=True)
class AutoDreamConfig:
    enabled: bool = True
    min_hours: float = 24.0
    min_sessions: int = 5
    max_session_files: int = 20
    lock_stale_seconds: float = TIMEOUT_HOUR

    @classmethod
    def from_settings(cls) -> AutoDreamConfig:
        return cls(
            enabled=bool(settings.auto_memory_dream_enabled),
            min_hours=float(settings.auto_memory_dream_min_hours),
            min_sessions=max(1, int(settings.auto_memory_dream_min_sessions)),
        )


class AutoDreamService:
    """Consolidate session markdown into durable topic memories when due."""

    def __init__(
        self,
        *,
        user_id: str,
        memory_dir: MemoryDir | None = None,
        consolidator: DreamConsolidator | None = None,
        config: AutoDreamConfig | None = None,
        active_session_id: str | None = None,
    ) -> None:
        self.user_id = user_id
        self.memory_dir = memory_dir or get_memory_dir(user_id)
        self.consolidator = consolidator
        self.config = config or AutoDreamConfig.from_settings()
        self.active_session_id = self._session_stem(active_session_id) if active_session_id else None
        self.state_path = self.memory_dir.root / "auto_dream_state.json"
        self.lock_path = self.memory_dir.root / ".auto_dream.lock"
        self.session_dir = self.memory_dir.root / "session_memory"

    @staticmethod
    def _session_stem(session_id: str | None) -> str:
        safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", (session_id or "default").strip()).strip("_")
        return safe_session or "default"

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    def _session_files(self, *, since: float = 0.0, exclude_session_id: str | None = None) -> list[Path]:
        if not self.session_dir.exists():
            return []
        excluded_stem = self._session_stem(exclude_session_id) if exclude_session_id else None
        files: list[tuple[float, Path]] = []
        for path in self.session_dir.glob("*.md"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if excluded_stem and path.stem == excluded_stem:
                continue
            if since > 0 and mtime <= since:
                continue
            files.append((mtime, path))
        files.sort(key=lambda item: item[0], reverse=True)
        return [path for _mtime, path in files[: self.config.max_session_files]]

    def should_run(self, *, now: float | None = None) -> tuple[bool, str]:
        if not self.config.enabled:
            return False, "disabled"
        state = self._read_state()
        last_run = float(state.get("last_run_at") or 0.0)
        current = time.time() if now is None else now
        if last_run and (current - last_run) < self.config.min_hours * 3600:
            return False, "too_soon"
        files = self._session_files(since=last_run, exclude_session_id=self.active_session_id)
        if len(files) < self.config.min_sessions:
            return False, "not_enough_sessions"
        return True, "due"

    def _lock_payload(self, *, now: float) -> dict[str, Any]:
        return {"pid": os.getpid(), "acquired_at": now}

    def _read_lock(self) -> dict[str, Any] | None:
        try:
            stat = self.lock_path.stat()
            raw = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
        except FileNotFoundError:
            return None
        except OSError:
            return {"pid": None, "acquired_at": 0.0}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            try:
                acquired_at = float(raw)
            except ValueError:
                acquired_at = stat.st_mtime
            return {"pid": None, "acquired_at": acquired_at}
        if not isinstance(payload, dict):
            return {"pid": None, "acquired_at": stat.st_mtime}
        try:
            pid = int(payload["pid"]) if payload.get("pid") is not None else None
        except (TypeError, ValueError):
            pid = None
        try:
            acquired_at = float(payload.get("acquired_at") or stat.st_mtime)
        except (TypeError, ValueError):
            acquired_at = stat.st_mtime
        return {"pid": pid, "acquired_at": acquired_at}

    def _process_is_running(self, pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def _lock_is_reclaimable(self, *, now: float) -> bool:
        payload = self._read_lock()
        if payload is None:
            return True
        acquired_at = float(payload.get("acquired_at") or 0.0)
        age = max(0.0, now - acquired_at)
        pid = payload.get("pid")
        if pid is None:
            return age >= max(0.0, self.config.lock_stale_seconds)
        if not self._process_is_running(int(pid)):
            return True
        return age >= max(0.0, self.config.lock_stale_seconds)

    def _write_lock(self, *, now: float) -> None:
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(self._lock_payload(now=now), sort_keys=True) + "\n")

    def _acquire_lock(self, *, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        while True:
            try:
                self._write_lock(now=current)
                return True
            except FileExistsError:
                if not self._lock_is_reclaimable(now=current):
                    return False
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    return False

    def _release_lock(self) -> None:
        payload = self._read_lock()
        if payload is not None and payload.get("pid") not in {None, os.getpid()}:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return

    def _session_corpus(self, files: Sequence[Path] | None = None) -> str:
        chunks: list[str] = []
        for path in files if files is not None else self._session_files():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                chunks.append("## %s\n%s" % (path.name, redact_sensitive_prompt_text(text)[:4000]))
        return "\n\n".join(chunks)

    async def _default_consolidator(self, corpus: str) -> list[ExtractedMemory]:
        if not settings.auto_memory_dream_enabled or not settings.openai_api_key:
            return []
        from services.openai_background import run_background_openai_response

        response = await run_background_openai_response(
            system_prompt=(
                "Consolidate Viola session memory markdown into durable memories. "
                "Return only JSON using categories preference, fact, correction, routine, context, or note."
            ),
            user_content=(
                "Session memory files:\n%s\n\n"
                "Return a JSON array of stable, deduplicated memory objects. "
                "Exclude transient tool status and anything already too vague to be useful."
            )
            % corpus,
            max_output_tokens=700,
            user_id=self.user_id,
            timeout_s=20.0,
        )
        return _parse_extractor_response(response)

    async def _consolidate(self, corpus: str) -> list[ExtractedMemory]:
        if self.consolidator is None:
            return await self._default_consolidator(corpus)
        result = self.consolidator(corpus)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        parsed: list[ExtractedMemory] = []
        for item in result:
            normalized = _normalize_extracted_memory(item)
            if normalized is not None:
                parsed.append(normalized)
        return parsed

    async def run_if_due(self, *, now: float | None = None) -> dict[str, Any]:
        due, reason = self.should_run(now=now)
        if not due:
            return {"ok": True, "skipped": reason, "written": 0}
        current = time.time() if now is None else now
        if not self._acquire_lock(now=current):
            return {"ok": True, "skipped": "locked", "written": 0}

        try:
            due, reason = self.should_run(now=now)
            if not due:
                return {"ok": True, "skipped": reason, "written": 0}
            state = self._read_state()
            last_run = float(state.get("last_run_at") or 0.0)
            files = self._session_files(since=last_run, exclude_session_id=self.active_session_id)
            corpus = self._session_corpus(files)
            if not corpus.strip():
                return {"ok": True, "skipped": "empty_corpus", "written": 0}
            from services.openai_background import BackgroundLLMUnavailable

            try:
                memories = await self._consolidate(corpus)
            except BackgroundLLMUnavailable as exc:
                logger.debug("autoDream consolidation skipped: %s", exc)
                return {"ok": False, "error": str(exc), "written": 0}
            written = 0
            for memory in memories:
                try:
                    result = self.memory_dir.store_semantic_memory(memory.content, category=memory.category)
                except (OSError, ValueError) as exc:
                    logger.debug("autoDream memory write skipped: %s", exc)
                    continue
                if not result.get("deduped"):
                    written += 1
            self._write_state({"last_run_at": time.time() if now is None else now, "written": written})
            return {"ok": True, "written": written, "candidates": len(memories)}
        finally:
            self._release_lock()


__all__ = [
    "AutoDreamConfig",
    "AutoDreamService",
]
