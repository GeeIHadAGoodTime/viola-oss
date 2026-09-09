from __future__ import annotations

import asyncio
import datetime
import inspect
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

from bootstrap.lazy_intent import LazyIntentBridge
from config.settings import get_runtime_base_url
from core.constants import (
    TIMEOUT_5_MINUTES,
    TIMEOUT_10_MINUTES,
    TIMEOUT_LONG,
)
from core.logging_config import get_logger
from fastapi import FastAPI
from ui.api.context import ApiContext
from ui.core.bindings import BroadcasterTaskState
from ui.core.player_state import to_player_state

log = get_logger(__name__)


async def _replay_router_startup_handlers(app: FastAPI) -> None:
    """Run the ``@app.on_event("startup")`` handlers registered elsewhere.

    Starlette's ``Router`` dropped the explicit ``.startup()``/``.shutdown()``
    methods once the lifespan protocol became the only supported mechanism
    (reached in the ``starlette==1.3.1`` pin — see requirements_desktop.txt's
    SEC #3335 note). ``@app.on_event(...)`` is still a working deprecated
    shim that appends callables to ``app.router.on_startup`` /
    ``.on_shutdown``, but nothing replays that list automatically once this
    module overwrites ``app.router.lifespan_context`` below (see
    ``tools/write_registry.py``'s "dead in production" gotcha) -- the old
    ``await app.router.startup()`` call this replaces silently regressed
    into an unconditional ``AttributeError`` that crashed every desktop/CI
    daemon boot, so on_startup/on_shutdown registrants (auth DB init,
    weather prefetch, Redis backend, asyncio-safe main-loop registration)
    were dead AND the whole app failed to start.
    """
    for handler in getattr(app.router, "on_startup", []):
        result = handler()
        if inspect.isawaitable(result):
            await result


async def _replay_router_shutdown_handlers(app: FastAPI) -> None:
    """Shutdown-side twin of ``_replay_router_startup_handlers`` above."""
    for handler in getattr(app.router, "on_shutdown", []):
        result = handler()
        if inspect.isawaitable(result):
            await result


