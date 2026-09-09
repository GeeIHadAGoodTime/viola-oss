"""Claude-style side-query memory selection."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from config.settings import settings
from core.logging_config import get_logger
from services.memory.dir import MemoryDir, MemoryHeader

logger = get_logger(__name__)

_SELECT_MEMORIES_SYSTEM_PROMPT = (
    "You are selecting memories that will be useful to Viola as it processes a user's query. "
    "You will be given the user's query and a list of available memory files with their "
    "filenames and descriptions.\n\n"
    "Return a JSON object with a selected_memories array containing filenames from the manifest. "
    "Only include memories that are clearly useful based on the name and description. "
    "Return at most the requested limit. Return an empty array when nothing is clearly useful."
)


@dataclass(frozen=True)
class MemorySelectionRequest:
    query: str
    manifest: str
    limit: int
    recent_tools: tuple[str, ...] = ()
    user_id: str = ""


MemorySideSelector = Callable[[MemorySelectionRequest], Sequence[str]]


def _json_selected_filenames(text: str) -> list[str]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        values = payload.get("selected_memories", [])
    elif isinstance(payload, list):
        values = payload
    else:
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _run_coroutine_blocking(coro: Any) -> Any:
    """Bridge the async side-query coroutine onto the shared registered loop.

    Issue #419 (ASYNC-1): the prior shape spun a brand-new throwaway event loop
    per call (``asyncio.run`` -- directly when no loop was running, else inside a
    ``ThreadPoolExecutor`` worker). On cloud this coroutine's call chain reaches
    ``LlmSpendReservation``/``LLMRateLimiter`` (services/llm/spend_accounting.py,
    services/llm/rate_limiter.py), which read the process-wide
    ``PostgresAuthDatabase`` singleton -- an asyncpg pool bound to the FastAPI
    lifespan loop. A throwaway side loop trips its cross-loop guard
    (``auth/postgres_database.py:_ensure_current_loop_pool`` -- "PostgresAuthDatabase
    pool is bound to a different event loop"), so the side-query silently failed
    safe (the broad-exception catch in ``select_relevant_memory_headers`` degrades
    to manifest-only) on every real cloud call, and the spend/rate-limit write was
    never recorded. ``run_async_synchronously`` (the same helper
    ``auth/audit.py._run_coroutine_blocking`` already uses) dispatches to the
    registered FastAPI main loop on cloud -- raising loudly if cloud forgot to
    register one -- and to a single shared worker loop on desktop, never a fresh
    loop per call.

    Issue #560 (desktop serving-loop starvation): on desktop the memory side-
    query is dispatched to the *isolated* worker loop, NOT the serving loop.
    ``build_frames`` runs synchronously on the desktop serving loop thread and
    ``build_all(concurrent_names={"memory"})`` then blocks that thread on
    ``future.result(...)``; a coroutine queued on the serving loop can never run
    while its own consumer parks that loop's thread, so every warm turn's side-
    query was starved to the full 15 s timeout and degraded to manifest-only
    SILENTLY (LOOP_LAG ~15.6 s, zero side-query HTTP). ``prefer_isolated_loop``
    runs it on a loop independent of whatever blocks the serving loop; cloud is
    unaffected (it keeps serving-loop dispatch for asyncpg pool reuse, #419).
    The timeout now emits a visible WARNING instead of degrading in silence.
    """
    from core.asyncio_safe import run_async_synchronously

    return run_async_synchronously(
        coro,
        timeout=15.0,
        timeout_result=None,
        timeout_log_message="Memory side-query timed out after %.1fs; degrading to manifest-only for this turn.",
        logger=logger,
        prefer_isolated_loop=True,
    )


def default_side_query_selector(request: MemorySelectionRequest) -> Sequence[str]:
    """Run the background selector, returning filenames from the manifest."""
    if not settings.auto_memory_side_query_enabled or not settings.openai_api_key:
        return []

    from services.openai_background import run_background_openai_response

    recent_tools = ", ".join(request.recent_tools) if request.recent_tools else "none"
    user_content = "User query:\n%s\n\nRecent tools: %s\n\nLimit: %d\n\nAvailable memory files:\n%s" % (
        request.query,
        recent_tools,
        request.limit,
        request.manifest,
    )
    text = _run_coroutine_blocking(
        run_background_openai_response(
            system_prompt=_SELECT_MEMORIES_SYSTEM_PROMPT,
            user_content=user_content,
            max_output_tokens=256,
            user_id=request.user_id,
            timeout_s=15.0,
        )
    )
    return _json_selected_filenames(str(text))


def _fallback_metadata_selector(headers: Sequence[MemoryHeader], query: str, limit: int) -> list[MemoryHeader]:
    clean = (query or "").strip().lower()
    if not clean:
        return []
    tokens = [token for token in re.findall(r"[a-z0-9_'-]+", clean) if len(token) > 1]
    if not tokens:
        return []
    scored: list[tuple[int, float, MemoryHeader]] = []
    for header in headers:
        haystack = " ".join(
            value.lower() for value in (header.name, header.description, header.type, header.filename) if value
        )
        if not haystack:
            continue
        score = sum(1 for token in tokens if token in haystack)
        if score:
            scored.append((score, header.mtime_ms, header))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [header for _score, _mtime, header in scored[: max(1, int(limit))]]


def select_relevant_memory_headers(
    directory: MemoryDir,
    query: str,
    *,
    limit: int = 5,
    excluded_filenames: set[str] | None = None,
    recent_tools: Sequence[str] = (),
    selector: MemorySideSelector | None = None,
    user_id: str = "",
    allow_metadata_fallback: bool = False,
) -> list[MemoryHeader]:
    """Select relevant headers using a Claude-style manifest side query."""
    headers = directory.scan_memory_files()
    excluded = excluded_filenames or set()
    available = [header for header in headers if header.filename not in excluded]
    if not available:
        return []

    capped_limit = max(1, int(limit))
    manifest = directory.format_memory_manifest(available)
    request = MemorySelectionRequest(
        query=query,
        manifest=manifest,
        limit=capped_limit,
        recent_tools=tuple(recent_tools),
        user_id=user_id,
    )
    select = selector or default_side_query_selector
    selected_filenames: list[str] = []
    try:
        selected_filenames = [str(name).strip() for name in select(request) if str(name).strip()]
    except Exception as exc:
        logger.debug("Memory side-query selector failed; returning no selected memories: %s", exc)

    by_filename = {header.filename: header for header in available}
    selected = [by_filename[name] for name in selected_filenames if name in by_filename]
    if selected:
        return selected[:capped_limit]

    if not allow_metadata_fallback:
        return []

    return _fallback_metadata_selector(available, query, capped_limit)


__all__ = [
    "MemorySelectionRequest",
    "MemorySideSelector",
    "default_side_query_selector",
    "select_relevant_memory_headers",
]
