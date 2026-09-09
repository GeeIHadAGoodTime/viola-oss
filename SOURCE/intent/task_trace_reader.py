"""NOTE: v1 reader paths (_iter_v1_records, _step_v1) are HISTORICAL READ-ONLY.
v1 writes were removed in this PR (Wave 1M). Existing v1 trace files on
developer machines and test fixtures still need to be readable.
Do not extend v1 reader behavior. New schema work goes to v2 paths only.

Schema-aware task trace reader.

TraceReader is the compatibility bridge between legacy plaintext task traces and
trace-v2's encrypted, blob-backed delta log. Indexing and streaming stay cheap;
step reconstruction and content diffs resolve blobs only when a caller asks for
full payloads.
"""

from __future__ import annotations

import copy
import difflib
import json
import os
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

from core.logging_config import get_logger
from core.platform import get_project_root
from intent.task_trace import (
    DEFAULT_TASK_TRACE_DIR,
    DEFAULT_TASK_TRACE_V2_DIR,
    TASK_TRACE_SCHEMA_VERSION,
    TRACE_V2_LENGTH_PREFIX_BYTES,
    _safe_path_segment,
    _user_id_hash,
)
from services.persistence.blob_store import BlobRef, BlobStore

if TYPE_CHECKING:
    from services.persistence.trace_keys import KeyProvider

logger = get_logger(__name__)

LEGACY_V1_SCHEMA_VERSION = 1
_DIFF_LINE_LIMIT = 1000


@dataclass(slots=True)
class Event:
    """One raw trace event returned by `TraceReader.stream()`."""

    event_type: str
    schema_version: int
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event(self) -> str:
        """Compatibility alias for the event name."""
        return self.event_type


@dataclass(slots=True)
class StepSummary:
    """Lightweight per-step metadata used by `TraceIndex`."""

    step: int
    step_kind: str
    tool_name: str | None
    ok: bool | None
    page_url: str | None


@dataclass(slots=True)
class TraceIndex:
    """Lightweight trace index for event counts, steps, and trace totals."""

    task_id: str
    schema_version: int
    started_at: str | None
    completed_at: str | None
    outcome: str | None
    total_steps: int
    event_counts: dict[str, int] = field(default_factory=dict)
    step_index: list[StepSummary] = field(default_factory=list)
    blob_count: int = 0
    trace_size_bytes: int = 0
    mode_flipped: bool = False


@dataclass(slots=True)
class StepView:
    """Reconstructed view of one trace step."""

    step: int
    step_kind: str
    ts: str
    cumulative_messages: list[dict[str, Any]] = field(default_factory=list)
    attempt_id: str | None = None
    llm_input: dict[str, Any] | None = None
    llm_output: dict[str, Any] | None = None
    reasoning: dict[str, Any] | None = None
    tool_execution: dict[str, Any] | None = None
    control: dict[str, Any] | None = None


@dataclass(slots=True)
class Diff:
    """Character-level diff for one normalized trace field."""

    field: str
    a_size: int
    b_size: int
    diff_lines: list[str] = field(default_factory=list)
    identical: bool = False