def _log_task_exception(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception:
        return
    if exc:
        log.error("Background task failed: %s", exc)


def _resolve_broadcast_user_id(hub: Any) -> str | None:  # hub parameter kept for call-site backward-compat
    """Resolve the current broadcast user for background/non-request callers.

    On desktop: returns the request-scoped user when in an HTTP request, else
    the desktop active principal (logged-in account or device identity) so the
    periodic player-state broadcaster and music-state callbacks never pass
    ``user_id=None`` to EventHub.broadcast.

    On cloud: raises LookupError when there is no request-scoped user — cloud
    code must always carry a principal.  The caller catches LookupError and
    skips the broadcast.

    The old fallback that inspected ``hub._user_clients`` for a single entry
    was unreliable — it returned None at startup before any WebSocket client
    connected, causing ``EventHub.broadcast refused userless broadcast for
    event state`` on every player state change.
    """
    try:
        from core.user_context import get_current_or_device_user_id

        return get_current_or_device_user_id()
    except LookupError:
        return None


def configure_lifecycle(context: ApiContext) -> None:
    app = context.app
    hub = context.hub
    music = context.bindings.music
    state = context.bindings.state
    weather_cache = context.weather_cache

    broadcaster_state = BroadcasterTaskState()

    async def warm_intent_bridge_before_ready() -> None:
        intent_proxy = context.bindings.intent
        if not isinstance(intent_proxy, LazyIntentBridge):
            return
        if intent_proxy.is_materialized():
            return

        start = time.perf_counter()
        log.info("Warming IntentBridge before marking backend ready")
        await asyncio.to_thread(intent_proxy.materialize)
        elapsed = time.perf_counter() - start
        log.info("IntentBridge warm-up completed in %.3fs", elapsed)

    def _read_player_state_as_active_principal(hub_authority) -> dict:
        """Read player state under the desktop active principal.

        The broadcaster runs outside any HTTP request, so no auth middleware
        ever bound ``current_user_id`` for it. Desktop is one-user-per-install:
        the active principal (logged-in account, else the device identity) is
        the real owner of player state. Without this scope the read raised
        LookupError every 5 s and the UI was fed empty state forever
        (lane-3 MF-A).
        """
        from core.user_context import desktop_active_principal_scope

        with desktop_active_principal_scope():
            return to_player_state(music, state, hub_authority=hub_authority).model_dump()

    async def periodic_state_broadcaster() -> None:
        log.info("Starting periodic state broadcaster (every 5 seconds, smart mode)")
        last_state_hash = None
        hub_authority = getattr(app.state, "hub_state_authority", None)

        while not broadcaster_state.should_stop:
            try:
                await asyncio.sleep(5.0)
                if hub._clients:
                    try:
                        player_state = await asyncio.to_thread(
                            _read_player_state_as_active_principal,
                            hub_authority,
                        )
                        # Inject multiroom mute fields so React can
                        # mute YouTube when hub-local is active.
                        from utils.api_helpers import (
                            inject_multiroom_info,
                            inject_preferences,
                        )

                        inject_preferences(player_state)
                        inject_multiroom_info(player_state)

                        state_hash = (
                            player_state.get("is_playing"),
                            (
                                player_state.get("now_playing", {}).get("id")
                                if player_state.get("now_playing")
                                else None
                            ),
                            player_state.get("volume"),
                            len(player_state.get("queue", [])),
                            player_state.get("position", 0) // 2 * 2,
                            player_state.get("yt_hub_muted"),
                            player_state.get("hub_local_playback_active"),
                            player_state.get("cef_active"),
                        )

                        if state_hash != last_state_hash:
                            await hub.broadcast(
                                "state",
                                player_state,
                                user_id=_resolve_broadcast_user_id(hub),
                                force=False,
                            )
                            last_state_hash = state_hash
                            log.debug("State changed, broadcasting update")
                    except Exception as exc:
                        log.debug("State broadcast error (non-critical): %s", exc)
            except asyncio.CancelledError:
                log.info("Periodic state broadcaster cancelled")
                break
            except Exception as exc:
                log.exception("Periodic state broadcaster error: %s", exc)
                await asyncio.sleep(5.0)

        log.info("Periodic state broadcaster stopped")

    async def background_weather_refresh() -> None:
        def is_low_usage_hour() -> bool:
            hour = datetime.datetime.now().hour
            return 3 <= hour < 5

        async def refresh_weather_for_key(cache_key: str, entry: dict[str, Any]) -> None:
            """Refresh a single weather cache entry via internal API call.

            cache_key comes from snapshot() which strips the 'weather_' prefix,
            so keys are either city names (e.g. 'milwaukee') or coordinates
            (e.g. '40.7128,74.0060').
            """
            try:
                import aiohttp

                # Build query params from cache key
                params: dict[str, str] = {}
                if "," in cache_key:
                    parts = cache_key.split(",")
                    if len(parts) == 2:
                        try:
                            params["lat"] = str(float(parts[0]))
                            params["lon"] = str(float(parts[1]))
                        except ValueError:
                            params["city"] = cache_key
                else:
                    params["city"] = cache_key

                base = get_runtime_base_url()
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                url = f"{base}/v1/weather?{qs}"
                headers: dict[str, str] = {}
                try:
                    from ui.security.config import get_security_config

                    sec = get_security_config()
                    if sec.auth_api_key:
                        headers["X-API-Key"] = sec.auth_api_key
                except Exception:
                    log.debug("Auth config unavailable for proactive weather refresh")

                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=TIMEOUT_LONG),
                    ) as resp:
                        if resp.status == 200:
                            log.debug("Proactive refresh successful for %s", cache_key)
                        else:
                            log.debug(
                                "Proactive refresh failed for %s (status %s)",
                                cache_key,
                                resp.status,
                            )
            except Exception as exc:
                log.debug("Error refreshing weather for %s: %s", cache_key, exc)

        while True:
            try:
                await asyncio.sleep(TIMEOUT_10_MINUTES)

                if is_low_usage_hour():
                    log.info("Low-usage hour: Refreshing all weather caches")
                    all_cached = weather_cache.snapshot()
                    for cache_key, entry in all_cached.items():
                        await refresh_weather_for_key(cache_key, entry)
                else:
                    all_cached = weather_cache.snapshot()
                    for cache_key, entry in all_cached.items():
                        updated_str = entry.get("updated", "")
                        if updated_str:
                            try:
                                updated = datetime.datetime.fromisoformat(updated_str)
                                age_hours = (datetime.datetime.now() - updated).total_seconds() / 3600
                                # Use default TTL since get_ttl_for_condition doesn't exist
                                ttl_hours = 0.25  # 15 minutes
                                if age_hours > ttl_hours * 0.9:
                                    log.debug(
                                        "Proactive refresh for %s (age: %.1fh, TTL: %.1fh)",
                                        cache_key,
                                        age_hours,
                                        ttl_hours,
                                    )
                                    await refresh_weather_for_key(cache_key, entry)
                            except Exception as exc:
                                log.debug(
                                    "Error checking cache age for %s: %s",
                                    cache_key,
                                    exc,
                                )
            except asyncio.CancelledError:
                log.info("Background weather refresh stopped")
                break
            except Exception as exc:
                log.error("Error in background weather refresh: %s", exc)
                await asyncio.sleep(TIMEOUT_5_MINUTES)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("[STARTUP] Phase: lifespan_begin")
        log.info("Connecting music player to WebSocket broadcaster...")
        if hasattr(state, "mark_not_ready"):
            state.mark_not_ready()

        # This custom lifespan replaces FastAPI's default one, so we must
        # explicitly run any startup handlers registered elsewhere (for example
        # auth DB initialization in ui.server).
        await _replay_router_startup_handlers(app)

        if not hasattr(app.state, "background_tasks"):
            app.state.background_tasks = []
        background_tasks = cast(list[asyncio.Task[Any]], app.state.background_tasks)

        broadcaster_state.should_stop = False
        if broadcaster_state.task is None or broadcaster_state.task.done():
            broadcaster_state.task = asyncio.create_task(periodic_state_broadcaster())
            broadcaster_state.task.add_done_callback(_log_task_exception)

        try:
            try:
                weather_task = asyncio.create_task(background_weather_refresh())
                weather_task.add_done_callback(_log_task_exception)
            except Exception as exc:
                log.warning("Failed to start background weather refresh: %s", exc)
            else:
                background_tasks.append(weather_task)
                log.info("Background weather refresh started")

            # macOS keychain pre-warm (parity with cloud_app's startup pre-warm).
            # The memory-encryption secret and the SecureSettingsManager master
            # key live in the OS keychain. On a freshly-signed macOS app the
            # first access pops a *blocking* SecurityAgent dialog; if that
            # happened lazily mid-command on the event loop it froze the whole
            # server (Windows uses DPAPI, which is silent — so this freeze is
            # macOS-specific and a real parity gap). Touch them once on a worker
            # thread at startup so any prompt fires OFF the event loop; the
            # server stays responsive and later command-path reads are fast.
            async def _prewarm_macos_keychain_secrets() -> None:
                import sys as _sys

                if not _sys.platform.startswith("darwin"):
                    return
                try:
                    from services.memory.store import _get_memory_encryption

                    await asyncio.to_thread(lambda: _get_memory_encryption().encryption_available)
                except Exception as exc:  # noqa: BLE001, RUF100 - pre-warm is best-effort
                    log.debug("memory-encryption keychain pre-warm skipped: %s", exc)
                try:
                    from utils.enhancements.secrets import SecureSettingsManager

                    await asyncio.to_thread(lambda: SecureSettingsManager(app_name="viola").encryption_enabled)
                except Exception as exc:  # noqa: BLE001, RUF100 - pre-warm is best-effort
                    log.debug("settings keychain pre-warm skipped: %s", exc)
                log.info("macOS keychain secrets pre-warmed off the event loop")

            try:
                prewarm_task = asyncio.create_task(_prewarm_macos_keychain_secrets())
                prewarm_task.add_done_callback(_log_task_exception)
                background_tasks.append(prewarm_task)
            except Exception as exc:  # noqa: BLE001, RUF100 - pre-warm is best-effort
                log.debug("keychain pre-warm task not started: %s", exc)

            # Start messaging platform listeners (Telegram, Slack)
            # Deferred into a background task so that accessing
            # context.bindings.intent (a LazyIntentBridge) does NOT
            # materialize the IntentBridge + LLM SDK chain during startup.
            async def _deferred_messaging_start() -> None:
                try:
                    poll_interval = 0.5
                    max_wait = 30.0
                    waited = 0.0
                    intent_wait_timed_out = False

                    while waited < max_wait:
                        pipeline = getattr(app.state, "intent_pipeline", None)
                        if pipeline is not None:
                            break

                        _intent_proxy = context.bindings.intent
                        if not isinstance(_intent_proxy, LazyIntentBridge):
                            break

                        if object.__getattribute__(_intent_proxy, "_real") is not None:
                            break

                        await asyncio.sleep(poll_interval)
                        waited += poll_interval
                    else:
                        intent_wait_timed_out = True
                        log.error(
                            "Messaging start proceeding after %.1fs wait; intent still not materialized",
                            max_wait,
                        )

                    from messaging.router import MessageRouter

                    pipeline = getattr(app.state, "intent_pipeline", None)
                    if pipeline is None:
                        _intent_proxy = context.bindings.intent
                        if _intent_proxy is not None:
                            if (
                                isinstance(_intent_proxy, LazyIntentBridge)
                                and object.__getattribute__(_intent_proxy, "_real") is None
                                and not intent_wait_timed_out
                            ):
                                log.debug("Skipping messaging start - intent not yet materialized")
                                return
                            pipeline = await asyncio.to_thread(getattr, _intent_proxy, "_pipeline", _intent_proxy)
                    if pipeline is not None:
                        message_router = MessageRouter()
                        await message_router.start(pipeline)
                        app.state.message_router = message_router
                        log.info("Messaging router started (deferred)")
                        # Register the hub singleton for outbound messaging
                        try:
                            from messaging.hub import register_messaging_hub

                            register_messaging_hub(message_router)
                        except Exception:
                            log.debug("messaging hub registration skipped")
                except Exception as exc:
                    log.debug("Messaging router not started: %s", exc)

            _msg_task = asyncio.create_task(_deferred_messaging_start())
            _msg_task.add_done_callback(_log_task_exception)
            background_tasks.append(_msg_task)

            try:
                main_loop = asyncio.get_running_loop()
                hub._main_loop = main_loop
                log.info("Main event loop captured for cross-thread broadcasts")
                # Also wire the hub broadcaster if it was created before
                # the event loop was running (sync bootstrap path).
                broadcaster = getattr(app.state, "hub_broadcaster", None)
                if broadcaster is not None:
                    broadcaster.set_event_loop(main_loop)
                    log.info("Hub broadcaster event loop set from startup")
            except RuntimeError:
                log.warning("Could not capture main event loop during startup")

            _setup_automatic_state_broadcast(hub=hub, music=music, state=state)
            log.info("Automatic state updates enabled")

            # Wire autoplay → WebSocket state broadcast
            _autoplay = getattr(music, "autoplay", None) or getattr(music, "autoplay_controller", None)
            if _autoplay is not None and hasattr(_autoplay, "set_broadcast_context"):
                _autoplay.set_broadcast_context(hub, main_loop)
                log.warning(
                    "[QUEUE_TRACE] WIRING SUCCESS autoplay=%s hub=%s",
                    _autoplay,
                    hub,
                )
            else:
                log.warning(
                    "[QUEUE_TRACE] WIRING FAILED autoplay=%s music_type=%s attrs=%s",
                    _autoplay,
                    type(music).__name__,
                    [a for a in dir(music) if "auto" in a.lower()],
                )

            log.info("Transcription endpoint registered at: POST /api/v1/transcribe")

            # Start scheduler if wired
            _scheduler = getattr(app.state, "scheduler", None)
            if _scheduler is not None:
                try:
                    await _scheduler.start()
                    log.info("Scheduler started from lifespan")
                except Exception as exc:
                    log.warning("Failed to start scheduler from lifespan: %s", exc)

            # Start calendar reminder sweep service if wired
            _calendar_reminders = getattr(app.state, "calendar_reminders", None)
            if _calendar_reminders is not None:
                try:
                    await _calendar_reminders.start()
                    log.info("Calendar reminder service started from lifespan")
                except Exception as exc:  # noqa: BLE001, RUF100 -- optional service start must fail open
                    log.warning("Failed to start calendar reminders from lifespan: %s", exc)

            # Drain the push notification queue (#4790). PushNotificationService
            # parks NORMAL-priority and quiet-hours-deferred notifications in an
            # in-memory queue that only `_queue_loop` empties -- and nothing in
            # the product ever called `start()`, so every parked notification
            # sat there until the process exited. Start it here alongside the
            # other lifespan services so a deferred notification is eventually
            # delivered instead of silently discarded.
            try:
                from services.notifications.push_service import get_push_service

                _push_service = get_push_service()
                await _push_service.start()
                app.state.push_service = _push_service
                log.info("Push notification queue processor started from lifespan")
            except Exception as exc:  # noqa: BLE001, RUF100 -- optional service start must fail open
                log.warning("Failed to start push notification service from lifespan: %s", exc)

            # Start health watchdog if wired
            _watchdog = getattr(app.state, "health_watchdog", None)
            if _watchdog is not None:
                try:
                    _watchdog.start()
                    log.info("Health watchdog started")
                except Exception as exc:
                    log.warning("Failed to start health watchdog: %s", exc)

            # Clean up orphan guest-checkout accounts on startup. Desktop/SQLite
            # only: cloud guest checkout is rejected outright (see the
            # `app_surface == "cloud"` guard in billing/routes.py), and the
            # legacy pre-GoTrue `public.users` table this cleanup targets was
            # dropped from cloud Postgres by migration 066, so
            # `PgUserRepository` no longer implements `cleanup_orphan_accounts`
            # (#2842). Skip cleanly on Postgres via the same backend-detection
            # helper account_lookup already uses, rather than relying on the
            # resulting AttributeError being swallowed below.
            async def _cleanup_orphan_accounts() -> None:
                try:
                    from auth.account_lookup import is_postgres_auth_db
                    from auth.database import get_auth_db

                    db = get_auth_db()
                    if is_postgres_auth_db(db):
                        log.debug("Orphan account cleanup skipped: Postgres backend has no legacy users table")
                        return
                    deleted = await db.users.cleanup_orphan_accounts(max_age_hours=168)
                    if deleted:
                        log.info(
                            "Startup orphan cleanup: removed %d orphan accounts",
                            deleted,
                        )
                except Exception:
                    log.debug("Orphan account cleanup skipped", exc_info=True)

            _orphan_task = asyncio.create_task(_cleanup_orphan_accounts())
            _orphan_task.add_done_callback(_log_task_exception)
            background_tasks.append(_orphan_task)

            log.info("IntentBridge warm-up deferred until post-bind background initialization")

            log.info("[STARTUP] Phase: services_initialized")
            if hasattr(state, "mark_ready"):
                state.mark_ready()
            else:
                log.info("Backend fully initialized - marking ready")

            log.info("[STARTUP] Phase: STARTUP_COMPLETE")
            yield
        finally:
            log.info("[STARTUP] Phase: shutdown_begin")
            if hasattr(state, "mark_not_ready"):
                state.mark_not_ready()
            broadcaster_state.should_stop = True
            broadcaster_task = broadcaster_state.task
            if broadcaster_task is not None:
                broadcaster_task.cancel()
                try:
                    await broadcaster_task
                except asyncio.CancelledError:
                    log.debug("Periodic state broadcaster task cancelled during shutdown")
                broadcaster_state.task = None
            log.info("Periodic state broadcaster stopped")

            # Stop scheduler
            _scheduler = getattr(app.state, "scheduler", None)
            if _scheduler is not None:
                try:
                    await _scheduler.stop()
                    log.info("Scheduler stopped")
                except Exception as exc:
                    log.warning("Failed to stop scheduler: %s", exc)

            # Stop calendar reminder service
            _calendar_reminders = getattr(app.state, "calendar_reminders", None)
            if _calendar_reminders is not None:
                try:
                    await _calendar_reminders.stop()
                    log.info("Calendar reminder service stopped")
                except Exception as exc:  # noqa: BLE001, RUF100 -- best-effort shutdown must not raise
                    log.warning("Failed to stop calendar reminders: %s", exc)

            # Stop the push notification queue processor (#4790)
            _push_service = getattr(app.state, "push_service", None)
            if _push_service is not None:
                try:
                    await _push_service.stop()
                    log.info("Push notification queue processor stopped")
                except Exception as exc:  # noqa: BLE001, RUF100 -- best-effort shutdown must not raise
                    log.warning("Failed to stop push notification service: %s", exc)

            # Stop health watchdog
            _watchdog = getattr(app.state, "health_watchdog", None)
            if _watchdog is not None:
                try:
                    _watchdog.stop()
                    log.info("Health watchdog stopped")
                except Exception as exc:
                    log.warning("Failed to stop health watchdog: %s", exc)

            # Shutdown messaging platforms
            _msg_router = getattr(app.state, "message_router", None)
            if _msg_router is not None:
                try:
                    await _msg_router.shutdown()
                except Exception as exc:
                    log.debug("Message router shutdown error: %s", exc)

            background_tasks = cast(list[asyncio.Task[Any]], getattr(app.state, "background_tasks", []))
            if background_tasks:
                log.info("Cancelling %d background tasks...", len(background_tasks))
                for task in list(background_tasks):
                    if task and not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            log.debug("Background task cancelled during shutdown")
                app.state.background_tasks = []
                log.info("Background tasks cancelled")

            await _replay_router_shutdown_handlers(app)

    app.router.lifespan_context = lifespan


