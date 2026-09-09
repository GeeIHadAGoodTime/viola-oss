"""
Wake Word Comprehensive Diagnostics API
========================================

Unified API endpoints for the complete wake word diagnostic system.

Endpoints:
- /v1/wake/audio-health - Audio pipeline health (clipping, noise floor, buffer)
- /v1/wake/aec - AEC effectiveness diagnostics
- /v1/wake/scores - Score history and distribution analysis
- /v1/wake/decision-trace - Recent decision traces with full context
- /v1/wake/state-sync - State synchronization diagnostics
- /v1/wake/analytics - Time-bucketed statistics and correlations
- /v1/wake/captures - Recent audio captures
- /v1/wake/captures/{id}/replay - Replay a specific capture
- /v1/wake/test-suite - Run wake word test suite
- /v1/wake/comprehensive - Full diagnostic snapshot (all systems)
"""

from __future__ import annotations

import datetime
from dataclasses import asdict

from fastapi.responses import JSONResponse

from core.logging_config import get_logger
from fastapi import Depends, Query
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth
from ui.api.routes.common import RouteToolbox

log = get_logger(__name__)


def register_wake_diagnostics_routes(context: ApiContext, toolbox: RouteToolbox) -> None:
    """Register comprehensive wake word diagnostic endpoints."""
    router = context.router

    @router.get("/v1/wake/audio-health", dependencies=[Depends(require_auth)])
    async def wake_audio_health():
        """Get audio pipeline health diagnostics.

        Returns:
        - clipping: Clipping ratio, max amplitude, recent clips
        - noise_floor: Current dB level, average, stability
        - buffer_health: Underruns, overruns, health score
        - sample_rate: Detected rate, drift, discontinuities
        """

        async def _inner():
            try:
                from diagnostics.audio_pipeline_health import get_audio_health_monitor

                monitor = get_audio_health_monitor()
                snapshot = monitor.get_health_snapshot()

                return {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "clipping": asdict(snapshot.clipping),
                        "noise_floor": asdict(snapshot.noise_floor),
                        "buffer": asdict(snapshot.buffer),
                        "sample_rate": asdict(snapshot.sample_rate),
                        "overall_status": snapshot.overall_status.value,
                        "issues": snapshot.issues,
                    },
                }
            except ImportError as exc:
                log.debug("Audio health module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "audio_pipeline_health module not available",
                    },
                )
            except Exception:
                log.exception("Audio health diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "audio_health_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/audio-health", method="GET")

    @router.get("/v1/wake/aec", dependencies=[Depends(require_auth)])
    async def wake_aec_diagnostics():
        """Get AEC (Acoustic Echo Cancellation) effectiveness diagnostics.

        Returns:
        - reduction: Current reduction dB, average, effectiveness
        - reference_buffer: Fill ratio, overruns, underruns
        - delay: Current delay ms, estimated delay, stability
        - overall_effectiveness: Score from 0-1
        """

        async def _inner():
            try:
                from diagnostics.aec_effectiveness import get_aec_diagnostics

                diag = get_aec_diagnostics()
                snapshot = diag.get_effectiveness_snapshot()

                return {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "reduction": asdict(snapshot.reduction),
                        "reference_buffer": asdict(snapshot.reference_buffer),
                        "calibration": asdict(snapshot.calibration),
                        "is_effective": snapshot.is_effective,
                        "status": snapshot.status.value,
                        "issues": snapshot.issues,
                    },
                }
            except ImportError as exc:
                log.debug("AEC diagnostics module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "aec_effectiveness module not available",
                    },
                )
            except Exception:
                log.exception("AEC diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "aec_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/aec", method="GET")

    @router.get("/v1/wake/scores", dependencies=[Depends(require_auth)])
    async def wake_score_history(
        limit: int = Query(default=100, ge=1, le=1000),
        include_distribution: bool = Query(default=True),
    ):
        """Get wake detection score history and distribution.

        Query params:
        - limit: Number of recent scores to return (default 100)
        - include_distribution: Include statistical distribution (default true)

        Returns:
        - recent_scores: List of score entries with context
        - distribution: Percentiles, histogram, statistics
        - patterns: Detected score patterns
        """

        async def _inner():
            try:
                from diagnostics.score_history import get_score_history

                history = get_score_history()

                result = {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "recent_scores": [asdict(entry) for entry in history.get_recent(limit)],
                        "total_recorded": history._total_scores,
                    },
                }

                if include_distribution:
                    dist = history.get_distribution()
                    result["data"]["distribution"] = asdict(dist)
                    result["data"]["patterns"] = history.find_patterns()

                return result
            except ImportError as exc:
                log.debug("Score history module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "score_history module not available",
                    },
                )
            except Exception:
                log.exception("Score history failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "scores_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/scores", method="GET")

    @router.get("/v1/wake/decision-trace", dependencies=[Depends(require_auth)])
    async def wake_decision_trace(
        limit: int = Query(default=20, ge=1, le=100),
    ):
        """Get recent wake decision traces with full context.

        Query params:
        - limit: Number of recent traces to return (default 20)

        Returns detailed traces including:
        - audio_input: RMS, clipping, noise floor at decision time
        - aec_state: Reference active, reduction, delay
        - model_output: Score, latency, features
        - threshold_breakdown: Base, adjustments, final threshold
        - layer_verdicts: VAD, score, echo veto, confirmation
        - final_decision: Accepted/rejected with reason
        """

        async def _inner():
            try:
                from diagnostics.wake_decision_trace import get_decision_tracer

                tracer = get_decision_tracer()
                traces = tracer.get_recent_traces(limit)

                return {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "traces": [t.to_dict() for t in traces],
                        "total_traces": len(tracer._traces),
                    },
                }
            except ImportError as exc:
                log.debug("Decision trace module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "wake_decision_trace module not available",
                    },
                )
            except Exception:
                log.exception("Decision trace failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "trace_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/decision-trace", method="GET")

    @router.get("/v1/wake/state-sync", dependencies=[Depends(require_auth)])
    async def wake_state_sync():
        """Get state synchronization diagnostics.

        Returns:
        - playback: Callback vs RMS agreement, disagreements
        - tts: TTS state, speaking status, recent utterances
        - volume: Current volume, history, changes
        """

        async def _inner():
            try:
                from diagnostics.wake_state_sync import get_state_sync_monitor

                monitor = get_state_sync_monitor()
                snapshot = monitor.get_sync_health()

                return {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "playback": snapshot.playback_sync.to_dict(),
                        "tts": snapshot.tts_state.to_dict(),
                        "volume": snapshot.volume_state.to_dict(),
                        "overall_sync_health": snapshot.overall_status.value,
                        "issues": snapshot.issues,
                    },
                }
            except ImportError as exc:
                log.debug("State sync module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "wake_state_sync module not available",
                    },
                )
            except Exception:
                log.exception("State sync diagnostics failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "state_sync_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/state-sync", method="GET")

    @router.get("/v1/wake/analytics", dependencies=[Depends(require_auth)])
    async def wake_analytics(
        include_correlations: bool = Query(default=True),
        include_layer_stats: bool = Query(default=True),
    ):
        """Get time-bucketed analytics and correlations.

        Query params:
        - include_correlations: Include trigger correlations (default true)
        - include_layer_stats: Include per-layer statistics (default true)

        Returns:
        - bucketed_stats: Statistics for 1min, 5min, 15min, 1h, 8h windows
        - correlations: Triggers vs playback/volume/time/TTS
        - layer_stats: Per-layer effectiveness and margins
        """

        async def _inner():
            try:
                from diagnostics.wake_analytics import get_wake_analytics

                analytics = get_wake_analytics()

                result = {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "bucketed_stats": analytics.get_bucketed_stats().to_dict(),
                    },
                }

                if include_correlations:
                    result["data"]["correlations"] = analytics.get_correlations().to_dict()

                if include_layer_stats:
                    layer_stats = analytics.get_layer_statistics()
                    result["data"]["layer_stats"] = {name: stats.to_dict() for name, stats in layer_stats.items()}

                return result
            except ImportError as exc:
                log.debug("Analytics module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "wake_analytics module not available",
                    },
                )
            except Exception:
                log.exception("Analytics failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "analytics_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/analytics", method="GET")

    @router.get("/v1/wake/captures", dependencies=[Depends(require_auth)])
    async def wake_captures(
        event_type: str | None = Query(default=None),
        limit: int = Query(default=10, ge=1, le=50),
    ):
        """Get recent audio captures.

        Query params:
        - event_type: Filter by type (trigger, false_positive, missed)
        - limit: Number to return (default 10)

        Returns list of captures with:
        - capture_id, correlation_id, event_type
        - wake_score, threshold_used, decision_accepted
        - diagnostics snapshot at capture time
        - audio_path if saved to disk
        """

        async def _inner():
            try:
                from diagnostics.wake_audio_buffer import get_wake_audio_buffer

                buffer = get_wake_audio_buffer()
                captures = buffer.get_recent_captures(event_type=event_type, limit=limit)

                return {
                    "ok": True,
                    "timestamp": datetime.datetime.now().isoformat(),
                    "data": {
                        "captures": [c.to_metadata_dict() for c in captures],
                        "buffer_stats": buffer.get_buffer_stats(),
                    },
                }
            except ImportError as exc:
                log.debug("Audio buffer module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={
                        "ok": False,
                        "error": "module_not_available",
                        "detail": "wake_audio_buffer module not available",
                    },
                )
            except Exception:
                log.exception("Get captures failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "captures_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/captures", method="GET")

    @router.post("/v1/wake/captures/{capture_id}/replay", dependencies=[Depends(require_auth)])
    async def replay_capture(capture_id: str):
        """Replay a captured audio through wake detection.

        Path params:
        - capture_id: ID of capture to replay

        Returns:
        - detected: Whether wake word was detected
        - max_score: Maximum score during replay
        - score_delta: Difference from original score
        - frame_results: Per-frame detection results
        """

        async def _inner():
            try:
                from diagnostics.wake_audio_buffer import (
                    WakeReplayPipeline,
                    get_wake_audio_buffer,
                )

                buffer = get_wake_audio_buffer()
                capture = buffer.get_capture_by_id(capture_id)

                if capture is None:
                    return JSONResponse(
                        status_code=404,
                        content={"ok": False, "error": "capture_not_found"},
                    )

                pipeline = WakeReplayPipeline()
                result = pipeline.replay_capture(capture)

                return {
                    "ok": True,
                    "data": {
                        "capture_id": result.capture_id,
                        "detected": result.detected,
                        "max_score": result.max_score,
                        "original_score": result.original_score,
                        "score_delta": result.score_delta,
                        "matches_original": result.matches_original(),
                        "detection_timestamps": result.detection_timestamps,
                        "frame_count": len(result.frame_results),
                    },
                }
            except ImportError as exc:
                log.debug("Replay module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={"ok": False, "error": "module_not_available"},
                )
            except Exception:
                log.exception("Replay failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "replay_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/captures/replay", method="POST")

    @router.post("/v1/wake/test-suite", dependencies=[Depends(require_auth)])
    async def run_test_suite(
        include_synthetic: bool = Query(default=False),
        include_regression: bool = Query(default=True),
        include_benchmark: bool = Query(default=False),
    ):
        """Run wake word test suite.

        Query params:
        - include_synthetic: Run synthetic degradation tests (default false)
        - include_regression: Run golden sample regression (default true)
        - include_benchmark: Run performance benchmark (default false)

        WARNING: This may take significant time. Use sparingly.

        Returns test results with pass/fail counts and details.
        """

        async def _inner():
            try:
                from diagnostics.wake_test_harness import WakeTestHarness

                harness = WakeTestHarness()
                results = harness.run_full_test_suite(
                    include_synthetic=include_synthetic,
                    include_regression=include_regression,
                    include_threshold=False,  # Takes too long for API
                    include_benchmark=include_benchmark,
                )

                return {
                    "ok": True,
                    "data": results,
                }
            except ImportError as exc:
                log.debug("Test harness module not available: %s", exc)
                return JSONResponse(
                    status_code=501,
                    content={"ok": False, "error": "module_not_available"},
                )
            except Exception:
                log.exception("Test suite failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "test_suite_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/test-suite", method="POST")

    @router.get("/v1/wake/comprehensive", dependencies=[Depends(require_auth)])
    async def comprehensive_wake_diagnostics():
        """Get comprehensive wake word diagnostic snapshot.

        Combines ALL diagnostic systems into a single response:
        - audio_health: Pipeline health metrics
        - aec: Echo cancellation effectiveness
        - scores: Score distribution and recent history
        - decisions: Recent decision traces
        - state_sync: State synchronization status
        - analytics: Bucketed statistics and correlations
        - captures: Recent capture summary
        - wake_metrics: Basic wake word metrics

        This is the primary endpoint for AI-assisted debugging.
        """

        async def _inner():
            timestamp = datetime.datetime.now().isoformat()
            result = {
                "ok": True,
                "timestamp": timestamp,
                "data": {},
                "errors": {},
            }

            # Audio Health
            try:
                from diagnostics.audio_pipeline_health import get_audio_health_monitor

                monitor = get_audio_health_monitor()
                snapshot = monitor.get_health_snapshot()
                result["data"]["audio_health"] = {
                    "clipping": asdict(snapshot.clipping),
                    "noise_floor": asdict(snapshot.noise_floor),
                    "overall_status": snapshot.overall_status.value,
                    "issues": snapshot.issues,
                }
            except Exception:
                result["errors"]["audio_health"] = "subsystem_error"

            # AEC
            try:
                from diagnostics.aec_effectiveness import get_aec_diagnostics

                diag = get_aec_diagnostics()
                snapshot = diag.get_effectiveness_snapshot()
                result["data"]["aec"] = {
                    "reduction_db": snapshot.reduction.reduction_db,
                    "is_effective": snapshot.is_effective,
                    "status": snapshot.status.value,
                    "issues": snapshot.issues,
                }
            except Exception:
                result["errors"]["aec"] = "subsystem_error"

            # Scores
            try:
                from diagnostics.score_history import get_score_history

                history = get_score_history()
                dist = history.get_distribution()
                result["data"]["scores"] = {
                    "total_recorded": history._total_scores,
                    "percentiles": asdict(dist),
                    "patterns": [p.to_dict() for p in history.find_patterns()],
                }
            except Exception:
                result["errors"]["scores"] = "subsystem_error"

            # Decision traces
            try:
                from diagnostics.wake_decision_trace import get_decision_tracer

                tracer = get_decision_tracer()
                recent = tracer.get_recent_traces(5)
                result["data"]["decisions"] = {
                    "total_traces": len(tracer._traces),
                    "recent_count": len(recent),
                    "recent_outcomes": [
                        {
                            "trace_id": t.trace_id,
                            "accepted": t.decision.value == "approved",
                            "score": t.model.raw_score,
                        }
                        for t in recent
                    ],
                }
            except Exception:
                result["errors"]["decisions"] = "subsystem_error"

            # State sync
            try:
                from diagnostics.wake_state_sync import get_state_sync_monitor

                monitor = get_state_sync_monitor()
                snapshot = monitor.get_sync_health()
                result["data"]["state_sync"] = {
                    "playback_agreement": snapshot.playback_sync.in_sync,
                    "tts_active": snapshot.tts_state.is_speaking,
                    "sync_health": snapshot.overall_status.value,
                    "issues": snapshot.issues,
                }
            except Exception:
                result["errors"]["state_sync"] = "subsystem_error"

            # Analytics
            try:
                from diagnostics.wake_analytics import get_wake_analytics

                analytics = get_wake_analytics()
                stats_1min = analytics.get_bucketed_stats("1min")
                stats_1h = analytics.get_bucketed_stats("1h")
                result["data"]["analytics"] = {
                    "last_1min_triggers": stats_1min.triggers_total,
                    "last_1min_accepted": stats_1min.triggers_approved,
                    "last_1h_triggers": stats_1h.triggers_total,
                    "last_1h_accepted": stats_1h.triggers_approved,
                }
            except Exception:
                result["errors"]["analytics"] = "subsystem_error"

            # Captures
            try:
                from diagnostics.wake_audio_buffer import get_wake_audio_buffer

                buffer = get_wake_audio_buffer()
                stats = buffer.get_buffer_stats()
                result["data"]["captures"] = {
                    "total_captures": stats["total_captures"],
                    "buffer_fill_pct": stats["buffer_fill_pct"],
                    "recent_count": stats["recent_captures_count"],
                }
            except Exception:
                result["errors"]["captures"] = "subsystem_error"

            # Basic wake metrics
            try:
                from diagnostics.wake_metrics import get_wake_metrics

                metrics = get_wake_metrics()
                result["data"]["wake_metrics"] = metrics.get_metrics_dict()
            except Exception:
                result["errors"]["wake_metrics"] = "subsystem_error"

            # Set ok=False if any errors
            if result["errors"]:
                result["partial"] = True

            return result

        return await toolbox.record_and_call(_inner, route="/v1/wake/comprehensive", method="GET")

    @router.post("/v1/wake/capture-manual", dependencies=[Depends(require_auth)])
    async def manual_capture():
        """Manually trigger an audio capture.

        Use this to capture audio when investigating issues.
        The capture will include the last 5 seconds of audio
        plus 2 seconds following this request.

        Returns the capture ID for later reference or replay.
        """

        async def _inner():
            try:
                from diagnostics.wake_audio_buffer import get_wake_audio_buffer

                buffer = get_wake_audio_buffer()
                capture = buffer.capture_trigger_event(
                    event_type="manual",
                    correlation_id=f"manual_{int(datetime.datetime.now().timestamp() * 1000)}",
                )

                if capture:
                    return {
                        "ok": True,
                        "data": capture.to_metadata_dict(),
                    }
                else:
                    return {
                        "ok": True,
                        "message": "Capture started, will complete after post-event audio collected",
                    }
            except Exception:
                log.exception("Manual capture failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "capture_failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/capture-manual", method="POST")

    @router.post("/v1/wake/mark-false-positive", dependencies=[Depends(require_auth)])
    async def mark_false_positive(
        correlation_id: str | None = Query(default=None),
    ):
        """Mark the most recent detection as a false positive.

        Query params:
        - correlation_id: Optional specific correlation ID to mark

        This captures audio around the false positive for
        training data improvement.
        """

        async def _inner():
            try:
                from diagnostics.wake_audio_buffer import get_wake_audio_buffer

                buffer = get_wake_audio_buffer()
                capture = buffer.capture_false_positive(correlation_id=correlation_id)

                return {
                    "ok": True,
                    "message": "False positive marked and captured",
                    "capture_id": capture.capture_id if capture else None,
                }
            except Exception:
                log.exception("Mark false positive failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/mark-false-positive", method="POST")

    @router.post("/v1/wake/mark-missed", dependencies=[Depends(require_auth)])
    async def mark_missed_detection():
        """Mark that a wake word was spoken but not detected.

        Call this immediately after speaking the wake word
        when no detection occurred. Captures audio for training.
        """

        async def _inner():
            try:
                from diagnostics.wake_audio_buffer import get_wake_audio_buffer

                buffer = get_wake_audio_buffer()
                capture = buffer.capture_missed_detection()

                return {
                    "ok": True,
                    "message": "Missed detection marked and captured",
                    "capture_id": capture.capture_id if capture else None,
                }
            except Exception:
                log.exception("Mark missed detection failed")
                return JSONResponse(
                    status_code=500,
                    content={
                        "ok": False,
                        "error": "failed",
                        "detail": "Couldn't load diagnostics right now. Please try again.",
                    },
                )

        return await toolbox.record_and_call(_inner, route="/v1/wake/mark-missed", method="POST")

    log.info("🎤 Wake diagnostics routes registered")
