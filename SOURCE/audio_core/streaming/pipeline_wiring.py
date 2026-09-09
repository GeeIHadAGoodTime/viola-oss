"""
Pipeline wiring for multi-room PCM streaming.

Provides setup functions for Source (broadcaster) and Device (receiver) sides
of the PCM streaming pipeline. Called during application bootstrap when
multiroom is enabled.

Source Side:
    Source plays normally -> ProcTap captures -> ChunkStamper timestamps ->
    WebSocket broadcasts to devices -> devices sync with each other.

Device Side:
    WebSocket -> DeviceAudioReceiver -> PlaybackScheduler -> AudioOutputDriver
"""

from __future__ import annotations

import asyncio
import collections
import math
import statistics
import threading
import time
from typing import TYPE_CHECKING, Any

from auth.ip_utils import extract_client_ip
from contracts.api_response import failure_response, success_response
from core.constants import DEFAULT_API_PORT, TIMEOUT_MEDIUM
from core.logging_config import get_logger
from fastapi import Request

if TYPE_CHECKING:
    from audio_core.capture.base import AudioCaptureProvider
    from audio_core.sync_engine import SyncEngine
    from fastapi import FastAPI
    from ui.api.routes.audio_stream import AudioStreamManager

    from .audio_tee import AudioTee
    from .chunk_stamper import ChunkStamper

    # See setup_device_pipeline body for the alias rationale — the headless
    # device IS the spoke; class lives under the Spoke* name.
    from .source_broadcaster import SourceAudioBroadcaster
    from .spoke_receiver import SpokeAudioReceiver as DeviceAudioReceiver

logger = get_logger(__name__)

# Module-level reference to the active AudioTee instance.
# This allows playback backends (e.g. SimpleBackend) to retrieve the tee
# without requiring access to FastAPI's app.state.  The reference is set
# by setup_source_pipeline() and cleared by teardown_pipeline().
_active_audio_tee: AudioTee | None = None

# Module-level reference to the active capture provider (Source side only).
_active_capture: AudioCaptureProvider | None = None

# Module-level reference to the active ChunkStamper (Source side only).
_active_chunk_stamper: ChunkStamper | None = None

# Module-level reference to the FastAPI app.
_app_ref: FastAPI | None = None

# Guard: prevent setup_source_pipeline from running more than once.
_pipeline_initialized: bool = False

# Capture gain: always 1.0 (no gain compensation in new architecture).
# Kept because _capture_fanout closure reads it.
_pipeline_gain: float = 1.0

# Source-local playback was removed in the 2026-04-12 simplification
# (the source-and-device architecture became canonical â€” the source
# is never audible, every Viola instance is a device).  These flags are
# always False in production; retained because
# ``music/runtime/playback_executor.py``, ``utils/api_helpers.py``,
# ``ui/api/routes/multiroom.py`` and a couple of regression tests still
# import them directly as module attributes for the source-state payload
# and multiroom-active gate.  Once those callers are migrated to a
# cleaner API, these can be deleted.
_source_local_active: bool = False
_sounddevice_muted: bool = False
# Compatibility aliases retained for older diagnostics/tests that still patch
# the former hub/spoke names.
_hub_local_active: bool = False

# Current device count: tracked by _on_device_count_changed.
_device_count: int = 0
_spoke_count: int = 0

# Demand-driven source capture lifecycle.  The source pipeline is registered at
# boot, but its CPU-expensive capture/stamper threads only run while something
# can consume their output.
_source_lifecycle_lock = threading.RLock()
_recording_active: bool = False
_pending_proctap_rescan: bool = False
_pending_proctap_pid: int | None = None

# Direct injection flag: when True, _capture_fanout skips feeding the
# ChunkStamper (ProcTap data is ignored for the binary WS path).
# Set by playback backends that inject PCM directly into the stamper
# (e.g. SimpleBackend for local file playback).
_direct_injection_active: bool = False

# Embedded source flag: when True, the audio source is a browser/webview
# (e.g. YouTube IFrame rendered by React in Chromium/QtWebEngine).
_embedded_source_active: bool = False

# TTS capture mute flag: same post-2026-04-12 story â€” the old TTS
# capture-guard pathway that set this to True was removed when the
# source-local subprocess went away.  Always False today; retained for
# import compatibility with ``ui/api/routes/multiroom.py``.
_source_local_capture_muted_for_tts: bool = False

# ---------- Per-Chunk Anomaly Detector State (Task 2) ----------
# These counters are updated by _capture_fanout on EVERY chunk (~50/sec).
# Use single-element lists for mutability inside the closure.
_fanout_discontinuity_count: list[int] = [0]
_fanout_clipping_count: list[int] = [0]
_fanout_nan_count: list[int] = [0]
_fanout_silence_to_audio_transitions: list[int] = [0]
_fanout_prev_last_sample: list[float] = [0.0]
_fanout_prev_rms_was_silence: list[bool] = [True]
_fanout_total_chunks: list[int] = [0]

# ---------- PCM Capture Timeline Ring Buffer (Task 3) ----------
# Stores per-chunk metrics for the last 30 seconds (1500 entries at 50/sec).
_pcm_timeline: collections.deque = collections.deque(maxlen=1500)

_oracle_audio_injection_lock = threading.Lock()
_oracle_audio_injection_stop: threading.Event | None = None
_oracle_audio_injection_thread: threading.Thread | None = None
_oracle_audio_injection_metrics: dict[str, Any] = {
    "running": False,
    "chunks_written": 0,
    "bytes_written": 0,
    "started_monotonic": None,
    "ended_monotonic": None,
    "error": None,
}


# ---------- Capture Provider Health ----------
# Tracks whether the ProcTap (or equivalent) capture subsystem is
# running.  Without this, an exception during capture.start() is
# logged once and then silently swallowed, leaving the source pipeline running
# with empty capture â†’ devices receive silence indefinitely with no
# operator-visible signal.  Health endpoint reads this via
# get_capture_health().
#
# States:
#   "not_started" â€” source setup has not attempted capture yet (default,
#                   also the state during device-only setups).
#   "ok"          â€” a real capture provider is running.
#   "degraded"    â€” a provider IS running (so nothing else looks broken),
#                   but it's a silent fallback (get_capture_provider()
#                   auto-selected TestToneProvider because no real capture
#                   provider was available -- see #2598).  ``provider`` and
#                   ``fallback_reason`` are set; devices are receiving a
#                   synthetic tone, not real system audio.  A deliberate,
#                   explicit VIOLA_AUDIO_CAPTURE=test_tone override does
#                   NOT set this state -- that's an intentional choice, not
#                   a silent failure being papered over.
#   "failed"      â€” provisioning threw; source pipeline is running without capture.
#
# The health endpoint treats "not_started" as OK (capture is optional),
# "degraded" as a warning (fake-success signal, not an outage), and
# "failed" as an error.
_capture_health_state: dict[str, Any] = {
    "state": "not_started",
    "error": None,
    "since_epoch": None,
    "provider": None,
    "fallback_reason": None,
}


def _set_capture_health(
    state: str,
    *,
    error: str | None = None,
    provider: str | None = None,
    fallback_reason: str | None = None,
) -> None:
    """Update the capture-provider health snapshot.

    Called from the source setup path to signal success/failure.  Kept
    module-private; external consumers go through
    :func:`get_capture_health`.
    """
    _capture_health_state["state"] = state
    _capture_health_state["error"] = error
    _capture_health_state["since_epoch"] = time.time()
    _capture_health_state["provider"] = provider
    _capture_health_state["fallback_reason"] = fallback_reason


def _capture_health_state_for(capture: AudioCaptureProvider) -> tuple[str, str | None]:
    """Classify a running capture provider as "ok" or a silent-fallback "degraded".

    A provider carries a non-``None`` ``fallback_reason`` only when
    ``get_capture_provider()`` auto-selected it because no real capture
    provider was available (#2598) -- never for an explicit
    ``VIOLA_AUDIO_CAPTURE`` override or a direct construction (e.g. tests).
    """
    reason = getattr(capture, "fallback_reason", None)
    return ("degraded", reason) if reason else ("ok", None)


def get_capture_health() -> dict[str, Any]:
    """Return the current capture-provider health snapshot.

    Fields:
        state: "not_started" | "ok" | "degraded" | "failed"
        error: Human-readable error message when state == "failed",
               otherwise ``None``.
        since_epoch: ``time.time()`` at the last state transition.
        provider: Name of the capture provider class when running
                  (e.g. "ProcTapProvider"); ``None`` before setup or
                  after a failure.
        fallback_reason: Set when state == "degraded" -- machine-readable
                  reason the auto-detected provider is a silent fallback
                  (e.g. "no_capture_provider_available"); ``None`` otherwise.
    """
    return dict(_capture_health_state)


# ---------- Output Driver Health ----------
# Device-side peer of Capture Provider Health above.  Tracks whether the
# device's audio output driver is a real sound-hardware driver or the
# silent NullAudioOutput fallback (get_output_driver() auto-selects it
# when ``sounddevice`` is not importable -- see #2598).  Without this,
# NullAudioOutput.write() returns cleanly for every chunk, so playback
# "succeeds" while the device produces no sound and nothing distinguishes
# that from a real, working speaker. Health endpoint reads this via
# get_output_health().
#
# States:
#   "not_started" â€” device pipeline has not been set up yet (default,
#                   also the state for source/hub-only setups).
#   "ok"          â€” a real output driver is active.
#   "degraded"    â€” NullAudioOutput is active because no real output
#                   driver was available; PCM is being silently discarded.
_output_health_state: dict[str, Any] = {
    "state": "not_started",
    "since_epoch": None,
    "provider": None,
    "fallback_reason": None,
}


