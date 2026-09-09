"""Remote GPU voice endpoint client for phone STT/TTS (Runpod serverless).

Feature-flagged remote execution of the exact production phone voice models
(faster-whisper small.en STT + Kokoro ONNX TTS) on a serverless GPU endpoint.
Default OFF: with the flag unset, every call is a no-op and the existing local
CPU path in telephony/call_manager.py runs unchanged.

Env (also declared on AppConfig in config/settings.py):
- VIOLA_PHONE_VOICE_REMOTE_ENABLED   -- "1"/"true"/"yes"/"on" to enable (default off)
- VIOLA_PHONE_VOICE_REMOTE_URL       -- load-balancing endpoint base, e.g.
                                        https://<endpoint_id>.api.runpod.ai
                                        (the client posts to <base>/op)
- VIOLA_PHONE_VOICE_REMOTE_API_KEY   -- bearer key for the endpoint
- VIOLA_PHONE_VOICE_REMOTE_TIMEOUT_MS      -- per-request wall clock (default 2500)
- VIOLA_PHONE_VOICE_REMOTE_COOLDOWN_SECS   -- breaker backoff window after the
                                              endpoint is judged genuinely DOWN
                                              (default 60)

Fallback semantics (fail-open to LOCAL, never to silence): any remote failure
-- disabled flag, missing url/key, HTTP error, timeout, endpoint-side error,
bad payload -- returns None and the caller runs the existing local model path.
An empty transcript ("") from a silent segment is a VALID remote result, not a
failure.

Endpoint lifecycle (see _EndpointHealth): the remote worker is RunPod
serverless -- it SCALES TO ZERO and cold-starts in ~16-34s (measured), warm in
~0.15s. The seam is remote-first with a health-aware circuit breaker that
deliberately treats a "cold/slow but coming up" worker DIFFERENTLY from a
"genuinely down" endpoint, so cold-start timeouts across the whole real
cold-start envelope can never blind a whole call. Callers that want
to WAKE the worker without serving a real request use ``warm_remote_voice()``.

This module deliberately contains no query classification, no model-output
parsing, and no prompt fragments -- it is a transport seam only, placed at the
same point production STT/TTS run so pipecat semantics are preserved.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from core.logging_config import get_logger

if TYPE_CHECKING:
    import numpy as np

logger = get_logger(__name__)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_DEFAULT_TIMEOUT_MS = 2500
_DEFAULT_COOLDOWN_SECS = 60
# Ceiling for a SINGLE warmup inference when the caller passes timeout_s=None.
# This bounds ONE attempt so it cannot hang the ring path forever; it is NOT the
# full cold-start budget. A real scaled-to-zero cold start is ~16-34s, longer
# than this ceiling, so the FIRST warmup against a stone-cold worker may miss --
# the keep-alive loop (telephony/call_manager._remote_warm_loop) retries every
# ~0.5s and lands the warm signal the instant the worker warms, and on a warm
# worker a warmup returns in ~0.15s well within this ceiling.
_DEFAULT_WARM_TIMEOUT_SECS = 10.0

# HTTP statuses that mean "workers are still scaling from zero", NOT "endpoint
# broken". A RunPod load-balancing front-end returns these while a cold worker
# boots; they are spin-up signals (retry next turn), not hard-down. Measured
# live against endpoint 40jjk52fz3jisn during a scaled-to-zero cold start: the
# LB front-end returns 429/503 ("workers scaling") AND 430 ("No Workers
# Available") AND 502 (bad gateway while the worker process is still coming up).
# Omitting 430/502 (the pre-fix set was {429, 503}) misclassified a legitimate
# cold start as a hard-down failure and tripped the breaker prematurely.
_SPINUP_STATUS = frozenset({429, 430, 502, 503})

# WALL-CLOCK tolerance for a worker scaling from zero. The measured real
# scaled-to-zero cold start of the RunPod voice worker is ~16-34s (process boot
# + CUDA + faster-whisper/Kokoro model load) -- i.e. ~6-13 phone turns at the
# ~2.5s per-request budget, NOT the ~7s (~2-3 turns) the pre-fix count budget
# assumed. So spin-up tolerance is keyed to the real cold-start ENVELOPE in wall
# time, not a failure COUNT: from the first spin-up failure of an episode we
# keep retrying (staying AVAILABLE so the call uses the worker the instant it
# warms) until this many seconds elapse. Only if spin-up failures persist PAST
# the envelope (the worker genuinely never comes up) do we trip the breaker, so
# we stop paying a timeout every turn against something that is not recovering.
# 45s covers the 34s upper bound with headroom. (A pre-fix COUNT budget of 3
# tripped at ~7.5s -- long before a real cold start finished -- pinning the whole
# call to local CPU for the cooldown and never using the worker that warmed at
# ~34s.)
_SPINUP_TOLERANCE_SECS = 45.0
# Measured cold-start upper bound the tolerance MUST cover (real live number).
# The envelope ratchet asserts _SPINUP_TOLERANCE_SECS >= this.
_SPINUP_COLDSTART_ENVELOPE_SECS = 34.0
# How many CONSECUTIVE hard failures (connection refused, non-scaling 5xx,
# malformed output) before we trip the breaker. Hard failures are unambiguous
# "endpoint broken", so the budget is small -- but >1 so a single blip against
# an otherwise-healthy endpoint does not blind the call.
_HARD_FAILURE_BUDGET = 2

# The language code the remote TTS path speaks. SINGLE SOURCE OF TRUTH shared by
# both the real serve default (remote_synthesize) AND the warmup op, so the two
# can never drift. This MUST be an espeak-accepted locale: the worker passes lang
# straight to kokoro.create -> espeak (telephony/remote_voice_worker/handler.py),
# whose espeak backend DETERMINISTICALLY REJECTS bare "en" (RuntimeError:
# language "en" is not supported) while "en-us" synthesizes in ~150ms. The pre-fix
# bug (Defect A) sent "en" in the warmup op only, so EVERY warmup errored ->
# warm_remote_voice() always returned False -> the dial warm-signal never fired
# and the keep-alive never confirmed warm, while the real serve path (which
# already used "en-us") worked. Keeping ONE constant makes that drift impossible;
# the phone-remote-voice-warmup-lang ratchet asserts the two stay equal.
_SERVE_DEFAULT_LANG = "en-us"

# Warmup op: a minimal TTS synth is the cheapest inference that proves the
# worker PROCESS and the model are both loaded (a health ping would not exercise
# the model). Kept tiny so a keep-alive costs almost nothing.
_WARMUP_TEXT = "ok"
_WARMUP_VOICE = "af_heart"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def phone_voice_remote_enabled() -> bool:
    return _env("VIOLA_PHONE_VOICE_REMOTE_ENABLED").lower() in _TRUE_VALUES


def phone_voice_remote_url() -> str:
    return _env("VIOLA_PHONE_VOICE_REMOTE_URL").rstrip("/")


def phone_voice_remote_api_key() -> str:
    return _env("VIOLA_PHONE_VOICE_REMOTE_API_KEY")


def phone_voice_remote_timeout_secs() -> float:
    raw = _env("VIOLA_PHONE_VOICE_REMOTE_TIMEOUT_MS")
    try:
        timeout_ms = int(raw) if raw else _DEFAULT_TIMEOUT_MS
    except ValueError:
        timeout_ms = _DEFAULT_TIMEOUT_MS
    return max(100, timeout_ms) / 1000.0


def phone_voice_remote_cooldown_secs() -> float:
    raw = _env("VIOLA_PHONE_VOICE_REMOTE_COOLDOWN_SECS")
    try:
        cooldown = int(raw) if raw else _DEFAULT_COOLDOWN_SECS
    except ValueError:
        cooldown = _DEFAULT_COOLDOWN_SECS
    return float(max(0, cooldown))


def _remote_configured() -> bool:
    """True when the remote flag is on and a url + key are present."""
    return phone_voice_remote_enabled() and bool(phone_voice_remote_url()) and bool(phone_voice_remote_api_key())


class _EndpointHealth:
    """Process-wide health of the remote voice endpoint (circuit breaker).

    Replaces the old single sticky ``_cooldown_until`` timestamp whose fatal
    flaw was that ONE cold-start read timeout opened a 60s GLOBAL blackout --
    pinning every subsequent turn of the call (and any concurrent call sharing
    the process) to the slow local CPU path even after the worker became warm.
    So one cold start poisoned the whole call.

    Root cause: that model conflated "cold/slow but coming up" with "genuinely
    down". This rework separates them by failure CLASS:

    - a SPIN-UP failure (per-request timeout, or a 429/430/502/503 "workers
      scaling / no workers available / bad gateway while booting" from the LB
      front-end) does NOT blind the call. The worker is booting; the very next
      turn retries and uses it once warm. Tolerance is keyed to the real
      cold-start ENVELOPE in WALL-CLOCK time (``_SPINUP_TOLERANCE_SECS``), not a
      failure COUNT: the measured scaled-to-zero cold start is ~16-34s (~6-13
      phone turns at the ~2.5s budget), so from the first spin-up failure of an
      episode we stay AVAILABLE and keep retrying until the envelope elapses.
      Only if spin-up failures persist PAST the envelope (the worker genuinely
      never comes up -- e.g. a dead endpoint that hangs every turn) do we trip
      the breaker, so we stop paying a timeout every turn against something that
      is not actually recovering. (A pre-fix COUNT budget of 3 tripped at ~7.5s,
      long before a real cold start finished, blinding the whole call.)
    - a HARD failure (connection refused, non-scaling 5xx, malformed output)
      counts toward ``_HARD_FAILURE_BUDGET``; repeated hard failures trip the
      breaker -- fail-fast against a genuinely broken endpoint.
    - ANY success immediately resets both counters and closes the breaker, so a
      warm worker is never held down by a stale transient.

    When tripped, the breaker opens for a bounded backoff (the configured
    cooldown secs) and then RE-PROBES: the next availability check lets exactly
    one attempt through; a fresh failure re-opens it, a success clears it. Every
    decision point (stay-available-during-spinup, trip, skip, re-probe, recover)
    is logged with reason + scope so one instrumented call explains itself.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._consecutive_spinup = 0
        self._consecutive_hard = 0
        # Monotonic timestamp of the FIRST spin-up failure of the current
        # spin-up episode (0.0 = no episode in progress). The wall-clock
        # cold-start envelope is measured from here, not from a failure count.
        self._spinup_started_at = 0.0
        self._breaker_open_until = 0.0

    def record_success(self) -> None:
        with self._lock:
            had_state = bool(
                self._breaker_open_until
                or self._consecutive_spinup
                or self._consecutive_hard
                or self._spinup_started_at
            )
            self._consecutive_spinup = 0
            self._consecutive_hard = 0
            self._spinup_started_at = 0.0
            self._breaker_open_until = 0.0
        if had_state:
            logger.debug("Remote phone voice: endpoint RESPONDED -- health reset (breaker closed, counters cleared)")

    def record_failure(self, category: str, reason: str) -> None:
        """Record a serve failure. ``category`` is 'spinup' or 'down'.

        Only ever called from the SERVE path (_post_op / the output validators).
        The warmup path never calls this -- a warmup is a wake, not a serve, and
        must not trip a cooldown.
        """
        cooldown = phone_voice_remote_cooldown_secs()
        tripped = False
        scope = ""
        count = 0
        with self._lock:
            now = time.monotonic()
            if category == "spinup":
                self._consecutive_spinup += 1
                count = self._consecutive_spinup
                if self._spinup_started_at == 0.0:
                    self._spinup_started_at = now
                elapsed = now - self._spinup_started_at
                tolerance = _SPINUP_TOLERANCE_SECS
                # Wall-clock envelope, NOT a failure count: a real cold start is
                # ~16-34s, so tolerate spin-up failures for the whole envelope so
                # the call uses the worker the instant it warms. Trip only once
                # they persist PAST it (the worker never came up).
                if elapsed < tolerance:
                    logger.debug(
                        "Remote phone voice: spin-up failure #%d, %.1fs/%.0fs into the cold-start "
                        "envelope (%s) -- staying AVAILABLE (worker booting), will retry next turn",
                        count,
                        elapsed,
                        tolerance,
                        reason,
                    )
                    return
                scope = "spin-up-envelope-exceeded-%.0fs" % elapsed
            else:  # "down"
                self._consecutive_hard += 1
                count = self._consecutive_hard
                budget = _HARD_FAILURE_BUDGET
                if count < budget:
                    logger.debug(
                        "Remote phone voice: hard failure %d/%d (%s) -- still AVAILABLE, " "one more trips the breaker",
                        count,
                        budget,
                        reason,
                    )
                    return
                scope = "hard-failures"
            self._breaker_open_until = now + cooldown
            tripped = True
        if tripped:
            logger.warning(
                "Remote phone voice: breaker OPEN [%s after %d consec] (%s) -- skipping remote "
                "for %.0fs then re-probing; local CPU path serves meanwhile",
                scope,
                count,
                reason,
                cooldown,
            )

    def available(self) -> bool:
        """True when the remote path should be ATTEMPTED for this request."""
        remaining = 0.0
        with self._lock:
            open_until = self._breaker_open_until
            if open_until == 0.0:
                return True
            now = time.monotonic()
            if now >= open_until:
                # Backoff elapsed -- re-probe: close the breaker so exactly ONE
                # attempt goes through. A fresh failure re-opens it; a success
                # resets it via record_success().
                self._breaker_open_until = 0.0
                self._consecutive_spinup = 0
                self._consecutive_hard = 0
                self._spinup_started_at = 0.0
                decision = "reprobe"
            else:
                decision = "skip"
                remaining = open_until - now
        if decision == "reprobe":
            logger.debug("Remote phone voice: breaker backoff elapsed -- re-probing endpoint (one attempt)")
            return True
        logger.debug(
            "Remote phone voice: SKIP remote -- breaker open %.0fs more (endpoint down/not-coming-up); "
            "local path serves this turn",
            remaining,
        )
        return False

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            open_now = self._breaker_open_until != 0.0 and now < self._breaker_open_until
            remaining = max(0.0, self._breaker_open_until - now) if self._breaker_open_until else 0.0
            spinup_elapsed = (now - self._spinup_started_at) if self._spinup_started_at else 0.0
            return {
                "breaker_open": open_now,
                "breaker_seconds_remaining": round(remaining, 2),
                "consecutive_spinup_failures": self._consecutive_spinup,
                "consecutive_hard_failures": self._consecutive_hard,
                "spinup_envelope_elapsed_s": round(spinup_elapsed, 2),
                "spinup_envelope_secs": _SPINUP_TOLERANCE_SECS,
            }

    def reset(self) -> None:
        with self._lock:
            self._consecutive_spinup = 0
            self._consecutive_hard = 0
            self._spinup_started_at = 0.0
            self._breaker_open_until = 0.0


