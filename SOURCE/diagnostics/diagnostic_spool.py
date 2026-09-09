"""Local outbox for anonymized crash diagnostics that cannot send yet.

A crash may happen before the disclosure card has been shown (or while the
master flag is still off). The founder's model is: queue those anonymized crash
payloads locally and flush them AFTER disclosure -- never drop them, never send
them early. This is that queue.

Only the anonymized diagnostic MINIMUM is ever written here (the same
allowlist-only dict that ``diagnostics.diagnostic_minimum.build_diagnostic_minimum``
produces). The identifiable extra is never spooled: it is assembled at send time
from live consent, so a queued crash can never resurrect an opt-in the user has
since revoked.

The spool is a bounded JSON-lines file under the logs dir. It is desktop-only
churn (Tier 3), never synced. Every operation is best-effort and never raises --
a spool failure must not turn a crash handler into a second crash.

Delivery is CLAIM then RELEASE, never a destructive read (#4227)
----------------------------------------------------------------

The previous ``drain()`` deleted the spool file at read time, before the caller
had sent anything, and the flush caller discarded the relay's failure return. A
desktop that was offline, or pointed at an ingest endpoint answering non-2xx,
therefore lost every queued crash report on the first flush -- exactly the
silent-loss shape #4227 was filed for. The outbox is now two-phase:

* :func:`claim` moves the spool aside to a sidecar THIS caller owns and returns
  a claim id plus the payloads. Nothing is destroyed.
* :func:`release` takes that claim id back with the indexes that did NOT send.
  Those go to the front of the spool; only then is the sidecar removed.

If the process dies mid-flush its sidecar simply survives on disk, and a later
:func:`claim` adopts it once it is provably orphaned. That is the opposite of
the old trade (lose the payload rather than risk a resend), and it is the right
one: a duplicate crash report groups harmlessly into the same GlitchTip issue,
while a lost one is gone forever.

Every sidecar is OWNED, because concurrent flushes are real
-----------------------------------------------------------

Several desktop surfaces flush the same logs dir concurrently: the Qt app, the
local daemon and the multiroom spoke each install crash hooks and each flush at
startup (``core.sentry_integration``), ``threading.excepthook`` is process-wide,
the consent route flushes from the API thread, and while armed EVERY crash
flushes before it sends. A flush of a full queue can hold payloads for minutes,
so two flushes overlapping is ordinary, not exotic.

An earlier revision of this module used ONE fixed sidecar path and had each
claim adopt whatever sidecar it found. That destroyed payloads neither flush had
delivered: the second claimer adopted and deleted the first's live sidecar, so
the first's ``release`` found nothing to put back. The sidecar name therefore
carries its owner (pid + token), :func:`release` only ever touches its own file,
and recovery adopts a sidecar only when its owning process is gone or it has sat
untouched past ``_INFLIGHT_STALE_SECONDS``. Overlap now costs at worst a
duplicate, which is what the docstring above promises.

Redelivery is bounded. Every entry carries an attempt counter, incremented at
claim time and persisted BEFORE the payloads are handed out, so a crash loop
against a permanently-unreachable endpoint retries at most
``_MAX_DELIVERY_ATTEMPTS`` times per payload instead of forever.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.logging_config import get_logger

log = get_logger(__name__)

_SPOOL_FILENAME = "diagnostic_spool.jsonl"
# Payloads handed to a caller that has not yet reported success/failure. One
# file PER CLAIM, stamped with the owning pid, so a concurrent flush can never
# adopt (and delete) a sidecar another flush is still working through.
_INFLIGHT_PREFIX = "diagnostic_spool.inflight."
_INFLIGHT_SUFFIX = ".jsonl"
# A sidecar is only adopted once its owner is provably gone: the pid is dead, or
# it has sat untouched this long. Comfortably beyond the worst-case flush
# (_MAX_SPOOLED payloads x the relay's 8s timeout).
_INFLIGHT_STALE_SECONDS = 900
# Keep the queue small: a crash-loop must not fill the disk. Oldest entries are
# dropped first when the cap is exceeded.
_MAX_SPOOLED = 50
_MAX_LINE_BYTES = 16_384
# A payload that has failed to send this many times is dropped, so an install
# that can never reach the ingest endpoint stops carrying it forever.
_MAX_DELIVERY_ATTEMPTS = 8

# Marks a spool line as the delivery envelope rather than a bare payload. A real
# diagnostic minimum can never carry this key: the builder emits only
# ``diagnostics.diagnostic_minimum._ALLOWED_TOP_LEVEL_KEYS``, so the check below
# is unambiguous, and older bare-payload lines (written before this envelope
# existed) still load correctly across an upgrade.
_ENVELOPE_MARKER = "_spool_envelope"


def _logs_dir() -> Path:
    from core.platform import get_logs_dir

    return get_logs_dir()


def _spool_path() -> Path:
    return _logs_dir() / _SPOOL_FILENAME


def _new_inflight_path() -> Path:
    name = "%s%d.%s%s" % (_INFLIGHT_PREFIX, os.getpid(), uuid.uuid4().hex[:12], _INFLIGHT_SUFFIX)
    return _logs_dir() / name


def _inflight_paths() -> list[Path]:
    try:
        return sorted(_logs_dir().glob(_INFLIGHT_PREFIX + "*" + _INFLIGHT_SUFFIX))
    except OSError:
        return []


def _owner_pid(path: Path) -> int | None:
    """The pid stamped into a sidecar name, or None if it is not parseable."""
    stem = path.name[len(_INFLIGHT_PREFIX) : -len(_INFLIGHT_SUFFIX)] if path.name.endswith(_INFLIGHT_SUFFIX) else ""
    head = stem.split(".", 1)[0]
    try:
        return int(head)
    except (TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness. Errs toward ALIVE, which only delays recovery."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # No signal-0 equivalent worth the risk here; let the age rule decide.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, ValueError, OverflowError):
        return True
    return True


def enqueue(payload: dict[str, Any]) -> bool:
    """Append one anonymized minimum payload to the outbox. Never raises."""
    return _append_envelopes([{"attempts": 0, "payload": payload}])


def claim() -> tuple[str, list[dict[str, Any]]]:
    """Take every queued payload for a delivery attempt. Never raises.

    Returns ``(claim_id, payloads)``. The caller MUST close the claim out with
    :func:`release`, passing the same ``claim_id`` and the indexes (into the
    returned list) that did not send. Anything not released stays in this
    claim's own sidecar and is recovered once this process is gone, so a crash
    between claim and release costs a possible duplicate, never a loss.
    """
    try:
        _recover_orphaned_inflight()
        path = _spool_path()
        if not path.exists():
            return "", []
        inflight = _new_inflight_path()
        try:
            path.replace(inflight)
        except OSError:
            log.debug("diagnostic-spool: could not move spool to inflight", exc_info=True)
            return "", []

        keep: list[dict[str, Any]] = []
        for envelope in _read_envelopes(inflight):
            attempts = envelope["attempts"] + 1
            if attempts > _MAX_DELIVERY_ATTEMPTS:
                log.debug("diagnostic-spool: dropping payload after %d delivery attempts", attempts - 1)
                continue
            keep.append({"attempts": attempts, "payload": envelope["payload"]})

        # Persist the incremented counters BEFORE handing the payloads out, so a
        # crash mid-send cannot reset a payload's attempt count and retry forever.
        # Return only what actually persisted: `release` addresses these entries
        # by index into the sidecar, so the two must not drift apart.
        persisted = _write_envelopes(inflight, keep)
        return inflight.name, [dict(envelope["payload"]) for envelope in persisted]
    except OSError:
        log.debug("diagnostic-spool: claim failed", exc_info=True)
        return "", []


def release(claim_id: str, unsent_indexes: Iterable[int]) -> int:
    """Close out a :func:`claim`: re-queue the entries that did not send.

    ``unsent_indexes`` indexes the list :func:`claim` returned. Those entries go
    back to the FRONT of the spool (they are older than anything enqueued since)
    and only then is this claim's sidecar removed. Returns how many were
    re-queued. Never raises.
    """
    if not claim_id:
        return 0
    try:
        inflight = _logs_dir() / claim_id
        if not inflight.exists():
            return 0
        envelopes = _read_envelopes(inflight)
        wanted = {int(index) for index in unsent_indexes}
        requeued = [envelope for index, envelope in enumerate(envelopes) if index in wanted]
        if not _retire(inflight, requeued):
            return 0
        return len(requeued)
    except (OSError, TypeError, ValueError):
        log.debug("diagnostic-spool: release failed", exc_info=True)
        return 0


def pending_count() -> int:
    """Number of payloads awaiting send (for the Settings panel).

    Read-only on purpose: this runs off a GET handler, and a read path must not
    be able to destroy the queue it is reporting on.
    """
    try:
        total = len(_read_envelopes(_spool_path()))
        for inflight in _inflight_paths():
            total += len(_read_envelopes(inflight))
        return total
    except OSError:
        return 0


def _retire(inflight: Path, requeued: list[dict[str, Any]]) -> bool:
    """Put ``requeued`` back on the spool, then drop ``inflight``. All or nothing.

    If the re-queue write fails (full or read-only disk), the sidecar is LEFT
    ALONE so a later claim can recover it -- unlinking anyway would destroy the
    very payloads the two-phase design exists to save, and an unlink succeeds
    even when there is no space left to write.
    """
    if requeued and not _prepend_envelopes(requeued):
        log.debug("diagnostic-spool: re-queue write failed; leaving payloads inflight for recovery")
        return False
    try:
        inflight.unlink(missing_ok=True)
    except OSError:
        log.debug("diagnostic-spool: could not clear inflight sidecar", exc_info=True)
    return True


def _recover_orphaned_inflight() -> None:
    """Fold sidecars whose owner is gone back into the spool.

    A sidecar is adopted only when its owning process is dead or it has sat
    untouched past ``_INFLIGHT_STALE_SECONDS``. A live flush's sidecar is never
    touched, which is what stops two overlapping flushes from destroying
    payloads neither of them delivered.
    """
    now = time.time()
    for inflight in _inflight_paths():
        try:
            if not _is_orphaned(inflight, now):
                continue
            envelopes = _read_envelopes(inflight)
            if envelopes:
                log.debug("diagnostic-spool: recovering %d payload(s) from an orphaned sidecar", len(envelopes))
            _retire(inflight, envelopes)
        except OSError:
            log.debug("diagnostic-spool: inflight recovery failed", exc_info=True)


def _is_orphaned(inflight: Path, now: float) -> bool:
    pid = _owner_pid(inflight)
    if pid is None:
        return True  # unparseable name: nobody can claim ownership of it
    if pid == os.getpid():
        # Ours. Only adopt it if it is far too old to be a live claim (a
        # previous run of this process that happened to get the same pid).
        return _age(inflight, now) > _INFLIGHT_STALE_SECONDS
    if not _pid_alive(pid):
        return True
    return _age(inflight, now) > _INFLIGHT_STALE_SECONDS


def _age(path: Path, now: float) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 0.0


def _append_envelopes(envelopes: list[dict[str, Any]]) -> bool:
    return _merge_envelopes(envelopes, front=False)


def _prepend_envelopes(envelopes: list[dict[str, Any]]) -> bool:
    return _merge_envelopes(envelopes, front=True)


def _merge_envelopes(envelopes: list[dict[str, Any]], *, front: bool) -> bool:
    encoded = [line for line in (_encode(envelope) for envelope in envelopes) if line is not None]
    if not encoded:
        return False
    try:
        path = _spool_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_lines(path)
        merged = encoded + existing if front else existing + encoded
        # Trim to the newest _MAX_SPOOLED entries (drop oldest first).
        if len(merged) > _MAX_SPOOLED:
            merged = merged[-_MAX_SPOOLED:]
        _atomic_write(path, merged)
        return True
    except OSError:
        log.debug("diagnostic-spool: spool write failed", exc_info=True)
        return False


def _write_envelopes(path: Path, envelopes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Write ``envelopes`` to ``path``, returning the ones that survived encoding."""
    kept: list[dict[str, Any]] = []
    encoded: list[str] = []
    for envelope in envelopes:
        line = _encode(envelope)
        if line is not None:
            kept.append(envelope)
            encoded.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, encoded)
    return kept