def _set_output_health(
    state: str,
    *,
    provider: str | None = None,
    fallback_reason: str | None = None,
) -> None:
    """Update the output-driver health snapshot.

    Called from the device setup path right after ``get_output_driver()``
    resolves.  Kept module-private; external consumers go through
    :func:`get_output_health`.
    """
    _output_health_state["state"] = state
    _output_health_state["since_epoch"] = time.time()
    _output_health_state["provider"] = provider
    _output_health_state["fallback_reason"] = fallback_reason


def get_output_health() -> dict[str, Any]:
    """Return the current output-driver health snapshot.

    Fields:
        state: "not_started" | "ok" | "degraded"
        since_epoch: ``time.time()`` at the last state transition.
        provider: Name of the output driver class (e.g.
                  "SounddeviceAudioOutput" or "NullAudioOutput"); ``None``
                  before device pipeline setup.
        fallback_reason: Set when state == "degraded" -- machine-readable
                  reason the driver is a silent fallback (e.g.
                  "sounddevice_unavailable"); ``None`` otherwise.
    """
    return dict(_output_health_state)


def _component_is_running(component: Any) -> bool:
    """Return a best-effort running flag for capture/stamper objects."""
    try:
        metrics_fn = getattr(component, "get_metrics", None)
        if callable(metrics_fn):
            metrics = metrics_fn()
            if isinstance(metrics, dict) and isinstance(metrics.get("running"), bool):
                return bool(metrics["running"])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        logger.debug("Failed to read component running state", exc_info=True)
    return bool(getattr(component, "_running", False))


def _source_pipeline_has_consumers() -> bool:
    """Return True when the source capture pipeline has a real consumer."""
    return _spoke_count > 0 or _recording_active


def _clear_stamper_runtime_buffers(stamper: ChunkStamper) -> None:
    clear_fn = getattr(stamper, "clear_runtime_buffers", None)
    if callable(clear_fn):
        clear_fn()


def _replay_pending_proctap_signals(capture: AudioCaptureProvider) -> None:
    """Replay PID/rescan hints that arrived while capture was demand-idle."""
    global _pending_proctap_pid, _pending_proctap_rescan

    pending_pid = _pending_proctap_pid
    pending_rescan = _pending_proctap_rescan
    _pending_proctap_pid = None
    _pending_proctap_rescan = False

    if pending_pid is not None and hasattr(capture, "notify_pid"):
        try:
            capture.notify_pid(pending_pid)
            logger.info("notify_proctap_pid: replayed pending PID %d", pending_pid)
        except Exception:
            logger.exception("notify_proctap_pid: pending PID replay failed")

    if pending_rescan and hasattr(capture, "request_rescan"):
        try:
            capture.request_rescan()
            logger.info("request_proctap_rescan: replayed pending rescan")
        except Exception:
            logger.exception("request_proctap_rescan: pending replay failed")


def _sync_source_pipeline_demand(app: FastAPI, *, reason: str) -> None:
    """Start or stop source capture/stamping to match current consumer demand."""
    global _active_capture

    with _source_lifecycle_lock:
        capture: AudioCaptureProvider | None = getattr(app.state, "source_capture_provider", None)
        stamper: ChunkStamper | None = getattr(app.state, "chunk_stamper", None)
        demand = _source_pipeline_has_consumers()

        if demand:
            if stamper is not None and not _component_is_running(stamper):
                try:
                    stamper.start()
                    logger.info("ChunkStamper started on demand: reason=%s", reason)
                except Exception:
                    logger.exception("Failed to start ChunkStamper on demand")

            if capture is None:
                _active_capture = None
                _set_capture_health("not_started")
                return

            if not _component_is_running(capture):
                try:
                    capture.start()
                    _active_capture = capture
                    health_state, fallback_reason = _capture_health_state_for(capture)
                    _set_capture_health(health_state, provider=type(capture).__name__, fallback_reason=fallback_reason)
                    if fallback_reason:
                        logger.warning(
                            "System audio capture started on demand with a SILENT FALLBACK provider: "
                            "provider=%s fallback_reason=%s reason=%s -- spokes are receiving a synthetic "
                            "tone, not real system audio",
                            type(capture).__name__,
                            fallback_reason,
                            reason,
                        )
                    else:
                        logger.info(
                            "System audio capture started on demand: provider=%s reason=%s",
                            type(capture).__name__,
                            reason,
                        )
                    _replay_pending_proctap_signals(capture)
                except Exception as exc:
                    _active_capture = None
                    logger.exception(
                        "System audio capture unavailable; source will rely on playback backend writing directly"
                    )
                    _set_capture_health("failed", error=str(exc), provider=type(capture).__name__)
            else:
                _active_capture = capture
                health_state, fallback_reason = _capture_health_state_for(capture)
                _set_capture_health(health_state, provider=type(capture).__name__, fallback_reason=fallback_reason)
                _replay_pending_proctap_signals(capture)
            return

        if capture is not None and _component_is_running(capture):
            try:
                capture.stop()
                logger.info("System audio capture stopped: no source pipeline consumers")
            except Exception:
                logger.exception("Error stopping source capture provider after demand dropped")
        _active_capture = None
        _set_capture_health("not_started")

        if stamper is not None and _component_is_running(stamper):
            try:
                stamper.stop()
                _clear_stamper_runtime_buffers(stamper)
                logger.info("ChunkStamper stopped: no source pipeline consumers")
            except Exception:
                logger.exception("Error stopping ChunkStamper after demand dropped")


def get_active_audio_tee() -> AudioTee | None:
    """
    Return the active AudioTee, or None if multi-room streaming is not enabled.

    Playback backends call this to obtain the tee for writing decoded PCM.
    Thread-safe: the reference is set once during startup and cleared on
    shutdown; no lock is required.
    """
    return _active_audio_tee


def get_active_chunk_stamper() -> ChunkStamper | None:
    """Return the active ChunkStamper, or None if multi-room streaming is not enabled.

    TTS engines and other audio sources call this to inject PCM bytes into the
    multi-room broadcast pipeline.  Thread-safe: the reference is set once
    during startup and cleared on shutdown.
    """
    return _active_chunk_stamper


def is_direct_injection_active() -> bool:
    """Return whether direct injection (CEF/local file PCM) is currently active.

    Used by the state broadcast layer to set ``cef_active`` in the player
    state payload so the React frontend can skip rendering the YouTube
    iframe (which would cause double-play).
    """
    return _direct_injection_active


def set_direct_injection(active: bool) -> None:
    """Signal that a playback backend is injecting PCM directly into the stamper.

    When *active* is True, ``_capture_fanout`` stops feeding ProcTap data
    into the ChunkStamper (prevents dual-source garbling).  The AudioTee
    path is unaffected â€” ProcTap data still flows for JSON-based devices.

    Called by SimpleBackend (local file playback) around its playback loop.
    """
    global _direct_injection_active, _embedded_source_active
    _direct_injection_active = active
    # DI and embedded source are mutually exclusive â€” DI means local file
    # audio is injected directly, not coming from a browser/webview.
    if active:
        _embedded_source_active = False
    stamper = _active_chunk_stamper
    if active and stamper is not None:
        # Flush residual ProcTap bytes so the first injected chunk is clean
        with stamper._capture_lock:
            stamper._capture_buf.clear()

    logger.info("Direct injection %s", "active" if active else "inactive")
    if _app_ref is not None:
        _sync_source_pipeline_demand(_app_ref, reason="direct-injection")


def set_embedded_source(active: bool) -> None:
    """Mark playback as coming from an embedded browser source (e.g. YouTube IFrame).

    Tracks ``_embedded_source_active`` and updates the diagnostics bus.

    Called by the EMBEDDED_DEFER path in playback_executor.py (event loop)
    and by music/player/playback.py at queue boundaries.
    """
    global _embedded_source_active, _direct_injection_active
    _embedded_source_active = active
    # Embedded source and DI are mutually exclusive â€” embedded means audio
    # comes from a browser/webview, DI means local files inject PCM directly.
    if active:
        _direct_injection_active = False

    logger.info("Embedded source %s", "active" if active else "inactive")
    if _app_ref is not None:
        _sync_source_pipeline_demand(_app_ref, reason="embedded-source")


def is_embedded_source_active() -> bool:
    """Return True when playback comes from an embedded browser source."""
    return _embedded_source_active


def set_source_recording_active(active: bool) -> None:
    """Signal that source PCM recording needs the capture/stamper pipeline."""
    global _recording_active
    with _source_lifecycle_lock:
        _recording_active = active
        logger.info("Source recording %s", "active" if active else "inactive")
        if _app_ref is not None:
            _sync_source_pipeline_demand(_app_ref, reason="recording")


def get_source_local_player():
    """Return the SourceLocalPlayback instance if running, else None."""
    if _app_ref is not None:
        return getattr(_app_ref.state, "source_local_player", None)
    return None


def get_fanout_diagnostics() -> dict[str, Any]:
    """Return per-chunk anomaly detector counters from _capture_fanout.

    These counters are updated on EVERY ProcTap callback (~50/sec) and
    track discontinuities, clipping, NaN, and silence-to-audio transitions
    in the capture data before it reaches the ChunkStamper.

    Returns:
        Dict with anomaly counts and total chunks processed.
    """
    return {
        "total_chunks": _fanout_total_chunks[0],
        "discontinuity_count": _fanout_discontinuity_count[0],
        "clipping_count": _fanout_clipping_count[0],
        "nan_count": _fanout_nan_count[0],
        "silence_to_audio_transitions": _fanout_silence_to_audio_transitions[0],
    }


def get_capture_timeline(last_n: int | None = None) -> list[dict[str, Any]]:
    """Return recent per-chunk metrics from the capture ring buffer.

    Args:
        last_n: If provided, return only the last N entries.  If None,
            return all entries (up to 1500 = 30 seconds at 50/sec).

    Returns:
        List of per-chunk metric dicts, oldest first.
    """
    snapshot = list(_pcm_timeline)
    if last_n is not None and last_n < len(snapshot):
        return snapshot[-last_n:]
    return snapshot