_health = _EndpointHealth()


def reset_remote_voice_cooldown_for_tests() -> None:
    global _client
    _health.reset()
    with _client_lock:
        if _client is not None:
            with suppress(Exception):
                _client.close()
        _client = None


def remote_voice_available() -> bool:
    """True when the remote path should be attempted for this request.

    Gated by config (flag/url/key) and endpoint health. A COLD or WARMING worker
    stays available (see _EndpointHealth) so the call retries and uses the worker
    as soon as it is warm, instead of a single cold-start timeout blinding the
    whole call. Only a GENUINELY down endpoint -- breaker tripped by repeated
    hard failures, or spin-up failures that never resolve -- returns False.
    """
    if not _remote_configured():
        return False
    return _health.available()


def remote_voice_state() -> dict[str, Any]:
    """Inspectable snapshot of the remote-endpoint lifecycle state.

    Exposes the breaker/health so callers and diagnostics can see WHY the remote
    path is or is not being attempted, instead of a hidden global sticky blackout.
    """
    snap = _health.snapshot()
    snap["enabled"] = phone_voice_remote_enabled()
    snap["configured"] = _remote_configured()
    return snap


# --- Early pre-warm session (C-302a) -----------------------------------------
# The in-call warm machine (telephony/call_manager._remote_warm_loop) can only
# start once the call task exists -- i.e. AFTER the user has approved the call.
# The RunPod worker's measured cold start (16-37s) therefore lands entirely in
# front of the dial, which is the dominant term in the observed 39.4s dial-to-
# first-word (three real calls: gate_wait 28.6s / 25.2s / 16.5s).
#
# Everything BEFORE approval -- Viola speaking the confirmation prompt, the user
# hearing it, thinking, and answering, then the confirming agent turn -- is dead
# time today. This session lets any caller that knows a call has become LIKELY
# start the wake early, so the in-call dial gate finds the worker already warm
# and opens immediately. It never changes WHEN we dial relative to warmth: the
# founder ruling (call_manager.py, 2026-07-05) that we wait for a genuinely warm
# worker rather than dial cold is preserved exactly -- we only stop arriving at
# that gate cold.
#
# Bounded by construction: one thread per process, a hard lifetime, and it stops
# itself. A pre-warm for a call the user ultimately declines costs one worker
# wake, which is why the lifetime is capped rather than open-ended.
_PREWARM_MAX_LIFETIME_SECS = 120.0
# Retry cadence while the worker is still cold. Matches the in-call loop's floor
# so a cold miss cannot hot-spin on a fast/no-op False.
_PREWARM_COLD_RETRY_SECS = 0.5
# Keep-alive cadence once warm. Must stay under the endpoint's ~10s idle timeout
# (same constraint as call_manager.REMOTE_WARM_KEEPALIVE_INTERVAL_S) or the
# worker re-cools while the user is still deciding.
_PREWARM_KEEPALIVE_SECS = 6.0
# Per-ping ceiling. One attempt is bounded; the loop -- not the ping -- spans the
# full cold-start envelope.
_PREWARM_PING_TIMEOUT_SECS = 8.0
# How long an observed warm signal is trusted by a later reader. Beyond this the
# worker may have re-cooled, so the in-call gate must prove warmth itself rather
# than dial on a stale observation.
_PREWARM_WARM_TTL_SECS = 20.0

