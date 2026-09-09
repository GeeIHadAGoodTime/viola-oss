from __future__ import annotations

import asyncio
from typing import Any

from fastapi.responses import JSONResponse

from config.settings import get_runtime_base_url, settings
from contracts.api_response import failure_response
from core.constants import DEFAULT_API_PORT
from core.logging_config import get_logger
from fastapi import Depends, FastAPI
from ui.api.context import ApiContext
from ui.api.routes.auth_dependencies import require_auth

log = get_logger(__name__)


def register_context_sync(context: ApiContext) -> None:
    app = context.app
    router = context.router
    state = context.bindings.state

    sync_service_instance = None
    conversation_store_instance = None
    device_discovery_instance = None

    disable_discovery = settings.disable_device_discovery

    try:
        from experimental.phase2_multiroom.context_sync import (
            ContextSyncService,
            create_sync_endpoints,
        )
        from experimental.phase2_multiroom.conversation_store import ConversationStore

        device_id = _resolve_device_id(state)

        conversation_store_instance = ConversationStore(device_id=device_id)

        if disable_discovery:
            log.info("Device discovery disabled via VIOLA_DISABLE_DEVICE_DISCOVERY")
        else:
            device_discovery_instance = _initialize_device_discovery(state, device_id)
            if device_discovery_instance:
                app.state.device_discovery = device_discovery_instance
                api_port = _resolve_api_port(state)

                # Defer mDNS start to after uvicorn serves (saves ~2s on critical path)
                async def _deferred_discovery_start(
                    _discovery=device_discovery_instance,
                    _port=api_port,
                    _conv_store=conversation_store_instance,
                    _dev_id=device_id,
                    _state=state,
                ):
                    try:
                        stop_event = _discovery.start(announce_port=_port)
                        app.state.device_discovery_stop_event = stop_event
                        log.info("Device discovery started (deferred)")
                    except Exception as exc:
                        log.warning("Deferred device discovery start failed: %s", exc)
                        return

                    # Start auto-sync after discovery is running
                    try:
                        if hasattr(_state, "settings_manager") and _state.settings_manager:
                            sync_enabled = _state.settings_manager.get(
                                "context_sync_enabled",
                                True,
                            )
                            if sync_enabled:
                                auto_sync_service = ContextSyncService(
                                    _conv_store,
                                    _discovery,
                                    _dev_id,
                                )
                                auto_sync_service.start_auto_sync(interval=10.0)
                                app.state._auto_sync_service = auto_sync_service
                                log.info("Context sync auto-sync started (deferred)")
                    except Exception as exc:
                        log.debug("Auto-sync not started: %s", exc)

                # Starlette >=1.3.1 dropped ``FastAPI.add_event_handler``; the
                # deprecated ``@app.on_event`` shim still works by appending
                # to this same list, so append directly (see
                # ui/api/routes/lifecycle.py's ``_replay_router_startup_handlers``,
                # which is what actually runs this list on desktop boot).
                app.router.on_startup.append(_deferred_discovery_start)

        settings_mgr_for_endpoints = _get_settings_manager_from_state(state)
        sync_service_instance = create_sync_endpoints(
            router,
            conversation_store_instance,
            device_discovery_instance,
            settings_manager=settings_mgr_for_endpoints,
            route_dependencies=[Depends(require_auth)],
        )
        log.info("Context sync endpoints registered")

    except ImportError as exc:
        log.debug("Context sync unavailable (missing dependencies): %s", exc)
    except Exception as exc:
        log.warning("Context sync initialization failed: %s", exc)

    if sync_service_instance or device_discovery_instance:

        async def shutdown_handler():
            # Stop auto-sync service if it was started in deferred handler
            auto_sync = getattr(app.state, "_auto_sync_service", None)
            if auto_sync:
                try:
                    auto_sync.stop_auto_sync()
                except Exception as exc:
                    log.debug("Auto-sync shutdown error: %s", exc)

            await _shutdown_context_services(
                app=app,
                sync_service=sync_service_instance,
                device_discovery=device_discovery_instance,
            )

        # See the startup-side comment above: replaced for the same reason.
        app.router.on_shutdown.append(shutdown_handler)

        if device_discovery_instance:
            app.state.device_discovery = device_discovery_instance
        if sync_service_instance:
            app.state.context_sync_service = sync_service_instance

    if device_discovery_instance:

        @router.get("/v1/sync/devices", dependencies=[Depends(require_auth)])
        async def get_discovered_devices():
            if not device_discovery_instance:
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "device_discovery_unavailable",
                        "Device discovery is temporarily unavailable.",
                        data={"devices": []},
                    ),
                )

            try:
                devices = device_discovery_instance.get_discovered_devices()
                device_payloads = [
                    {
                        "device_id": d.device_id,
                        "device_name": d.device_name,
                        "host": d.host,
                        "port": d.port,
                        "last_seen": d.last_seen,
                        "capabilities": d.capabilities,
                    }
                    for d in devices
                ]
                return {"ok": True, "devices": device_payloads}
            except Exception:
                log.exception("Failed to get discovered devices")
                return JSONResponse(
                    status_code=500,
                    content=failure_response(
                        "discovered_devices_unavailable",
                        "Try again in a moment",
                        data={"devices": []},
                    ),
                )