def _oracle_audio_injection_snapshot() -> dict[str, Any]:
    with _oracle_audio_injection_lock:
        return dict(_oracle_audio_injection_metrics)


def stop_oracle_audio_injection(timeout_sec: float = 2.0) -> dict[str, Any]:
    """Stop the local-only oracle audio injection worker if it is running."""
    global _oracle_audio_injection_stop, _oracle_audio_injection_thread

    with _oracle_audio_injection_lock:
        stop_event = _oracle_audio_injection_stop
        thread = _oracle_audio_injection_thread

    if stop_event is not None:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0.0, timeout_sec))

    with _oracle_audio_injection_lock:
        still_running = _oracle_audio_injection_thread is not None and _oracle_audio_injection_thread.is_alive()
        _oracle_audio_injection_metrics["running"] = still_running
        if not still_running:
            _oracle_audio_injection_stop = None
            _oracle_audio_injection_thread = None
        return dict(_oracle_audio_injection_metrics)


def start_oracle_audio_injection(
    *,
    duration_sec: float = 90.0,
    frequency_hz: float = 997.0,
    amplitude: float = 0.22,
) -> dict[str, Any]:
    """Feed deterministic PCM through the active app-owned source pipeline."""
    import numpy as np

    from .chunk_protocol import pack_float32_to_int24

    global _oracle_audio_injection_stop, _oracle_audio_injection_thread, _oracle_audio_injection_metrics

    tee = _active_audio_tee
    stamper = _active_chunk_stamper
    gaps: list[str] = []
    if tee is None:
        gaps.append("active app-owned AudioTee is not available")
    if stamper is None:
        gaps.append("active app-owned ChunkStamper is not available")
    if gaps:
        return {
            "ok": False,
            "gaps": gaps,
            "tee_available": tee is not None,
            "stamper_available": stamper is not None,
            "previous_injection": _oracle_audio_injection_snapshot(),
        }

    stop_oracle_audio_injection(timeout_sec=1.0)

    duration_sec = max(1.0, min(float(duration_sec), 180.0))
    frequency_hz = max(80.0, min(float(frequency_hz), 8000.0))
    amplitude = max(0.01, min(float(amplitude), 0.8))
    sample_rate = 48000
    channels = 2
    chunk_sec = 0.02
    frames_per_chunk = int(sample_rate * chunk_sec)
    injection_stop = threading.Event()
    started = time.monotonic()
    before = stamper.get_metrics() if hasattr(stamper, "get_metrics") else {}

    with _oracle_audio_injection_lock:
        _oracle_audio_injection_stop = injection_stop
        _oracle_audio_injection_metrics = {
            "running": True,
            "chunks_written": 0,
            "bytes_written": 0,
            "started_monotonic": started,
            "ended_monotonic": None,
            "duration_sec": duration_sec,
            "frequency_hz": frequency_hz,
            "amplitude": amplitude,
            "sample_rate": sample_rate,
            "channels": channels,
            "chunk_sec": chunk_sec,
            "stamper_audio_chunks_before": before.get("audio_chunks"),
            "stamper_total_chunks_before": before.get("total_chunks"),
            "error": None,
        }

    def _worker() -> None:
        sample_index = 0
        next_deadline = time.monotonic()
        set_direct_injection(True)
        try:
            while not injection_stop.is_set() and time.monotonic() - started < duration_sec:
                frame_indices = np.arange(sample_index, sample_index + frames_per_chunk, dtype=np.float32)
                mono = (amplitude * np.sin(math.tau * frequency_hz * frame_indices / sample_rate)).astype(np.float32)
                stereo = np.repeat(mono[:, None], channels, axis=1).reshape(-1)
                pcm16 = np.clip(stereo * 32767.0, -32768.0, 32767.0).astype("<i2").tobytes()
                pcm24 = pack_float32_to_int24(stereo)

                tee.write(pcm16)
                stamper.on_capture_data(pcm24, sample_rate, channels, 3)

                sample_index += frames_per_chunk
                with _oracle_audio_injection_lock:
                    _oracle_audio_injection_metrics["chunks_written"] += 1
                    _oracle_audio_injection_metrics["bytes_written"] += len(pcm24)

                next_deadline += chunk_sec
                delay = next_deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
        except Exception as exc:
            logger.exception("Oracle audio injection failed")
            with _oracle_audio_injection_lock:
                _oracle_audio_injection_metrics["error"] = str(exc)
        finally:
            set_direct_injection(False)
            after = stamper.get_metrics() if hasattr(stamper, "get_metrics") else {}
            with _oracle_audio_injection_lock:
                _oracle_audio_injection_metrics["running"] = False
                _oracle_audio_injection_metrics["ended_monotonic"] = time.monotonic()
                _oracle_audio_injection_metrics["stamper_audio_chunks_after"] = after.get("audio_chunks")
                _oracle_audio_injection_metrics["stamper_total_chunks_after"] = after.get("total_chunks")
                _oracle_audio_injection_metrics["stamper_rms_after"] = after.get("rms")

    thread = threading.Thread(target=_worker, name="l5-oracle-audio-injection", daemon=True)
    with _oracle_audio_injection_lock:
        _oracle_audio_injection_thread = thread
    thread.start()

    deadline = time.monotonic() + 1.0
    snapshot = _oracle_audio_injection_snapshot()
    while time.monotonic() < deadline:
        snapshot = _oracle_audio_injection_snapshot()
        if int(snapshot.get("chunks_written", 0) or 0) > 0 or snapshot.get("error"):
            break
        time.sleep(0.02)

    gaps = []
    if snapshot.get("error"):
        gaps.append(f"oracle audio injection worker failed: {snapshot.get('error')}")
    if int(snapshot.get("chunks_written", 0) or 0) <= 0:
        gaps.append("oracle audio injection did not write any PCM chunks")
    after_start = stamper.get_metrics() if hasattr(stamper, "get_metrics") else {}
    audio_delta = int(after_start.get("audio_chunks", 0) or 0) - int(before.get("audio_chunks", 0) or 0)
    snapshot.update(
        {
            "ok": not gaps,
            "gaps": gaps,
            "tee_available": True,
            "stamper_available": True,
            "stamper_audio_chunks_after_start": after_start.get("audio_chunks"),
            "stamper_total_chunks_after_start": after_start.get("total_chunks"),
            "stamper_rms_after_start": after_start.get("rms"),
            "stamper_audio_chunks_delta_start": audio_delta,
        }
    )
    return snapshot


def request_proctap_rescan() -> None:
    """Wake ProcTap's PID discovery poll on a new play event.
    This reduces the delay before ProcTap discovers the new audio renderer
    process (Bug #29 Fix 3).
    """
    global _pending_proctap_rescan
    with _source_lifecycle_lock:
        capture = _active_capture
        if capture is not None and hasattr(capture, "request_rescan"):
            capture.request_rescan()
            logger.warning("request_proctap_rescan: called on %s", type(capture).__name__)
        else:
            _pending_proctap_rescan = True
            logger.info(
                "request_proctap_rescan: deferred until source capture demand returns; capture=%s hasattr=%s",
                type(capture).__name__ if capture else "None",
                hasattr(capture, "request_rescan") if capture else "N/A",
            )


def notify_proctap_pid(pid: int) -> None:
    """Directly notify ProcTap of a known audio PID (Bug #29 Fix 5).

    Use when the caller already knows which PID will produce audio
    (e.g. ``os.getpid()`` for local file playback via sounddevice).
    This bypasses PID discovery and avoids wasting time on wrong PIDs.
    """
    global _pending_proctap_pid
    with _source_lifecycle_lock:
        capture = _active_capture
        if capture is not None and hasattr(capture, "notify_pid"):
            capture.notify_pid(pid)
            logger.info("notify_proctap_pid: notified PID %d", pid)
        else:
            _pending_proctap_pid = pid
            logger.info("notify_proctap_pid: deferred PID %d until source capture demand returns", pid)