def _setup_automatic_state_broadcast(*, hub: Any, music: Any, state: Any) -> None:
    try:
        music_player = music.player if hasattr(music, "player") else music
        if not hasattr(music_player, "on_state_change"):
            log.warning("Music player does not support on_state_change callback")
            return

        import threading
        import time
        from dataclasses import asdict, is_dataclass

        last_state_change_time: list[float] = [0.0]
        pending_state_change: list[dict[str, Any] | None] = [None]
        last_broadcast_is_playing: list[bool | None] = [None]
        state_debounce_lock = threading.Lock()
        state_debounce_ms = 200

        # Spoke forwarding dedup tracking
        last_fwd_video_id: list[str | None] = [None]
        last_fwd_is_playing: list[bool] = [False]
        last_fwd_time: list[float] = [0.0]

        def on_music_state_change(player_state: Any) -> None:
            try:
                if hasattr(player_state, "model_dump"):
                    state_dict = player_state.model_dump()
                elif is_dataclass(player_state):
                    state_dict = asdict(cast(Any, player_state))
                elif isinstance(player_state, dict):
                    state_dict = player_state
                else:
                    log.warning("Unexpected player state type: %s", type(player_state))
                    return

                # Inject multiroom fields (cef_active, yt_hub_muted, etc.)
                # so the FIRST WS broadcast after play includes cef_active,
                # preventing React from rendering a duplicate YouTube iframe.
                try:
                    from utils.api_helpers import inject_multiroom_info

                    inject_multiroom_info(state_dict)
                except Exception:
                    pass

                current_time = time.time() * 1000
                with state_debounce_lock:
                    time_since_last = current_time - last_state_change_time[0]
                    pending_state_change[0] = state_dict
                    incoming_is_playing = bool(state_dict.get("is_playing"))
                    playback_toggle = (
                        last_broadcast_is_playing[0] is not None and incoming_is_playing != last_broadcast_is_playing[0]
                    )
                    if time_since_last < state_debounce_ms and not playback_toggle:
                        log.debug(
                            "State change debounced (%dms < %dms)",
                            int(time_since_last),
                            state_debounce_ms,
                        )
                        return
                    if playback_toggle:
                        log.debug(
                            "Bypassing state debounce for playback toggle: %s -> %s",
                            last_broadcast_is_playing[0],
                            incoming_is_playing,
                        )
                    last_state_change_time[0] = current_time
                    state_dict_to_broadcast = pending_state_change[0]
                    pending_state_change[0] = None

                if state_dict_to_broadcast is None:
                    return

                now_playing = state_dict_to_broadcast.get("now_playing")
                if now_playing:
                    title = (
                        now_playing.get("title", "Unknown")
                        if isinstance(now_playing, dict)
                        else getattr(now_playing, "title", "Unknown")
                    )
                    artist = (
                        now_playing.get("artist")
                        if isinstance(now_playing, dict)
                        else getattr(now_playing, "artist", None)
                    )
                    video_id = (
                        now_playing.get("video_id")
                        if isinstance(now_playing, dict)
                        else getattr(now_playing, "video_id", None)
                    )
                    log.info(
                        "State change: '%s' by '%s' (video_id: %s)",
                        title,
                        artist,
                        video_id,
                    )
                else:
                    log.debug("State change: no song playing")

                try:
                    main_loop = getattr(hub, "_main_loop", None)
                    if main_loop is None:
                        log.warning("Main event loop not available for broadcast (app not fully started?)")
                        return

                    # Capture state_dict in closure to avoid late binding issues
                    state_to_send = state_dict_to_broadcast
                    user_id = _resolve_broadcast_user_id(hub)

                    def schedule_broadcast():
                        """Schedule broadcast and add error callback."""
                        task = asyncio.ensure_future(hub.broadcast("state", state_to_send, user_id=user_id))

                        def on_task_done(t):
                            exc = t.exception() if not t.cancelled() else None
                            if exc:
                                log.error(
                                    "STATE_BROADCAST_FAILED: %s (volume=%s)",
                                    exc,
                                    state_to_send.get("volume"),
                                )

                        task.add_done_callback(on_task_done)

                    main_loop.call_soon_threadsafe(schedule_broadcast)
                except Exception as exc:
                    log.warning("Failed to schedule broadcast: %s", exc)

                log.info(
                    "STATE_BROADCAST_SCHEDULED: volume=%s is_playing=%s",
                    state_dict_to_broadcast.get("volume"),
                    state_dict_to_broadcast.get("is_playing"),
                )
                last_broadcast_is_playing[0] = bool(state_dict_to_broadcast.get("is_playing"))

                # ── Spoke forwarding (consolidated single point) ──
                fwd_is_playing = bool(state_dict_to_broadcast.get("is_playing"))
                fwd_np = state_dict_to_broadcast.get("now_playing")
                fwd_video_id = (fwd_np.get("video_id") or fwd_np.get("url")) if isinstance(fwd_np, dict) else None
                fwd_now = time.time()
                was_playing = last_fwd_is_playing[0]
                prev_vid = last_fwd_video_id[0]
                raw_position = state_dict_to_broadcast.get("position", 0)
                try:
                    fwd_position = float(raw_position) if raw_position is not None else 0.0
                except (TypeError, ValueError):
                    fwd_position = 0.0

                fwd_command: str | None = None
                fwd_kwargs: dict[str, object] = {}

                if fwd_is_playing and fwd_video_id and fwd_video_id != prev_vid:
                    # New track → forward play
                    fwd_command = "play"
                    fwd_kwargs = {
                        "track_url": fwd_video_id,
                        "position": max(0.0, fwd_position),
                        "play_at": fwd_now + 2.0,
                    }
                    last_fwd_video_id[0] = fwd_video_id
                elif fwd_is_playing and not was_playing:
                    # Was paused → resume
                    fwd_command = "resume"
                    fwd_kwargs = {"position": max(0.0, fwd_position)}
                elif not fwd_is_playing and was_playing:
                    # Was playing → pause or stop
                    fwd_command = "pause" if fwd_np else "stop"

                last_fwd_is_playing[0] = fwd_is_playing

                if fwd_command and main_loop:
                    try:
                        from services.multiroom.command_forwarder import (
                            get_command_forwarder,
                        )

                        fwd_inst = get_command_forwarder()
                        _cmd = fwd_command
                        _kw = fwd_kwargs
                        _user_id = user_id

                        def schedule_spoke_forward():
                            if not _user_id:
                                log.debug(
                                    "[SPOKE_FWD] skipped %s — no resolved user_id",
                                    _cmd,
                                )
                                return
                            asyncio.ensure_future(
                                fwd_inst.forward_to_group(
                                    _cmd,
                                    "local",
                                    user_id=_user_id,
                                    **_kw,
                                )
                            )

                        main_loop.call_soon_threadsafe(schedule_spoke_forward)
                        last_fwd_time[0] = fwd_now
                        log.info(
                            "[SPOKE_FWD] lifecycle: forwarding %s " "video_id=%s trigger=state_change",
                            _cmd,
                            fwd_video_id,
                        )
                    except Exception as fwd_exc:
                        log.debug(
                            "[SPOKE_FWD] lifecycle: forwarding skipped: %s",
                            fwd_exc,
                        )
            except Exception as exc:
                log.exception("Error in on_music_state_change callback: %s", exc)

        music_player.on_state_change = on_music_state_change
        log.info("STATE_CALLBACK_WIRED: music_player.on_state_change set to on_music_state_change")
        log.info("Music player connected to WebSocket broadcaster - automatic state updates enabled!")
    except Exception as exc:
        log.exception("Failed to setup automatic state broadcast: %s", exc)


__all__ = ["configure_lifecycle"]