async def _shutdown_context_services(
    *,
    app: FastAPI,
    sync_service: Any | None,
    device_discovery: Any | None,
) -> None:
    if sync_service:
        try:
            sync_service.stop_auto_sync()
        except Exception as exc:
            log.debug("Context sync shutdown error: %s", exc)

    if device_discovery:
        try:
            log.info("Stopping device discovery service...")
            device_discovery.stop()
        except Exception as exc:
            log.debug("Device discovery shutdown error: %s", exc)

    stop_event = getattr(app.state, "device_discovery_stop_event", None)
    if stop_event:
        try:
            await asyncio.to_thread(stop_event.wait, 2.0)
        except Exception as exc:
            log.debug("Error waiting for discovery stop event: %s", exc)
        finally:
            app.state.device_discovery_stop_event = None


def _is_mock(obj: Any) -> bool:
    """Check if object is a unittest Mock."""
    return obj.__class__.__name__ in ("Mock", "MagicMock", "NonCallableMock")


def _resolve_device_id(state: Any) -> str | None:
    try:
        if _is_mock(state):
            return None
        if hasattr(state, "settings_manager") and state.settings_manager:
            if _is_mock(state.settings_manager):
                return None
            device_id = state.settings_manager.get("device_id")
            if not device_id or _is_mock(device_id):
                import uuid

                device_id = str(uuid.uuid4())
                state.settings_manager.set("device_id", device_id, save_immediately=False)
            return device_id
    except Exception as exc:
        log.debug("Failed to resolve device_id: %s", exc)
        return None
    return None


def _initialize_device_discovery(state: Any, device_id: str | None):
    try:
        from experimental.phase2_multiroom.device_discovery import DeviceDiscovery

        device_name = None
        try:
            if not _is_mock(state) and hasattr(state, "settings_manager") and state.settings_manager:
                if not _is_mock(state.settings_manager):
                    device_name = state.settings_manager.get("device_name")
                    if _is_mock(device_name):
                        device_name = None
        except Exception as exc:
            log.debug("Failed to get device_name: %s", exc)
            device_name = None

        discovery = DeviceDiscovery(device_id=device_id, device_name=device_name)
        return discovery
    except ImportError:
        log.debug("Device discovery not available (zeroconf may not be installed)")
    except Exception as exc:
        log.debug("Device discovery unavailable: %s", exc)
    return None


def _resolve_api_port(state: Any) -> int:
    try:
        if not _is_mock(state) and hasattr(state, "settings_manager") and state.settings_manager:
            if not _is_mock(state.settings_manager):
                api_port = state.settings_manager.get("api_port")
                if _is_mock(api_port):
                    api_port = None
                elif isinstance(api_port, str):
                    api_port = api_port.strip()
                if api_port:
                    return int(api_port)
    except Exception as exc:
        log.debug("Failed to read api_port from settings manager: %s", exc)

    try:
        from config import settings as config_settings

        return config_settings.api_port
    except Exception as exc:
        log.debug("Falling back to runtime base URL for port extraction: %s", exc)
        # Extract port from runtime base URL (e.g., "http://127.0.0.1:8756" -> 8756)
        try:
            base_url = get_runtime_base_url()
            if ":" in base_url:
                port_str = base_url.split(":")[-1].rstrip("/")
                return int(port_str)
        except Exception as e:
            log.debug("Port extraction from runtime base URL failed (non-critical): %s", e)
            pass
        # Ultimate fallback
        return DEFAULT_API_PORT


def _get_settings_manager_from_state(state: Any):
    try:
        if _is_mock(state):
            return None
        if hasattr(state, "settings_manager") and state.settings_manager:
            if _is_mock(state.settings_manager):
                return None
            return state.settings_manager
    except Exception as exc:
        log.debug("Failed to get settings_manager from state: %s", exc)
        return None
    return None


__all__ = ["register_context_sync"]