def setup_source_pipeline(
    app: FastAPI,
    room_id: str,
    user_id: str | None = None,
) -> SourceAudioBroadcaster | None:
    """
    Set up the source-side PCM streaming pipeline.

    Creates an AudioTee and SourceAudioBroadcaster, wires them together, and
    stores them on ``app.state`` for access by other components.

    The AudioTee splits decoded PCM to both local playback and the broadcaster.
    The broadcaster timestamps chunks and sends them to Device devices via
    the EventHub WebSocket layer.

    Args:
        app: FastAPI application instance (must have ``app.state.event_hub``).
        room_id: Room identifier for scoped broadcasts.
        user_id: Optional owner used by user-scoped room broadcasts.

    Returns:
        The started SourceAudioBroadcaster, or None if the event hub is not
        available or setup fails.
    """
    global _pipeline_initialized
    if _pipeline_initialized:
        logger.debug("Source pipeline already initialized; skipping duplicate setup")
        return getattr(app.state, "source_broadcaster", None)

    from .audio_tee import AudioTee
    from .source_broadcaster import SourceAudioBroadcaster

    event_hub = getattr(app.state, "event_hub", None)
    if event_hub is None:
        logger.debug("event_hub not available on app.state; skipping source pipeline setup")
        return None

    try:
        broadcaster = SourceAudioBroadcaster(event_hub, room_id, user_id=user_id)
        tee = AudioTee()

        # Register the broadcaster callback on the tee so every PCM write
        # is forwarded to the broadcaster for timestamping and broadcast.
        tee.add_output(broadcaster.on_pcm_data)

        # Set the event loop so the broadcaster can bridge sync-to-async
        # when broadcasting chunks from the audio decode thread. If we are on
        # a sync thread, fall back to the FastAPI lifespan loop captured by
        # core.asyncio_safe.set_main_loop() (desktop ASYNC-1 fix). Without that
        # fallback the broadcaster silently drops every chunk until something
        # else wires its loop, causing "no event loop; chunks will be dropped"
        # warnings and zero device audio fanout.
        try:
            loop = asyncio.get_running_loop()
            broadcaster.set_event_loop(loop)
        except RuntimeError:
            from core.asyncio_safe import get_main_loop

            captured = get_main_loop()
            if captured is not None and not captured.is_closed():
                broadcaster.set_event_loop(captured)
                logger.info("Source broadcaster wired to captured main loop (sync-thread bootstrap)")
            else:
                logger.warning(
                    "Source broadcaster created with no running loop and no captured main loop; "
                    "chunks will drop until lifespan wires it"
                )

        broadcaster.start()

        # Publish tee at module level so playback backends can find it
        # without needing access to app.state.
        global _active_audio_tee, _active_capture, _app_ref
        _active_audio_tee = tee
        _app_ref = app

        # Store on app.state for other components (e.g. playback engine) to
        # write PCM through the tee, and for teardown on shutdown.
        app.state.audio_tee = tee
        app.state.source_broadcaster = broadcaster

        # --- Binary WebSocket streaming pipeline ---
        # ChunkStamper maintains a continuous 20ms stream (silence-padded).
        # Create it BEFORE the capture provider so the fan-out callback
        # can feed both the AudioTee and the ChunkStamper.
        stamper = None
        try:
            from .chunk_stamper import ChunkStamper

            global _active_chunk_stamper
            stamper = ChunkStamper(bit_depth=24)
            _active_chunk_stamper = stamper
            app.state.chunk_stamper = stamper
        except Exception:
            logger.exception("Failed to create ChunkStamper")

        # Create system audio capture and wire it into the tee (and stamper).
        # The capture provider taps the source's audio output (WASAPI loopback
        # on Windows, PulseAudio monitor on Linux, or a test tone for CI)
        # and feeds PCM into the tee for broadcast to devices.
        # Local playback is NOT affected --- the system is already playing
        # audio to speakers; the capture just duplicates the stream.
        #
        # The provider is deliberately not started here.  The source capture
        # pipeline is demand-driven: it starts when at least one spoke or an
        # explicit recording session can consume it.
        try:
            from audio_core.capture import get_capture_provider

            capture = get_capture_provider()

            # Fan-out: capture callback feeds both the AudioTee (JSON path)
            # and the ChunkStamper (binary WS path).
            #
            # Gain compensation: _pipeline_gain stays 1.0 when no devices
            # are connected (provider at full volume).  When devices
            # connect, provider is JS-muted to 1% and _pipeline_gain
            # is set to 100x to compensate.
            _stamper_ref = stamper  # close over for callback
            _stamper_is_24bit = stamper is not None and getattr(stamper, "_bit_depth", 16) == 24

            _noise_diag_counter = [0]  # mutable counter for throttling

            def _capture_fanout(
                data: bytes,
                sr: int,
                ch: int,
                sw: int,
            ) -> None:
                import numpy as _np

                gain = _pipeline_gain  # dynamic module-level variable

                # --- Temporary diagnostic: detect noise during non-playing state ---
                # Log when ProcTap delivers non-trivial audio while nothing is playing.
                # Throttle to 1 log per 50 calls (~1/sec at 50FPS).
                _noise_diag_counter[0] += 1
                if _noise_diag_counter[0] % 50 == 0:
                    try:
                        if sw == 4:
                            _diag_samples = _np.frombuffer(data, dtype=_np.float32)
                        else:
                            _diag_samples = _np.frombuffer(data, dtype=_np.int16).astype(_np.float32) / 32768.0
                        _diag_rms = float(_np.sqrt(_np.mean(_diag_samples**2)))
                        if _diag_rms > 0.001:
                            from core.state_selectors import select_is_playing

                            _diag_playing = select_is_playing()
                            if not _diag_playing:
                                logger.debug(
                                    "NOISE: capture_rms=%.4f while not playing " "(embedded=%s, di=%s, gain=%.1f)",
                                    _diag_rms,
                                    _embedded_source_active,
                                    _direct_injection_active,
                                    gain,
                                )
                    except Exception:  # nosec B110 â€” diagnostic only, safe to suppress
                        logger.debug("Pipeline wiring diagnostic block failed")

                # Skip stamper feed when a backend is injecting directly
                # (prevents dual-source garbling â€” Bug #29 Fix 9).  The
                # TTS-mute disjunct is a legacy guard from the source-local
                # architecture that is always False today; retained so
                # future callers can re-enable a mute path without
                # reshaping this hot loop.
                skip_stamper = _direct_injection_active or _source_local_capture_muted_for_tts

                # --- Per-chunk anomaly detection (runs on EVERY chunk) ---
                # Extract first/last sample for discontinuity tracking and
                # compute max_abs for clipping.  Cost: ~2-5us at 50/sec.
                _fanout_total_chunks[0] += 1
                if sw == 4:
                    # float32 path
                    _anom_samples = _np.frombuffer(data, dtype=_np.float32)
                    _anom_peak = float(_np.max(_np.abs(_anom_samples))) if len(_anom_samples) > 0 else 0.0
                    if _anom_peak > 1.0:
                        _anom_samples = _anom_samples / _anom_peak
                    _anom_first = float(_anom_samples[0]) if len(_anom_samples) > 0 else 0.0
                    # Use [-2] not [-1]: stereo interleaved, so [-1]=R[last]
                    # and [-2]=L[last].  Compare same channel (L) for real
                    # discontinuity detection.
                    _anom_last = float(_anom_samples[-2]) if len(_anom_samples) > 1 else 0.0
                    _anom_abs = _np.abs(_anom_samples)
                    _anom_max = float(_np.max(_anom_abs)) if len(_anom_samples) > 0 else 0.0
                    # RMS from first 100 samples (cheap approximation)
                    _anom_rms_slice = _anom_samples[:100]
                    _anom_rms = float(_np.sqrt(_np.mean(_anom_rms_slice**2))) if len(_anom_rms_slice) > 0 else 0.0
                    # NaN check (float32 only)
                    if _np.any(_np.isnan(_anom_samples)):
                        _fanout_nan_count[0] += 1
                    # Clipping check
                    if _anom_max > 0.99:
                        _fanout_clipping_count[0] += 1
                else:
                    # int16 path â€” extract first/last via struct for speed
                    _anom_first = (
                        float(_np.frombuffer(data[:2], dtype=_np.int16)[0]) / 32768.0 if len(data) >= 2 else 0.0
                    )
                    # Use [-4:-2] not [-2:]: stereo interleaved int16,
                    # last 2 bytes = R[last], bytes -4:-2 = L[last].
                    _anom_last = (
                        float(_np.frombuffer(data[-4:-2], dtype=_np.int16)[0]) / 32768.0 if len(data) >= 4 else 0.0
                    )
                    _anom_i16 = _np.frombuffer(data, dtype=_np.int16)
                    _anom_abs_i16 = _np.abs(_anom_i16)
                    _anom_max = float(_np.max(_anom_abs_i16)) / 32768.0 if len(_anom_i16) > 0 else 0.0
                    # RMS from first 100 int16 samples
                    _anom_rms_i16 = _anom_i16[:100].astype(_np.float32) / 32768.0
                    _anom_rms = float(_np.sqrt(_np.mean(_anom_rms_i16**2))) if len(_anom_rms_i16) > 0 else 0.0
                    # Clipping check (0.99 * 32768 ~ 32440)
                    if len(_anom_i16) > 0 and int(_np.max(_anom_abs_i16)) > 32440:
                        _fanout_clipping_count[0] += 1

                # Discontinuity: compare first sample of this chunk to
                # last sample of previous chunk.
                if _fanout_total_chunks[0] > 1:
                    if abs(_anom_first - _fanout_prev_last_sample[0]) > 0.3:
                        _fanout_discontinuity_count[0] += 1
                _fanout_prev_last_sample[0] = _anom_last

                # Silence-to-audio transition detection (potential DC click)
                _anom_is_silence = _anom_rms < 0.001
                _anom_is_audio = _anom_rms > 0.01
                if _fanout_prev_rms_was_silence[0] and _anom_is_audio:
                    _fanout_silence_to_audio_transitions[0] += 1
                _fanout_prev_rms_was_silence[0] = _anom_is_silence

                # --- PCM timeline ring buffer entry ---
                _pcm_timeline.append(
                    {
                        "t": time.monotonic(),
                        "rms": round(_anom_rms, 6),
                        "max": round(_anom_max, 6),
                        "first": round(_anom_first, 6),
                        "last": round(_anom_last, 6),
                        "gain": gain,
                        "emb": _embedded_source_active,
                        "di": _direct_injection_active,
                        "skip": skip_stamper,
                    }
                )

                # --- Fast path: no gain + int16 input = pass-through ---
                if gain == 1.0 and sw == 2:
                    tee.write(data)
                    if _stamper_ref is not None and not skip_stamper:
                        if _stamper_is_24bit:
                            from audio_core.streaming.chunk_protocol import (
                                pack_float32_to_int24,
                            )

                            norm = _np.frombuffer(data, dtype=_np.int16).astype(_np.float32) / 32768.0
                            stamper_data = pack_float32_to_int24(norm)
                            _stamper_ref.on_capture_data(stamper_data, sr, ch, 3)
                        else:
                            _stamper_ref.on_capture_data(data, sr, ch, sw)
                    return

                # --- Normalize to float32 [-1, 1] for all processing ---
                if sw == 4:
                    # float32 from WASAPI â€” should be in [-1, 1] but can exceed
                    # on hot masters or certain Windows audio drivers.
                    samples = _np.frombuffer(data, dtype=_np.float32).copy()
                    # Soft peak limiter: normalise if any sample is out of range.
                    # Hard-clip in pack_float32_to_int24 causes distortion bursts;
                    # this preserves waveform shape with zero audible penalty.
                    _f32_peak = float(_np.max(_np.abs(samples)))
                    if _f32_peak > 1.0:
                        samples = samples / _f32_peak
                else:
                    # int16 â€” normalize to [-1, 1]
                    samples = _np.frombuffer(data, dtype=_np.int16).astype(_np.float32) / 32768.0

                # --- Gain stage (all math in [-1, 1] range) ---
                if gain != 1.0:
                    raw_peak = float(_np.max(_np.abs(samples)))
                    samples *= gain
                    gained_peak = float(_np.max(_np.abs(samples)))
                    clip_count = int(_np.sum(samples > 1.0) + _np.sum(samples < -1.0))
                    # Peak limiter: scale down if any sample exceeds [-1, 1].
                    # Preserves waveform shape (no hard-clip distortion).
                    normalizer_scale = 1.0
                    if gained_peak > 1.0:
                        normalizer_scale = 1.0 / gained_peak
                        samples *= normalizer_scale
                    # Push gain metrics to stamper (scale to int16 range for display compat)
                    if _stamper_ref is not None:
                        _stamper_ref.update_gain_metrics(
                            raw_peak * 32768.0,
                            gained_peak * 32768.0,
                            clip_count,
                            normalizer_scale,
                        )

                # --- AudioTee always gets int16 for backwards compat ---
                amplified = _np.clip(samples * 32768.0, -32768, 32767).astype(_np.int16).tobytes()
                tee.write(amplified)

                # --- Feed stamper: normalized float32 -> int24 ---
                if _stamper_ref is not None and not skip_stamper:
                    if _stamper_is_24bit:
                        from audio_core.streaming.chunk_protocol import (
                            pack_float32_to_int24,
                        )

                        stamper_data = pack_float32_to_int24(samples)
                        _stamper_ref.on_capture_data(stamper_data, sr, ch, 3)
                    else:
                        _stamper_ref.on_capture_data(amplified, sr, ch, 2)

            capture.set_callback(_capture_fanout)
            app.state.source_capture_provider = capture
            _set_capture_health("not_started", provider=type(capture).__name__)
            logger.info(
                "System audio capture ready on demand: provider=%s",
                type(capture).__name__,
            )
        except Exception as exc:
            logger.exception(
                "System audio capture unavailable; source will rely on " "playback backend writing to AudioTee directly"
            )
            # Surface the failure so /health/details can report it
            # instead of silently degrading to "source runs fine, devices
            # hear silence forever".  The source pipeline continues â€” direct
            # injection by playback backends is still a valid path â€”
            # but the state flips to "failed" for operators.
            _set_capture_health("failed", error=str(exc))

        # Register the binary WS endpoint.  The ChunkStamper thread is also
        # demand-driven; the stopped object remains registered so the first
        # spoke can activate it through the stream manager callback.
        if stamper is not None:
            try:
                from ui.api.routes.audio_stream import register_audio_stream_ws

                stream_manager = register_audio_stream_ws(app, stamper)
                app.state.audio_stream_manager = stream_manager

                logger.info("Binary audio stream pipeline registered " "(ChunkStamper + /ws/audio-stream)")
            except Exception:
                logger.exception("Failed to register binary audio stream pipeline")

        # Wire device-count callback for tracking (no source-local lifecycle).
        if stamper is not None:
            stream_mgr: AudioStreamManager | None = getattr(app.state, "audio_stream_manager", None)
            if stream_mgr is not None:

                def _device_cb(count: int) -> None:
                    _on_device_count_changed(app, stamper, count)

                callback_setter = getattr(stream_mgr, "set_spoke_change_callback", None)
                if not callable(callback_setter):
                    callback_setter = getattr(stream_mgr, "set_device_change_callback", None)

                if callable(callback_setter):
                    callback_setter(_device_cb)
                    logger.info("Device count tracking wired")
                else:
                    logger.warning(
                        "Audio stream manager has no spoke/device change callback; " "device count tracking disabled"
                    )

        _sync_source_pipeline_demand(app, reason="source-setup")

        # Register pipeline diagnostics endpoint (Layers 1-3)
        try:
            _register_pipeline_debug_endpoint(app)
        except Exception:
            logger.exception("Failed to register pipeline debug endpoint (non-fatal)")

        # Register subscribe_room command so remote devices can join the room
        _register_subscribe_room_handler(app)

        _pipeline_initialized = True
        logger.info(
            "Source PCM streaming pipeline wired: room_id=%s",
            room_id,
        )
        return broadcaster

    except Exception:
        logger.exception("Failed to setup source PCM streaming pipeline")
        return None