_prewarm_lock = threading.Lock()
# The live session, as a (thread, its own stop event) pair. The stop event is
# per-session rather than module-global so a replacement session can never
# reach in and clear the signal an outgoing thread is still unwinding on, and
# an outgoing thread can never be silenced by the newcomer's fresh event.
_prewarm_session: tuple[threading.Thread, threading.Event] | None = None
_warm_observed_monotonic: float | None = None


def _note_remote_voice_warm() -> None:
    """Record that a warmup inference RETURNED just now (worker + model live)."""
    global _warm_observed_monotonic
    with _prewarm_lock:
        _warm_observed_monotonic = time.monotonic()


def remote_voice_warm_age_secs() -> float | None:
    """Seconds since a warmup inference last returned, or None if never."""
    with _prewarm_lock:
        observed = _warm_observed_monotonic
    if observed is None:
        return None
    return max(0.0, time.monotonic() - observed)


def is_remote_voice_warm(*, max_age_secs: float | None = None) -> bool:
    """True when a warmup inference returned recently enough to still be warm.

    This is an OBSERVATION, never an assumption: it is set only where a real
    warmup response came back, and it expires, so a caller can never dial on a
    stale "probably warm". A caller that gets False must prove warmth itself.
    """
    if not _remote_configured():
        return False
    age = remote_voice_warm_age_secs()
    if age is None:
        return False
    ttl = _PREWARM_WARM_TTL_SECS if max_age_secs is None else max(0.0, float(max_age_secs))
    # Treat the expiry instant as stale. A zero-second limit must never accept
    # an observation merely because both clocks round to the same value.
    return age < ttl


