"""Multi-room role assignment, sync status, and calibration API routes.

Provides the ``POST /api/v1/multiroom/role`` endpoint that allows
Instance A (Hub) to instruct Instance B to act as a Spoke, or to
revert it back to standalone mode.

Endpoints:
    POST /api/v1/multiroom/role                      - Set the multiroom role for this instance
    GET  /api/v1/multiroom/sync-status               - Get position sync diagnostics
    GET  /api/v1/multiroom/sync-diag                 - Comprehensive hub + spoke diagnostics
    GET  /api/v1/multiroom/{room_id}/calibration     - Get per-room calibration offset
    PUT  /api/v1/multiroom/{room_id}/calibration     - Set per-room calibration offset
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from contracts.api_response import failure_response, success_response
from core.logging_config import get_logger
from fastapi import APIRouter, Depends, HTTPException, Request
from ui.api.routes.auth_dependencies import (
    get_current_user_id,
    require_auth,
    require_operator_auth,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/multiroom", tags=["multiroom"])


def _source_state_attr(app: Any, current_name: str, legacy_name: str) -> Any:
    """Return current source-pipeline state, with legacy hub-name fallback."""
    current = getattr(app.state, current_name, None)
    if current is not None:
        return current
    return getattr(app.state, legacy_name, None)


def _require_dev_mode() -> None:
    """Gate diagnostic endpoints behind dev_mode setting."""
    from config.settings import settings

    if not settings.dev_mode:
        raise HTTPException(status_code=404, detail="Not Found")


def _scope_room_id(user_id: str, room_id: str) -> str:
    """Prefix room_id with user_id in cloud mode to enforce ownership."""
    from config.settings import settings

    app_surface = getattr(settings, "app_surface", "desktop")
    if app_surface == "cloud":
        user_prefix = "%s:" % user_id
        if room_id.startswith(user_prefix):
            return room_id
        return "%s:%s" % (user_id, room_id)
    return room_id


def _event_hub_room_id(user_id: str, room_id: str) -> str:
    """Return the raw room key expected by EventHub's user-scoped room API."""
    from config.settings import settings

    app_surface = getattr(settings, "app_surface", "desktop")
    if app_surface != "cloud":
        return room_id
    user_prefix = "%s:" % user_id
    if room_id.startswith(user_prefix):
        return room_id[len(user_prefix) :]
    return room_id


class RoleRequest(BaseModel):
    """Request body for setting the multiroom role."""

    role: str = Field(
        ...,
        description="Multiroom role: 'spoke', 'hub', or 'standalone'",
        pattern="^(spoke|hub|standalone)$",
    )
    hub_host: str | None = Field(
        None,
        description="Hub host address (required when role='spoke' unless pairing_code is given)",
    )
    hub_port: int | None = Field(
        None,
        description="Hub port (required when role='spoke')",
    )
    pairing_code: str | None = Field(
        None,
        min_length=1,
        max_length=32,
        description="Pairing word or base36 code; decoded to hub_host when hub_host is absent",
    )
    room_id: str = Field(
        "default",
        min_length=1,
        max_length=100,
        description="Room identifier for scoped broadcasts",
    )


