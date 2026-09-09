from __future__ import annotations

from typing import Any, cast

from bootstrap.factory import Bootstrap, bootstrap_viola
from core.constants import DEFAULT_API_PORT, LOCALHOST
from core.logging_config import get_logger
from core.server_factory import StoppableServer, UvicornServer, create_server
from diagnostics.startup_telemetry import record_process_start

logger = get_logger(__name__)


def bootstrap(
    host: str = LOCALHOST,
    port: int = DEFAULT_API_PORT,
    voice: bool = True,
    wake: bool = True,
    desktop_mode: bool = False,
) -> Bootstrap:
    """
    Bootstrap all Viola subsystems using the modern factory pipeline.

    Returns a ``Bootstrap`` dataclass that mirrors the legacy interface exposed
    by ``viola_main.bootstrap`` so that existing code (Qt client, tests, etc.)
    keeps working unchanged.
    """
    record_process_start()
    result = bootstrap_viola(
        host=host,
        port=port,
        voice=voice,
        wake=wake,
        desktop_mode=desktop_mode,
    )

    uvicorn_server = result.uvicorn_server
    uvicorn_handle: StoppableServer | None = None
    if uvicorn_server is not None and hasattr(uvicorn_server, "stop"):
        uvicorn_handle = cast(StoppableServer, uvicorn_server)

    return Bootstrap(
        state=result.state,
        music=result.music,
        tts=result.tts,
        intent=result.intent,
        app=result.app,
        voice=result.voice,
        uvicorn_server=uvicorn_handle,
        runtime_profile=getattr(result, "runtime_profile", None),
        telemetry_scheduler=getattr(result, "telemetry_scheduler", None),
        health_checker=getattr(result, "health_checker", None),
        wake_data_services=getattr(result, "wake_data_services", ()),
        voice_disabled_reason=getattr(result, "voice_disabled_reason", None),
        voice_missing_dependencies=getattr(result, "voice_missing_dependencies", ()),
        degraded_components=getattr(result, "degraded_components", ()),
    )


def run_uvicorn(app: Any, host: str, port: int) -> StoppableServer:
    """
    Start the FastAPI application using the shared server factory.

    Returns an ``UvicornServer`` wrapper that can be stopped gracefully.
    """
    logger.info(
        "🔧 run_uvicorn called: host=%s, port=%d, app=%s",
        host,
        port,
        type(app).__name__,
    )
    # One shared TLS context per process for all httpx clients. The codebase builds
    # httpx clients per call across 50+ sites; without this each build runs
    # create_default_context (~150-280ms, GIL-held) and freezes the desktop UI
    # (shared interpreter lock). Installed here, before any request-path client.
    try:
        from core.http_ssl import install_shared_ssl_context

        install_shared_ssl_context()
    except ImportError as exc:  # optional perf shim; never block startup
        logger.debug("shared SSL context shim not installed: %s", exc)
    try:
        server = create_server(app, host, port)
        logger.info("✅ run_uvicorn completed successfully, server=%s", type(server).__name__)
        return server
    except Exception as e:
        logger.exception("❌ run_uvicorn failed: %s", e)
        raise


def start_backend_server(
    host: str = LOCALHOST,
    port: int = DEFAULT_API_PORT,
    voice: bool = False,
    wake: bool = False,
    desktop_mode: bool = False,
) -> Bootstrap:
    """
    Convenience helper used by scripts/tests to start a full backend instance.
    """
    boot = bootstrap(
        host=host,
        port=port,
        voice=voice,
        wake=wake,
        desktop_mode=desktop_mode,
    )
    if boot.state:
        mark_not_ready = getattr(boot.state, "mark_not_ready", None)
        if callable(mark_not_ready):
            mark_not_ready()
        if boot.degraded_components:
            logger.warning(
                "Marking ready in DEGRADED mode — unavailable: %s",
                ", ".join(boot.degraded_components),
            )
    if boot.app:
        boot.uvicorn_server = run_uvicorn(boot.app, host, port)
    if voice and boot.voice:
        boot.voice.start()
    return boot