def _prewarm_loop(stop: threading.Event, deadline: float) -> None:
    """Ping until warm, then keep-alive, until the deadline or a stop request."""
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            warm = warm_remote_voice(timeout_s=_PREWARM_PING_TIMEOUT_SECS)
        except Exception as exc:  # noqa: BLE001, RUF100 - a pre-warm must never raise out of its thread
            logger.debug("Remote phone voice pre-warm ping raised (ignored): %s", exc)
            warm = False
        # Wait on the stop event rather than sleeping, so a call that adopts this
        # session takes the keep-alive over immediately instead of after a nap.
        stop.wait(_PREWARM_KEEPALIVE_SECS if warm else _PREWARM_COLD_RETRY_SECS)
    logger.debug("Remote phone voice pre-warm session ended (stopped=%s)", stop.is_set())


def prewarm_remote_voice() -> bool:
    """Start waking the remote worker now, without blocking the caller.

    Idempotent and non-blocking: safe to call on every phone-call approval
    request, including repeats of the same one. Returns True when a pre-warm
    session is running (or was already running), False when there is nothing to
    warm because the remote path is not configured -- in which case the local
    STT/TTS path runs and there is no cold start to hide.
    """
    global _prewarm_session
    if not _remote_configured():
        return False
    with _prewarm_lock:
        existing = _prewarm_session
        # A thread that has been asked to stop is NOT a running session even
        # while it is still unwinding: reporting it as one would return True
        # with nothing warming. Start a replacement instead, and let the
        # outgoing thread exit on the stop event it already holds.
        if existing is not None and existing[0].is_alive() and not existing[1].is_set():
            return True
        stop = threading.Event()
        thread = threading.Thread(
            target=_prewarm_loop,
            args=(stop, time.monotonic() + _PREWARM_MAX_LIFETIME_SECS),
            name="phone-remote-prewarm",
            daemon=True,
        )
        _prewarm_session = (thread, stop)
    logger.info(
        "Remote phone voice pre-warm started (lifetime=%.0fs) — waking the worker "
        "while the call is still being confirmed",
        _PREWARM_MAX_LIFETIME_SECS,
    )
    thread.start()
    return True