@router.post(
    "/role",
    dependencies=[Depends(require_operator_auth)],
)
async def set_multiroom_role(
    body: RoleRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> Any:
    """Set the multiroom role for this instance.

    When ``role`` is ``spoke``, the spoke PCM streaming pipeline is
    initialised and the ``pcm_chunk`` WebSocket command handler is
    registered on the EventHub so incoming audio can be received from
    the Hub.

    When ``role`` is ``standalone``, any active pipeline (Hub or Spoke)
    is torn down and the instance reverts to normal operation.
    """
    app = request.app
    event_hub_room_id = _event_hub_room_id(user_id, body.room_id)
    body.room_id = _scope_room_id(user_id, body.room_id)

    if body.role == "spoke" and not body.hub_host and body.pairing_code:
        from utils.pairing_codec import decode_input

        decoded = decode_input(body.pairing_code)
        if decoded is None:
            return JSONResponse(
                status_code=400,
                content=failure_response(
                    "invalid_pairing_code",
                    "Pairing code is not a known word or valid 6-character code.",
                ),
            )
        body.hub_host = decoded

    if body.role == "spoke":
        return await _activate_spoke(app, body)

    if body.role == "standalone":
        return _deactivate_pipeline(app)

    if body.role == "hub":
        return await _activate_hub(app, body, user_id, event_hub_room_id=event_hub_room_id)

    # Unreachable due to pydantic regex, but be defensive.
    return JSONResponse(
        status_code=400,
        content=failure_response(
            "invalid_role",
            "Role must be 'spoke', 'hub', or 'standalone'",
        ),
    )


# ----------------------------------------------------------------------- #
# Helpers                                                                   #
# ----------------------------------------------------------------------- #


async def _activate_spoke(app: Any, body: RoleRequest) -> Any:
    """Initialise the spoke pipeline + start the authenticated bridge to the hub."""
    import os

    from audio_core.streaming.pipeline_wiring import (
        setup_spoke_pipeline,
        start_spoke_ws_client,
    )

    try:
        # setup_spoke_pipeline performs initial clock synchronization with the hub.
        # (The legacy pcm_chunk EventHub command handler was removed in F-2;
        # the only inbound path is the outbound bridge started below.)
        spoke_kwargs: dict[str, Any] = {}
        if body.hub_host:
            spoke_kwargs["source_host"] = body.hub_host
        if body.hub_port:
            spoke_kwargs["source_port"] = body.hub_port
        receiver = setup_spoke_pipeline(app, body.room_id, **spoke_kwargs)
        if receiver is None:
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "spoke_setup_failed",
                    "Failed to initialise spoke pipeline",
                ),
            )

        # Start the outbound WebSocket bridge to the hub.  Read the spoke
        # HMAC token from VIOLA_SPOKE_TOKEN so the handshake can present an
        # X-Spoke-Token header (Round 1 F-3, 2026-05-29).  An auth-enabled
        # hub closes the connection with 1008 without this header.
        spoke_token = os.environ.get("VIOLA_SPOKE_TOKEN") or None
        if body.hub_host and body.hub_port:
            start_spoke_ws_client(
                app,
                body.hub_host,
                body.hub_port,
                body.room_id,
                spoke_token=spoke_token,
            )

        logger.info(
            "Multiroom role set to spoke: room_id=%s hub=%s:%s",
            body.room_id,
            body.hub_host,
            body.hub_port,
        )
        return success_response(
            {
                "role": "spoke",
                "room_id": body.room_id,
                "hub_host": body.hub_host,
                "hub_port": body.hub_port,
            }
        )

    except Exception:
        logger.exception("Failed to activate spoke role")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "spoke_activation_failed",
                "Couldn't connect to the multiroom group. Check that the hub device is online.",
            ),
        )


async def _activate_hub(
    app: Any,
    body: RoleRequest,
    user_id: str,
    *,
    event_hub_room_id: str,
) -> Any:
    """Initialise the hub pipeline."""
    from audio_core.streaming.pipeline_wiring import setup_source_pipeline

    try:
        broadcaster = setup_source_pipeline(app, event_hub_room_id, user_id=user_id)
        if broadcaster is None:
            return JSONResponse(
                status_code=500,
                content=failure_response(
                    "hub_setup_failed",
                    "Failed to initialise hub pipeline",
                ),
            )

        logger.info("Multiroom role set to hub: room_id=%s", body.room_id)
        return success_response(
            {
                "role": "hub",
                "room_id": body.room_id,
            }
        )

    except Exception:
        logger.exception("Failed to activate hub role")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "hub_activation_failed",
                "Couldn't start multiroom hosting. Check your audio pipeline settings.",
            ),
        )


def _deactivate_pipeline(app: Any) -> Any:
    """Tear down whatever pipeline is active and revert to standalone."""
    from audio_core.streaming.pipeline_wiring import teardown_pipeline

    try:
        teardown_pipeline(app)
        logger.info("Multiroom role set to standalone")
        return success_response({"role": "standalone"})
    except Exception:
        logger.exception("Failed to deactivate pipeline")
        return JSONResponse(
            status_code=500,
            content=failure_response(
                "teardown_failed",
                "Couldn't disconnect from multiroom. Try restarting Viola.",
            ),
        )


