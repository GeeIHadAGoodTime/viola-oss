"""Env-gated wall-clock span recorder for latency decomposition.

Purpose: attribute every millisecond of a live text turn to a named owner
(_diag/2026-07-08/latency_decomposition/PLAN.md). Enabled ONLY when the
environment variable ``VIOLA_LATENCY_SPANS_DIR`` names a writable directory;
otherwise every call is a no-op costing one attribute lookup. Never enabled in
production images (the variable is unset there).

Rows are JSONL, one file per process (``spans_<pid>.jsonl``):

- ``{"kind": "anchor", "pid", "epoch", "perf"}`` — maps ``perf_counter`` to
  epoch time once per process so files from different processes align.
- ``{"kind": "span", "turn", "name", "t0", "t1", "meta"}`` — a closed interval
  on the process ``perf_counter`` clock.
- ``{"kind": "event", "turn", "name", "t", "meta"}`` — a point sample (used for
  socket-level httpx/httpcore trace events and loop-iteration markers).

Design constraints: no new dependency, fail-open (a recorder error must never
break a turn), single monotonic clock per process, writes are small
appends under a lock (turn rate here is ~0.1 Hz; contention is nil).
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import json
import threading
import time
from pathlib import Path
from typing import Any

from config import env as config_env
from core.logging_config import get_logger

logger = get_logger(__name__)

_SPANS_DIR: str = (config_env.get("VIOLA_LATENCY_SPANS_DIR") or "").strip()

#: True when span recording is active for this process.
enabled: bool = bool(_SPANS_DIR)

_turn_var: contextvars.ContextVar[str] = contextvars.ContextVar("latency_turn", default="")
_turn_counter = itertools.count(1)
_http_req_counter = itertools.count(1)
_lock = threading.Lock()
_anchor_written = False


def _spans_file() -> Path:
    import os

    return Path(_SPANS_DIR) / ("spans_%d.jsonl" % os.getpid())


def _write_row(row: dict[str, Any]) -> None:
    global _anchor_written
    if not enabled:
        return
    try:
        with _lock:
            path = _spans_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                if not _anchor_written:
                    _anchor_written = True
                    import os

                    fh.write(
                        json.dumps(
                            {
                                "kind": "anchor",
                                "pid": os.getpid(),
                                "epoch": time.time(),
                                "perf": time.perf_counter(),
                            }
                        )
                        + "\n"
                    )
                fh.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001, RUF100 - fail-open: recording must never break a turn
        logger.debug("latency span write failed", exc_info=True)


def new_turn(label: str = "") -> str:
    """Start a new turn scope; returns the turn id bound to this context."""
    turn_id = "turn-%03d-%d" % (next(_turn_counter), int(time.time() * 1000))
    _turn_var.set(turn_id)
    if label:
        event("TURN_LABEL", label=label)
    return turn_id


def current_turn() -> str:
    return _turn_var.get()


def event(name: str, **meta: Any) -> None:
    if not enabled:
        return
    _write_row(
        {
            "kind": "event",
            "turn": _turn_var.get(),
            "name": name,
            "t": time.perf_counter(),
            "meta": meta or {},
        }
    )


@contextlib.contextmanager
def span(name: str, **meta: Any):
    """Record a wall-clock interval on the process perf_counter clock.

    Works around both sync and async code (no awaits inside the recorder).
    """
    if not enabled:
        yield
        return
    t0 = time.perf_counter()
    error: str | None = None
    try:
        yield
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        row_meta = dict(meta) if meta else {}
        if error:
            row_meta["error"] = error
        _write_row(
            {
                "kind": "span",
                "turn": _turn_var.get(),
                "name": name,
                "t0": t0,
                "t1": time.perf_counter(),
                "meta": row_meta,
            }
        )


# ---------------------------------------------------------------------------
# Event-loop lag heartbeat: catches time hiding as "await" while another
# coroutine starves the loop. Written only when lag exceeds the threshold.
# ---------------------------------------------------------------------------

_heartbeat_started = False
_HEARTBEAT_INTERVAL_S = 0.05
_HEARTBEAT_REPORT_LAG_S = 0.025


def start_event_loop_heartbeat() -> None:
    """Idempotently start the loop-lag sampler on the running event loop."""
    global _heartbeat_started
    if not enabled or _heartbeat_started:
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _heartbeat_started = True

    async def _sample() -> None:
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            lag = time.perf_counter() - t0 - _HEARTBEAT_INTERVAL_S
            if lag > _HEARTBEAT_REPORT_LAG_S:
                _write_row(
                    {
                        "kind": "event",
                        "turn": _turn_var.get(),
                        "name": "LOOP_LAG",
                        "t": time.perf_counter(),
                        "meta": {"lag_ms": round(lag * 1000.0, 2)},
                    }
                )

    loop.create_task(_sample())


# ---------------------------------------------------------------------------
# Socket-level instrumented httpx client for the OpenAI SDK.
#
# httpcore honours a per-request ``trace`` extension: an async callable that
# receives fine-grained events (connection.connect_tcp.*, connection.start_tls.*,
# http11.send_request_headers.*, http11.send_request_body.*,
# http11.receive_response_headers.*, http11.receive_response_body.*,
# http11.response_closed.*). Recording each with a perf_counter timestamp
# yields the isolated request-bytes-written -> first-response-byte span the
# decomposition needs, measured at the socket layer, not inferred from logs.
# ---------------------------------------------------------------------------


def build_instrumented_openai_http_client() -> Any | None:
    """Return an httpx.AsyncClient whose transport records socket timings.

    Mirrors the OpenAI SDK's own client defaults (timeout/limits/redirects) so
    behaviour is unchanged apart from observation. Returns None when disabled
    or when httpx internals are unavailable.
    """
    if not enabled:
        return None
    try:
        import httpx

        try:
            from openai import DEFAULT_CONNECTION_LIMITS, DEFAULT_TIMEOUT

            limits = DEFAULT_CONNECTION_LIMITS
            timeout = DEFAULT_TIMEOUT
        except Exception:  # noqa: BLE001, RUF100 - fall back to safe client defaults
            limits = httpx.Limits(max_connections=100, max_keepalive_connections=20)
            timeout = httpx.Timeout(600.0, connect=5.0)

        class _TracingTransport(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request: Any) -> Any:
                rid = "http-%04d" % next(_http_req_counter)
                url_path = str(getattr(request.url, "path", ""))
                turn_ctx = _turn_var.get()

                async def _trace(event_name: str, info: dict[str, Any]) -> None:
                    _write_row(
                        {
                            "kind": "event",
                            "turn": turn_ctx,
                            "name": "HTTPX.%s" % event_name,
                            "t": time.perf_counter(),
                            "meta": {"rid": rid, "path": url_path},
                        }
                    )

                request.extensions = dict(request.extensions)
                request.extensions["trace"] = _trace
                event(
                    "HTTPX.request_begin",
                    rid=rid,
                    path=url_path,
                    host=str(getattr(request.url, "host", "")),
                )
                response = await super().handle_async_request(request)
                headers = getattr(response, "headers", {}) or {}
                event(
                    "HTTPX.response_headers_returned",
                    rid=rid,
                    path=url_path,
                    status=getattr(response, "status_code", 0),
                    openai_processing_ms=headers.get("openai-processing-ms"),
                    x_request_id=headers.get("x-request-id"),
                    content_encoding=headers.get("content-encoding"),
                )
                return response

        return httpx.AsyncClient(
            transport=_TracingTransport(limits=limits),
            timeout=timeout,
            follow_redirects=True,
            limits=limits,
        )
    except Exception:  # noqa: BLE001, RUF100 - diagnostics-only client; None degrades cleanly
        logger.debug("instrumented httpx client construction failed", exc_info=True)
        return None