def _encode(envelope: dict[str, Any]) -> str | None:
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or not payload:
        return None
    record = {_ENVELOPE_MARKER: 1, "attempts": int(envelope.get("attempts", 0)), "payload": payload}
    try:
        line = json.dumps(record, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        log.debug("diagnostic-spool: payload not serializable; dropping")
        return None
    if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
        log.debug("diagnostic-spool: payload exceeds line cap; dropping")
        return None
    return line


def _read_envelopes(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in _read_lines(path):
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(obj, dict) or not obj:
            continue
        if obj.get(_ENVELOPE_MARKER):
            payload = obj.get("payload")
            if isinstance(payload, dict) and payload:
                out.append({"attempts": _as_attempts(obj.get("attempts")), "payload": payload})
            continue
        # Pre-envelope line written by an older build: a bare payload dict.
        out.append({"attempts": 0, "payload": obj})
    return out


def _as_attempts(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8", errors="replace") as handle:
        return [line.strip() for line in handle if line.strip()]


def _atomic_write(path: Path, lines: list[str]) -> None:
    import tempfile

    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="diagspool_", suffix=".tmp")
    try:
        with open(tmp_fd, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")
        Path(tmp_name).replace(path)
    except OSError:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


__all__ = ["claim", "enqueue", "pending_count", "release"]