def _collect_send_timelines(stream_mgr: Any) -> dict[str, Any]:
    """Return the manager's per-spoke send timelines (room -> per-second
    buckets), tolerating managers that don't expose them (tests, mocks)."""
    getter = getattr(stream_mgr, "get_send_timelines", None)
    if not callable(getter):
        return {}
    try:
        timelines = getter()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        logger.debug("Failed to collect spoke send timelines", exc_info=True)
        return {}
    return timelines if isinstance(timelines, dict) else {}


@router.get(
    "/streaming-metrics",
    dependencies=[Depends(require_auth), Depends(_require_dev_mode)],
)
async def get_streaming_metrics(request: Request) -> Any:
    """Return PCM streaming pipeline metrics for both Hub and Spoke sides.

    Used by integration tests to verify chunk flow without needing
    real audio hardware.

    NOTE: Auth is intentionally symmetric with POST /role.  When spoke-to-hub
    mTLS is implemented the spoke service account will satisfy this dependency
    automatically; no route-level change will be needed at that point.
    """
    app = request.app
    metrics: dict[str, Any] = {"hub": None, "spoke": None, "capture": None}

    broadcaster = _source_state_attr(app, "source_broadcaster", "hub_broadcaster")
    if broadcaster is not None:
        metrics["hub"] = broadcaster.get_metrics()

    receiver = getattr(app.state, "spoke_receiver", None)
    if receiver is not None:
        metrics["spoke"] = receiver.get_metrics()

    scheduler = getattr(app.state, "playback_scheduler", None)
    if scheduler is not None:
        metrics["scheduler"] = scheduler.get_metrics()

    tee = getattr(app.state, "audio_tee", None)
    if tee is not None:
        metrics["tee"] = tee.get_metrics()

    capture = _source_state_attr(app, "source_capture_provider", "hub_capture_provider")
    if capture is not None:
        metrics["capture"] = type(capture).__name__
        if hasattr(capture, "get_metrics"):
            metrics["capture_metrics"] = capture.get_metrics()

    # Binary WebSocket streaming path (actual data path for browser spokes)
    stream_mgr = getattr(app.state, "audio_stream_manager", None)
    if stream_mgr is not None:
        metrics["stream_manager"] = stream_mgr.get_metrics()
        metrics["spoke_send_timelines"] = _collect_send_timelines(stream_mgr)

    stamper = getattr(app.state, "chunk_stamper", None)
    if stamper is not None and hasattr(stamper, "get_metrics"):
        metrics["stamper"] = stamper.get_metrics()

    hub_local = getattr(app.state, "hub_local_player", None)
    if hub_local is not None and hasattr(hub_local, "get_metrics"):
        metrics["hub_local_playback"] = hub_local.get_metrics()

    return success_response(metrics)