def _on_device_count_changed(
    app: FastAPI,
    stamper: ChunkStamper,
    count: int,
) -> None:
    """Track device count and keep source capture aligned to demand."""
    del stamper
    global _device_count, _spoke_count
    with _source_lifecycle_lock:
        _device_count = count
        _spoke_count = count
        logger.info("Device count changed: %d", count)
        _sync_source_pipeline_demand(app, reason="spoke-count")


# Maximum acceptable round-trip for a clock probe.  Probes slower than
# this are discarded because their midpoint-based offset estimate is
# dominated by RTT asymmetry.  Set generous (1 s) because the first
# probe in a burst pays DNS + TCP + TLS setup cost; the JS side uses
# a tighter filter with an adaptive baseline.
_CLOCK_PROBE_MAX_RTT_S: float = 1.0


def _initial_clock_sync(source_host: str, source_port: int) -> float:
    """Perform initial clock synchronization with the source.

    Fetches the source's monotonic clock up to 5 times, 200 ms apart,
    computes the offset between Source time and local time for each
    qualified sample, and returns the median offset.

    Failure-mode behaviour:
        * 3+ qualified samples â€” happy path, logged at INFO.
        * 1-2 qualified samples â€” *degraded* but usable.  Returns the
          median of whatever we got and logs WARN.  This avoids the
          silent-0.0 failure where a flaky or high-RTT network left
          the device playing with a wrong clock offset, producing
          glitches until ongoing sync converged.
        * 0 qualified samples â€” true failure.  Returns 0.0 with an
          ERROR log.  The device will still start playing; the ongoing
          WebSocket-based sync loop is expected to converge.

    Args:
        source_host: Source host address.
        source_port: Source HTTP port.

    Returns:
        Median source_time_offset (source_time = local_time + offset).  0.0
        only when no probe qualified.
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not available; skipping initial clock sync")
        return 0.0

    clock_url = "http://%s:%d/api/v1/clock" % (source_host, source_port)
    offsets: list[float] = []
    rejected_high_rtt = 0
    failed = 0
    num_probes = 5
    probe_interval = 0.2  # 200 ms between probes

    for i in range(num_probes):
        try:
            local_before = time.monotonic()
            with httpx.Client(timeout=TIMEOUT_MEDIUM) as client:
                response = client.get(clock_url)
                response.raise_for_status()
            local_after = time.monotonic()

            rtt = local_after - local_before
            if rtt > _CLOCK_PROBE_MAX_RTT_S:
                logger.warning(
                    "Clock probe %d discarded: rtt=%.1fms > %.0fms cap",
                    i + 1,
                    rtt * 1000.0,
                    _CLOCK_PROBE_MAX_RTT_S * 1000.0,
                )
                rejected_high_rtt += 1
                continue

            midpoint = (local_before + local_after) / 2.0
            data = response.json()

            # Extract source_time from response: check both data.source_time
            # and top-level source_time for compatibility.
            hub_data = data
            if isinstance(data, dict) and "data" in data:
                hub_data = data["data"]
            source_time = hub_data.get("source_time") if isinstance(hub_data, dict) else None

            if source_time is None:
                logger.warning("Clock probe %d: response missing source_time", i + 1)
                failed += 1
                continue

            offset = float(source_time) - midpoint
            offsets.append(offset)
            logger.debug(
                "Clock probe %d: offset=%.6fs, rtt=%.1fms",
                i + 1,
                offset,
                rtt * 1000.0,
            )
        except Exception:
            logger.exception("Clock probe %d failed", i + 1)
            failed += 1

        # Sleep between probes (skip after the last one)
        if i < num_probes - 1:
            time.sleep(probe_interval)

    if not offsets:
        # All probes failed or were discarded.  The device will start
        # with offset=0.0 and is expected to converge via the ongoing
        # WebSocket-based sync loop.  Logged at ERROR so this shows up
        # in health dashboards rather than hiding in DEBUG.
        logger.error(
            "Clock sync failed: 0/%d probes qualified "
            "(failed=%d, rejected_high_rtt=%d); device starting with "
            "offset=0.0 â€” ongoing sync must recover.",
            num_probes,
            failed,
            rejected_high_rtt,
        )
        return 0.0

    median_offset = statistics.median(offsets)
    # 3+ probes is the happy path; 1-2 is degraded but usable.  We
    # prefer degraded sync over silent-0.0 because the drift corrector
    # can absorb residual error without audible artefacts.
    is_high_confidence = len(offsets) >= 3
    log_fn = logger.info if is_high_confidence else logger.warning
    log_fn(
        "Device clock sync: source_time_offset=%.6fs from %d/%d samples "
        "(%s confidence; rejected_high_rtt=%d, failed=%d)",
        median_offset,
        len(offsets),
        num_probes,
        "high" if is_high_confidence else "low",
        rejected_high_rtt,
        failed,
    )
    return median_offset


def setup_device_pipeline(
    app: FastAPI,
    room_id: str,
    source_host: str = "127.0.0.1",
    source_port: int = DEFAULT_API_PORT,
) -> DeviceAudioReceiver | None:
    """
    Set up the Device-side PCM streaming pipeline.

    Creates a PlaybackScheduler and DeviceAudioReceiver, wires them together,
    and stores them on ``app.state``.  Also starts a background playback loop
    that drains due chunks from the scheduler into the audio output driver,
    and registers a ``pcm_chunk`` command handler on the EventHub so that
    incoming WebSocket messages are routed to the receiver.

    Performs initial clock synchronization with the source via HTTP clock probes,
    then starts a SyncEngine for ongoing drift correction.

    Args:
        app: FastAPI application instance.
        room_id: Room identifier (reserved for future per-room receiver config).
        source_host: Source host address for clock sync (default: "127.0.0.1").
        source_port: Source HTTP port for clock sync (default: DEFAULT_API_PORT).

    Returns:
        The started DeviceAudioReceiver, or None if setup fails.
    """
    from audio_core.output import get_output_driver

    # The playback-loop and receiver classes live under their original "Spoke"
    # names because the headless device IS the spoke in the multiroom topology.
    # The aliases here keep the locally-readable "Device" vocabulary inside
    # this function body while resolving to the canonical class objects.
    # The real-execution test in tests/unit/spoke/test_spoke_integration.py
    # (TestSetupDevicePipelineExecutes) locks these aliases in as a gate.
    from audio_core.output.playback_loop import SpokePlaybackLoop as DevicePlaybackLoop
    from audio_core.sync_engine import SyncEngine

    from .chunk_protocol import BYTES_PER_SAMPLE, CHANNELS, SAMPLE_RATE
    from .playback_scheduler import PlaybackScheduler
    from .spoke_receiver import SpokeAudioReceiver as DeviceAudioReceiver

    try:
        scheduler = PlaybackScheduler(late_policy=None)
        receiver = DeviceAudioReceiver(scheduler)
        receiver.start()

        # -- Clock synchronization --
        # Perform initial multi-probe clock sync to compute the source-time offset.
        offset = _initial_clock_sync(source_host, source_port)
        scheduler.set_hub_time_offset(offset)

        # Start persistent SyncEngine for ongoing drift correction.
        # The engine periodically re-fetches the Source clock and updates its
        # internal offset.  We subscribe to its SyncPulse events to forward
        # updated offsets to the PlaybackScheduler.
        from core.events.bus import LocalEventBus

        event_bus = getattr(app.state, "event_bus", None)
        if event_bus is None:
            event_bus = LocalEventBus()
            logger.debug("No event_bus on app.state; created LocalEventBus for device sync")

        clock_url = "http://%s:%d/api/v1/clock" % (source_host, source_port)
        sync_engine = SyncEngine(hub_clock_url=clock_url, event_bus=event_bus)

        # Wire sync engine offset updates to the scheduler: subscribe to
        # SyncPulse events and forward the latest source-time offset whenever
        # the engine re-syncs.
        from audio_core.events import SyncPulse

        def _on_sync_pulse(_event: SyncPulse) -> None:
            """Forward updated offset from SyncEngine to PlaybackScheduler."""
            state = sync_engine.get_state()
            scheduler.set_hub_time_offset(state.hub_time_offset)

        event_bus.subscribe(SyncPulse, _on_sync_pulse)

        sync_engine.start()
        app.state.device_sync_engine = sync_engine

        # Create and start the audio output driver + playback loop
        driver = get_output_driver()
        output_fallback_reason = getattr(driver, "fallback_reason", None)
        if output_fallback_reason:
            logger.warning(
                "Device audio output driver is a SILENT FALLBACK: provider=%s "
                "fallback_reason=%s -- PCM will be discarded, playback will "
                "report success but produce NO SOUND",
                type(driver).__name__,
                output_fallback_reason,
            )
        _set_output_health(
            "degraded" if output_fallback_reason else "ok",
            provider=type(driver).__name__,
            fallback_reason=output_fallback_reason,
        )
        driver.start(
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            sample_width=BYTES_PER_SAMPLE,
        )
        playback_loop = DevicePlaybackLoop(scheduler, driver)
        playback_loop.start()

        app.state.device_receiver = receiver
        app.state.playback_scheduler = scheduler
        app.state.device_output_driver = driver
        app.state.device_playback_loop = playback_loop

        # NOTE: We DO NOT register a pcm_chunk command handler on the EventHub.
        # The only legitimate inbound source for pcm_chunk frames is the
        # outbound _device_ws_bridge connection that this spoke itself dials
        # to the trusted hub.  An EventHub command handler would accept
        # {"action": "pcm_chunk", ...} from ANY authenticated /ws/events
        # client (including any other paired spoke holding a valid HMAC
        # token), letting an unrelated client inject audio out the
        # speakers - see Round 1 Condition-1 F-2 (2026-05-29).  The bridge
        # path (pipeline_wiring._device_ws_bridge) filters
        # msg["type"] == "pcm_chunk" inside its dedicated trusted-source
        # connection and calls receiver.on_websocket_message directly; no
        # EventHub command surface is needed.

        logger.info(
            "Device PCM streaming pipeline wired: room_id=%s, source=%s:%d, offset=%.6fs",
            room_id,
            source_host,
            source_port,
            offset,
        )

        # Tier A: Auto-detect device output latency
        try:
            from audio_core.calibration.device_latency import detect_output_latency

            latency_info = detect_output_latency()
            latency_ms = latency_info.get("latency_ms", 0.0)
            if latency_ms > 0:
                # Store for calibration API
                app.state.auto_detected_latency = latency_info
                logger.info(
                    "Auto-detected output latency: %.1fms (method=%s, device=%s)",
                    latency_ms,
                    latency_info.get("method", "unknown"),
                    latency_info.get("device_name", "unknown"),
                )
        except Exception:
            logger.exception("Tier A latency auto-detection failed (non-fatal)")

        return receiver

    except Exception:
        logger.exception("Failed to setup device PCM streaming pipeline")
        return None


def probe_active_chunk_stamper_aec_reference(
    *,
    frame_samples: int = 160,
    target_rate: int = 16000,
    timeout_sec: float = 1.5,
) -> dict[str, Any]:
    """Probe AEC reference frames from the running app-owned ChunkStamper."""
    import numpy as np

    from .chunk_stamper_aec_adapter import ChunkStamperAECAdapter

    gaps: list[str] = []
    adapter: ChunkStamperAECAdapter | None = None
    stamper = _active_chunk_stamper
    evidence: dict[str, Any] = {
        "ok": False,
        "stamper_source": "audio_core.streaming.pipeline_wiring._active_chunk_stamper",
        "app_owned_stamper": stamper is not None,
        "self_instantiated_stamper": False,
        "capture_callback": "ChunkStamper.on_capture_data",
        "direct_injection_active": _direct_injection_active,
        "capture_health": get_capture_health(),
        "fanout": get_fanout_diagnostics(),
    }
    if stamper is None:
        evidence["gaps"] = ["active app-owned ChunkStamper is not available"]
        return evidence

    try:
        before = stamper.get_metrics()
        adapter = ChunkStamperAECAdapter(stamper, buffer_target_sec=0.005)
        started = adapter.start()
        evidence.update(
            {
                "started": started,
                "stamper_class": type(stamper).__name__,
                "adapter_class": type(adapter).__name__,
                "stamper_bit_depth": before.get("bit_depth"),
                "stamper_audio_chunks_before": before.get("audio_chunks"),
                "stamper_total_chunks_before": before.get("total_chunks"),
                "stamper_rms_before": before.get("rms"),
            }
        )
        if not started:
            gaps.append("ChunkStamperAECAdapter did not start against app-owned ChunkStamper")
            evidence["gaps"] = gaps
            return evidence

        before_audio = int(before.get("audio_chunks", 0) or 0)
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            current = stamper.get_metrics()
            audio_delta = int(current.get("audio_chunks", 0) or 0) - before_audio
            if audio_delta > 0 and adapter.has_sufficient_reference(frame_samples):
                break
            time.sleep(0.02)

        fill_before = adapter.get_buffer_fill_level()
        frame = adapter.get_aec_reference_frame(frame_samples, target_rate)
        recent = adapter.get_recent_reference(frame_samples * 2)
        fill_after = adapter.get_buffer_fill_level()
        after = stamper.get_metrics()

        frame_rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2))) if len(frame) else 0.0
        recent_rms = float(np.sqrt(np.mean(recent.astype(np.float32) ** 2))) if len(recent) else 0.0
        audio_delta = int(after.get("audio_chunks", 0) or 0) - before_audio
        evidence.update(
            {
                "fill_before": list(fill_before),
                "fill_after": list(fill_after),
                "frame_samples": len(frame),
                "frame_rms": round(frame_rms, 2),
                "recent_samples": len(recent),
                "recent_rms": round(recent_rms, 2),
                "has_reference_before_stop": adapter.has_aec_reference(),
                "stamper_audio_chunks_after": after.get("audio_chunks"),
                "stamper_total_chunks_after": after.get("total_chunks"),
                "stamper_audio_chunks_delta": audio_delta,
                "stamper_sequence": after.get("sequence"),
                "stamper_rms_after": after.get("rms"),
            }
        )

        if type(stamper).__name__ != "ChunkStamper":
            gaps.append(f"AEC probe used {type(stamper).__name__}, not ChunkStamper")
        if before.get("bit_depth") != 24:
            gaps.append(
                f"AEC probe did not use production 24-bit ChunkStamper path; bit_depth={before.get('bit_depth')}"
            )
        if audio_delta <= 0:
            gaps.append("app-owned ChunkStamper did not produce fresh non-silent capture PCM during AEC probe")
        if len(frame) != frame_samples:
            gaps.append(f"AEC frame length was {len(frame)}, expected {frame_samples}")
        if frame_rms <= 1.0:
            gaps.append(f"AEC reference frame was silent/near-silent, rms={frame_rms}")
        if fill_before[0] <= 0:
            gaps.append("AEC adapter did not buffer stamped PCM from the app-owned ChunkStamper")
    except Exception as exc:
        gaps.append(f"AEC app-owned probe failed: {exc}")
    finally:
        if adapter is not None:
            adapter.stop()

    evidence["gaps"] = gaps
    evidence["ok"] = not gaps
    return evidence


def _register_pipeline_debug_endpoint(app: FastAPI) -> None:
    """Register GET /api/v1/debug/audio-pipeline for per-layer diagnostics."""

    @app.get("/api/v1/debug/audio-pipeline")
    async def audio_pipeline_debug():
        capture = getattr(app.state, "source_capture_provider", None)
        stamper = getattr(app.state, "chunk_stamper", None)
        manager = getattr(app.state, "audio_stream_manager", None)

        cap_m = capture.get_metrics() if capture and hasattr(capture, "get_metrics") else {}
        stmp_m = stamper.get_metrics() if stamper else {}
        ws_m = manager.get_metrics() if manager else {}

        return success_response(
            {
                "capture": {
                    "rms": cap_m.get("rms", 0),
                    "provider": cap_m.get("provider", "unknown"),
                    "chunks_produced": cap_m.get("chunks_produced", 0),
                },
                "stamper": {
                    "rms": stmp_m.get("rms", 0),
                    "frames_stamped": stmp_m.get("total_chunks", 0),
                    "silence_frames": stmp_m.get("silence_chunks", 0),
                    "capture_trims": stmp_m.get("capture_trims", 0),
                    "capture_trim_unaligned_count": stmp_m.get("capture_trim_unaligned_count", 0),
                    "capture_buffer_bytes": stmp_m.get("capture_buffer_bytes", 0),
                    "tick_jitter_avg_ms": stmp_m.get("tick_jitter_avg_ms", 0.0),
                    "tick_jitter_max_ms": stmp_m.get("tick_jitter_max_ms", 0.0),
                    "gain_clip_pct": stmp_m.get("gain_clip_pct", 0.0),
                },
                "fanout": get_fanout_diagnostics(),
                "websocket": {
                    "connected_devices": ws_m.get("connected_spokes", 0),
                    "frames_sent": ws_m.get("frames_broadcast", 0),
                    "frames_per_sec": ws_m.get("frames_per_sec", 0),
                },
                "muting": {
                    "sounddevice_muted": _sounddevice_muted,
                    "pipeline_gain": _pipeline_gain,
                    "direct_injection_active": _direct_injection_active,
                    "embedded_source_active": _embedded_source_active,
                    "source_local_active": _source_local_active,
                    "device_count": _device_count,
                },
            }
        )

    @app.get("/api/v1/debug/aec-reference")
    async def aec_reference_debug():
        return success_response(await asyncio.to_thread(probe_active_chunk_stamper_aec_reference))

    @app.post("/api/v1/debug/oracle-audio-injection")
    async def oracle_audio_injection(request: Request, body: dict[str, Any] | None = None):
        from fastapi.responses import JSONResponse

        client_host = extract_client_ip(request) or ""
        if client_host not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse(
                status_code=403,
                content=failure_response(
                    "oracle_audio_injection_local_only",
                    "Oracle audio injection is available only from localhost.",
                    details={"client_host": client_host},
                ),
            )

        payload = body or {}
        result = await asyncio.to_thread(
            start_oracle_audio_injection,
            duration_sec=float(payload.get("duration_sec", 90.0)),
            frequency_hz=float(payload.get("frequency_hz", 997.0)),
            amplitude=float(payload.get("amplitude", 0.22)),
        )
        return success_response(result)

    logger.info("Pipeline debug endpoint registered at /api/v1/debug/audio-pipeline")

    _register_capture_timeline_endpoint(app)
    _register_sync_offset_endpoint(app)


def _register_capture_timeline_endpoint(app: FastAPI) -> None:
    """Register GET /api/v1/debug/capture-timeline for per-chunk PCM metrics.

    Returns the last N entries from the capture ring buffer (30 seconds
    at 50 chunks/sec = 1500 entries max).  Each entry has rms, max,
    first/last sample, gain, and pipeline flags.
    """
    from fastapi import Query as _Query

    @app.get("/api/v1/debug/capture-timeline")
    async def capture_timeline(
        last: int = _Query(default=10, ge=1, le=1500, description="Number of entries to return"),
    ):
        entries = get_capture_timeline(last_n=last)
        return success_response(
            {
                "count": len(entries),
                "entries": entries,
                "fanout": get_fanout_diagnostics(),
            }
        )

    logger.info("Capture timeline endpoint registered at /api/v1/debug/capture-timeline")


def _register_sync_offset_endpoint(app: FastAPI) -> None:
    """Register GET /api/v1/debug/sync-offset for empirical sync measurement.

    The device sends its last N play timestamps (converted to source-clock time).
    The source matches chunk sequence numbers and computes the median offset
    between when the source played each chunk and when the device scheduled it.

    A positive offset means the device is scheduled LATER (behind source).
    A negative offset means the device is AHEAD of the source.
    """
    import json as _json  # noqa: I001

    from fastapi import Query
    from fastapi.responses import JSONResponse

    @app.get("/api/v1/debug/sync-offset")
    async def sync_offset(
        device_timestamps: str = Query(
            ...,
            description="JSON array of {seq, scheduledSourceTime} objects",
        ),
    ):
        # Parse device timestamps
        try:
            device_entries = _json.loads(device_timestamps)
        except (ValueError, TypeError):
            return JSONResponse(
                {"error": "Invalid JSON in device_timestamps"},
                status_code=400,
            )

        if not isinstance(device_entries, list) or not device_entries:
            return JSONResponse(
                {"error": "device_timestamps must be a non-empty JSON array"},
                status_code=400,
            )

        # Get source play timestamps
        source_player = getattr(app.state, "source_local_player", None)
        if source_player is None:
            return JSONResponse(
                {"error": "Source local playback not active"},
                status_code=503,
            )

        source_m = source_player.get_metrics()
        source_ts_list = source_m.get("source_play_timestamps", [])

        # Index source timestamps by sequence number
        source_by_seq: dict[int, float] = {entry["seq"]: entry["played_at"] for entry in source_ts_list}

        # Match and compute offsets
        offsets = []
        for entry in device_entries:
            seq = entry.get("seq")
            device_time = entry.get("scheduledSourceTime")
            if seq is None or device_time is None:
                continue
            source_time = source_by_seq.get(seq)
            if source_time is not None:
                # offset = device_scheduled - source_played
                # Negative means device is AHEAD (plays before source)
                offsets.append(device_time - source_time)

        if not offsets:
            return JSONResponse(
                {
                    "offset_ms": 0,
                    "samples": 0,
                    "confidence": "low",
                    "detail": "No matching sequence numbers",
                }
            )

        median_offset = statistics.median(offsets)
        std_dev = statistics.stdev(offsets) if len(offsets) > 1 else 0.0
        confidence = "high" if len(offsets) >= 10 and std_dev < 0.010 else "low"

        return JSONResponse(
            {
                "offset_ms": round(median_offset * 1000, 2),
                "samples": len(offsets),
                "std_dev_ms": round(std_dev * 1000, 2),
                "confidence": confidence,
            }
        )

    logger.info("Sync offset endpoint registered at /api/v1/debug/sync-offset")


def _register_subscribe_room_handler(app: FastAPI) -> None:
    """Register a ``subscribe_room`` command on the EventHub.

    Remote device clients send ``{"action": "subscribe_room", "payload": {"room_id": "..."}}``
    after connecting to the source's WebSocket.  This subscribes them to
    room-scoped broadcasts (including ``pcm_chunk``).
    """
    event_hub = getattr(app.state, "event_hub", None)
    if event_hub is None:
        logger.warning("event_hub not available; subscribe_room handler not registered")
        return

    async def _handler(action: str, payload: dict[str, Any], ws: Any) -> None:
        room_id = payload.get("room_id")
        if room_id:
            await event_hub.subscribe_to_room(ws, room_id)
            logger.info("WebSocket client subscribed to room %s via command", room_id)

    event_hub.register_command_handler("subscribe_room", _handler)
    logger.info("Registered subscribe_room command handler on EventHub")


async def _device_ws_bridge(
    app: FastAPI,
    source_host: str,
    source_port: int,
    room_id: str,
    spoke_token: str | None = None,
) -> None:
    """Connect to the source WebSocket and forward pcm_chunk messages to the device receiver.

    ``spoke_token``: when set, sent as the ``X-Spoke-Token`` HMAC header on the
    outbound WebSocket handshake.  Without it, an auth-enabled hub closes the
    connection with code 1008 (Round 1 Condition-1 F-3, 2026-05-29) and path-B
    is effectively dead on any non-loopback / non-auth-disabled hub.  The
    spoke already holds the credential in ``VIOLA_SPOKE_TOKEN`` for its
    outbound MicStreamer channel; this threads the same credential into the
    PCM downlink so the bridge can subscribe to the hub's room over an
    authenticated WS.
    """
    import json

    import websockets

    receiver = getattr(app.state, "device_receiver", None)
    if receiver is None:
        logger.error("device_ws_bridge: no device_receiver on app.state")
        return

    ws_url = "ws://%s:%d/ws/events" % (source_host, source_port)
    # The hub gates /ws/events with an Origin/CSRF check
    # (ui/core/security.check_websocket_origin). This bridge is a non-browser
    # client, so it gets no Origin for free the way a browser spoke does -- and
    # a no-Origin handshake from a non-loopback LAN peer is rejected (1008)
    # before any pcm_chunk arrives. Present an Origin identifying the hub we are
    # dialing; check_websocket_origin accepts a private-IP origin on the hub's
    # api_port (the LAN spoke trust model).
    origin = "http://%s:%d" % (source_host, source_port)
    auth_headers: list[tuple[str, str]] = [("Origin", origin)]
    if spoke_token:
        # When the hub has auth enabled it accepts the spoke HMAC via this
        # header; without it the hub responds 1008 and tears the connection
        # down before any pcm_chunk arrives.
        auth_headers.append(("X-Spoke-Token", spoke_token))
    logger.info(
        "Device WS bridge connecting to %s (room=%s, authed=%s)",
        ws_url,
        room_id,
        bool(spoke_token),
    )

    try:
        async with websockets.connect(ws_url, additional_headers=auth_headers) as ws:
            # Subscribe to the source room for pcm_chunk broadcasts
            await ws.send(
                json.dumps(
                    {
                        "action": "subscribe_room",
                        "payload": {"room_id": room_id},
                    }
                )
            )
            logger.info("Device WS bridge subscribed to source room %s", room_id)

            async for raw_msg in ws:
                try:
                    msg = json.loads(raw_msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                if msg.get("type") == "pcm_chunk":
                    receiver.on_websocket_message(msg.get("payload", {}))
    except asyncio.CancelledError:
        logger.info("Device WS bridge cancelled")
    except Exception:
        logger.exception("Device WS bridge connection error")


def start_device_ws_client(
    app: FastAPI,
    source_host: str,
    source_port: int,
    room_id: str,
    spoke_token: str | None = None,
) -> None:
    """Start the background WebSocket client bridging source chunks to the device receiver.

    ``spoke_token`` is threaded into ``_device_ws_bridge`` so the outbound
    handshake to the hub's ``/ws/events`` can present an ``X-Spoke-Token``
    header.  Without it the connection is rejected with code 1008 on any
    auth-enabled hub (Round 1 Condition-1 F-3, 2026-05-29).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("No running event loop; cannot start device WS bridge")
        return

    task = loop.create_task(_device_ws_bridge(app, source_host, source_port, room_id, spoke_token=spoke_token))
    app.state.device_ws_bridge_task = task
    logger.info(
        "Device WS bridge task started for %s:%s room=%s",
        source_host,
        source_port,
        room_id,
    )


