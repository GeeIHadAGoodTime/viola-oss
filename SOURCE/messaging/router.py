"""Message router -- starts and manages all messaging platform listeners.

On startup, checks which platforms are enabled (token present + enabled flag)
and launches each as an async background task.

Credentials are read from SettingsManager first (the runtime source of truth
for user preferences set via the Settings UI), falling back to AppConfig
(environment variables / .env) for backwards compatibility.

IMPORTANT: Two separate Telegram bots exist in this project:
  1. Viola bot (8061354581) — the user-facing assistant in messaging/channels/telegram.py
  2. Manager bot (8515233452) — Claude Code agent coordination in .viola/agents/telegram_bot.py
These must NEVER share a token. The guard below prevents cross-contamination.
"""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from typing import Any

from config.settings import settings
from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Connection state tracking (G2)
# ---------------------------------------------------------------------------


class ConnectionState(Enum):
    """Connection state for a messaging platform listener."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


# Bot IDs that belong to the Claude Code manager system — NOT Viola.
# If a Viola channel token starts with any of these, it's cross-contaminated.
_MANAGER_BOT_IDS = frozenset(
    {
        "8515233452",  # Claudingtonbottinmire_bot (manager)
    }
)


def _is_manager_token(token: str) -> bool:
    """Return True if *token* belongs to the Claude Code manager bot.

    Extracts the bot-ID prefix (digits before the first colon) and checks
    it against the known manager bot IDs.  This prevents accidental
    cross-contamination where the manager token gets written into Viola's
    SettingsManager or .env.
    """
    bot_id = token.split(":")[0] if ":" in token else ""
    return bot_id in _MANAGER_BOT_IDS


def _get_messaging_config(key: str, default: Any = None, *, user_id: str | None = None) -> Any:
    """Read a messaging setting, checking per-user DB credentials first.

    Resolution order for token keys (``*_bot_token``, ``*_access_token``):
    1. ``user_credentials`` table in auth DB (per-user, encrypted)
    2. SettingsManager (settings.json — legacy, device-scoped)
    3. AppConfig (.env — environment variables)

    Non-token keys (e.g. ``telegram_enabled``) skip step 1.
    """
    # Token keys that may live in user_credentials DB.
    # Supported messaging channels: Telegram (product decision 2026-04-17 —
    # WhatsApp/Signal removed, Slack hidden; 2026-05-12 — Discord/Matrix removed).
    _DB_SERVICE_MAP = {
        "telegram_bot_token": "telegram",
    }

    service_name = _DB_SERVICE_MAP.get(key)
    if service_name:
        try:
            from auth.database import get_auth_db

            db = get_auth_db()
            if db._initialized and hasattr(db, "user_credentials"):
                uid = user_id or "local"
                # The SQLite repository's async methods are actually sync under
                # the hood (no real I/O await).  We use the coroutine directly
                # via asyncio.run() or get_event_loop().run_until_complete()
                # depending on whether a loop is already running.
                import asyncio

                coro = db.user_credentials.get_credential(uid, service_name)
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    # Already in async context — create a task via a
                    # one-shot thread to avoid "cannot run nested loop".
                    import concurrent.futures

                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        value = pool.submit(asyncio.run, coro).result(timeout=2.0)
                else:
                    value = asyncio.run(coro)

                if value:
                    return value
        except Exception:
            pass  # DB not available — fall through to legacy

    try:
        from ui.settings_manager import get_settings_manager

        sm = get_settings_manager()
        value = sm.get(key)
        if value is not None and value != "" and value != default:
            return value
    except Exception:
        pass  # SettingsManager not available (headless, tests, etc.)

    # Fallback to AppConfig (env vars / .env)
    return getattr(settings, key, default)


class MessageRouter:
    """Starts and manages all messaging platform listeners.

    Usage::

        router = MessageRouter()
        await router.start(pipeline)
        # ... later ...
        await router.shutdown()
    """

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._listeners: list[Any] = []
        self._listener_activity: dict[str, float] = {}
        self._restart_counts: dict[str, list[float]] = {}
        self._health_task: asyncio.Task | None = None
        self._connection_states: dict[str, ConnectionState] = {}
        self._connection_since: dict[str, float] = {}  # platform -> monotonic timestamp
        self._pending_messages: dict[str, list[Any]] = {}  # platform -> pending outbound

    async def start(self, pipeline: Any) -> None:
        """Start all enabled platform listeners.

        Each listener runs as an async background task.  Messages arriving
        on any platform are routed through the shared ``pipeline``.

        Args:
            pipeline: An ``IntentPipeline`` (or compatible) instance.
        """
        started: list[str] = []

        # -- Telegram --
        tg_enabled = _get_messaging_config("telegram_enabled", False)
        tg_token = _get_messaging_config("telegram_bot_token")
        if tg_enabled and tg_token and _is_manager_token(tg_token):
            logger.critical(
                "BLOCKED: telegram_bot_token is the Claude Code MANAGER bot, "
                "not the Viola bot. This would send Viola messages to the "
                "wrong bot. Fix telegram_bot_token in Settings or .env to "
                "use the Viola bot token (bot ID 8061354581). "
                "Telegram listener will NOT start."
            )
            tg_token = None  # prevent startup
        # CHAN-R4: prevent dual-path Telegram. When ``VIOLA_CLOUD_URL`` is
        # set the cloud app registers ``/telegram/webhook`` with Telegram,
        # and Telegram routes all bot updates to that webhook. A polling
        # listener running against the same token would silently receive
        # nothing (webhook wins) yet hold a ``getUpdates`` connection and
        # burn quota. Skip startup with a loud warning so operators can
        # see which side is live.
        import os as _os

        _cloud_url = (_os.environ.get("VIOLA_CLOUD_URL", "") or "").strip()
        if tg_enabled and tg_token and _cloud_url:
            logger.warning(
                "Telegram listener NOT started: VIOLA_CLOUD_URL is set, so the "
                "cloud app registers the webhook path for the same bot token. "
                "Telegram delivers updates to one transport only — leave the "
                "webhook active or unset VIOLA_CLOUD_URL to fall back to "
                "polling. Active path: webhook."
            )
            tg_token = None
        if tg_enabled and tg_token:
            try:
                from messaging.channels.telegram import TelegramListener

                listener = TelegramListener(tg_token, pipeline)
                self._listeners.append(listener)
                self._tasks.append(
                    asyncio.create_task(
                        self._run_with_reconnect("telegram", listener.start),
                        name="messaging-telegram",
                    )
                )
                started.append("telegram")
            except Exception:
                logger.exception("Failed to start Telegram listener")

        # Slack stays behind VIOLA_EXPERIMENTAL_CHANNELS=1 — the product
        # does not advertise it in the Settings UI, but the code path is
        # retained for internal pilots. WhatsApp and Signal listener/
        # capability code was removed (product decision 2026-04-17).
        if started:
            logger.info("Messaging platforms started: %s", ", ".join(started))
            for name in started:
                self._set_connection_state(name, ConnectionState.CONNECTING)
        else:
            logger.debug("No messaging platforms enabled")

        # Start health check loop after all listeners are up
        self._health_task = asyncio.create_task(self._health_check_loop())

    async def shutdown(self) -> None:
        """Clean shutdown of all listeners.

        Waits briefly for pending outbound messages to drain before
        cancelling tasks (graceful shutdown — G2).
        """
        # Cancel health check first
        if self._health_task is not None and not self._health_task.done():
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None

        # Signal all listeners to stop gracefully
        for listener in self._listeners:
            name = getattr(listener, "channel_type", "unknown")
            self._set_connection_state(name, ConnectionState.DISCONNECTED)
            stop = getattr(listener, "stop", None)
            if callable(stop):
                try:
                    await asyncio.wait_for(stop(), timeout=10.0)
                except (TimeoutError, Exception) as exc:
                    logger.debug("Listener stop error: %s", exc)

        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._tasks.clear()
        self._listeners.clear()
        self._connection_states.clear()
        self._connection_since.clear()
        logger.info("All messaging listeners stopped")

    @property
    def active_platforms(self) -> list[str]:
        """Names of currently running platform tasks."""
        return [t.get_name().replace("messaging-", "") for t in self._tasks if not t.done()]

    # -- Connection state tracking (G2) ------------------------------------------

    def _set_connection_state(self, platform: str, state: ConnectionState) -> None:
        """Update the connection state for a platform."""
        prev = self._connection_states.get(platform)
        self._connection_states[platform] = state
        if state == ConnectionState.CONNECTED and prev != ConnectionState.CONNECTED:
            self._connection_since[platform] = time.monotonic()
        if prev != state:
            logger.debug("Platform '%s' state: %s -> %s", platform, prev, state)

    def get_health(self, platform: str) -> dict[str, Any]:
        """Get health status for a specific platform listener.

        Returns a dict with:
            - state: ConnectionState value
            - uptime_seconds: time since last connection (0 if not connected)
            - last_activity: seconds since last message (None if no activity)
            - restart_count: number of restarts in the last hour
        """
        state = self._connection_states.get(platform, ConnectionState.DISCONNECTED)
        since = self._connection_since.get(platform)
        now = time.monotonic()

        uptime = 0.0
        if since and state == ConnectionState.CONNECTED:
            uptime = now - since

        last_activity = self._listener_activity.get(platform)
        activity_age = (now - last_activity) if last_activity else None

        restarts = self._restart_counts.get(platform, [])
        recent_restarts = len([t for t in restarts if now - t < 3600.0])

        return {
            "state": state.value,
            "uptime_seconds": round(uptime, 1),
            "last_activity_seconds": round(activity_age, 1) if activity_age else None,
            "restart_count_1h": recent_restarts,
        }

    def get_all_health(self) -> dict[str, dict[str, Any]]:
        """Get health status for all registered platforms."""
        platforms = set()
        for task in self._tasks:
            platforms.add(task.get_name().replace("messaging-", ""))
        return {p: self.get_health(p) for p in sorted(platforms)}

    def record_activity(self, platform: str) -> None:
        """Record that a platform listener received activity."""
        self._listener_activity[platform] = time.monotonic()
        # Mark as connected on first activity
        if self._connection_states.get(platform) in (
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
        ):
            self._set_connection_state(platform, ConnectionState.CONNECTED)
        try:
            from admin.instrumentation import record_messaging_event

            record_messaging_event(platform, "message_received")
        except Exception:
            pass

    async def _health_check_loop(
        self,
        interval: float = 300.0,
        grace_period: float = 60.0,
    ) -> None:
        """Check listener health every 5 minutes."""
        start_time = time.monotonic()
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()

                # Skip during grace period
                if now - start_time < grace_period:
                    continue

                for task in list(self._tasks):
                    name = task.get_name().replace("messaging-", "")

                    # Check for dead tasks
                    if task.done():
                        exc = task.exception() if not task.cancelled() else None
                        logger.warning("Listener '%s' is dead (exception=%s)", name, exc)
                        try:
                            from admin.instrumentation import record_messaging_event

                            record_messaging_event(name, "dead_task")
                        except Exception:
                            pass
                        if self._can_restart(name):
                            logger.info("Restarting listener '%s'", name)
                            # NOTE: actual restart requires re-creating the listener
                            # For now, just log the detection
                        continue

                    # Check for stuck connections (no activity for 2x interval)
                    last = self._listener_activity.get(name, start_time)
                    if now - last > interval * 2:
                        logger.warning(
                            "Listener '%s' appears stuck (no activity for %.0fs)",
                            name,
                            now - last,
                        )
        except asyncio.CancelledError:
            return

    def _can_restart(self, platform: str) -> bool:
        """Check circuit breaker: max 3 restarts per hour."""
        now = time.monotonic()
        restarts = self._restart_counts.get(platform, [])
        # Remove entries older than 1 hour
        restarts = [t for t in restarts if now - t < 3600.0]
        self._restart_counts[platform] = restarts
        can = len(restarts) < 3
        if not can:
            try:
                from admin.instrumentation import record_messaging_event

                record_messaging_event(platform, "circuit_break")
            except Exception:
                pass
        return can

    async def _run_with_reconnect(
        self,
        name: str,
        start_fn: Any,
        max_retries: int = 10,
        backoff_base: float = 5.0,
    ) -> None:
        """Run a listener with automatic reconnection on failure.

        Uses exponential backoff with jitter. Classifies errors as
        transient (retry) or permanent (give up immediately).
        """
        retries = 0
        while retries < max_retries:
            try:
                self._set_connection_state(
                    name, ConnectionState.CONNECTING if retries == 0 else ConnectionState.RECONNECTING
                )
                await start_fn()
                self._set_connection_state(name, ConnectionState.DISCONNECTED)
                return  # Clean exit (e.g. shutdown signal)
            except asyncio.CancelledError:
                self._set_connection_state(name, ConnectionState.DISCONNECTED)
                return
            except Exception as exc:
                error_type = _classify_error(exc)
                if error_type == "permanent":
                    logger.error(
                        "%s listener hit permanent error, will not retry: %s",
                        name,
                        exc,
                    )
                    self._set_connection_state(name, ConnectionState.ERROR)
                    try:
                        from admin.instrumentation import record_messaging_event

                        record_messaging_event(name, "permanent_error")
                    except Exception:
                        pass
                    return

                retries += 1
                self._set_connection_state(name, ConnectionState.ERROR)
                # Track restart for circuit breaker
                restarts = self._restart_counts.setdefault(name, [])
                restarts.append(time.monotonic())

                delay = min(backoff_base * (2 ** (retries - 1)), 300.0)
                jitter = delay * 0.1 * random.random()  # 0-10% jitter
                total_delay = delay + jitter
                logger.warning(
                    "%s listener crashed (attempt %d/%d, type=%s), reconnecting in %.1fs: %s",
                    name,
                    retries,
                    max_retries,
                    error_type,
                    total_delay,
                    exc,
                )
                try:
                    from admin.instrumentation import record_messaging_event

                    record_messaging_event(name, "reconnect")
                except Exception:
                    pass
                await asyncio.sleep(total_delay)

        self._set_connection_state(name, ConnectionState.ERROR)
        logger.error("%s listener exhausted retries -- giving up", name)


def _classify_error(exc: Exception) -> str:
    """Classify an error as 'transient' or 'permanent'.

    Permanent errors indicate misconfiguration or revoked credentials
    that won't be fixed by retrying.

    Returns:
        'transient' or 'permanent'
    """
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__.lower()

    # Permanent: authentication / authorization failures
    permanent_keywords = (
        "unauthorized",
        "forbidden",
        "invalid token",
        "authentication failed",
        "invalid credentials",
        "revoked",
        "not found",
        "improper token",
        "401",
        "403",
    )
    for keyword in permanent_keywords:
        if keyword in exc_str:
            return "permanent"

    # Permanent: specific exception types
    permanent_types = ("authenticationerror", "loginfailure", "invalidtoken")
    if exc_type in permanent_types:
        return "permanent"

    return "transient"