def stop_remote_voice_prewarm() -> None:
    """Stop the pre-warm session (idempotent).

    Called when a live call's own keep-alive takes over, so exactly one warm
    machine is pinging the worker at a time.
    """
    with _prewarm_lock:
        session = _prewarm_session
    if session is None:
        return
    session[1].set()


def reset_remote_voice_prewarm_for_tests() -> None:
    global _prewarm_session, _warm_observed_monotonic
    with _prewarm_lock:
        session = _prewarm_session
        _prewarm_session = None
        _warm_observed_monotonic = None
    if session is not None:
        session[1].set()
        session[0].join(timeout=5.0)


_client_lock = threading.Lock()
_client: Any = None


def _shared_client() -> Any:
    """Process-wide pooled HTTP client (httpx.Client is thread-safe).

    A fresh client per op pays a full TLS handshake every phone turn
    (~150-300 ms measured against api.runpod.ai); pooling removes it from
    the per-turn budget. Timeout is enforced per-request, not per-client.
    """
    global _client
    with _client_lock:
        if _client is None:
            import httpx

            _client = httpx.Client()
        return _client


def _classify_transport_exc(exc: Exception) -> tuple[str, str]:
    """Map a transport exception to (category, reason).

    'spinup' = worker cold/scaling; do NOT blind the call (retry next turn).
    'down'   = endpoint genuinely unreachable/broken.

    A per-request TIMEOUT is treated as spin-up because a RunPod worker scaling
    from zero is ~16-34s to first response, far past the ~2.5s per-turn budget --
    the first several turns against a cold worker WILL time out and that is
    expected, not a reason to blacklist the endpoint. Only timeouts that persist
    PAST the wall-clock cold-start envelope (worker never comes up) escalate to a
    trip inside _EndpointHealth. A connection error (refused / DNS / reset) is an
    unambiguous "nothing is answering" -> down.
    """
    reason = "%s: %s" % (type(exc).__name__, exc)
    with suppress(Exception):
        import httpx

        if isinstance(exc, httpx.TimeoutException):
            return "spinup", reason
    if isinstance(exc, TimeoutError):  # stdlib socket.timeout is an alias of this
        return "spinup", reason
    return "down", reason


