"""Canonical encrypted per-task trace writer for agent runs.

The trace is an append-only JSONL artifact: one file per logical task, with
event rows for task start/resume, per-step activity, compaction, gate events,
and completion. Existing daily step JSONL files remain compatibility outputs,
but this module is the canonical runtime artifact.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import struct
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger
from core.platform import get_data_dir, get_logs_dir
from intent.log_redaction import redact_card_data, redact_pii

if TYPE_CHECKING:
    from services.persistence.blob_store import BlobStore
    from services.persistence.trace_keys import KeyProvider

logger = get_logger(__name__)

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - fallback is for cold/dev environments.
    zstd = None

TASK_TRACE_SCHEMA_VERSION = 2
DEFAULT_TASK_TRACE_DIR = get_logs_dir() / "traces"
DEFAULT_TASK_TRACE_V2_DIR = get_data_dir() / "traces" / "by_user"
DEFAULT_TRACE_BLOB_THRESHOLD_BYTES = 64 * 1024
DEFAULT_TRACE_EVENT_CAP_BYTES = 256 * 1024
DEFAULT_TRACE_SIZE_CAP_BYTES = 25 * 1024 * 1024
TRACE_V2_FLUSH_EVENT_COUNT = 10
TRACE_V2_FLUSH_BYTES = 4 * 1024
TRACE_V2_PREVIEW_CHARS = 240
TRACE_V2_ZSTD_LEVEL = 3
TRACE_V2_LENGTH_PREFIX_BYTES = 4
# Plaintext breadcrumb written beside a trace that was never produced, so a
# missing trace carries its own reason instead of looking like an idle run.
TRACE_DISABLED_MARKER_SUFFIX = ".trace-disabled.json"
TRACE_DISABLED_MARKER_SCHEMA = 1
TRACE_SANITIZE_MAX_DEPTH = 80
TRACE_SANITIZER_ERROR_MARKER = "[REDACTED:TRACE_SANITIZER_ERROR]"
TRACE_SANITIZER_CYCLE_MARKER = "[REDACTED:TRACE_CYCLE]"
TRACE_SANITIZER_MAX_DEPTH_MARKER = "[REDACTED:TRACE_MAX_DEPTH]"
_TRACE_SANITIZER_EXCEPTIONS = (
    ArithmeticError,
    AttributeError,
    LookupError,
    OSError,
    RuntimeError,
    TypeError,
    UnicodeError,
    ValueError,
)
TRACE_V2_COMMON_FIELDS = frozenset(
    {
        "event",
        "schema_version",
        "task_id",
        "user_id_hash",
        "session_id",
        "ts",
    }
)

_INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\s]+')


def _safe_path_segment(value: str) -> str:
    """Return a Windows-safe path segment for user/session scoped traces."""
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    cleaned = _INVALID_PATH_CHARS_RE.sub("_", raw)
    cleaned = cleaned.strip("._")
    return cleaned or "unknown"


def _trace_date_segment(started_at: str | None) -> str:
    """Return YYYYMMDD for the trace partition path."""
    try:
        if started_at:
            dt = datetime.fromisoformat(started_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
        else:
            dt = datetime.now(UTC)
    except Exception:
        dt = datetime.now(UTC)
    return dt.astimezone(UTC).strftime("%Y%m%d")


def _user_id_hash(user_id: str) -> str:
    """Return the per-user filesystem partition hash."""
    return hashlib.sha256(user_id.encode("utf-8", errors="replace")).hexdigest()[:16]


def _binary_fingerprint(data: str) -> dict[str, Any]:
    """Summarize large binary-ish strings without storing the raw payload."""
    encoded = data.encode("utf-8", errors="replace")
    return {
        "_binary_redacted": True,
        "sha256": hashlib.sha256(encoded).hexdigest()[:16],
        "chars": len(data),
    }


def _safe_trace_string(value: Any) -> str:
    try:
        return str(value)
    except _TRACE_SANITIZER_EXCEPTIONS:
        return TRACE_SANITIZER_ERROR_MARKER


def _trace_key_needs_card_context(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "", key.casefold())
    return any(
        marker in normalized
        for marker in (
            "card",
            "cardnumber",
            "creditcard",
            "cvc",
            "cvv",
            "expiration",
            "pan",
            "securitycode",
        )
    )


def _redact_trace_value_for_key(key: str, value: Any) -> Any:
    if not _trace_key_needs_card_context(key):
        return value
    try:
        wrapped = redact_card_data({key: value})
        return wrapped.get(key, value) if isinstance(wrapped, dict) else value
    except _TRACE_SANITIZER_EXCEPTIONS:
        return TRACE_SANITIZER_ERROR_MARKER


def _sanitize_for_trace(
    value: Any,
    _seen: set[int] | None = None,
    _depth: int = 0,
    _cache: dict[int, Any] | None = None,
) -> Any:
    """Make trace payloads JSON-safe while keeping model-visible structure."""
    if _depth > TRACE_SANITIZE_MAX_DEPTH:
        return TRACE_SANITIZER_MAX_DEPTH_MARKER
    if _seen is None:
        _seen = set()
    if _cache is None:
        _cache = {}
    if isinstance(value, str):
        if len(value) > TRACE_V2_PREVIEW_CHARS:
            value_id = id(value)
            cached = _cache.get(value_id)
            if cached is not None:
                return cached
            redacted = redact_card_data(value)
            _cache[value_id] = redacted
            return redacted
        return redact_card_data(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, Path):
        return str(value)

    if is_dataclass(value) and not isinstance(value, type):
        try:
            return _sanitize_for_trace(asdict(value), _seen, _depth + 1, _cache)
        except _TRACE_SANITIZER_EXCEPTIONS:
            return TRACE_SANITIZER_ERROR_MARKER

    if isinstance(value, dict):
        value_id = id(value)
        if value_id in _seen:
            return TRACE_SANITIZER_CYCLE_MARKER
        _seen.add(value_id)
        sanitized: dict[str, Any] = {}
        try:
            for key, inner in value.items():
                safe_key = _safe_trace_string(key)
                if key == "image_base64" and isinstance(inner, str):
                    sanitized[safe_key] = _binary_fingerprint(inner)
                    continue
                if (
                    key == "data"
                    and isinstance(inner, str)
                    and value.get("type") == "image"
                    and isinstance(value.get("source"), dict)
                ):
                    sanitized[safe_key] = "[binary redacted]"
                    sanitized["data_fingerprint"] = _binary_fingerprint(inner)
                    continue
                sanitized[safe_key] = _redact_trace_value_for_key(
                    safe_key,
                    _sanitize_for_trace(inner, _seen, _depth + 1, _cache),
                )

            if value.get("type") == "image" and isinstance(sanitized.get("source"), dict):
                source = dict(sanitized["source"])
                raw_source = value.get("source", {})
                raw_data = raw_source.get("data") if isinstance(raw_source, dict) else None
                if isinstance(raw_data, str):
                    source["data"] = "[binary redacted]"
                    source["data_fingerprint"] = _binary_fingerprint(raw_data)
                sanitized["source"] = source
            return sanitized
        except _TRACE_SANITIZER_EXCEPTIONS:
            return TRACE_SANITIZER_ERROR_MARKER
        finally:
            _seen.discard(value_id)

    if isinstance(value, (list, tuple, set)):
        value_id = id(value)
        if value_id in _seen:
            return TRACE_SANITIZER_CYCLE_MARKER
        _seen.add(value_id)
        try:
            return [_sanitize_for_trace(item, _seen, _depth + 1, _cache) for item in value]
        except _TRACE_SANITIZER_EXCEPTIONS:
            return TRACE_SANITIZER_ERROR_MARKER
        finally:
            _seen.discard(value_id)

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return _safe_trace_string(value)


def _utc_now_iso() -> str:
    """Return a UTC timestamp in the trace schema's ISO shape."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Any) -> bytes:
    """Serialize compact JSON for trace size measurement and JSONL rows."""
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _json_line_bytes(value: Any) -> bytes:
    return _json_bytes(value) + b"\n"