class TraceReader:
    """Read v1 JSONL traces and v2 encrypted zstd traces through one interface."""

    def __init__(
        self,
        task_id: str,
        user_id: str,
        key_provider: KeyProvider,
        root_dir: Path | None = None,
        *,
        recover_corrupt_tail: bool = False,
        audit_actor: str | None = None,
    ) -> None:
        """Create a reader and auto-detect schema_version=1 vs 2 from trace files."""
        self.task_id = task_id
        self.user_id = user_id
        self.user_id_hash = _user_id_hash(user_id)
        self.root_dir = Path(root_dir) if root_dir is not None else None
        self._key_provider_source = key_provider
        self._key_provider: KeyProvider | None = None
        self._fernet: Fernet | None = None
        self._blob_store: BlobStore | None = None
        self._blob_value_cache: dict[tuple[str, str], Any] = {}
        self._v2_base_dir: Path | None = None
        self._recover_corrupt_tail = recover_corrupt_tail
        self._audit_actor = audit_actor

        located = self._locate_trace_file()
        self.path = located[0]
        self.schema_version = located[1]
        if self.schema_version == TASK_TRACE_SCHEMA_VERSION:
            self._v2_base_dir = self._infer_v2_base_dir(self.path)

    def index(self) -> TraceIndex:
        """Return a lightweight index without materializing blob content."""
        event_counts: dict[str, int] = {}
        step_index: list[StepSummary] = []
        blob_refs: set[str] = set()
        started_at: str | None = None
        completed_at: str | None = None
        outcome: str | None = None
        total_steps: int | None = None
        complete_blob_count: int | None = None
        complete_trace_size: int | None = None
        mode_flipped = False

        audit_state = {"audited": False}
        for event in self._stream_events(reason="trace_reader.index", audit_state=audit_state):
            payload = event.payload
            event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1
            blob_refs.update(_iter_blob_ref_paths(payload))

            if event.event_type in {"trace_start", "trace_resume"} and started_at is None:
                started_at = _as_optional_str(payload.get("started_at") or payload.get("ts"))
            if event.event_type == "trace_step":
                step_index.append(_step_summary(payload))
            elif event.event_type == "trace_complete":
                completed_at = _as_optional_str(payload.get("completed_at") or payload.get("ts"))
                outcome = _as_optional_str(payload.get("outcome"))
                total_steps = _as_optional_int(payload.get("total_steps"))
                complete_blob_count = _as_optional_int(payload.get("blob_count"))
                complete_trace_size = _as_optional_int(payload.get("trace_size_bytes"))
            elif event.event_type == "trace_mode_flip":
                mode_flipped = True

        return TraceIndex(
            task_id=self.task_id,
            schema_version=self.schema_version,
            started_at=started_at,
            completed_at=completed_at,
            outcome=outcome,
            total_steps=total_steps if total_steps is not None else len(step_index),
            event_counts=event_counts,
            step_index=step_index,
            blob_count=complete_blob_count if complete_blob_count is not None else len(blob_refs),
            trace_size_bytes=complete_trace_size if complete_trace_size is not None else self.path.stat().st_size,
            mode_flipped=mode_flipped,
        )

    def step(self, n: int) -> StepView:
        """Return one reconstructed step by replaying events from trace start."""
        if self.schema_version == LEGACY_V1_SCHEMA_VERSION:
            return self._step_v1(n)
        return self._step_v2(n)

    def diff(self, other: TraceReader, *, field: str) -> Diff:
        """Compare this trace with another trace on a specific normalized field."""
        left = self._field_value(field)
        right = other._field_value(field)
        left_text = _stringify_for_diff(left)
        right_text = _stringify_for_diff(right)
        identical = left_text == right_text
        diff_lines: list[str] = []

        if not identical:
            diff_lines = list(
                difflib.unified_diff(
                    left_text.splitlines(),
                    right_text.splitlines(),
                    fromfile="a",
                    tofile="b",
                    lineterm="",
                )
            )
            if len(diff_lines) > _DIFF_LINE_LIMIT:
                diff_lines = diff_lines[: _DIFF_LINE_LIMIT - 1]
                diff_lines.append("... diff truncated at 1000 lines ...")

        return Diff(
            field=field,
            a_size=len(left_text),
            b_size=len(right_text),
            diff_lines=diff_lines,
            identical=identical,
        )

    def stream(self) -> Iterator[Event]:
        """Yield raw events in append order without blob dereferencing."""
        audit_state = {"audited": False}
        yield from self._stream_events(reason="trace_reader.stream", audit_state=audit_state)

    def _stream_events(self, *, reason: str, audit_state: dict[str, bool]) -> Iterator[Event]:
        for record in self._iter_records(reason=reason, audit_state=audit_state):
            schema_version = _as_optional_int(record.get("schema_version")) or self.schema_version
            yield Event(
                event_type=str(record.get("event") or ""),
                schema_version=schema_version,
                payload=record,
            )

    def _locate_trace_file(self) -> tuple[Path, int]:
        v2_path = self._find_v2_trace_path()
        if v2_path is not None:
            return v2_path, TASK_TRACE_SCHEMA_VERSION

        v1_path = self._find_v1_trace_path()
        if v1_path is not None:
            return v1_path, LEGACY_V1_SCHEMA_VERSION

        searched = ", ".join(str(path) for path in self._trace_root_candidates(DEFAULT_TASK_TRACE_V2_DIR))
        searched_v1 = ", ".join(str(path) for path in self._trace_root_candidates(DEFAULT_TASK_TRACE_DIR))
        msg = "No trace found for task %s; searched v2 roots [%s] then v1 roots [%s]" % (
            self.task_id,
            searched,
            searched_v1,
        )
        raise FileNotFoundError(msg)

    def _trace_root_candidates(self, default_base: Path) -> list[Path]:
        if self.root_dir is None:
            return [default_base]
        root = self.root_dir
        candidates = [root]

        rel_base = _relative_to_cwd(default_base)
        if rel_base is not None:
            candidates.insert(0, root / rel_base)
        elif not default_base.is_absolute():
            candidates.insert(0, root / default_base)

        # Compatibility for old test fixtures and developer traces that were
        # written before trace-v2 moved under the project-owned .viola tree.
        if default_base == DEFAULT_TASK_TRACE_V2_DIR:
            candidates.insert(0, root / "data" / "traces" / "by_user")
            candidates.insert(0, root / ".viola" / "traces" / "by_user")
        elif default_base == DEFAULT_TASK_TRACE_DIR:
            candidates.insert(0, root / "logs" / "traces")

        return _dedupe_paths(candidates)

    def _find_v2_trace_path(self) -> Path | None:
        filename = "%s.trace.jsonl.zst.enc" % self.task_id
        matches: list[Path] = []
        for base in self._trace_root_candidates(DEFAULT_TASK_TRACE_V2_DIR):
            user_dir = base / self.user_id_hash
            matches.extend(user_dir.glob("*/%s" % filename))
            if base.name == self.user_id_hash:
                matches.extend(base.glob("*/%s" % filename))
            direct = base / filename
            if direct.exists():
                matches.append(direct)
        return _latest_trace_match(matches)

    def _find_v1_trace_path(self) -> Path | None:
        filename = "%s.trace.jsonl" % self.task_id
        user_scope = _safe_path_segment(self.user_id)
        matches: list[Path] = []
        for base in self._trace_root_candidates(DEFAULT_TASK_TRACE_DIR):
            user_dir = base / user_scope
            matches.extend(user_dir.glob("*/%s" % filename))
            if base.name == user_scope:
                matches.extend(base.glob("*/%s" % filename))
            direct = base / filename
            if direct.exists():
                matches.append(direct)
        return _latest_trace_match(matches)

    def _infer_v2_base_dir(self, path: Path) -> Path | None:
        parents = path.parents
        if len(parents) >= 3 and parents[1].name == self.user_id_hash:
            return parents[2]
        return self.root_dir

    def _iter_records(self, *, reason: str, audit_state: dict[str, bool]) -> Iterator[dict[str, Any]]:
        if self.schema_version == TASK_TRACE_SCHEMA_VERSION:
            yield from self._iter_v2_records(reason=reason, audit_state=audit_state)
            return
        yield from self._iter_v1_records()

    def _iter_v1_records(self) -> Iterator[dict[str, Any]]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                stripped = line.strip()
                if stripped:
                    yield _loads_json_line(stripped, self.path, line_number)

    def _iter_v2_records(self, *, reason: str, audit_state: dict[str, bool]) -> Iterator[dict[str, Any]]:
        self._audit_decrypt_once(reason=reason, audit_state=audit_state)
        encrypted = self.path.read_bytes()
        for payload in self._decrypt_trace_payloads(encrypted):
            yield from _loads_jsonl_bytes(payload, self.path)

    def _decrypt_trace_payloads(self, encrypted: bytes) -> Iterator[bytes]:
        if _looks_like_jsonl(encrypted):
            raise ValueError("Trace v2 payload is plaintext JSONL; encrypted trace required")

        saw_segment = False
        for payload in self._decrypt_trace_segments(encrypted):
            saw_segment = True
            yield payload
        if saw_segment:
            return

        decrypted = self._get_fernet().decrypt(encrypted)
        yield _maybe_decompress_zstd(decrypted)

    def _decrypt_trace_segments(self, encrypted: bytes) -> Iterator[bytes]:
        offset = 0
        successful_segments_yielded = 0
        while offset < len(encrypted):
            header_end = offset + TRACE_V2_LENGTH_PREFIX_BYTES
            if header_end > len(encrypted):
                if self._handle_corrupt_encrypted_tail(
                    "Corrupt encrypted trace tail segment: truncated length prefix",
                    offset=offset,
                    successful_segments_yielded=successful_segments_yielded,
                ):
                    return
                return
            length = struct.unpack(">I", encrypted[offset:header_end])[0]
            if length <= 0:
                if self._handle_corrupt_encrypted_tail(
                    "Corrupt encrypted trace tail segment: invalid segment length",
                    offset=offset,
                    successful_segments_yielded=successful_segments_yielded,
                ):
                    return
                return
            token_end = header_end + length
            if token_end > len(encrypted):
                if self._handle_corrupt_encrypted_tail(
                    "Corrupt encrypted trace tail segment: truncated encrypted token",
                    offset=offset,
                    successful_segments_yielded=successful_segments_yielded,
                ):
                    return
                return
            token = encrypted[header_end:token_end]
            try:
                decrypted = self._get_fernet().decrypt(token)
            except InvalidToken as exc:
                if successful_segments_yielded == 0:
                    raise
                if self._handle_corrupt_encrypted_tail(
                    "Corrupt encrypted trace tail segment: invalid encrypted token",
                    offset=offset,
                    successful_segments_yielded=successful_segments_yielded,
                    exc=exc,
                ):
                    return
                return
            yield _maybe_decompress_zstd(decrypted)
            successful_segments_yielded += 1
            offset = token_end

    def _handle_corrupt_encrypted_tail(
        self,
        message: str,
        *,
        offset: int,
        successful_segments_yielded: int,
        exc: Exception | None = None,
    ) -> bool:
        if successful_segments_yielded == 0:
            return False
        if self._recover_corrupt_tail:
            logger.warning(
                "Dropped corrupt encrypted trace tail segment",
                extra={
                    "segments_recovered": successful_segments_yielded,
                    "bytes_dropped": self.path.stat().st_size - offset,
                    "tail_offset": offset,
                },
            )
            return True
        error = ValueError(message)
        if exc is not None:
            raise error from exc
        raise error

    def _get_key_provider(self) -> KeyProvider:
        if self._key_provider is not None:
            return self._key_provider

        source = self._key_provider_source
        if isinstance(source, type):
            self._key_provider = source(self.user_id)
        elif hasattr(source, "unwrap_trace_key"):
            self._key_provider = source
        elif callable(source):
            self._key_provider = source(self.user_id)
        else:
            msg = "TraceReader requires a KeyProvider instance or factory for v2 traces"
            raise TypeError(msg)
        return self._key_provider

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            key = self._get_key_provider().unwrap_trace_key()
            if isinstance(key, str):
                key = key.encode("utf-8")
            self._fernet = Fernet(key)
        return self._fernet

    def _audit_decrypt_once(self, *, reason: str, audit_state: dict[str, bool]) -> None:
        if self.schema_version != TASK_TRACE_SCHEMA_VERSION or audit_state.get("audited", False):
            return
        actor = self._audit_actor or os.environ.get("VIOLA_OPERATOR", "system")
        self._get_key_provider().audit_decrypt(task_id=self.task_id, reason=reason, actor=actor)
        audit_state["audited"] = True

    def _get_blob_store(self) -> BlobStore:
        if self._blob_store is None:
            self._blob_store = BlobStore(
                self.user_id,
                self._get_key_provider(),
                root_dir=self._v2_blob_root(),
            )
        if getattr(self._blob_store, "_cached_fernet", None) is None:
            try:
                self._blob_store._cached_fernet = self._get_fernet()
            except AttributeError:
                pass
        return self._blob_store

    def _v2_blob_root(self) -> Path | None:
        if self._v2_base_dir is None:
            return None
        if self._v2_base_dir.name == "blobs":
            return self._v2_base_dir
        if self._v2_base_dir.name == self.user_id_hash:
            return self._v2_base_dir / "blobs"
        return self._v2_base_dir / self.user_id_hash / "blobs"

    def _step_v1(self, n: int) -> StepView:
        for event in self.stream():
            if event.event_type != "trace_step":
                continue
            payload = event.payload
            if _as_optional_int(payload.get("step")) != n:
                continue
            agent_step = payload.get("agent_step") if isinstance(payload.get("agent_step"), dict) else {}
            messages = agent_step.get("messages")
            if not isinstance(messages, list):
                llm_input = payload.get("llm_input") if isinstance(payload.get("llm_input"), dict) else {}
                messages = llm_input.get("messages") if isinstance(llm_input.get("messages"), list) else []
            return StepView(
                step=n,
                step_kind=str(payload.get("step_kind") or ""),
                ts=str(payload.get("ts") or ""),
                cumulative_messages=copy.deepcopy(messages),
                llm_input=_optional_dict(payload.get("llm_input")),
                llm_output=_optional_dict(payload.get("llm_output")),
                tool_execution=_optional_dict(payload.get("tool_execution")),
                control=_optional_dict(payload.get("control")),
            )
        raise IndexError("Trace step %s not found in %s" % (n, self.path))

    def _step_v2(self, n: int) -> StepView:
        messages: list[dict[str, Any]] = []
        request: dict[str, Any] = {}
        runtime: dict[str, Any] = {}
        continuity: dict[str, Any] = {}
        tool_executions_by_step: dict[int, dict[str, Any]] = {}
        pending_tool_executions: list[dict[str, Any]] = []
        audit_state = {"audited": False}

        for event in self._stream_events(reason="trace_reader.step", audit_state=audit_state):
            payload = event.payload
            if event.event_type in {"trace_start", "trace_resume"}:
                request = _optional_dict(payload.get("request")) or {}
                runtime = _optional_dict(payload.get("runtime")) or runtime
                continuity = _optional_dict(payload.get("continuity")) or continuity
                checkpoint = _optional_dict(payload.get("checkpoint")) or {}
                messages = _initial_messages_from_request(request, checkpoint, messages)
                continue

            if event.event_type == "trace_tool_execution":
                tool_execution_payload = _tool_execution_event_payload(payload)
                if tool_execution_payload is not None:
                    event_step = _as_optional_int(payload.get("step"))
                    if event_step is None:
                        pending_tool_executions.append(tool_execution_payload)
                    else:
                        tool_executions_by_step[event_step] = tool_execution_payload
                continue

            if event.event_type != "trace_step":
                continue

            current_step = _as_optional_int(payload.get("step"))
            event_tool_execution = tool_executions_by_step.pop(current_step, None) if current_step is not None else None
            if event_tool_execution is None and pending_tool_executions:
                event_tool_execution = pending_tool_executions.pop(0)

            delta_messages = payload.get("delta_messages")
            if isinstance(delta_messages, list):
                messages.extend(copy.deepcopy(delta_messages))
            else:
                agent_step = _optional_dict(payload.get("agent_step")) or {}
                full_messages = agent_step.get("messages")
                if isinstance(full_messages, list):
                    messages = copy.deepcopy(full_messages)

            if current_step != n:
                continue

            cumulative_messages = self._resolve_refs(
                copy.deepcopy(messages),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            resolved_request = self._resolve_refs(
                copy.deepcopy(request),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            resolved_runtime = self._resolve_refs(
                copy.deepcopy(runtime),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            resolved_continuity_before = self._resolve_refs(
                copy.deepcopy(payload.get("continuity_before")),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            resolved_llm_input = self._resolve_refs(
                copy.deepcopy(payload.get("llm_input_ref")),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            if isinstance(resolved_llm_input, dict):
                llm_input = resolved_llm_input
            else:
                llm_input = _reconstructed_llm_input(
                    request=resolved_request,
                    runtime=_optional_dict(resolved_runtime) or {},
                    continuity_before=_optional_dict(resolved_continuity_before),
                    messages=cumulative_messages,
                )
            llm_output = self._resolve_refs(
                copy.deepcopy(payload.get("llm_output")),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            llm_output_dict = _optional_dict(llm_output)
            attempt_id = _as_optional_str(payload.get("attempt_id"))
            reasoning = _reasoning_from_llm_output(llm_output_dict)
            if reasoning is None and attempt_id:
                reasoning = _reasoning_from_stream_events(
                    self._stream_events(reason="trace_reader.step", audit_state=audit_state),
                    attempt_id=attempt_id,
                )
            raw_tool_execution = _merge_tool_execution(
                _optional_dict(payload.get("tool_execution")),
                event_tool_execution,
            )
            tool_execution = self._resolve_refs(
                copy.deepcopy(raw_tool_execution),
                reason="trace_reader.step",
                audit_state=audit_state,
            )
            control = self._resolve_refs(
                copy.deepcopy(payload.get("control")),
                reason="trace_reader.step",
                audit_state=audit_state,
            )

            return StepView(
                step=n,
                step_kind=str(payload.get("step_kind") or ""),
                ts=str(payload.get("ts") or ""),
                cumulative_messages=cumulative_messages,
                attempt_id=attempt_id,
                llm_input=llm_input,
                llm_output=llm_output_dict,
                reasoning=reasoning,
                tool_execution=_optional_dict(tool_execution),
                control=_optional_dict(control),
            )

        raise IndexError("Trace step %s not found in %s" % (n, self.path))

    def _resolve_refs(self, value: Any, *, reason: str, audit_state: dict[str, bool]) -> Any:
        if self.schema_version != TASK_TRACE_SCHEMA_VERSION:
            return value
        if _is_blob_ref(value):
            return self._read_blob_ref(value, reason=reason, audit_state=audit_state)
        if _is_truncation_marker(value):
            return self._read_blob_ref(value["blob_ref"], reason=reason, audit_state=audit_state)
        if isinstance(value, dict):
            return {
                key: self._resolve_refs(inner, reason=reason, audit_state=audit_state) for key, inner in value.items()
            }
        if isinstance(value, list):
            return [self._resolve_refs(item, reason=reason, audit_state=audit_state) for item in value]
        return value

    def _read_blob_ref(self, ref_payload: dict[str, Any], *, reason: str, audit_state: dict[str, bool]) -> Any:
        if not audit_state.get("audited", False):
            actor = self._audit_actor or os.environ.get("VIOLA_OPERATOR", "system")
            self._get_key_provider().audit_decrypt(task_id=self.task_id, reason=reason, actor=actor)
            audit_state["audited"] = True

        content_type = str(ref_payload.get("content_type") or "application/json")
        blob_ref = BlobRef(
            sha256=_blob_sha(ref_payload),
            size=int(ref_payload.get("size") or 0),
            content_type=content_type,
            first_chars=_as_optional_str(ref_payload.get("first_chars")),
        )
        cache_key = (blob_ref.sha256, content_type)
        if cache_key in self._blob_value_cache:
            return copy.deepcopy(self._blob_value_cache[cache_key])
        content = self._get_blob_store().get(blob_ref)
        decoded = _decode_blob_bytes(content, content_type)
        if isinstance(decoded, (dict, list)):
            decoded = self._resolve_refs(decoded, reason=reason, audit_state=audit_state)
        self._blob_value_cache[cache_key] = copy.deepcopy(decoded)
        return decoded

    def _field_value(self, field_path: str) -> Any:
        if field_path.startswith("step:"):
            step_selector, _, remainder = field_path.partition(".")
            step_value = int(step_selector.removeprefix("step:"))
            value: Any = self.step(step_value)
            return _lookup_path(value, remainder) if remainder else value

        event_name, _, remainder = field_path.partition(".")
        audit_state = {"audited": False}
        for event in self._stream_events(reason="trace_reader.diff", audit_state=audit_state):
            if event.event_type != event_name:
                continue
            value = _lookup_path(event.payload, remainder) if remainder else event.payload
            return self._resolve_refs(copy.deepcopy(value), reason="trace_reader.diff", audit_state=audit_state)
        return None

    def attempt_chunks(self, attempt_id: str) -> Iterator[Event]:
        """Yield stream chunks for one LLM attempt ordered by raw chunk index."""
        chunks = [
            event
            for event in self.stream()
            if event.event_type == "trace_llm_stream_chunk" and event.payload.get("attempt_id") == attempt_id
        ]
        chunks.sort(key=lambda event: _as_optional_int(event.payload.get("chunk_index")) or 0)
        yield from chunks


def _loads_json_line(line: str, path: Path, line_number: int) -> dict[str, Any]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        msg = "Invalid JSON in %s at line %s" % (path, line_number)
        raise ValueError(msg) from exc
    if not isinstance(record, dict):
        msg = "Trace row in %s at line %s is not an object" % (path, line_number)
        raise ValueError(msg)
    return record


def _loads_jsonl_bytes(payload: bytes, path: Path) -> Iterator[dict[str, Any]]:
    text = payload.decode("utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped:
            yield _loads_json_line(stripped, path, line_number)


def _maybe_decompress_zstd(payload: bytes) -> bytes:
    if _looks_like_jsonl(payload):
        return payload
    try:
        import zstandard as zstd
    except ModuleNotFoundError as exc:
        import gzip

        try:
            return gzip.decompress(payload)
        except OSError:
            pass
        msg = "Trace v2 payload is zstd-compressed, but the zstandard package is not installed"
        raise RuntimeError(msg) from exc
    try:
        return zstd.ZstdDecompressor().decompress(payload)
    except zstd.ZstdError as exc:
        from io import BytesIO

        try:
            with zstd.ZstdDecompressor().stream_reader(BytesIO(payload)) as reader:
                return reader.read()
        except zstd.ZstdError:
            import gzip

            try:
                return gzip.decompress(payload)
            except OSError:
                msg = "Failed to decompress trace v2 zstd payload"
                raise ValueError(msg) from exc


def _looks_like_jsonl(payload: bytes) -> bool:
    return payload.lstrip().startswith(b"{")


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _relative_to_cwd(path: Path) -> Path | None:
    if not path.is_absolute():
        return path
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return None


def _latest_trace_match(paths: Iterable[Path]) -> Path | None:
    existing = [path for path in paths if path.exists()]
    if not existing:
        return None
    return max(existing, key=lambda path: (path.parent.name, path.stat().st_mtime, str(path)))


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _reasoning_from_llm_output(llm_output: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(llm_output, dict):
        return None
    reasoning = llm_output.get("reasoning")
    if not isinstance(reasoning, dict):
        return None
    text = reasoning.get("text")
    blocks = reasoning.get("blocks")
    if not (isinstance(text, str) and text.strip()) and not blocks:
        return None
    payload = copy.deepcopy(reasoning)
    payload.setdefault("kind", "provider_reasoning")
    payload.setdefault("format", "blocks" if blocks else "text")
    payload.setdefault("source", "llm_output.reasoning")
    return payload


def _reasoning_delta_text(delta: Any) -> str:
    if isinstance(delta, str):
        return delta.strip()
    if isinstance(delta, dict):
        parts: list[str] = []
        for key in ("text", "content", "reasoning", "delta", "summary"):
            if key in delta:
                value = _reasoning_delta_text(delta[key])
                if value:
                    parts.append(value)
        return "\n".join(parts).strip()
    if isinstance(delta, list):
        parts = [_reasoning_delta_text(item) for item in delta]
        return "\n".join(part for part in parts if part).strip()
    text = getattr(delta, "text", None)
    if isinstance(text, str):
        return text.strip()
    return ""


def _reasoning_from_stream_events(events: Iterable[Event], *, attempt_id: str) -> dict[str, Any] | None:
    parts: list[str] = []
    for event in events:
        if event.event_type != "trace_llm_stream_chunk":
            continue
        payload = event.payload
        if str(payload.get("attempt_id") or "") != attempt_id:
            continue
        chunk_kind = str(payload.get("chunk_kind") or "")
        if "reasoning" not in chunk_kind and "thinking" not in chunk_kind:
            continue
        text = _reasoning_delta_text(payload.get("delta"))
        if text:
            parts.append(text)
    if not parts:
        return None
    return {
        "kind": "provider_reasoning",
        "format": "text",
        "source": "trace_llm_stream_chunk",
        "text": "\n".join(parts).strip(),
    }


def _tool_execution_event_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    nested = _optional_dict(payload.get("tool_execution"))
    if nested is not None:
        tool_execution = copy.deepcopy(nested)
        for key in ("tool_name", "ok", "duration_ms", "result", "error"):
            if key in payload and key not in tool_execution:
                tool_execution[key] = payload[key]
        return tool_execution

    tool_execution = {
        key: copy.deepcopy(payload[key])
        for key in ("tool_name", "ok", "duration_ms", "result", "error")
        if key in payload
    }
    return tool_execution or None


def _merge_tool_execution(
    step_tool_execution: dict[str, Any] | None,
    event_tool_execution: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if step_tool_execution is None:
        return event_tool_execution
    if event_tool_execution is None:
        return step_tool_execution

    merged = copy.deepcopy(step_tool_execution)
    for key, value in event_tool_execution.items():
        if key not in merged or merged[key] in (None, "", {}, []):
            merged[key] = copy.deepcopy(value)
    return merged


def _step_summary(payload: dict[str, Any]) -> StepSummary:
    agent_step = _optional_dict(payload.get("agent_step")) or {}
    tool_execution = _optional_dict(payload.get("tool_execution")) or {}
    llm_output = _optional_dict(payload.get("llm_output")) or {}
    tool_call = _optional_dict(llm_output.get("tool_call")) or {}
    ok = payload.get("ok", tool_execution.get("ok"))

    return StepSummary(
        step=_as_optional_int(payload.get("step")) or 0,
        step_kind=str(payload.get("step_kind") or ""),
        tool_name=_first_str(
            payload.get("tool_name"),
            agent_step.get("tool_name"),
            tool_execution.get("tool_name"),
            tool_call.get("name"),
        ),
        ok=ok if isinstance(ok, bool) else None,
        page_url=_first_str(payload.get("page_url"), agent_step.get("page_url"), tool_execution.get("page_url")),
    )


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value:
            return str(value)
    return None


def _iter_blob_ref_paths(value: Any) -> Iterator[str]:
    if _is_blob_ref(value):
        yield str(value["$ref"])
        return
    if isinstance(value, dict):
        for inner in value.values():
            yield from _iter_blob_ref_paths(inner)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_blob_ref_paths(item)


def _is_blob_ref(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    ref = value.get("$ref")
    if not isinstance(ref, str) or not ref:
        return False
    if ref.startswith("#"):
        return False
    ref_path = ref.replace("\\", "/")
    return (
        bool(value.get("sha256"))
        or ref_path.startswith("blobs/")
        or ref_path.endswith((".blob", ".blob.enc", ".blob.zst.enc", ".zst.enc"))
    )


def _is_truncation_marker(value: Any) -> bool:
    return isinstance(value, dict) and value.get("truncated") is True and _is_blob_ref(value.get("blob_ref"))


def _blob_sha(ref_payload: dict[str, Any]) -> str:
    sha = ref_payload.get("sha256")
    if isinstance(sha, str) and sha:
        return sha

    ref_path = str(ref_payload.get("$ref") or "").replace("\\", "/")
    parts = [part for part in ref_path.split("/") if part]
    if not parts:
        return ""
    leaf = parts[-1]
    for suffix in (".blob.zst.enc", ".blob.enc", ".zst.enc", ".blob"):
        if leaf.endswith(suffix):
            leaf = leaf[: -len(suffix)]
            break
    if len(parts) >= 2 and len(parts[-2]) == 2 and len(leaf) == 62:
        return "%s%s" % (parts[-2], leaf)
    if len(parts) >= 2 and len(parts[-2]) == 2 and not leaf.startswith(parts[-2]):
        return "%s%s" % (parts[-2], leaf)
    return leaf


def _decode_blob_bytes(content: bytes, content_type: str) -> Any:
    text = content.decode("utf-8", errors="replace")
    if content_type == "application/json" or text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _initial_messages_from_request(
    request: dict[str, Any],
    checkpoint: dict[str, Any],
    current_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    checkpoint_messages = checkpoint.get("messages")
    if isinstance(checkpoint_messages, list):
        return copy.deepcopy(checkpoint_messages)

    request_messages = request.get("initial_model_messages")
    if isinstance(request_messages, list):
        return copy.deepcopy(request_messages)

    if request.get("user_text") is not None and not current_messages:
        return [{"role": "user", "content": request["user_text"]}]
    return current_messages


def _reconstructed_llm_input(
    *,
    request: dict[str, Any],
    runtime: dict[str, Any],
    continuity_before: dict[str, Any] | None,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    llm_input: dict[str, Any] = {"messages": messages}
    for key in ("system_prompt_text", "tools", "user_text"):
        if key in request:
            llm_input[key] = request[key]
    if runtime:
        llm_input["runtime"] = runtime
    if continuity_before:
        llm_input["continuity_before"] = continuity_before
    return llm_input


def _lookup_path(value: Any, path: str) -> Any:
    if not path:
        return value
    current = value
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if index < len(current) else None
        else:
            current = getattr(current, segment, None)
        if current is None:
            return None
    return current


def _stringify_for_diff(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)