# Backward-compatible spoke aliases.  Browser/Pi spoke code and tests use the
# spoke names; the implementation was renamed to "device" when the multiroom
# pipeline terminology changed.
setup_spoke_pipeline = setup_device_pipeline
start_spoke_ws_client = start_device_ws_client


def teardown_pipeline(app: FastAPI) -> None:
    """
    Tear down the PCM streaming pipeline (both source and device sides).

    Stops the broadcaster and receiver if they exist and clears the
    corresponding ``app.state`` references.  Safe to call even if only
    one side (or neither) was initialised.

    Args:
        app: FastAPI application instance.
    """
    global _active_capture, _active_chunk_stamper, _active_audio_tee, _app_ref
    global _pipeline_initialized, _source_local_active, _pipeline_gain
    global _direct_injection_active, _sounddevice_muted, _device_count, _spoke_count
    global _recording_active, _pending_proctap_pid, _pending_proctap_rescan

    stop_oracle_audio_injection(timeout_sec=1.0)

    # Cancel device WS bridge task if running
    ws_task = getattr(app.state, "device_ws_bridge_task", None)
    if ws_task is not None and not ws_task.done():
        ws_task.cancel()
        logger.info("Device WS bridge task cancelled")
    app.state.device_ws_bridge_task = None

    # Stop device sync engine
    sync_engine: SyncEngine | None = getattr(app.state, "device_sync_engine", None)
    if sync_engine is not None:
        try:
            sync_engine.stop()
            logger.info("Device sync engine stopped")
        except Exception:
            logger.exception("Error stopping device sync engine")
        app.state.device_sync_engine = None

    # Stop capture provider first (it feeds the tee and stamper)
    capture = getattr(app.state, "source_capture_provider", None)
    if capture is not None:
        try:
            capture.stop()
            logger.info("Source capture provider stopped")
        except Exception:
            logger.exception("Error stopping source capture provider")
        app.state.source_capture_provider = None
    _active_capture = None

    # Stop chunk stamper (binary WS pipeline)
    stamper = getattr(app.state, "chunk_stamper", None)
    if stamper is not None:
        try:
            stamper.stop()
            _clear_stamper_runtime_buffers(stamper)
            logger.info("ChunkStamper stopped")
        except Exception:
            logger.exception("Error stopping ChunkStamper")
        app.state.chunk_stamper = None
    _active_chunk_stamper = None

    # Clear audio stream manager reference
    stream_mgr = getattr(app.state, "audio_stream_manager", None)
    if stream_mgr is not None:
        app.state.audio_stream_manager = None

    # Stop source broadcaster
    broadcaster = getattr(app.state, "source_broadcaster", None)
    if broadcaster is not None:
        try:
            broadcaster.stop()
            logger.info("Source broadcaster stopped")
        except Exception:
            logger.exception("Error stopping source broadcaster")
        app.state.source_broadcaster = None

    # Clear audio tee (both app.state and module-level reference)
    _active_audio_tee = None

    tee = getattr(app.state, "audio_tee", None)
    if tee is not None:
        try:
            tee.clear_outputs()
        except Exception:
            logger.exception("Error clearing audio tee outputs")
        app.state.audio_tee = None

    # Stop device playback loop (must stop before receiver/scheduler)
    playback_loop = getattr(app.state, "device_playback_loop", None)
    if playback_loop is not None:
        try:
            # stop() returns False when the loop thread is still inside
            # AudioOutputDriver.write() -- i.e. the output device was removed or
            # wedged. The driver's own stop() defers its close in that case, so
            # teardown stays correct either way; this only records the truth
            # instead of assuming the thread exited.
            if playback_loop.stop() is False:
                logger.warning(
                    "Device playback loop did not exit before the output driver "
                    "is torn down; the output device is removed or wedged"
                )
            else:
                logger.info("Device playback loop stopped")
        except Exception:
            logger.exception("Error stopping device playback loop")
        app.state.device_playback_loop = None

    # Stop device output driver
    output_driver = getattr(app.state, "device_output_driver", None)
    if output_driver is not None:
        try:
            output_driver.stop()
            logger.info("Device output driver stopped")
        except Exception:
            logger.exception("Error stopping device output driver")
        app.state.device_output_driver = None

    # Reset output-driver health so a fresh device setup starts from
    # "not_started" rather than inheriting the previous session's state.
    _set_output_health("not_started")

    # Stop device receiver
    receiver = getattr(app.state, "device_receiver", None)
    if receiver is not None:
        try:
            receiver.stop()
            logger.info("Device receiver stopped")
        except Exception:
            logger.exception("Error stopping device receiver")
        app.state.device_receiver = None

    # Clear playback scheduler
    scheduler = getattr(app.state, "playback_scheduler", None)
    if scheduler is not None:
        try:
            scheduler.clear()
        except Exception:
            logger.exception("Error clearing playback scheduler")
        app.state.playback_scheduler = None

    _pipeline_initialized = False
    _source_local_active = False
    _pipeline_gain = 1.0
    _direct_injection_active = False
    _embedded_source_active = False
    _sounddevice_muted = False
    _device_count = 0
    _spoke_count = 0
    _recording_active = False
    _pending_proctap_pid = None
    _pending_proctap_rescan = False
    _app_ref = None

    # Reset capture-provider health so a fresh source setup starts from
    # "not_started" rather than inheriting the previous session's state.
    _set_capture_health("not_started")

    # Reset anomaly detector and timeline state
    _fanout_discontinuity_count[0] = 0
    _fanout_clipping_count[0] = 0
    _fanout_nan_count[0] = 0
    _fanout_silence_to_audio_transitions[0] = 0
    _fanout_prev_last_sample[0] = 0.0
    _fanout_prev_rms_was_silence[0] = True
    _fanout_total_chunks[0] = 0
    _pcm_timeline.clear()

    logger.debug("PCM streaming pipeline teardown complete")