@router.get("/sync-diag", dependencies=[Depends(require_auth), Depends(_require_dev_mode)])
async def get_sync_diagnostics(request: Request) -> Any:
    """Comprehensive hub + spoke diagnostics in a single response.

    Collects hub pipeline state (capture, stamper, playback) then sends a
    ``request_diagnostics`` control message to every connected spoke via the
    existing audio WebSocket and waits up to 5 seconds for responses.
    """
    app = request.app
    ts = time.time()

    # ── Hub metrics ─────────────────────────────────────────────────
    hub: dict[str, Any] = {}

    capture = _source_state_attr(app, "source_capture_provider", "hub_capture_provider")
    if capture is not None and hasattr(capture, "get_metrics"):
        cap_m = capture.get_metrics()
        hub["capture_rms"] = cap_m.get("rms", 0)
    else:
        cap_m = {}

    stamper = getattr(app.state, "chunk_stamper", None)
    if stamper is not None and hasattr(stamper, "get_metrics"):
        stmp_m = stamper.get_metrics()
        hub["stamper_sequence"] = stmp_m.get("sequence", 0)
        hub["stamper_rms"] = stmp_m.get("rms", 0)
        hub["audio_chunks"] = stmp_m.get("audio_chunks", 0)
        hub["silence_frames"] = stmp_m.get("silence_chunks", 0)
        hub["capture_buffer_chunks"] = stmp_m.get("capture_buffer_bytes", 0)
        hub["capture_trims"] = stmp_m.get("capture_trims", 0)
        # Gain / clipping diagnostics
        hub["gain_raw_peak"] = stmp_m.get("gain_raw_peak", 0)
        hub["gain_gained_peak"] = stmp_m.get("gain_gained_peak", 0)
        hub["gain_clip_count"] = stmp_m.get("gain_clip_count", 0)
        hub["gain_normalizer_scale"] = stmp_m.get("gain_normalizer_scale", 1.0)
        hub["gain_chunks_measured"] = stmp_m.get("gain_chunks_measured", 0)
        hub["gain_chunks_clipped"] = stmp_m.get("gain_chunks_clipped", 0)
        hub["gain_clip_pct"] = stmp_m.get("gain_clip_pct", 0)
        hub["gain_total_clipped_samples"] = stmp_m.get("gain_total_clipped_samples", 0)
        hub["gain_max_gained_peak"] = stmp_m.get("gain_max_gained_peak", 0)
        hub["gain_max_clip_count"] = stmp_m.get("gain_max_clip_count", 0)
        hub["last_broadcast_time"] = stmp_m.get("last_broadcast_time", 0)

    # HubLocalPlayback metrics (only populated when hub_local_playback=True)
    hub_player = getattr(app.state, "hub_local_player", None)
    if hub_player is not None and hasattr(hub_player, "get_metrics"):
        hp_m = hub_player.get_metrics()
        hub["hub_local_playback_active"] = hp_m.get("running", False)
        hub["hub_local_running"] = hp_m.get("running", False)
        hub["hub_local_subprocess_pid"] = hp_m.get("subprocess_pid")
        hub["hub_local_delay_chunks"] = hp_m.get("delay_chunks", 0)
        hub["playback_buffer_depth"] = hp_m.get("delay_buffer_depth", 0)
        hub["playback_drops"] = hp_m.get("pipe_drops", 0)
        hub["underruns"] = hp_m.get("underruns", 0)
        hub["hub_local_chunks_sent"] = hp_m.get("chunks_sent", 0)
        hub["hub_local_output_device_name"] = hp_m.get("output_device_name", "")
        hub["hub_local_output_device_index"] = hp_m.get("output_device_index")
        hub["hub_local_volume"] = hp_m.get("volume")
        hub["hub_local_output_is_virtual"] = hp_m.get("output_device_is_virtual", False)
        # Dynamic sync controller metrics
        sync_m = hp_m.get("sync", {})
        hub["hub_local_effective_delay_ms"] = sync_m.get("effective_delay_ms", 0)
        hub["hub_local_sync_error_ms"] = sync_m.get("sync_smoothed_error_ms", 0)
        hub["hub_local_sync_adjustments"] = sync_m.get("sync_adjustments", 0)
        hub["hub_local_subprocess_buffer"] = sync_m.get("subprocess_buffer_depth", 0)
        hub["hub_local_subprocess_underruns"] = sync_m.get("subprocess_underruns", 0)
        # Gate: FAIL if hub-local is running but playing into a virtual device
        _dev_name = hp_m.get("output_device_name", "")
        if hp_m.get("running", False) and hp_m.get("output_device_is_virtual", False):
            hub["hub_local_device_check"] = f"FAIL: hub-local playing into virtual device [{_dev_name}]"
        elif hp_m.get("running", False):
            hub["hub_local_device_check"] = f"OK: hub-local device [{_dev_name}]"
        else:
            hub["hub_local_device_check"] = "NOT_RUNNING"
    else:
        hub["hub_local_playback_active"] = False
        hub["hub_local_device_check"] = "NOT_RUNNING"

    # ── Direct injection & pipeline gain ─────────────────────────────
    from audio_core.streaming import pipeline_wiring

    _di_active = bool(getattr(pipeline_wiring, "_direct_injection_active", False))
    _es_active = bool(getattr(pipeline_wiring, "_embedded_source_active", False))
    _tts_muted = bool(getattr(pipeline_wiring, "_source_local_capture_muted_for_tts", False))
    _pg = float(getattr(pipeline_wiring, "_pipeline_gain", 1.0))
    _sd_muted = bool(getattr(pipeline_wiring, "_sounddevice_muted", False))

    hub["direct_injection_active"] = _di_active
    hub["embedded_source_active"] = _es_active
    hub["pipeline_gain"] = _pg
    hub["sounddevice_muted"] = _sd_muted
    hub["tts_capture_muted"] = _tts_muted
    hub["skip_stamper"] = _di_active or _tts_muted

    # ── ProcTap stats ─────────────────────────────────────────────
    # NOTE: muted_pid is legacy (mute worker removed, replaced by JS/CDP callbacks).
    # pids_match means the active ProcTap target matches the last explicit
    # PID notification.
    proctap_pid: int | None = None
    if capture is not None and hasattr(capture, "get_metrics"):
        proctap_pid = cap_m.get("pid")
        hub["capture_mode"] = cap_m.get("capture_mode")
        hub["last_notified_pid"] = cap_m.get("last_notified_pid")
    hub["proctap_pid"] = proctap_pid

    # Hub-mute callbacks removed — no muting in new architecture.
    hub["muted_pid"] = None
    last_notified_pid = hub.get("last_notified_pid")
    hub["pids_match"] = bool(
        proctap_pid is not None and last_notified_pid is not None and last_notified_pid == proctap_pid
    )

    # ── Stream manager (used for buffer config and spoke diagnostics) ──
    stream_mgr = getattr(app.state, "audio_stream_manager", None)

    # ── Hub buffer config ────────────────────────────────────────
    quantized_buffer_ms = 0
    if stream_mgr is not None:
        quantized_buffer_ms = stream_mgr._get_actual_buffer_ms()
    hub["spoke_buffer_target_ms"] = quantized_buffer_ms

    # ── Spoke diagnostics via WebSocket ─────────────────────────────
    spoke_reports: dict[str, Any] = {}
    spoke_connections: list[dict] = []

    if stream_mgr is not None:
        # NOTE: check_and_broadcast_buffer_change() removed from here.
        # It was an observer effect: every diagnostic poll broadcast config
        # to spokes, which reset the drift corrector warmup timer, permanently
        # preventing drift correction. Config changes are broadcast from the
        # sync controller tick instead.

        spoke_connections = stream_mgr.get_spoke_info_snapshot()

        request_id = str(uuid.uuid4())
        spoke_reports = await stream_mgr.request_spoke_diagnostics(
            request_id,
            timeout_sec=5.0,
        )

    # ── Build spoke list ────────────────────────────────────────────
    unresponsive = spoke_reports.pop("_unresponsive", [])
    spokes: list[dict[str, Any]] = []

    # Hub-side per-second send timelines (passive; no spoke round-trip).
    send_timelines: dict[str, Any] = {}
    if stream_mgr is not None:
        send_timelines = _collect_send_timelines(stream_mgr)

    for conn in spoke_connections:
        room_name = conn.get("room_name", "unknown")
        connected_at = conn.get("connected_at", 0)
        send_timeline = send_timelines.get(room_name, [])
        if not isinstance(send_timeline, list):
            send_timeline = []

        report = spoke_reports.get(room_name, {})
        drift_debug = report.get("driftDebug", {})
        spokes.append(
            {
                "room": room_name,
                "connected_at": connected_at,
                "buffer_depth_ms": report.get("bufferDepthMs", 0),
                "effective_buffer_depth_ms": report.get("effectiveBufferDepthMs", 0),
                "scheduled_ahead_ms": report.get("scheduledAheadMs", 0),
                "drift_buffer_depth_ms": report.get("driftBufferDepthMs", 0),
                "chunks_received": report.get("chunksReceived", 0),
                "chunks_played": report.get("chunksPlayed", 0),
                "chunks_dropped": report.get("chunksDropped", 0),
                "chunks_trimmed": report.get("chunksTrimmed", 0),
                "sequence_gaps": report.get("sequenceGaps", 0),
                "drift_ppm": report.get("driftPpm", 0),
                "raw_drift_ppm": report.get("rawDriftPpm", report.get("driftPpm", 0)),
                "drift_correction_ppm": report.get("driftCorrectionPpm", 0),
                "oracle_clock_skew_ppm": report.get("oracleClockSkewPpm", 0),
                "clock_offset_ms": report.get("clockOffsetMs", 0),
                "clock_rtt_ms": report.get("clockRttMs", 0),
                "rms": report.get("rms", 0),
                "peak_rms": report.get("peakRms", 0),
                "analyser_rms": report.get("analyserRms", 0),
                "last_sequence": report.get("lastSequence", 0),
                "output_latency_ms": report.get("outputLatencyMs", 0),
                "buffer_target_ms": report.get("bufferTargetMs", 0),
                "currently_playing_seq": report.get("currentlyPlayingSeq", -1),
                "current_play_time_hub": report.get("currentPlayTimeHub", 0),
                "pipeline_latency_ms": report.get("pipelineLatencyMs", 0),
                "user_agent": report.get("userAgent", ""),
                # Signal layers (per-stage RMS)
                "last_received_chunk_rms": report.get("lastReceivedChunkRMS", 0),
                "last_converted_rms": report.get("lastConvertedRMS", 0),
                "last_converted_min": report.get("lastConvertedMin", 0),
                "last_converted_max": report.get("lastConvertedMax", 0),
                "last_buffer_rms": report.get("lastBufferRMS", 0),
                # Clipping detection
                "clip_count": report.get("clipCount", 0),
                "clip_rate": report.get("clipRate", 0),
                "soft_limit_rate": report.get("softLimitRate", 0),
                # Chunk boundary continuity (click/pop detection)
                "boundary_discontinuity_avg": report.get("boundaryDiscontinuityAvg", 0),
                "boundary_discontinuity_max": report.get("boundaryDiscontinuityMax", 0),
                "boundary_discontinuity_count": report.get("boundaryDiscontinuityCount", 0),
                "post_crossfade_disc_max": report.get("postCrossfadeDiscMax", 0),
                # Drift correction artifact energy
                "correction_artifact_energy": report.get("correctionArtifactEnergy", 0),
                # Spectral quality (FFT)
                "signal_quality": report.get("signalQuality"),
                # Pipeline internals
                "audio_context_state": report.get("audioContextState"),
                "active_source_nodes": report.get("activeSourceNodes", 0),
                "last_schedule_delta": report.get("lastScheduleDelta", 0),
                "pipeline_healthy": report.get("pipelineHealthy", False),
                "first_silent_layer": report.get("firstSilentLayer"),
                "responded": bool(report) and not report.get("_stale", False),
                # Scheduler diagnostics — reveals buffer growth root cause
                "scheduler_headroom_ms": report.get("schedulerHeadroomMs", 0),
                "safety_clamp_count": report.get("safetyClampCount", 0),
                "re_anchor_count": report.get("reAnchorCount", 0),
                "last_re_anchor_ms_ago": report.get("lastReAnchorMsAgo"),
                # Stall-recovery watchdog (spoke-reported)
                "stall_recovery_count": report.get("stallRecoveryCount", 0),
                "last_stall_recovery_ms_ago": report.get("lastStallRecoveryMsAgo"),
                # Hub-side per-second send accounting for this spoke
                "send_timeline": send_timeline,
                "sync_anchor_ms": report.get("syncAnchorMs", 0),
                "hub_padding_ms": report.get("hubPaddingMs", 0),
                "applied_padding_ms": report.get("appliedPaddingMs", 0),
                "audio_context_sample_rate": report.get("audioContextSampleRate", 0),
                "audio_context_base_latency": report.get("audioContextBaseLatency", 0),
                "buffer_target_chunks": report.get("bufferTargetChunks", 0),
                "oracle_events": report.get("oracleEvents", []),
                # Drift corrector internals (from getDebugState())
                "drift_corrector": {
                    "smoothed_base_drift_ppm": drift_debug.get("smoothedBaseDriftPpm", 0),
                    "smoothed_buffer_trend_ppm": drift_debug.get("smoothedBufferTrendPpm", 0),
                    "correction_term": drift_debug.get("correctionTerm", 0),
                    "active_drift_ppm": drift_debug.get("activeDriftPpm", 0),
                    "samples_added": drift_debug.get("samplesAdded", 0),
                    "samples_removed": drift_debug.get("samplesRemoved", 0),
                    "smoothed_buffer_depth_chunks": drift_debug.get("smoothedBufferDepthChunks", 0),
                    "smoothed_buffer_depth_ms": drift_debug.get("smoothedBufferDepthMs", 0),
                    "buffer_target_chunks": drift_debug.get("bufferTargetChunks", 0),
                    "buffer_target_ms": drift_debug.get("bufferTargetMs", 0),
                    "buffer_error_chunks": drift_debug.get("bufferErrorChunks", 0),
                    "dead_zone_chunks": drift_debug.get(
                        "deadZoneChunks",
                        drift_debug.get("deadZoneChunksShrink", 0),
                    ),
                    "dead_zone_chunks_shrink": drift_debug.get(
                        "deadZoneChunksShrink",
                        drift_debug.get("deadZoneChunks", 0),
                    ),
                    "dead_zone_chunks_grow": drift_debug.get(
                        "deadZoneChunksGrow",
                        drift_debug.get("deadZoneChunks", 0),
                    ),
                    "is_warmup": drift_debug.get("isWarmup", False),
                    "warmup_remaining_ms": drift_debug.get("warmupRemainingMs", 0),
                    "is_perturbation_freeze": drift_debug.get("isPerturbationFreeze", False),
                    "perturbation_freeze_remaining_ms": drift_debug.get("perturbationFreezeRemainingMs", 0),
                    "depth_history_length": drift_debug.get("depthHistoryLength", 0),
                    "accumulator": drift_debug.get("accumulator", 0),
                    "accumulator_overflow": drift_debug.get("accumulatorOverflow", 0),
                    "clock_drift_ppm": drift_debug.get("clockDriftPpm", 0),
                    "buffer_drift_ppm": drift_debug.get("bufferDriftPpm", 0),
                    "self_heal_resets": drift_debug.get("selfHealResets", 0),
                },
            }
        )

    # ── Sync summary ────────────────────────────────────────────────
    # Use stamper_sequence as the hub reference point.  HubLocalPlayback is
    # disabled so hub_played_sequence is always 0 — stamper_sequence is the
    # most recent chunk broadcast by the hub.
    hub_seq = hub.get("stamper_sequence", 0)
    spoke_sequences: dict[str, int] = {}
    sequence_gaps: dict[str, int] = {}
    pipeline_delay_chunks: dict[str, int] = {}
    pipeline_delay_ms: dict[str, int] = {}
    playing_seqs: list[tuple[str, int]] = []  # for inter-spoke sync

    for s in spokes:
        name = s["room"]
        last = s["last_sequence"]
        playing = s["currently_playing_seq"]
        spoke_sequences[name] = last
        sequence_gaps[name] = hub_seq - last if last is not None and last >= 0 else 0

        # Pipeline delay: how far behind is this spoke's playback cursor
        # from the hub's latest broadcast?
        if s["responded"] and playing >= 0:
            gap = hub_seq - playing
            pipeline_delay_chunks[name] = gap
            pipeline_delay_ms[name] = gap * 20
            playing_seqs.append((name, playing))
        else:
            pipeline_delay_chunks[name] = 0
            pipeline_delay_ms[name] = 0

    # Inter-spoke sync: difference between fastest and slowest spoke's
    # AUDIBLE delay (pipeline_delay + scheduler_headroom + output_latency).
    # Raw sequence spread ignores AudioContext scheduling differences
    # (e.g. Safari headroom=120ms vs Chrome headroom=30ms), so two spokes
    # with identical audible timing would show 90ms raw spread.  Using
    # audible delay gives the true ear-to-ear gap.
    inter_spoke_sync: dict[str, Any] = {}
    if len(playing_seqs) >= 2:
        # Build per-spoke audible delay for spread calculation
        spoke_audible_delays: dict[str, float] = {}
        for s in spokes:
            name = s["room"]
            pipe_ms = pipeline_delay_ms.get(name, 0)
            if not isinstance(pipe_ms, (int, float)) or pipe_ms < 0:
                continue
            headroom = s.get("scheduler_headroom_ms") or 0
            # Default minimum headroom for spokes that don't report it
            if not headroom:
                headroom = 20.0
            out_lat = s.get("output_latency_ms", 0) or 0
            spoke_audible_delays[name] = float(pipe_ms) + float(headroom) + float(out_lat)

        if len(spoke_audible_delays) >= 2:
            delays = list(spoke_audible_delays.values())
            spread_ms = round(max(delays) - min(delays))
            fastest = min(spoke_audible_delays, key=spoke_audible_delays.get)
            slowest = max(spoke_audible_delays, key=spoke_audible_delays.get)
        else:
            # Fall back to raw sequence spread
            seqs = [seq for _, seq in playing_seqs]
            spread_ms = (max(seqs) - min(seqs)) * 20
            fastest = next(n for n, s in playing_seqs if s == max(seqs))
            slowest = next(n for n, s in playing_seqs if s == min(seqs))

        inter_spoke_sync = {
            "spread_ms": spread_ms,
            "per_spoke": spoke_audible_delays or {name: seq for name, seq in playing_seqs},
            "fastest": fastest,
            "slowest": slowest,
            "in_sync": spread_ms < 30,
            "threshold_ms": 30,
        }

    # ── Hub-vs-spoke offset ─────────────────────────────────────────
    # Compares hub effective delay against spoke AUDIBLE delay:
    # spoke_audible = pipeline_delay + scheduler_headroom + output_latency.
    # pipeline_delay_ms alone only measures to the scheduled position;
    # AudioContext buffering (headroom) and hardware latency must be added
    # for a true ear-to-ear comparison.
    # Positive offset = hub plays later (spoke ahead).
    hub_vs_spoke: dict[str, Any] = {}
    hub_eff_ms = hub.get("hub_local_effective_delay_ms")
    if isinstance(hub_eff_ms, (int, float)) and pipeline_delay_ms:
        # Build per-spoke overhead from scheduler_headroom + output_latency
        spoke_overhead: dict[str, float] = {}
        for s in spokes:
            name = s["room"]
            headroom = s.get("scheduler_headroom_ms", 0) or 0
            out_lat = s.get("output_latency_ms", 0) or 0
            spoke_overhead[name] = float(headroom) + float(out_lat)

        offsets: dict[str, float] = {}
        for name, pipe_delay in pipeline_delay_ms.items():
            if isinstance(pipe_delay, (int, float)) and pipe_delay > 0:
                spoke_audible = float(pipe_delay) + spoke_overhead.get(name, 0.0)
                offsets[name] = round(float(hub_eff_ms) - spoke_audible, 1)
        if offsets:
            avg_offset = round(sum(offsets.values()) / len(offsets), 1)
            hub_vs_spoke = {
                "per_spoke": offsets,
                "avg_offset_ms": avg_offset,
                "hub_effective_ms": round(float(hub_eff_ms), 1),
                "spoke_ahead": avg_offset > 0,
            }

    # Config-based offset: what the system is CONFIGURED to produce
    from audio_core.streaming.pipeline_wiring import is_embedded_source_active

    hub_vs_spoke["embedded_mode"] = is_embedded_source_active()
    if hub_vs_spoke.get("embedded_mode"):
        hub_vs_spoke["note"] = (
            "embedded mode: hub plays via browser (unmeasured latency). " "Offset may not reflect what user hears."
        )

    # ── Sync calibration state ──────────────────────────────────────
    calibration: dict[str, Any] = {}
    if stream_mgr is not None:
        calibration = {
            "headroom_ema": {r: round(h, 1) for r, h in stream_mgr._spoke_headroom_ema.items()},
            "effective_delay_ema": {r: round(h, 1) for r, h in stream_mgr._spoke_effective_delay_ema.items()},
            "intrinsic_delay_ema": {r: round(h, 1) for r, h in stream_mgr._spoke_intrinsic_delay_ema.items()},
            "components": dict(stream_mgr._spoke_calibration_components),
            "padding_ms": dict(stream_mgr._spoke_padding),
            "reports_received": dict(stream_mgr._spoke_sync_reports),
        }

    response_data = {
        "timestamp": ts,
        "hub": hub,
        "spokes": spokes,
        "unresponsive_spokes": unresponsive,
        "sync": {
            "hub_broadcast_sequence": hub_seq,
            "spoke_sequences": spoke_sequences,
            "sequence_gaps": sequence_gaps,
            "pipeline_delay_chunks": pipeline_delay_chunks,
            "pipeline_delay_ms": pipeline_delay_ms,
            "inter_spoke_sync": inter_spoke_sync,
            "hub_vs_spoke": hub_vs_spoke,
            "calibration": calibration,
        },
    }

    return success_response(response_data)


def create_multiroom_router() -> APIRouter:
    """Factory function for the multiroom router."""
    return router


__all__ = [
    "RoleRequest",
    "create_multiroom_router",
    "router",
]
