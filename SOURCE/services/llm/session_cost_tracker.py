"""Per-session cost tracker with restore/persist parity.

Mirrors Claude Code's ``src/cost-tracker.ts`` shape
so a session's total cost, per-model usage map, and duration counters
survive an app restart and can be displayed in a cost-summary surface
identical to ``formatTotalCost()``.

Topology
--------

This module owns a process-local registry of trackers keyed by
``(user_id, session_id)``. A session is the user-facing
conversation/turn-loop (see ``intent/session.py``); each LLM call inside
that session calls ``add_llm_call(...)`` to accumulate usage. Each
user/session pair persists to its own JSON file under
``core.platform.get_data_dir()/cost/session_cost/users/.../sessions`` on
every ``save()`` so concurrent sessions never race for the same snapshot.

Three-tier storage rule (CLAUDE.md): this is Tier 3 (desktop-only,
NEVER cloud) — cost telemetry that goes to the cloud dashboard is the
opt-in TelemetryAccumulator surface owned by ``telemetry/accumulator.py``.

Parity references
-----------------

- Storage shape: Claude Code ``src/cost-tracker.ts`` ``StoredCostState``
  (``totalCostUSD``, ``totalAPIDuration``, ``totalLinesAdded``,
  ``lastModelUsage`` keyed by model)
- Per-model row: Claude Code ``src/entrypoints/agentSdkTypes.ts``
  ``ModelUsage`` (inputTokens, outputTokens, cacheReadInputTokens,
  cacheCreationInputTokens, webSearchRequests, costUSD)
- Unknown-model warning: ``services.llm.pricing.has_unknown_model_cost``
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from services.llm.pricing import (
    calculate_cost_usd,
    has_unknown_model_cost,
    reset_unknown_model_cost_state,
    usage_to_pricing_kwargs,
)

logger = get_logger(__name__)
_active_session_id: ContextVar[str | None] = ContextVar("session_cost_active_session_id", default=None)
_active_user_id: ContextVar[str | None] = ContextVar("session_cost_active_user_id", default=None)
_SAFE_PATH_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Per-model usage row (parity with Claude Code's ``ModelUsage``)
# ---------------------------------------------------------------------------


@dataclass
class ModelUsage:
    """Accumulated per-model usage for one session.

    Field names mirror Claude Code's ``ModelUsage`` (camelCase rendered
    snake_case in Python) so persisted JSON is portable.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    web_search_requests: int = 0
    cost_usd: float = 0.0

    def add(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_input_tokens: int,
        cache_creation_input_tokens: int,
        web_search_requests: int,
        cost_usd: float,
    ) -> None:
        self.input_tokens += int(input_tokens)
        self.output_tokens += int(output_tokens)
        self.cache_read_input_tokens += int(cache_read_input_tokens)
        self.cache_creation_input_tokens += int(cache_creation_input_tokens)
        self.web_search_requests += int(web_search_requests)
        self.cost_usd += float(cost_usd)


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


@dataclass
class _StoredState:
    """JSON-serialisable snapshot of a session's cost state."""

    session_id: str = ""
    total_cost_usd: float = 0.0
    total_wall_duration_ms: int = 0
    total_api_duration_ms: int = 0
    total_api_duration_without_retries_ms: int = 0
    total_tool_duration_ms: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0
    last_duration_ms: int = 0
    model_usage: dict[str, dict[str, float | int]] = field(default_factory=dict)
    unknown_model_warning: bool = False


def _format_duration_ms(ms: int) -> str:
    """Human-readable duration string for cost summary output."""
    if ms <= 0:
        return "0ms"
    if ms < 1000:
        return "%dms" % ms
    seconds = ms / 1000.0
    if seconds < 60:
        return "%.2fs" % seconds
    minutes, secs = divmod(seconds, 60)
    return "%dm %ds" % (int(minutes), int(secs))


def _legacy_state_path() -> Path:
    """Return the pre-R13 exact persisted-state path for direct trackers."""

    override = os.environ.get("VIOLA_SESSION_COST_PATH")
    if override:
        return Path(override)
    try:
        from core.platform import get_data_dir

        return get_data_dir() / "cost" / "session_cost.json"
    except Exception:
        # Project-relative fallback. Never resolves to user-home.
        return Path(__file__).resolve().parents[2] / ".viola" / "cost" / "session_cost.json"