def _trace_positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s value for trace writer", name)
        return default
    if parsed <= 0:
        logger.warning("Ignoring non-positive %s value for trace writer", name)
        return default
    return parsed


def _compress_trace_block(jsonl_bytes: bytes) -> bytes:
    """Compress one JSONL segment before encryption.

    zstandard level 3 is the production codec. gzip is a defensive fallback so
    a cold developer checkout can still import and exercise the writer before
    requirements are installed; readers can distinguish both by magic bytes.
    """
    if zstd is not None:
        return zstd.ZstdCompressor(level=TRACE_V2_ZSTD_LEVEL).compress(jsonl_bytes)
    return gzip.compress(jsonl_bytes)


@dataclass(slots=True)
class TaskTraceWriter:
    """Append-only writer for encrypted trace schema v2.

    Trace files are segment streams, not one Fernet token for the whole file:
    each flush writes a 4-byte big-endian encrypted-token length followed by
    that Fernet token. The token decrypts to one compressed JSONL block. This
    keeps every segment independently decryptable while preserving append-only
    writes.

    `append_step` stores delta-only message lists. If callers pass a cumulative
    `agent_step["messages"]` list, the writer compares it with the previous
    step's stored messages and writes only the suffix under `delta_messages`.
    Callers that already know the delta may pass `delta_messages` directly.
    """

    task_id: str
    path: Path
    user_id: str
    user_id_hash: str
    session_id: str | None = None
    key_provider: KeyProvider | None = None
    blob_store: BlobStore | None = None
    schema_version: int = TASK_TRACE_SCHEMA_VERSION
    _buffer: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _buffer_bytes: int = field(default=0, init=False, repr=False)
    _bytes_written: int = field(default=0, init=False, repr=False)
    _blob_count: int = field(default=0, init=False, repr=False)
    _blob_total_bytes: int = field(default=0, init=False, repr=False)
    _blob_hashes_seen: set[str] = field(default_factory=set, init=False, repr=False)
    _blob_ref_cache: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _blob_threshold_bytes: int = field(default=DEFAULT_TRACE_BLOB_THRESHOLD_BYTES, init=False, repr=False)
    _event_cap_bytes: int = field(default=DEFAULT_TRACE_EVENT_CAP_BYTES, init=False, repr=False)
    _trace_size_threshold_bytes: int = field(default=DEFAULT_TRACE_SIZE_CAP_BYTES, init=False, repr=False)
    _summary_mode: bool = field(default=False, init=False, repr=False)
    _mode_flip_emitted: bool = field(default=False, init=False, repr=False)
    _prev_messages_seen: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    _fernet: Any | None = field(default=None, init=False, repr=False)
    _disabled_reason: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize counters and env-tunable caps after dataclass construction."""
        if not str(self.user_id or "").strip():
            raise ValueError("TaskTraceWriter requires a non-empty user_id")
        self._blob_threshold_bytes = _trace_positive_int_env(
            "VIOLA_TRACE_BLOB_THRESHOLD_BYTES",
            DEFAULT_TRACE_BLOB_THRESHOLD_BYTES,
        )
        try:
            self._bytes_written = self.path.stat().st_size if self.path.exists() else 0
        except OSError:
            logger.exception("Failed to stat existing task trace %s", self.path)
            self._bytes_written = 0

    @classmethod
    def for_task(
        cls,
        *,
        task_id: str,
        user_scope: str | None = None,
        user_id: str | None = None,
        started_at: str | None,
        session_id: str | None = None,
        root_dir: Path | None = None,
        explicit_path: str | Path | None = None,
        key_provider: KeyProvider | None = None,
        blob_store: BlobStore | None = None,
    ) -> TaskTraceWriter:
        """Construct a writer path under the per-user trace partition."""
        resolved_user_id = str(user_id if user_id is not None else user_scope or "").strip()
        if not str(resolved_user_id or "").strip():
            raise ValueError("TaskTraceWriter.for_task requires user_scope or user_id")
        user_hash = _user_id_hash(resolved_user_id)
        if explicit_path is not None:
            path = Path(explicit_path)
        else:
            base_dir = root_dir or DEFAULT_TASK_TRACE_V2_DIR
            path = base_dir / user_hash / _trace_date_segment(started_at) / ("%s.trace.jsonl.zst.enc" % task_id)
        return cls(
            task_id=task_id,
            path=path,
            user_id=resolved_user_id,
            user_id_hash=user_hash,
            session_id=session_id,
            key_provider=key_provider,
            blob_store=blob_store,
        )

    async def warm_up_keys_async(self) -> None:
        """Pre-resolve trace + blob Fernet keys via the async chain.

        Call this from any async context BEFORE the first synchronous
        ``append_event`` / ``flush`` / blob ``put`` happens so the hot-path
        sync code can use the cached Fernet without falling into
        ``KeyProvider.unwrap_trace_key`` -> ``run_async_synchronously``
        (which trips the ASYNC-1 cross-loop guard on the cloud's main loop).

        Idempotent: subsequent calls are no-ops once ``self._fernet`` and
        the underlying blob store's cache are populated.
        """
        if self._disabled_reason is not None:
            return
        if self._fernet is None:
            from cryptography.fernet import Fernet

            trace_key = await self._get_key_provider().unwrap_trace_key_async()
            self._fernet = Fernet(trace_key)
        # Warm the blob store too — its put() path also needs the trace key.
        blob_store = self._get_blob_store()
        warm = getattr(blob_store, "warm_up_async", None)
        if callable(warm):
            await warm()

    def disable(self, reason: str) -> None:
        """Disable trace writes after a non-fatal setup failure -- LOUDLY.

        A disabled writer must never be silent. Losing the trace loses the
        project's own oracle for that run and strips the trace context off any
        bug report the user files afterwards, so the caller that disables gets
        an ERROR log AND a durable, plaintext marker beside where the trace
        would have been written. The marker is what makes "no trace here"
        mechanically distinguishable from "a trace was skipped, for this
        reason" -- the 2026-08-01 marketing shoot produced zero traces across
        28 turns and nothing on disk said why (#4793).

        The marker holds metadata only (task id, hashed user id, reason,
        timestamp) -- no user content -- so it needs no trace key, which is
        exactly the situation that disables the writer in the first place.
        """
        self._disabled_reason = str(reason or "disabled")
        self._buffer.clear()
        self._buffer_bytes = 0
        logger.error(
            "Task trace DISABLED for task %s (user_id_hash=%s): %s -- this run writes no trace",
            self.task_id,
            self.user_id_hash,
            self._disabled_reason,
            extra={
                "event": "task_trace_disabled",
                "task_id": self.task_id,
                "user_id_hash": self.user_id_hash,
                "session_id": self.session_id,
                "reason": self._disabled_reason,
            },
        )
        self._write_disabled_marker(self._disabled_reason)

    @property
    def disabled_marker_path(self) -> Path:
        """Path of the plaintext marker written when this trace is disabled."""
        return self.path.parent / ("%s%s" % (self.task_id, TRACE_DISABLED_MARKER_SUFFIX))

    def _write_disabled_marker(self, reason: str) -> None:
        marker_path = self.disabled_marker_path
        record = {
            "schema": TRACE_DISABLED_MARKER_SCHEMA,
            "task_id": self.task_id,
            "user_id_hash": self.user_id_hash,
            "session_id": self.session_id,
            "reason": reason,
            "ts": datetime.now(tz=UTC).isoformat(),
            "trace_path_name": self.path.name,
        }
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        except OSError:
            # The marker is the loud signal; if even it cannot be written, say
            # so at ERROR rather than letting the disable go fully dark.
            logger.exception(
                "Could not write the task-trace disabled marker at %s",
                marker_path,
                extra={
                    "event": "task_trace_disabled_marker_write_failed",
                    "task_id": self.task_id,
                    "user_id_hash": self.user_id_hash,
                },
            )

    def append_event(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        force_full: bool = False,
        flush_now: bool = False,
    ) -> None:
        """Append one schema v2 event row after sidecar/blob/cap processing."""
        if self._disabled_reason is not None:
            return
        from diagnostics import latency_spans

        with latency_spans.span("TRACE_APPEND", event=event):
            record = self._prepare_event_record(event, payload, force_full=force_full)
            self._buffer_record(record, flush_now=flush_now)

    def _prepare_event_record(
        self,
        event: str,
        payload: dict[str, Any],
        *,
        force_full: bool,
    ) -> dict[str, Any]:
        from diagnostics import latency_spans

        with latency_spans.span("TRACE_BUILD_RECORD", event=event):
            record = self._build_record(event, payload)
        if self._summary_mode and not force_full:
            record = self._summary_record(record)

        with latency_spans.span("TRACE_BLOB_REFS_AND_CAPS", event=event):
            return self._apply_blob_refs_and_caps(record, force_blob_fields=self._forced_blob_fields(event))

    def _buffer_record(self, record: dict[str, Any], *, flush_now: bool) -> None:
        record_bytes = _json_line_bytes(record)
        self._buffer.append(record)
        self._buffer_bytes += len(record_bytes)
        if flush_now or len(self._buffer) >= TRACE_V2_FLUSH_EVENT_COUNT or self._buffer_bytes >= TRACE_V2_FLUSH_BYTES:
            self.flush()

    def flush(self) -> None:
        """Flush buffered rows as one compressed, encrypted, length-prefixed segment."""
        if self._disabled_reason is not None:
            return
        if not self._buffer:
            return

        from diagnostics import latency_spans

        rows = list(self._buffer)
        with latency_spans.span("TRACE_FLUSH", rows=len(rows), buffer_bytes=self._buffer_bytes):
            jsonl_bytes = b"".join(_json_line_bytes(row) for row in rows)
            compressed = _compress_trace_block(jsonl_bytes)
            encrypted = self._encrypt_trace_block(compressed)
        if len(encrypted) >= 2 ** (8 * TRACE_V2_LENGTH_PREFIX_BYTES):
            raise ValueError("trace segment exceeds 4-byte length prefix capacity")

        segment = struct.pack(">I", len(encrypted)) + encrypted
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("ab") as handle:
                handle.write(segment)
        except OSError:
            logger.exception("Failed to append task trace v2 segment for %s", self.task_id)
            return

        self._buffer.clear()
        self._buffer_bytes = 0
        self._bytes_written += len(segment)
        if self._bytes_written >= self._trace_size_threshold_bytes and not self._mode_flip_emitted:
            self._mode_flip_emitted = True
            self._summary_mode = True
            self.append_mode_flip(
                at_step=self._last_buffer_step(rows),
                reason="trace_size_cap",
                bytes_written=self._bytes_written,
                bytes_threshold=self._trace_size_threshold_bytes,
            )

    def close(self) -> None:
        """Flush pending rows. Kept explicit so callers do not rely on destructors."""
        self.flush()

    def append_start(
        self,
        *,
        started_at: str,
        request: dict[str, Any],
        runtime: dict[str, Any],
        continuity: dict[str, Any],
        initial_response: dict[str, Any] | None = None,
        force_full: bool = False,
    ) -> None:
        """Append `trace_start` using v2 request refs and inline runtime metadata."""
        payload = {
            "ts": started_at,
            "started_at": started_at,
            "artifact_path": self._relative_artifact_path(),
            "request": request,
            "runtime": runtime,
            "continuity": continuity,
        }
        if initial_response is not None:
            payload["initial_response"] = initial_response
        self.append_event("trace_start", payload, force_full=force_full, flush_now=True)
        self._seed_previous_messages(request=request)

    def append_resume(
        self,
        *,
        ts: str,
        request: dict[str, Any],
        continuity: dict[str, Any],
        checkpoint: dict[str, Any],
        force_full: bool = False,
    ) -> None:
        """Append `trace_resume`, blob-referencing large checkpoints."""
        self.append_event(
            "trace_resume",
            {
                "ts": ts,
                "artifact_path": self._relative_artifact_path(),
                "request": request,
                "continuity": continuity,
                "checkpoint": checkpoint,
            },
            force_full=force_full,
            flush_now=True,
        )
        self._seed_previous_messages(request=request, checkpoint=checkpoint)

    def append_step(
        self,
        *,
        step: int,
        ts: str,
        step_kind: str,
        agent_step: dict[str, Any],
        llm_input: dict[str, Any] | None,
        llm_output: dict[str, Any] | None,
        continuity_before: dict[str, Any] | None,
        continuity_after: dict[str, Any] | None,
        attempt_id: str | None = None,
        control: dict[str, Any] | None = None,
        tool_execution: dict[str, Any] | None = None,
        delta_messages: list[dict[str, Any]] | None = None,
        force_full: bool = False,
    ) -> None:
        """Append `trace_step` with delta-only messages and optional full override."""
        agent_step_payload = dict(agent_step or {})
        raw_messages = agent_step_payload.pop("messages", None)
        message_delta = self._resolve_delta_messages(raw_messages, delta_messages)

        payload: dict[str, Any] = {
            "ts": ts,
            "step": step,
            "step_kind": step_kind,
            "delta_messages": message_delta,
            "agent_step": agent_step_payload,
            "llm_output": llm_output or {},
            "continuity_before": continuity_before or {},
            "continuity_after": continuity_after or {},
        }
        if llm_input:
            payload["llm_input_ref"] = llm_input
        if attempt_id:
            payload["attempt_id"] = attempt_id
        if control is not None:
            payload["control"] = control
        if tool_execution is not None:
            payload["tool_execution"] = tool_execution
        if self._summary_mode and not force_full:
            summary_source = {
                "agent_step": agent_step_payload,
                "llm_output": llm_output or {},
                "tool_execution": tool_execution or {},
            }
            summary_payload: dict[str, Any] = {
                "ts": ts,
                "step": step,
                "step_kind": step_kind,
            }
            if attempt_id:
                summary_payload["attempt_id"] = attempt_id
            tool_name = self._extract_tool_name(summary_source)
            if tool_name is not None:
                summary_payload["tool_name"] = tool_name
            ok = self._extract_ok(summary_source)
            if ok is not None:
                summary_payload["ok"] = ok
            duration_ms = self._extract_duration_ms(summary_source)
            if duration_ms is not None:
                summary_payload["duration_ms"] = duration_ms
            self.append_event("trace_step", summary_payload, force_full=force_full)
            return
        self.append_event("trace_step", payload, force_full=force_full)

    def append_llm_attempt_start(
        self,
        *,
        ts: str,
        attempt_id: str,
        call_kind: str,
        request_mode: str,
        provider: dict[str, Any],
        first_turn: bool,
        continuity_before: dict[str, Any] | None,
        request: dict[str, Any],
        force_full: bool = False,
    ) -> None:
        """Append `trace_llm_attempt_start` with the request stored as a blob ref."""
        self.append_event(
            "trace_llm_attempt_start",
            {
                "ts": ts,
                "attempt_id": attempt_id,
                "call_kind": call_kind,
                "request_mode": request_mode,
                "provider": provider,
                "first_turn": first_turn,
                "continuity_before": continuity_before or {},
                "request": request,
            },
            force_full=force_full,
        )

    def append_llm_provider_payload(
        self,
        *,
        ts: str,
        attempt_id: str,
        call_kind: str,
        payload_stage: str,
        payload: dict[str, Any],
        exact: bool = True,
        force_full: bool = False,
    ) -> None:
        """Append `trace_llm_provider_payload` with provider payload as a blob ref."""
        self.append_event(
            "trace_llm_provider_payload",
            {
                "ts": ts,
                "attempt_id": attempt_id,
                "call_kind": call_kind,
                "payload_stage": payload_stage,
                "exact": exact,
                "payload": payload,
            },
            force_full=force_full,
        )

    def append_llm_attempt_response(
        self,
        *,
        ts: str,
        attempt_id: str,
        call_kind: str,
        response: dict[str, Any],
        continuity_after: dict[str, Any] | None,
        force_full: bool = False,
    ) -> None:
        """Append `trace_llm_attempt_response` with response stored as a blob ref."""
        self.append_event(
            "trace_llm_attempt_response",
            {
                "ts": ts,
                "attempt_id": attempt_id,
                "call_kind": call_kind,
                "response": response,
                "continuity_after": continuity_after or {},
            },
            force_full=force_full,
        )

    def append_llm_attempt_failure(
        self,
        *,
        ts: str,
        attempt_id: str,
        call_kind: str,
        error: dict[str, Any],
        continuity_after: dict[str, Any] | None,
        force_full: bool = False,
    ) -> None:
        """Append `trace_llm_attempt_failure`, blob-referencing large error details."""
        self.append_event(
            "trace_llm_attempt_failure",
            {
                "ts": ts,
                "attempt_id": attempt_id,
                "call_kind": call_kind,
                "error": error,
                "continuity_after": continuity_after or {},
            },
            force_full=force_full,
        )

    def append_llm_retry_fallback(
        self,
        *,
        ts: str,
        attempt_id: str | None,
        call_kind: str,
        kind: str,
        reason: str,
        next_action: str,
        detail: dict[str, Any] | None = None,
        force_full: bool = False,
    ) -> None:
        """Append `trace_llm_retry_fallback` unchanged except common v2 fields."""
        payload: dict[str, Any] = {
            "ts": ts,
            "call_kind": call_kind,
            "kind": kind,
            "reason": reason,
            "next_action": next_action,
        }
        if attempt_id:
            payload["attempt_id"] = attempt_id
        if detail is not None:
            payload["detail"] = detail
        self.append_event("trace_llm_retry_fallback", payload, force_full=force_full)

    def append_compaction(
        self,
        *,
        ts: str,
        step: int,
        compaction: dict[str, Any],
        force_full: bool = False,
    ) -> None:
        """Append `trace_compaction` with large states stored as blob refs."""
        self.append_event(
            "trace_compaction",
            {
                "ts": ts,
                "step": step,
                "compaction": compaction,
            },
            force_full=force_full,
        )

    def append_gate(
        self,
        *,
        ts: str,
        gate: str,
        state: str,
        message: str,
        tool_name: str | None = None,
        page_url: str | None = None,
        gate_origin: str | None = None,
        force_full: bool = False,
    ) -> None:
        """Append the small `trace_gate` event."""
        payload = {
            "ts": ts,
            "gate": gate,
            "state": state,
            "message": message,
        }
        if tool_name:
            payload["tool_name"] = tool_name
        if page_url:
            payload["page_url"] = page_url
        if gate_origin:
            payload["gate_origin"] = gate_origin
        self.append_event("trace_gate", payload, force_full=force_full)

    def append_progress(
        self,
        *,
        ts: str,
        step: int,
        message: str,
        status: str,
        tool_name: str | None = None,
        estimated_total_steps: int | None = None,
        force_full: bool = False,
    ) -> None:
        """Append the small `trace_progress` event."""
        payload: dict[str, Any] = {
            "ts": ts,
            "step": step,
            "message": message,
            "status": status,
        }
        if tool_name:
            payload["tool_name"] = tool_name
        if estimated_total_steps is not None:
            payload["estimated_total_steps"] = estimated_total_steps
        self.append_event("trace_progress", payload, force_full=force_full)

    def append_complete(
        self,
        *,
        completed_at: str,
        outcome: str,
        final_answer: str,
        total_steps: int,
        total_duration_s: float,
        final_context_usage_pct: float,
        continuity: dict[str, Any],
        gate_state: dict[str, Any],
        control: dict[str, Any] | None = None,
        force_full: bool = False,
    ) -> None:
        """Append `trace_complete` with trace and blob totals."""
        if self._disabled_reason is not None:
            return
        self.flush()
        payload = {
            "ts": completed_at,
            "completed_at": completed_at,
            "outcome": outcome,
            "final_answer": final_answer,
            "total_steps": total_steps,
            "total_duration_s": total_duration_s,
            "final_context_usage_pct": final_context_usage_pct,
            "continuity": continuity,
            "gate_state": gate_state,
        }
        if control is not None:
            payload["control"] = control
        record = self._prepare_event_record("trace_complete", payload, force_full=force_full)
        record = self._finalize_complete_record_totals(record)
        self._buffer_record(record, flush_now=True)

    def append_mode_flip(
        self,
        *,
        at_step: int,
        reason: str,
        bytes_written: int,
        bytes_threshold: int,
    ) -> None:
        """Append the v2-only `trace_mode_flip` event."""
        self.append_event(
            "trace_mode_flip",
            {
                "ts": _utc_now_iso(),
                "at_step": at_step,
                "reason": reason,
                "bytes_written": bytes_written,
                "bytes_threshold": bytes_threshold,
            },
            force_full=True,
            flush_now=True,
        )

    def _build_record(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            sanitized_payload = _sanitize_for_trace(payload)
        except _TRACE_SANITIZER_EXCEPTIONS:
            logger.exception("Trace sanitizer failed for %s event on task %s", event, self.task_id)
            sanitized_payload = {"payload": TRACE_SANITIZER_ERROR_MARKER}
        if not isinstance(sanitized_payload, dict):
            sanitized_payload = {"payload": sanitized_payload}
        ts = sanitized_payload.pop("ts", None) or _utc_now_iso()
        record: dict[str, Any] = {
            "event": event,
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "user_id_hash": self.user_id_hash,
            "session_id": self.session_id,
            "ts": ts,
        }
        record.update(sanitized_payload)
        return record

    def _finalize_complete_record_totals(self, record: dict[str, Any]) -> dict[str, Any]:
        finalized = dict(record)
        predicted_trace_size = self._bytes_written
        for _ in range(4):
            finalized["trace_size_bytes"] = predicted_trace_size
            finalized["blob_count"] = self._blob_count
            finalized["blob_total_bytes"] = self._blob_total_bytes
            finalized = self._enforce_event_cap(finalized)
            finalized["blob_count"] = self._blob_count
            finalized["blob_total_bytes"] = self._blob_total_bytes
            next_trace_size = self._bytes_written + self._segment_size_for_rows([finalized])
            if next_trace_size == predicted_trace_size:
                break
            predicted_trace_size = next_trace_size
        finalized["trace_size_bytes"] = predicted_trace_size
        return finalized

    def _segment_size_for_rows(self, rows: list[dict[str, Any]]) -> int:
        jsonl_bytes = b"".join(_json_line_bytes(row) for row in rows)
        compressed = _compress_trace_block(jsonl_bytes)
        encrypted = self._encrypt_trace_block(compressed)
        return TRACE_V2_LENGTH_PREFIX_BYTES + len(encrypted)

    def _forced_blob_fields(self, event: str) -> set[str]:
        if event == "trace_llm_attempt_start":
            return {"request"}
        if event == "trace_llm_provider_payload":
            return {"payload"}
        if event == "trace_llm_attempt_response":
            return {"response"}
        if event == "trace_step":
            return {"llm_input_ref"}
        return set()

    def _apply_blob_refs_and_caps(
        self,
        record: dict[str, Any],
        *,
        force_blob_fields: set[str],
    ) -> dict[str, Any]:
        processed = dict(record)
        for key, value in list(processed.items()):
            if key in TRACE_V2_COMMON_FIELDS:
                continue
            if key in force_blob_fields:
                processed[key] = self._blob_ref_for_value(self._maybe_blob_value(value, path=(key,)))
            else:
                processed[key] = self._maybe_blob_value(value, path=(key,))
        return self._enforce_event_cap(processed)

    def _maybe_blob_value(self, value: Any, *, path: tuple[str, ...]) -> Any:
        processed, _size = self._maybe_blob_value_sized(value, path=path)
        return processed

    def _maybe_blob_value_sized(self, value: Any, *, path: tuple[str, ...]) -> tuple[Any, int]:
        """Recursive blob-ref promotion pass, sized bottom-up in one traversal.

        Returns ``(processed_value, compact_json_byte_length)`` where the length is
        the size ``processed_value`` would occupy if serialized via ``_json_bytes``
        (the same compact `json.dumps` format used everywhere else in this module).

        The prior implementation re-derived that length at *every* ancestor level by
        calling ``_value_to_blob_bytes`` -- a fresh ``json.dumps`` over the entire
        already-processed subtree -- while walking back up. A value nested K levels
        deep was therefore fully re-serialized O(K) times (measured: seconds per
        turn on the event loop; see
        `_diag/2026-07-08/latency_decomposition/ATTRIBUTION.md`). This version
        computes each node's size by summing its children's sizes plus a few
        constant-size separators, so every subtree is measured in O(1) work
        relative to its own children -- no re-serialization, O(N) total instead of
        O(depth * N). Actual bytes (a single real ``json.dumps``) are materialized
        only at the moment a subtree is promoted to a blob ref, which collapses it
        into a small ref dict for every ancestor above it.
        """
        if self._is_blob_ref(value) or self._is_truncation_marker(value):
            return value, len(_json_bytes(value))

        if isinstance(value, dict):
            processed: dict[str, Any] = {}
            size = 2  # "{" + "}"
            for index, (key, inner) in enumerate(value.items()):
                key_str = str(key)
                child, child_size = self._maybe_blob_value_sized(inner, path=(*path, key_str))
                processed[key_str] = child
                if index:
                    size += 1  # ","
                size += len(_json_bytes(key_str)) + 1 + child_size  # key + ":" + child

            threshold = 1024 if path == ("request", "system_prompt_text") else self._blob_threshold_bytes
            if path == ("request", "tools") or size > threshold:
                promoted = self._blob_ref_for_value(processed, prepared=self._json_blob_prepared(processed))
                return promoted, len(_json_bytes(promoted))
            return processed, size

        if isinstance(value, list):
            processed_list: list[Any] = []
            size = 2  # "[" + "]"
            for index, item in enumerate(value):
                child, child_size = self._maybe_blob_value_sized(item, path=(*path, str(index)))
                processed_list.append(child)
                if index:
                    size += 1  # ","
                size += child_size

            if path == ("request", "tools") or size > self._blob_threshold_bytes:
                promoted = self._blob_ref_for_value(processed_list, prepared=self._json_blob_prepared(processed_list))
                return promoted, len(_json_bytes(promoted))
            return processed_list, size

        if isinstance(value, str):
            threshold = 1024 if path == ("request", "system_prompt_text") else self._blob_threshold_bytes
            raw_bytes = value.encode("utf-8", errors="replace")
            if len(raw_bytes) > threshold:
                preview = redact_pii(redact_card_data(value[:TRACE_V2_PREVIEW_CHARS]))
                promoted = self._blob_ref_for_value(
                    value,
                    prepared=(raw_bytes, "text/plain", str(preview)[:TRACE_V2_PREVIEW_CHARS]),
                )
                return promoted, len(_json_bytes(promoted))
            return value, len(_json_bytes(value))

        return value, len(_json_bytes(value))

    def _json_blob_prepared(self, processed_value: Any) -> tuple[bytes, str, str]:
        """Materialize the ``(content, content_type, preview)`` tuple for a dict/list blob.

        Called only at the point of actual promotion (not for every subtree visited
        while measuring size), matching the previous behavior where a preview was
        computed but discarded for any value that never crossed the blob threshold.
        """
        content = _json_bytes(processed_value)
        preview_text = content.decode("utf-8", errors="replace")[:TRACE_V2_PREVIEW_CHARS]
        preview = redact_pii(redact_card_data(preview_text))
        return content, "application/json", str(preview)[:TRACE_V2_PREVIEW_CHARS]

    def _enforce_event_cap(self, record: dict[str, Any]) -> dict[str, Any]:
        if len(_json_line_bytes(record)) <= self._event_cap_bytes:
            return record

        capped = dict(record)
        candidates = []
        for key, value in capped.items():
            if key in TRACE_V2_COMMON_FIELDS or self._is_blob_ref(value) or self._is_truncation_marker(value):
                continue
            candidates.append((len(self._value_to_blob_bytes(value)[0]), key, value))

        for _, key, value in sorted(candidates, reverse=True):
            blob_ref = self._blob_ref_for_value(value)
            original_size = blob_ref.get("size", len(self._value_to_blob_bytes(value)[0]))
            capped[key] = {
                "truncated": True,
                "blob_ref": blob_ref,
                "original_size": original_size,
            }
            if len(_json_line_bytes(capped)) <= self._event_cap_bytes:
                return capped

        logger.warning("Trace v2 event for task %s remains above cap after truncation", self.task_id)
        return capped

    def _blob_ref_for_value(
        self,
        value: Any,
        *,
        prepared: tuple[bytes, str, str | None] | None = None,
    ) -> dict[str, Any]:
        content, content_type, preview = prepared if prepared is not None else self._value_to_blob_bytes(value)
        content_hash = hashlib.sha256(content).hexdigest()
        cached = self._blob_ref_cache.get((content_hash, content_type))
        if cached is not None:
            return dict(cached)

        ref = self._get_blob_store_for_put().put(content, content_type=content_type)
        if ref.sha256 not in self._blob_hashes_seen:
            self._blob_hashes_seen.add(ref.sha256)
            self._blob_count += 1
            self._blob_total_bytes += ref.size

        first_chars = ref.first_chars if ref.first_chars is not None else preview
        ref_dict: dict[str, Any] = {
            "$ref": "blobs/%s/%s.blob" % (ref.sha256[:2], ref.sha256[2:]),
            "size": ref.size,
            "sha256": ref.sha256,
            "content_type": ref.content_type,
        }
        if first_chars:
            ref_dict["first_chars"] = str(first_chars)[:TRACE_V2_PREVIEW_CHARS]
        self._blob_ref_cache[(ref.sha256, ref.content_type)] = dict(ref_dict)
        return ref_dict

    def _value_to_blob_bytes(self, value: Any) -> tuple[bytes, str, str | None]:
        if isinstance(value, str):
            content = value.encode("utf-8", errors="replace")
            preview = redact_pii(redact_card_data(value[:TRACE_V2_PREVIEW_CHARS]))
            return content, "text/plain", str(preview)[:TRACE_V2_PREVIEW_CHARS]

        content = _json_bytes(value)
        preview_text = content.decode("utf-8", errors="replace")[:TRACE_V2_PREVIEW_CHARS]
        preview = redact_pii(redact_card_data(preview_text))
        return content, "application/json", str(preview)[:TRACE_V2_PREVIEW_CHARS]

    def _get_blob_store(self) -> BlobStore:
        if self.blob_store is None:
            from services.persistence.blob_store import BlobStore

            self.blob_store = BlobStore(
                self.user_id,
                self._get_key_provider(),
                root_dir=self._user_blob_root(),
            )
        return self.blob_store

    def _get_blob_store_for_put(self) -> BlobStore:
        blob_store = self._get_blob_store()
        if getattr(blob_store, "_cached_fernet", None) is None:
            if self._fernet is None:
                from cryptography.fernet import Fernet

                self._fernet = Fernet(self._get_key_provider().unwrap_trace_key())
            try:
                blob_store._cached_fernet = self._fernet
            except AttributeError:
                pass
        return blob_store

    def _get_key_provider(self) -> KeyProvider:
        if self.key_provider is None:
            from services.persistence.trace_keys import KeyProvider

            self.key_provider = KeyProvider(self.user_id)
        return self.key_provider

    def _user_trace_root(self) -> Path:
        parents = self.path.parents
        if len(parents) >= 2 and parents[1].name == self.user_id_hash:
            return parents[1]
        if parents and parents[0].name == self.user_id_hash:
            return parents[0]
        return self.path.parent / self.user_id_hash

    def _user_blob_root(self) -> Path:
        return self._user_trace_root() / "blobs"

    def _relative_artifact_path(self) -> str:
        """Return the trace path relative to the user partition root.

        The persisted trace must never embed the host's *absolute* filesystem
        path. An absolute path drags host- and run-specific components into the
        encrypted artifact -- e.g. a CI temp-dir counter such as
        ``pytest-12362`` -- and a payment-sentinel scan of the raw trace then
        flags that coincidental digit run (``123``) as a card leak even though no
        card field ever reached the trace (GH #3181). The v2 trace schema
        documents ``artifact_path`` as a partition-relative path, not
        ``str(self.path)``; anchoring on the ``user_id_hash`` segment yields
        ``<user_id_hash>/<date>/<file>`` and keeps the host prefix out of the
        trace entirely.
        """
        parts = self.path.parts
        for idx in range(len(parts) - 1, -1, -1):
            if parts[idx] == self.user_id_hash:
                return str(Path(*parts[idx:]))
        return self.path.name

    def _encrypt_trace_block(self, compressed: bytes) -> bytes:
        if self._fernet is None:
            from cryptography.fernet import Fernet

            self._fernet = Fernet(self._get_key_provider().unwrap_trace_key())
        return self._fernet.encrypt(compressed)

    def _resolve_delta_messages(
        self,
        raw_messages: Any,
        delta_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        sanitized_full = _sanitize_for_trace(raw_messages) if isinstance(raw_messages, list) else None
        if delta_messages is not None:
            sanitized_delta = _sanitize_for_trace(delta_messages)
            delta = sanitized_delta if isinstance(sanitized_delta, list) else []
            if isinstance(sanitized_full, list):
                self._prev_messages_seen = list(sanitized_full)
            else:
                self._prev_messages_seen.extend(item for item in delta if isinstance(item, dict))
            return delta

        if not isinstance(sanitized_full, list):
            return []

        previous = self._prev_messages_seen
        if len(sanitized_full) >= len(previous) and sanitized_full[: len(previous)] == previous:
            delta = sanitized_full[len(previous) :]
        else:
            delta = sanitized_full
        self._prev_messages_seen = list(sanitized_full)
        return delta

    def _seed_previous_messages(
        self,
        *,
        request: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        sanitized_checkpoint = _sanitize_for_trace(checkpoint) if isinstance(checkpoint, dict) else {}
        if isinstance(sanitized_checkpoint, dict):
            checkpoint_messages = sanitized_checkpoint.get("messages")
            if isinstance(checkpoint_messages, list):
                self._prev_messages_seen = list(checkpoint_messages)
                return

        sanitized_request = _sanitize_for_trace(request)
        if not isinstance(sanitized_request, dict):
            return

        request_messages = sanitized_request.get("initial_model_messages")
        if isinstance(request_messages, list):
            self._prev_messages_seen = list(request_messages)
            return

        user_text = sanitized_request.get("user_text")
        if user_text is not None:
            self._prev_messages_seen = [{"role": "user", "content": user_text}]

    def _summary_record(self, record: dict[str, Any]) -> dict[str, Any]:
        summary = {key: record[key] for key in TRACE_V2_COMMON_FIELDS if key in record}
        for key in (
            "step",
            "step_kind",
            "attempt_id",
            "call_kind",
            "payload_stage",
            "request_mode",
            "first_turn",
            "exact",
            "gate",
            "state",
            "status",
            "estimated_total_steps",
            "outcome",
            "total_steps",
            "total_duration_s",
            "trace_size_bytes",
            "blob_count",
            "blob_total_bytes",
            "kind",
            "next_action",
        ):
            if key in record:
                summary[key] = record[key]

        tool_name = self._extract_tool_name(record)
        if tool_name is not None:
            summary["tool_name"] = tool_name

        ok = self._extract_ok(record)
        if ok is not None:
            summary["ok"] = ok

        error_type = self._extract_error_type(record)
        if error_type is not None:
            summary["error_type"] = error_type

        duration_ms = self._extract_duration_ms(record)
        if duration_ms is not None:
            summary["duration_ms"] = duration_ms
        return summary

    def _extract_tool_name(self, record: dict[str, Any]) -> str | None:
        if isinstance(record.get("tool_name"), str):
            return record["tool_name"]
        for key in ("tool_execution", "agent_step"):
            value = record.get(key)
            if isinstance(value, dict) and isinstance(value.get("tool_name"), str):
                return value["tool_name"]
        llm_output = record.get("llm_output")
        if isinstance(llm_output, dict):
            tool_call = llm_output.get("tool_call")
            if isinstance(tool_call, dict) and isinstance(tool_call.get("name"), str):
                return tool_call["name"]
        return None

    def _extract_ok(self, record: dict[str, Any]) -> bool | None:
        if isinstance(record.get("ok"), bool):
            return record["ok"]
        for key in ("tool_execution", "agent_step"):
            value = record.get(key)
            if isinstance(value, dict) and isinstance(value.get("ok"), bool):
                return value["ok"]
            if isinstance(value, dict) and isinstance(value.get("success"), bool):
                return value["success"]
        if record.get("event") == "trace_llm_attempt_failure":
            return False
        return None

    def _extract_error_type(self, record: dict[str, Any]) -> str | None:
        if isinstance(record.get("error_type"), str):
            return record["error_type"]
        error = record.get("error")
        if isinstance(error, dict):
            for key in ("type", "error_type", "code"):
                if isinstance(error.get(key), str):
                    return error[key]
        tool_execution = record.get("tool_execution")
        if isinstance(tool_execution, dict):
            for key in ("error_type", "error"):
                if isinstance(tool_execution.get(key), str):
                    return tool_execution[key]
        return None

    def _extract_duration_ms(self, record: dict[str, Any]) -> int | float | None:
        for key in ("duration_ms", "elapsed_ms"):
            if isinstance(record.get(key), (int, float)):
                return record[key]
        tool_execution = record.get("tool_execution")
        if isinstance(tool_execution, dict):
            for key in ("duration_ms", "elapsed_ms"):
                if isinstance(tool_execution.get(key), (int, float)):
                    return tool_execution[key]
        return None

    def _last_buffer_step(self, rows: list[dict[str, Any]]) -> int:
        for row in reversed(rows):
            step = row.get("step")
            if isinstance(step, int):
                return step
        return 0

    def _is_blob_ref(self, value: Any) -> bool:
        return isinstance(value, dict) and isinstance(value.get("$ref"), str)

    def _is_truncation_marker(self, value: Any) -> bool:
        return isinstance(value, dict) and value.get("truncated") is True and "blob_ref" in value

    def append_tool_execution(
        self,
        *,
        ts: str,
        step: int | None,
        tool_name: str,
        ok: bool,
        duration_ms: int | float | None,
        result: Any,
        error: Any,
        force_full: bool = False,
    ) -> None:
        """Append `trace_tool_execution`, blob-referencing large results."""
        if isinstance(result, dict) and any(
            key in result for key in ("tool_input", "tool_result", "model_visible_result", "tool_use_id")
        ):
            tool_execution = dict(result)
            tool_execution.setdefault("tool_name", tool_name)
            tool_execution.setdefault("ok", ok)
            tool_execution.setdefault("duration_ms", duration_ms)
            tool_execution.setdefault("error", error)
            if "result" not in tool_execution:
                tool_result = tool_execution.get("tool_result")
                if isinstance(tool_result, dict) and "data" in tool_result:
                    tool_execution["result"] = tool_result.get("data")
                else:
                    tool_execution["result"] = result
        else:
            tool_execution = {
                "tool_name": tool_name,
                "ok": ok,
                "duration_ms": duration_ms,
                "result": result,
                "error": error,
            }
        payload: dict[str, Any] = {
            "ts": ts,
            "tool_execution": tool_execution,
        }
        if step is not None:
            payload["step"] = step
        self.append_event("trace_tool_execution", payload, force_full=force_full)

    def append_llm_stream_chunk(
        self,
        *,
        ts: str,
        attempt_id: str,
        chunk_index: int,
        chunk_kind: str,
        delta: dict[str, Any],
    ) -> None:
        self.append_event(
            "trace_llm_stream_chunk",
            {
                "ts": ts,
                "attempt_id": attempt_id,
                "chunk_index": chunk_index,
                "chunk_kind": chunk_kind,
                "delta": delta,
            },
        )