def handle_pcm_chunk_message(app: FastAPI, payload: dict[str, Any]) -> bool:
    """
    Route an incoming ``pcm_chunk`` WebSocket message to the DeviceAudioReceiver.

    This function is intended to be called from the EventHub's dispatch path
    (or a WebSocket message handler) when a message with type ``pcm_chunk``
    is received.

    Integration note:
        The EventHub dispatches incoming client messages via
        ``dispatch_command()`` which matches on the ``action`` field.  To wire
        PCM chunks, register a command handler on the event bus::

            event_bus.register_command_handler("pcm_chunk", handler)

        Or call this function directly from the WebSocket receive loop when
        ``message["type"] == "pcm_chunk"``.

    Args:
        app: FastAPI application instance.
        payload: Decoded JSON payload from the WebSocket message.  Expected
            to contain ``{"chunk": "<base64>", "sequence": int, "play_at": float}``.

    Returns:
        True if the chunk was handled by the receiver, False otherwise
        (e.g. no device receiver is configured on this node).
    """
    receiver = getattr(app.state, "device_receiver", None)
    if receiver is None:
        return False

    try:
        return receiver.on_websocket_message(payload)
    except Exception:
        logger.exception("Error handling pcm_chunk message")
        return False


__all__ = [
    "get_active_audio_tee",
    "get_active_chunk_stamper",
    "get_capture_health",
    "get_capture_timeline",
    "get_fanout_diagnostics",
    "handle_pcm_chunk_message",
    "is_embedded_source_active",
    "notify_proctap_pid",
    "probe_active_chunk_stamper_aec_reference",
    "request_proctap_rescan",
    "set_direct_injection",
    "set_embedded_source",
    "set_source_recording_active",
    "setup_device_pipeline",
    "setup_source_pipeline",
    "start_device_ws_client",
    "start_oracle_audio_injection",
    "stop_oracle_audio_injection",
    "teardown_pipeline",
]