def _default_state_path() -> Path:
    """Return the default path for direct, explicitly constructed trackers.

    Production access should use :func:`get_session_cost_tracker`, which
    supplies a per-user/per-session path. Direct construction keeps the
    legacy exact path for focused tests and compatibility.
    """

    return _legacy_state_path()


def _default_state_root() -> Path:
    """Return the root directory for partitioned cost snapshots."""

    override = os.environ.get("VIOLA_SESSION_COST_PATH")
    if override:
        candidate = Path(override)
        if candidate.suffix:
            return candidate.parent / ("%s_scoped" % candidate.stem)
        return candidate
    try:
        from core.platform import get_data_dir

        return get_data_dir() / "cost" / "session_cost"
    except (ImportError, RuntimeError, AttributeError, OSError, ValueError):
        # Project-relative fallback. Never resolves to user-home.
        return Path(__file__).resolve().parents[2] / ".viola" / "cost" / "session_cost"


def _is_cloud_surface() -> bool:
    try:
        from config.settings import settings
    except (ImportError, RuntimeError, AttributeError, ValueError):
        return False
    surface = str(getattr(settings, "app_surface", "desktop") or "desktop").strip().lower()
    deployment = str(getattr(settings, "deployment_mode", "") or "").strip().lower()
    return surface == "cloud" or deployment == "cloud"


def _normalize_user_id(value: str | None, *, source: str) -> str:
    user_id = str(value or "").strip()
    if not user_id:
        raise ValueError("SessionCostTracker requires a non-empty user_id from %s" % source)
    return user_id


def _resolve_user_id(user_id: str | None = None) -> str:
    """Resolve the owner for a session-cost tracker."""

    if user_id is not None:
        return _normalize_user_id(user_id, source="explicit user_id")
    active_user_id = _active_user_id.get()
    if active_user_id:
        return _normalize_user_id(active_user_id, source="active session-cost binding")
    try:
        from core.user_context import get_current_user_id

        return _normalize_user_id(get_current_user_id(), source="current user context")
    except (LookupError, ValueError) as exc:
        if _is_cloud_surface():
            raise RuntimeError("SessionCostTracker requires authenticated user_id on cloud surface") from exc
    try:
        from core.user_context import get_device_user_id

        return _normalize_user_id(get_device_user_id(), source="device user_id")
    except (ImportError, RuntimeError, AttributeError, OSError, ValueError) as exc:
        raise RuntimeError("SessionCostTracker requires user_id or desktop device user_id") from exc


def _normalize_session_id(value: str | None, *, source: str) -> str:
    session_id = str(value or "").strip()
    if not session_id:
        raise ValueError("SessionCostTracker requires a non-empty session_id from %s" % source)
    return session_id


def _safe_path_component(value: str) -> str:
    normalized = str(value or "").strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    cleaned = _SAFE_PATH_COMPONENT_RE.sub("_", normalized).strip("._-")
    if not cleaned:
        cleaned = "scope"
    return "%s-%s" % (cleaned[:64], digest)


def _state_path_for_scope(*, user_id: str, session_id: str) -> Path:
    return (
        _default_state_root()
        / "users"
        / _safe_path_component(user_id)
        / "sessions"
        / ("%s.json" % _safe_path_component(session_id))
    )


def empty_session_cost_snapshot(*, session_id: str = "") -> dict[str, Any]:
    """Return a JSON-safe empty snapshot for a user with no active session."""

    return asdict(_StoredState(session_id=session_id))


def format_empty_session_cost_summary() -> str:
    """Return the standard summary text for a user with no active session."""

    return "\n".join(
        [
            "Total cost: $0.0000",
            "Total wall duration: 0ms",
            "Total API duration: 0ms",
            "Total tool duration: 0ms",
            "Code changes: +0 / -0",
        ]
    )