def _execute_op(
    url: str, payload: dict[str, Any], *, timeout_s: float
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """POST one op straight to the endpoint. Pure transport + classification.

    Returns (output_dict, None) on success, or (None, (category, reason)) on any
    failure. Deliberately has NO health/cooldown side effects and NO logging of
    lifecycle decisions -- so the SERVE path (which records health) and the
    WARMUP path (which must never trip a cooldown) share one implementation and
    each decides what to do with the outcome.
    """
    try:
        response = _shared_client().post(
            url,
            json=payload,
            headers={"Authorization": "Bearer %s" % phone_voice_remote_api_key()},
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - any transport failure is classified + falls back
        return None, _classify_transport_exc(exc)
    if response.status_code != 200:
        status = response.status_code
        if status in _SPINUP_STATUS:
            return None, ("spinup", "HTTP %d (workers scaling)" % status)
        return None, ("down", "HTTP %d" % status)
    try:
        result = response.json()
    except Exception as exc:  # noqa: BLE001, RUF100 - malformed body is a hard endpoint failure
        return None, ("down", "bad JSON body: %s" % exc)
    if not isinstance(result, dict) or result.get("error"):
        detail = result.get("error") if isinstance(result, dict) else "non-dict body"
        return None, ("down", "endpoint error: %s" % detail)
    return result, None


def _post_op(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Serve one op to the load-balancing endpoint; None on any failure.

    The RunPod LOAD-BALANCING worker (telephony/remote_voice_worker/http_server.py)
    exposes ``POST /op`` whose JSON body IS the op dict and whose 200 response IS
    the worker output dict directly -- there is NO job queue and NO
    ``{"status": "COMPLETED", "output": {...}}`` envelope. That envelope belongs
    to the separate queue API (``/v2/<id>/runsync``); an LB endpoint has no
    ``/runsync`` route and 404s it, which would silently fall every phone turn
    back to the local CPU path and make the GPU seam a no-op. So we post the op
    dict straight to ``/op`` and read the output dict straight from the body.

    Failures are classified (spin-up vs hard-down) and recorded on the endpoint
    health/circuit-breaker, which is what keeps a single cold-start timeout from
    blinding the whole call (see _EndpointHealth).
    """
    url = phone_voice_remote_url()
    if not url.endswith("/op"):
        url = "%s/op" % url
    result, failure = _execute_op(url, payload, timeout_s=phone_voice_remote_timeout_secs())
    if failure is not None:
        category, reason = failure
        _health.record_failure(category, reason)
        return None
    _health.record_success()
    return result


def warm_remote_voice(*, timeout_s: float | None = None) -> bool:
    """Fire ONE warmup inference at the endpoint to wake/keep the worker warm.

    Returns True IFF a warmup inference RETURNED within ``timeout_s`` -- a
    returned response after a cold spin-up counts, because the response IS
    the warm signal (worker process + model both loaded). Returns False (a
    no-op) when the remote flag/url/key are unset (nothing to warm). NEVER raises
    into the caller. ``timeout_s=None`` uses a sensible ceiling (~10s) that
    covers a cold GPU spin-up.

    A warmup is a WAKE, not a serve:
    - it is NOT gated by the breaker (its whole job is to recover a cold/idle
      worker, so it must run even when the breaker is open), and
    - it NEVER trips a cooldown -- on failure it just returns False and leaves
      endpoint health untouched; on success it RESETS health (a warm worker
      clears any stale transient).
    Safe to call repeatedly as a keep-alive -- it never accumulates state.
    """
    if not _remote_configured():
        return False
    ceiling = _DEFAULT_WARM_TIMEOUT_SECS if timeout_s is None else max(0.1, float(timeout_s))
    url = phone_voice_remote_url()
    if not url.endswith("/op"):
        url = "%s/op" % url
    started = time.perf_counter()
    try:
        result, failure = _execute_op(
            url,
            {"op": "tts", "text": _WARMUP_TEXT, "voice": _WARMUP_VOICE, "speed": 1.0, "lang": _SERVE_DEFAULT_LANG},
            timeout_s=ceiling,
        )
    except Exception as exc:  # noqa: BLE001, RUF100 - a warmup must never raise into the ring/keep-alive path
        logger.debug(
            "Remote phone voice warmup raised (ignored, NOT tripping cooldown): %s: %s",
            type(exc).__name__,
            exc,
        )
        return False
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if result is not None:
        # A returned warmup response means worker + model are live. Clear any
        # stale breaker/counters so the next serve turn goes straight to remote.
        _health.record_success()
        # Record the observation so a later reader (the in-call dial gate) can
        # see that the worker was proven warm, whoever proved it (C-302a).
        _note_remote_voice_warm()
        logger.info(
            "Remote phone voice WARM: warmup inference returned in %.0fms (worker + model ready)",
            elapsed_ms,
        )
        return True
    category, reason = failure
    logger.info(
        "Remote phone voice warmup did NOT land in %.1fs (%s: %s) -- worker still cold/unreachable; "
        "NOT tripping cooldown (warmup is a wake, not a serve)",
        ceiling,
        category,
        reason,
    )
    return False


def _language_str(language: Any) -> str:
    """Normalize a language argument to a plain code string.

    pipecat settings may carry a Language str-enum (use .value) or a
    NOT_GIVEN sentinel (not a str at all) -- anything that is not a
    non-empty string maps to "en", matching the phone default.
    """
    value = getattr(language, "value", language)
    if not isinstance(value, str) or not value.strip():
        return "en"
    return value.strip()


def remote_transcribe_pcm(
    audio: bytes,
    sample_rate: int,
    *,
    language: Any,
    beam_size: int,
    hotwords: str = "",
    initial_prompt: str = "",
) -> str | None:
    """Transcribe raw PCM16 on the remote endpoint; None -> use local path."""
    if not audio or not remote_voice_available():
        return None
    started = time.perf_counter()
    output = _post_op(
        {
            "op": "stt",
            "audio_b64": base64.b64encode(audio).decode("ascii"),
            "sample_rate": int(sample_rate),
            "language": _language_str(language),
            "beam_size": int(beam_size),
            "hotwords": hotwords.strip(),
            "initial_prompt": initial_prompt.strip(),
        }
    )
    if output is None:
        return None
    text = output.get("text")
    if not isinstance(text, str):
        # 200 but malformed body -- worker is up yet broke its own contract.
        _health.record_failure("down", "stt output missing text")
        return None
    logger.debug(
        "Remote phone STT: %.0fms round-trip (worker stt_ms=%s gpu=%s)",
        (time.perf_counter() - started) * 1000.0,
        output.get("stt_ms"),
        output.get("gpu"),
    )
    return text


def remote_synthesize(
    text: str,
    *,
    voice: str,
    speed: float = 1.0,
    lang: Any = _SERVE_DEFAULT_LANG,
) -> tuple[np.ndarray, int] | None:
    """Synthesize text on the remote endpoint.

    Returns (float32 samples in [-1, 1], sample_rate) matching the local
    kokoro.create contract, or None -> use the local Kokoro runtime.
    """
    if not text.strip() or not remote_voice_available():
        return None
    started = time.perf_counter()
    output = _post_op(
        {
            "op": "tts",
            "text": text,
            "voice": voice,
            "speed": float(speed),
            "lang": _language_str(lang),
        }
    )
    if output is None:
        return None
    audio_b64 = output.get("audio_b64")
    sample_rate = output.get("sample_rate")
    if not isinstance(audio_b64, str) or not audio_b64 or not isinstance(sample_rate, int) or sample_rate <= 0:
        # 200 but malformed body -- worker is up yet broke its own contract.
        _health.record_failure("down", "tts output missing audio")
        return None
    import numpy as np

    samples = np.frombuffer(base64.b64decode(audio_b64), dtype=np.int16).astype(np.float32) / 32767.0
    logger.debug(
        "Remote phone TTS: %.0fms round-trip (worker tts_ms=%s gpu=%s audio=%ss)",
        (time.perf_counter() - started) * 1000.0,
        output.get("tts_ms"),
        output.get("gpu"),
        output.get("audio_seconds"),
    )
    return samples, sample_rate


class RemoteFirstKokoro:
    """Kokoro-compatible proxy: remote GPU synth first, local runtime fallback.

    Exposes the same create/create_stream surface pipecat's KokoroTTSService
    uses, so swapping it in preserves pipecat semantics exactly. Any remote
    failure (or the flag being off) delegates to the wrapped local Kokoro.
    """

    def __init__(self, local_kokoro: Any) -> None:
        self._local = local_kokoro

    def __getattr__(self, name: str) -> Any:
        return getattr(self._local, name)

    def create(self, text: str, voice: str, speed: float = 1.0, lang: str = "en-us", **kwargs: Any) -> Any:
        remote = remote_synthesize(text, voice=voice, speed=speed, lang=lang)
        if remote is not None:
            return remote
        return self._local.create(text, voice=voice, speed=speed, lang=lang, **kwargs)

    async def create_stream(self, text: str, voice: str, speed: float = 1.0, lang: str = "en-us", **kwargs: Any) -> Any:
        import asyncio

        remote = await asyncio.to_thread(remote_synthesize, text, voice=voice, speed=speed, lang=lang)
        if remote is not None:
            yield remote
            return
        async for chunk in self._local.create_stream(text, voice=voice, speed=speed, lang=lang, **kwargs):
            yield chunk


def maybe_remote_first_kokoro(local_kokoro: Any) -> Any:
    """Wrap the local Kokoro runtime when the remote flag is on.

    Checked at service-creation time (per call); with the flag off the local
    runtime is returned untouched so the default path is byte-identical.
    """
    if phone_voice_remote_enabled():
        return RemoteFirstKokoro(local_kokoro)
    return local_kokoro
