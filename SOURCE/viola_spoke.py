#!/usr/bin/env python3
"""
Viola Spoke — headless relay entry point for Pi Zero and constrained devices.

Boots ONLY what a headless spoke needs:
  - sounddevice mic capture → WebSocket stream to hub
  - PCM music playback from hub → sounddevice speakers
  - TTS audio from hub → sounddevice speakers
  - mDNS auto-discovery of hub
  - Minimal FastAPI health endpoint

Does NOT import: bootstrap, intent, music, services/llm, voice/synthesis,
voice/wake_detector, ui (React/Qt), PyQt6, CEF, onnxruntime, scipy.

Target: 80-120 MB RAM on Pi Zero (512 MB).

Usage:
    # Auto-discover hub via mDNS:
    python viola_spoke.py --room "Kitchen"

    # Explicit hub:
    python viola_spoke.py --hub-host 192.168.1.100 --room "Kitchen"

    # With spoke token authentication:
    VIOLA_SPOKE_TOKEN=secret python viola_spoke.py --room "Kitchen"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import uuid
from typing import Any

# Set spoke mode before any Viola imports touch config
os.environ.setdefault("VIOLA_SPOKE_MODE", "1")
os.environ.setdefault("TEST_MODE", "0")

from core.constants import DEFAULT_API_PORT, SAMPLE_RATE_16K
from core.logging_config import get_logger
from core.sentry_integration import install_process_exception_hooks
from services.sentry_init import init_sentry

logger = get_logger(__name__)
init_sentry("viola_spoke")

# #1419: the spoke is a non-GUI desktop process (headless audio relay) that
# previously had no crash sink at all -- init_sentry() alone no-ops on desktop
# (the legacy Sentry DSN is empty there), and nothing routed an unhandled
# exception to the anonymized diagnostic relay/spool the way
# core.sentry_integration.capture_qt_python_exception does for the Qt GUI
# process. core.sentry_integration is already imported transitively via
# services.sentry_init above, so this adds no new dependency weight to the
# Pi-Zero-constrained process this module documents itself as being.
install_process_exception_hooks("desktop_spoke", entry_point="viola_spoke")

# Segfault/native-crash visibility: the spoke drives sounddevice/portaudio
# directly (mic capture + speaker playback), which this project has already
# hit native concurrent-init crashes on (see portaudio_guard). faulthandler
# dumps a Python traceback to a file on SIGSEGV/SIGABRT/SIGFPE/SIGBUS/SIGILL
# instead of the process vanishing with zero trace -- a LOCAL file only, not
# by itself relayed to GlitchTip (see the crash-telemetry report for #1419).
try:
    import faulthandler

    from core.crash_forensics import install_crash_forensics

    # Run-scoped + dated + module-mapped. The spoke drives PortAudio directly,
    # which is exactly the surface the undated dumps in #4650 could not be
    # attributed to; see core/crash_forensics.py.
    _crash_forensics = install_crash_forensics("viola_spoke")
    faulthandler.enable(file=_crash_forensics.stream, all_threads=True)
except (OSError, ValueError, RuntimeError, ImportError):
    logger.debug("Spoke faulthandler setup failed (non-critical)", exc_info=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Viola Spoke — headless relay for Pi Zero",
    )
    parser.add_argument(
        "--hub-host",
        default=os.environ.get("VIOLA_HUB_HOST", ""),
        help="Hub hostname/IP (auto-discovered via mDNS if omitted)",
    )
    parser.add_argument(
        "--hub-port",
        type=int,
        default=int(os.environ.get("VIOLA_HUB_PORT", str(DEFAULT_API_PORT))),
        help=f"Hub API port (default: {DEFAULT_API_PORT})",
    )
    parser.add_argument(
        "--room",
        default=os.environ.get("VIOLA_ROOM_NAME", "Spoke"),
        help="Room name for this spoke (default: 'Spoke')",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("VIOLA_SPOKE_PORT", "8757")),
        help="Local health endpoint port (default: 8757)",
    )
    parser.add_argument(
        "--input-device",
        default=os.environ.get("VIOLA_INPUT_DEVICE"),
        help="sounddevice input device index or name",
    )
    parser.add_argument(
        "--output-device",
        default=os.environ.get("VIOLA_OUTPUT_DEVICE"),
        help="sounddevice output device index or name",
    )
    parser.add_argument(
        "--discovery-timeout",
        type=float,
        default=15.0,
        help="mDNS hub discovery timeout in seconds (default: 15)",
    )
    return parser.parse_args()


def _create_health_app(
    health_monitor: SpokeHealthMonitor,
    device_id: str,
    room: str,
) -> FastAPI:
    """Create a minimal FastAPI app with only a health endpoint."""
    from fastapi.responses import JSONResponse

    from fastapi import FastAPI

    app = FastAPI(
        title="Viola Spoke",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        status = health_monitor.status.to_dict()
        status["device_id"] = device_id
        status["room"] = room
        status["mode"] = "spoke_relay"
        return JSONResponse(status)

    return app


async def _guarded_spoke_task(name: str, coro: Any) -> Any:
    """Run one spoke coroutine, reporting an unhandled failure the instant it happens.

    ``asyncio.gather(..., return_exceptions=True)`` (below) deliberately keeps
    one failed coroutine -- e.g. the mic streamer hitting a torn-down audio
    device -- from taking down the whole spoke process. But because ``gather``
    only returns once EVERY coroutine finishes, and the health server runs
    until shutdown, a mic-streamer crash was previously invisible for the rest
    of the process's life: nothing logged it, nothing told GlitchTip, and the
    "crash" would only even become observable (via the gather's return value)
    at final process teardown -- if that ever happened before the box
    rebooted. This wraps each coroutine so a real failure is captured through
    the same crash sink the excepthook uses THE MOMENT it happens, then
    re-raises so gather's existing fault-isolation behavior (one dead task
    doesn't kill its siblings) is otherwise unchanged (#1419).
    """
    from core.sentry_integration import FILTERED_EXCEPTIONS, capture_process_exception

    try:
        return await coro
    except FILTERED_EXCEPTIONS:
        raise
    except BaseException as exc:
        logger.error("Viola Spoke task '%s' crashed: %s", name, exc, exc_info=exc)
        try:
            capture_process_exception(exc, surface="desktop_spoke", entry_point="viola_spoke.%s" % name)
        except Exception:  # noqa: BLE001, RUF100 - crash handler must never raise
            logger.debug("Spoke task-crash capture failed for %s", name, exc_info=True)
        raise


async def _run_spoke(args: argparse.Namespace) -> None:
    """Main async entry point."""
    from core.sentry_integration import install_asyncio_exception_handler

    # #1419: sys.excepthook/threading.excepthook (installed at import time,
    # above) never fire for an exception that only ever lives inside an
    # asyncio Task nobody awaited. Must be called from inside the running
    # loop, hence here rather than at import time.
    install_asyncio_exception_handler("desktop_spoke", entry_point="viola_spoke")

    from spoke.health import SpokeHealthMonitor
    from spoke.mic_streamer import MicStreamer
    from spoke.tts_receiver import TTSReceiver

    device_id = str(uuid.uuid4())
    spoke_token = os.environ.get("VIOLA_SPOKE_TOKEN", "")

    # -- Resolve hub address -----------------------------------------------
    hub_host = args.hub_host
    hub_port = args.hub_port

    if not hub_host:
        logger.info("No --hub-host specified, attempting mDNS auto-discovery...")
        from spoke.auto_pair import discover_hub

        hub_info = await discover_hub(
            timeout=args.discovery_timeout,
            local_device_id=device_id,
        )
        if hub_info:
            hub_host = hub_info.host
            hub_port = hub_info.port
            logger.info(
                "Auto-discovered hub: %s (%s:%d)",
                hub_info.room_name,
                hub_host,
                hub_port,
            )
        else:
            logger.error(
                "No hub found via mDNS. Specify --hub-host explicitly or "
                "ensure the hub is running with VIOLA_ENABLE_MULTIROOM=1"
            )
            sys.exit(1)

    logger.info(
        "Viola Spoke starting: room=%s, hub=%s:%d, device_id=%s",
        args.room,
        hub_host,
        hub_port,
        device_id[:8],
    )

    # -- Set up spoke pipeline (music PCM from hub) ------------------------
    from audio_core.streaming.pipeline_wiring import (
        setup_spoke_pipeline,
        start_spoke_ws_client,
    )
    from fastapi import FastAPI

    # Minimal app.state container for pipeline_wiring compatibility
    pipeline_app = FastAPI()

    receiver = setup_spoke_pipeline(
        pipeline_app,
        room_id=args.room,
        source_host=hub_host,
        source_port=hub_port,
    )
    if receiver:
        logger.info("Spoke PCM pipeline ready (music playback from hub)")
        # Thread the spoke HMAC token into the outbound /ws/events handshake so
        # an auth-enabled hub accepts the connection (Round 1 F-3, 2026-05-29).
        # Without this header the hub rejects with code 1008 before any
        # pcm_chunk reaches the receiver.
        start_spoke_ws_client(
            pipeline_app,
            hub_host,
            hub_port,
            args.room,
            spoke_token=spoke_token or None,
        )
    else:
        logger.warning("Spoke PCM pipeline setup failed — music relay disabled")

    # -- TTS receiver ------------------------------------------------------
    output_device = args.output_device
    if output_device and output_device.isdigit():
        output_device = int(output_device)
    tts_receiver = TTSReceiver(device=output_device)

    # -- Mic streamer (voice → hub) ----------------------------------------
    input_device = args.input_device
    if input_device and input_device.isdigit():
        input_device = int(input_device)
    mic = MicStreamer(
        hub_host=hub_host,
        hub_port=hub_port,
        room=args.room,
        tts_receiver=tts_receiver,
        input_device=input_device,
        spoke_token=spoke_token or None,
    )

    # -- Health monitor ----------------------------------------------------
    health = SpokeHealthMonitor(hub_host=hub_host, hub_port=hub_port)
    health.start()

    # -- Health HTTP endpoint ----------------------------------------------
    health_app = _create_health_app(health, device_id, args.room)

    import uvicorn

    config = uvicorn.Config(
        health_app,
        host="0.0.0.0",  # nosec B104 — spoke needs network accessibility
        port=args.port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    # -- Run everything concurrently ---------------------------------------
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await asyncio.gather(
            _guarded_spoke_task("mic_streamer", mic.run()),
            _guarded_spoke_task("health_server", server.serve()),
            shutdown_event.wait(),
            return_exceptions=True,
        )
    finally:
        mic.stop()
        health.stop()
        # Teardown spoke pipeline
        from audio_core.streaming.pipeline_wiring import teardown_pipeline

        teardown_pipeline(pipeline_app)
        logger.info("Viola Spoke shutdown complete")


def main() -> None:
    args = parse_args()
    logger.info("=" * 60)
    logger.info("  Viola Spoke — Headless Relay Mode")
    logger.info("  Room: %s", args.room)
    logger.info("=" * 60)

    try:
        asyncio.run(_run_spoke(args))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