class SessionCostTracker:
    """Per-user/per-session cost accumulator.

    Thread-safe. Persistence is best-effort: a failed write logs but
    does NOT raise — losing cost telemetry must never break an LLM call.
    """

    def __init__(self, *, state_path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._state_path: Path = state_path or _default_state_path()
        self._state: _StoredState = _StoredState()
        self._model_usage: dict[str, ModelUsage] = {}
        self._session_wall_started_at: float | None = None

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def start_session(self, session_id: str, *, restore: bool = True) -> bool:
        """Bind the tracker to ``session_id``.

        If ``restore`` is true AND a persisted state for ``session_id``
        exists, the in-memory totals are repopulated from disk. Returns
        ``True`` when restoration happened, ``False`` for a fresh session.
        """
        with self._lock:
            reset_unknown_model_cost_state()
            if restore and self._restore_locked(session_id):
                self._session_wall_started_at = time.monotonic()
                return True
            self._state = _StoredState(session_id=session_id)
            self._model_usage = {}
            self._session_wall_started_at = time.monotonic()
            return False

    def reset(self) -> None:
        """Drop in-memory state (does NOT delete persisted file)."""
        with self._lock:
            session_id = self._state.session_id
            self._state = _StoredState(session_id=session_id)
            self._model_usage = {}
            self._session_wall_started_at = time.monotonic() if session_id else None

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    def add_llm_call(
        self,
        *,
        model: str,
        usage: Any,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        api_duration_ms: int = 0,
        api_duration_without_retries_ms: int = 0,
    ) -> float:
        """Add a single LLM call's usage and return its USD cost.

        ``usage`` accepts the Viola ``LlmTokenUsage`` shape, an
        Anthropic/OpenAI usage object, or a dict — anything that
        ``services.llm.pricing.usage_to_pricing_kwargs`` understands.
        Either ``input_tokens``/``output_tokens`` can be passed
        explicitly for callers that compute them outside the usage
        object; otherwise we pull them from the usage shape.
        """
        ipt, opt, kwargs = self._extract_tokens(usage, input_tokens, output_tokens)
        try:
            cost_usd = calculate_cost_usd(
                model,
                ipt,
                opt,
                kwargs["cached_tokens"],
                cache_write_tokens=kwargs["cache_write_tokens"],
                web_search_requests=kwargs["web_search_requests"],
            )
        except Exception:
            logger.debug("session cost: cost calc failed for model %s", model, exc_info=True)
            cost_usd = 0.0

        with self._lock:
            self._state.total_cost_usd += float(cost_usd)
            self._state.total_api_duration_ms += int(api_duration_ms)
            self._state.total_api_duration_without_retries_ms += int(api_duration_without_retries_ms)
            if has_unknown_model_cost():
                self._state.unknown_model_warning = True

            row = self._model_usage.setdefault(model, ModelUsage())
            row.add(
                input_tokens=ipt,
                output_tokens=opt,
                cache_read_input_tokens=kwargs["cached_tokens"],
                cache_creation_input_tokens=kwargs["cache_write_tokens"],
                web_search_requests=kwargs["web_search_requests"],
                cost_usd=cost_usd,
            )
        return float(cost_usd)

    def add_tool_duration_ms(self, ms: int) -> None:
        with self._lock:
            self._state.total_tool_duration_ms += int(ms)
        self.save()

    def add_lines_changed(self, *, added: int = 0, removed: int = 0) -> None:
        with self._lock:
            self._state.total_lines_added += int(added)
            self._state.total_lines_removed += int(removed)
        self.save()

    # ------------------------------------------------------------------
    # Read-side accessors (parity with Claude Code's cost-tracker exports)
    # ------------------------------------------------------------------

    def total_cost_usd(self) -> float:
        with self._lock:
            return self._state.total_cost_usd

    def model_usage(self) -> dict[str, ModelUsage]:
        with self._lock:
            return {model: ModelUsage(**asdict(row)) for model, row in self._model_usage.items()}

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot for telemetry / UI rendering."""
        with self._lock:
            state_dict = asdict(self._state)
            state_dict["total_wall_duration_ms"] = self._effective_wall_duration_ms_locked()
            state_dict["model_usage"] = {model: asdict(row) for model, row in self._model_usage.items()}
            return state_dict

    def format_total_cost(self) -> str:
        """Return Claude-Code-style summary text.

        Mirrors ``formatTotalCost()`` in ``src/cost-tracker.ts``. The
        unknown-model warning suffix is appended whenever the pricing
        fallback fired during this session.
        """
        with self._lock:
            cost = self._state.total_cost_usd
            warning = (
                " (costs may be inaccurate due to usage of unknown models)" if self._state.unknown_model_warning else ""
            )
        if cost > 0.5:
            cost_str = f"${cost:.2f}"
        else:
            cost_str = f"${cost:.4f}"
        return f"Total cost: {cost_str}{warning}"

    def format_session_summary(self) -> str:
        """F-052: multi-line Claude-Code-style ``/cost`` summary.

        Extends :meth:`format_total_cost` with per-model usage, the
        cumulative API duration, and the unknown-model warning suffix
        (when applicable). The shape matches Claude's ``cost.ts``
        command output so the UI and exit hook can render it directly.
        """
        with self._lock:
            cost = self._state.total_cost_usd
            wall_ms = self._effective_wall_duration_ms_locked()
            api_ms = self._state.total_api_duration_ms
            api_no_retry_ms = self._state.total_api_duration_without_retries_ms
            tool_ms = self._state.total_tool_duration_ms
            lines_added = self._state.total_lines_added
            lines_removed = self._state.total_lines_removed
            unknown_warning = self._state.unknown_model_warning
            model_usage = {model: ModelUsage(**asdict(row)) for model, row in self._model_usage.items()}

        lines: list[str] = [self.format_total_cost()]
        lines.append("Total wall duration: %s" % _format_duration_ms(wall_ms))
        lines.append("Total API duration: %s" % _format_duration_ms(api_ms))
        if api_no_retry_ms and api_no_retry_ms != api_ms:
            lines.append("Total API duration (excl. retries): %s" % _format_duration_ms(api_no_retry_ms))
        lines.append("Total tool duration: %s" % _format_duration_ms(tool_ms))
        lines.append("Code changes: +%d / -%d" % (lines_added, lines_removed))

        if model_usage:
            lines.append("")
            lines.append("Usage by model:")
            for model, row in sorted(model_usage.items()):
                lines.append(
                    "  %s: in=%d out=%d cache_read=%d cache_write=%d web_search=%d  $%.4f"
                    % (
                        model,
                        row.input_tokens,
                        row.output_tokens,
                        row.cache_read_input_tokens,
                        row.cache_creation_input_tokens,
                        row.web_search_requests,
                        row.cost_usd,
                    )
                )

        if unknown_warning:
            lines.append("")
            lines.append("(costs may be inaccurate due to usage of unknown models)")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> bool:
        """Persist the current state to disk. Returns ``True`` on success.

        Best-effort: catches every exception class (disk full,
        read-only volume, permission denied) and logs at debug so the
        caller — typically a hot LLM-call path — never sees a failure.
        """
        with self._lock:
            state_dict = self.snapshot()
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(state_dict, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_path)
            return True
        except Exception:
            logger.debug("session cost: persist failed at %s", self._state_path, exc_info=True)
            return False

    def get_stored_session_costs(self, session_id: str) -> dict[str, Any] | None:
        """Read the persisted snapshot if its session_id matches.

        Mirrors Claude Code's ``getStoredSessionCosts()``. Returns the
        raw JSON payload or ``None`` if no match.
        """
        try:
            if not self._state_path.exists():
                return None
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("session cost: read failed at %s", self._state_path, exc_info=True)
            return None
        if not isinstance(payload, dict):
            return None
        if str(payload.get("session_id", "")) != str(session_id):
            return None
        return payload

    def restore_for_session(self, session_id: str) -> bool:
        """Restore in-memory state from disk if session_id matches."""
        with self._lock:
            return self._restore_locked(session_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _restore_locked(self, session_id: str) -> bool:
        payload = self.get_stored_session_costs(session_id)
        if payload is None:
            return False
        try:
            model_usage_raw = payload.pop("model_usage", {}) or {}
            self._state = _StoredState(**payload)
            self._state.session_id = session_id
            self._session_wall_started_at = time.monotonic()
            self._model_usage = {}
            for model_name, row in model_usage_raw.items():
                if not isinstance(row, dict):
                    continue
                # Filter unknown keys defensively to survive schema drift.
                allowed = {f.name for f in ModelUsage.__dataclass_fields__.values()}
                clean = {k: v for k, v in row.items() if k in allowed}
                self._model_usage[model_name] = ModelUsage(**clean)
            return True
        except Exception:
            logger.debug("session cost: restore parse failed", exc_info=True)
            # Don't leave the tracker in a half-restored state.
            self._state = _StoredState(session_id=session_id)
            self._model_usage = {}
            self._session_wall_started_at = time.monotonic()
            return False

    def _effective_wall_duration_ms_locked(self) -> int:
        total = int(self._state.total_wall_duration_ms)
        if self._session_wall_started_at is None:
            return max(0, total)
        elapsed_ms = int(max(0.0, time.monotonic() - self._session_wall_started_at) * 1000)
        return max(0, total + elapsed_ms)

    @staticmethod
    def _extract_tokens(
        usage: Any, input_tokens: int | None, output_tokens: int | None
    ) -> tuple[int, int, dict[str, int]]:
        """Pull ``(input, output, pricing_kwargs)`` from a usage object.

        Falls back to direct args when the usage object doesn't expose
        a field. ``pricing_kwargs`` contains ``cached_tokens``,
        ``cache_write_tokens``, ``web_search_requests`` (all non-negative
        ints).
        """

        def _read(name: str) -> int | None:
            if usage is None:
                return None
            if isinstance(usage, dict):
                v = usage.get(name)
            else:
                v = getattr(usage, name, None)
            if v is None:
                return None
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return None

        ipt = input_tokens if input_tokens is not None else (_read("input_tokens") or 0)
        opt = output_tokens if output_tokens is not None else (_read("output_tokens") or 0)
        kwargs = usage_to_pricing_kwargs(usage)
        return int(ipt), int(opt), kwargs


# ---------------------------------------------------------------------------
# Module-level partitioned accessors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionCostBindingToken:
    """ContextVar reset tokens for an active session-cost binding."""

    session_token: Token[str | None]
    user_token: Token[str | None]


_TRACKER_LOCK = threading.RLock()
_TRACKERS_BY_SCOPE: dict[tuple[str, str], SessionCostTracker] = {}
_ACTIVE_SESSION_BY_USER: dict[str, str] = {}


def _coerce_optional_user_id(value: str | None) -> str | None:
    resolved = str(value or "").strip()
    return resolved or None


def _active_session_for_user(resolved_user_id: str) -> str | None:
    active_user_id = _active_user_id.get()
    active_session_id = _active_session_id.get()
    if active_user_id == resolved_user_id and active_session_id:
        return active_session_id
    with _TRACKER_LOCK:
        return _ACTIVE_SESSION_BY_USER.get(resolved_user_id)


def get_session_cost_tracker(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
) -> SessionCostTracker:
    """Return the tracker for one ``(user_id, session_id)`` scope."""

    resolved_user_id = _resolve_user_id(_coerce_optional_user_id(user_id))
    resolved_session_id = str(session_id or "").strip()
    if not resolved_session_id:
        resolved_session_id = _active_session_for_user(resolved_user_id) or ""
    resolved_session_id = _normalize_session_id(resolved_session_id, source="session_id or active session")

    key = (resolved_user_id, resolved_session_id)
    with _TRACKER_LOCK:
        tracker = _TRACKERS_BY_SCOPE.get(key)
        if tracker is None:
            tracker = SessionCostTracker(
                state_path=_state_path_for_scope(user_id=resolved_user_id, session_id=resolved_session_id)
            )
            tracker.start_session(resolved_session_id, restore=True)
            _TRACKERS_BY_SCOPE[key] = tracker
        elif str(tracker.snapshot().get("session_id") or "") != resolved_session_id:
            tracker.start_session(resolved_session_id, restore=True)
        _ACTIVE_SESSION_BY_USER[resolved_user_id] = resolved_session_id
        return tracker


def get_active_session_cost_tracker(*, user_id: str | None = None) -> SessionCostTracker | None:
    """Return the current user's active tracker, if one exists."""

    resolved_user_id = _resolve_user_id(_coerce_optional_user_id(user_id))
    resolved_session_id = _active_session_for_user(resolved_user_id)
    if not resolved_session_id:
        return None
    return get_session_cost_tracker(user_id=resolved_user_id, session_id=resolved_session_id)


def set_active_session_cost_session(
    session_id: str,
    *,
    user_id: str | None = None,
) -> SessionCostBindingToken:
    """Bind provider-level usage emission to a session-cost session."""

    resolved_session_id = _normalize_session_id(session_id, source="active session binding")
    resolved_user_id = _resolve_user_id(_coerce_optional_user_id(user_id))
    with _TRACKER_LOCK:
        _ACTIVE_SESSION_BY_USER[resolved_user_id] = resolved_session_id
    session_token = _active_session_id.set(resolved_session_id)
    user_token = _active_user_id.set(resolved_user_id)
    return SessionCostBindingToken(session_token=session_token, user_token=user_token)


def reset_active_session_cost_session(token: Token[str | None] | SessionCostBindingToken) -> None:
    """Restore the previous provider-level session-cost binding."""

    if isinstance(token, SessionCostBindingToken):
        _active_session_id.reset(token.session_token)
        _active_user_id.reset(token.user_token)
        return
    _active_session_id.reset(token)


def get_active_session_cost_session() -> str | None:
    """Return the current provider-level session-cost binding, if any."""
    return _active_session_id.get()


def record_active_session_llm_call(
    *,
    model: str,
    usage: Any,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int = 0,
    user_id: str | None = None,
) -> float | None:
    """Record a provider usage emission against the active session tracker."""
    session_id = get_active_session_cost_session()
    if not session_id:
        return None
    return record_session_llm_call(
        session_id=session_id,
        model=model,
        usage=usage,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        user_id=user_id or _active_user_id.get(),
    )


def record_session_llm_call(
    *,
    session_id: str,
    model: str,
    usage: Any,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int = 0,
    user_id: str | None = None,
) -> float | None:
    """Record provider usage against an explicit session id."""
    resolved_session_id = str(session_id or "").strip()
    if not resolved_session_id:
        return None
    try:
        tracker = get_session_cost_tracker(user_id=user_id, session_id=resolved_session_id)
        cost = tracker.add_llm_call(
            model=model,
            usage=usage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            api_duration_ms=latency_ms,
            api_duration_without_retries_ms=latency_ms,
        )
        tracker.save()
        return cost
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("session cost: provider usage record failed", exc_info=True)
        return None


def record_session_tool_duration_ms(
    *,
    session_id: str,
    duration_ms: int,
    user_id: str | None = None,
) -> bool:
    """Record tool wall-clock duration against an explicit session id."""
    resolved_session_id = str(session_id or "").strip()
    if not resolved_session_id:
        return False
    try:
        tracker = get_session_cost_tracker(user_id=user_id, session_id=resolved_session_id)
        tracker.add_tool_duration_ms(duration_ms)
        return True
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("session cost: tool duration record failed", exc_info=True)
        return False


def record_active_session_tool_duration_ms(duration_ms: int) -> bool:
    """Record tool wall-clock duration against the active session, if any."""
    session_id = get_active_session_cost_session()
    if not session_id:
        return False
    return record_session_tool_duration_ms(
        session_id=session_id,
        duration_ms=duration_ms,
        user_id=_active_user_id.get(),
    )


def record_session_lines_changed(
    *,
    session_id: str,
    added: int = 0,
    removed: int = 0,
    user_id: str | None = None,
) -> bool:
    """Record filesystem edit deltas against an explicit session id."""
    resolved_session_id = str(session_id or "").strip()
    if not resolved_session_id:
        return False
    try:
        tracker = get_session_cost_tracker(user_id=user_id, session_id=resolved_session_id)
        tracker.add_lines_changed(added=added, removed=removed)
        return True
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        logger.debug("session cost: line-change record failed", exc_info=True)
        return False


def record_active_session_lines_changed(*, added: int = 0, removed: int = 0) -> bool:
    """Record filesystem edit deltas against the active session, if any."""
    session_id = get_active_session_cost_session()
    if not session_id:
        return False
    return record_session_lines_changed(
        session_id=session_id,
        added=added,
        removed=removed,
        user_id=_active_user_id.get(),
    )


def reset_for_tests() -> None:
    """Test-only: drop all cached trackers."""

    with _TRACKER_LOCK:
        _TRACKERS_BY_SCOPE.clear()
        _ACTIVE_SESSION_BY_USER.clear()
