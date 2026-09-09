"""Push notification service with priority routing and optional Web Push."""

from __future__ import annotations

import asyncio
import enum
import hashlib
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from core.quiet_hours import is_quiet_hours

logger = get_logger(__name__)

_LOCK = threading.Lock()
_SINGLETON: PushNotificationService | None = None

_DEFAULT_THROTTLE_MAX = 10
_DEFAULT_THROTTLE_WINDOW = 3600
_DEFAULT_QUEUE_MAX_PER_USER = 100
_DEFAULT_QUEUE_MAX_TOTAL = 1000
_DEDUP_WINDOW = 300
_QUEUE_CHECK_INTERVAL = 60
_QUIET_HOURS_QUEUED_META = "quiet_hours_queued"
_DELIVERY_ERRORS = (ImportError, RuntimeError, OSError, TypeError, ValueError)
_LOCAL_THROTTLE_BUCKET = "__local_device__"


class Priority(enum.Enum):
    """Notification priority levels."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"

    @classmethod
    def from_string(cls, value: str) -> Priority:
        """Parse a priority string (case-insensitive)."""
        try:
            return cls(value.lower().strip())
        except ValueError:
            return cls.NORMAL


@dataclass
class Notification:
    """A pending or sent notification."""

    message: str
    priority: Priority = Priority.NORMAL
    channel: str | None = None
    user_id: str | None = None
    title: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.monotonic)
    content_hash: str = ""

    def __post_init__(self) -> None:
        if not self.content_hash:
            content = "|".join(
                part
                for part in (
                    self.message,
                    self.priority.value,
                    self.channel or "",
                    self.user_id or "",
                    self.title or "",
                )
            )
            self.content_hash = hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest()


class PushNotificationService:
    """Manage notification routing, throttling, and deduplication."""

    def __init__(
        self,
        throttle_max: int = _DEFAULT_THROTTLE_MAX,
        throttle_window: float = _DEFAULT_THROTTLE_WINDOW,
        queue_max_per_user: int = _DEFAULT_QUEUE_MAX_PER_USER,
        queue_max_total: int = _DEFAULT_QUEUE_MAX_TOTAL,
    ) -> None:
        self._throttle_max = throttle_max
        self._throttle_window = throttle_window
        self._queue_max_per_user = queue_max_per_user
        self._queue_max_total = queue_max_total
        self._sent_timestamps_by_user: dict[str, deque[float]] = {}
        self._sent_hashes: dict[str, float] = {}
        self._queue: deque[Notification] = deque()
        self._running = False
        self._queue_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the notification queue processor."""
        if self._running:
            return
        self._running = True
        self._queue_task = asyncio.create_task(self._queue_loop())
        logger.info("Push notification service started")

    async def stop(self) -> None:
        """Stop the notification service."""
        self._running = False
        if self._queue_task is not None:
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass
            self._queue_task = None
        logger.info("Push notification service stopped")

    async def send(
        self,
        message: str,
        priority: str = "normal",
        channel: str | None = None,
        user_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        user_requested: bool = False,
    ) -> bool:
        """Send a notification.

        ``user_requested`` marks a notification the user personally asked to
        receive *at this moment* -- a scheduled reminder or alarm firing, not
        an ambient/proactive nudge. Those two behave differently and must
        (#4790):

        * **deliver now, not eventually.** A plain NORMAL notification is
          parked in ``_queue`` for the background drain; a reminder that lands
          up to ``_QUEUE_CHECK_INTERVAL`` seconds after the time the user named
          is a broken promise, so it goes straight out and reports the real
          delivery outcome.
        * **survive quiet hours.** Quiet hours exist to mute notifications the
          user did not ask for. Someone who says "remind me at 3am" means 3am;
          deferring it to 07:00 silently discards the whole feature. This is
          the same call #1404 already made for timers -- a timer the user set
          is surfaced "regardless of quiet hours".
        """
        prio = Priority.from_string(priority)
        notification = Notification(
            message=message,
            priority=prio,
            channel=channel,
            user_id=user_id,
            title=title,
            metadata=metadata,
        )

        if self._is_duplicate(notification):
            logger.debug("Notification deduplicated: %s", message[:60])
            return False

        if not user_requested and prio not in {Priority.LOW, Priority.URGENT} and is_quiet_hours(user_id=user_id):
            self._mark_quiet_hours_queued(notification)
            queued = self._enqueue(notification)
            if queued:
                logger.info("Notification queued during quiet hours: %s", message[:60])
            return queued

        if prio == Priority.LOW:
            logger.info("LOW notification (logged only): %s", message[:100])
            self._record_sent(notification)
            return True

        if prio == Priority.URGENT:
            return await self._send_urgent(notification)

        if prio == Priority.HIGH:
            if self._is_throttled(notification.user_id):
                logger.warning("HIGH notification throttled, queuing: %s", message[:60])
                return self._enqueue(notification)
            return await self._send_high(notification)

        if user_requested:
            return await self._send_normal(notification)

        queued = self._enqueue(notification)
        if queued:
            logger.debug("NORMAL notification queued: %s", message[:60])
        return queued

    async def send_from_schedule(
        self,
        task_name: str,
        result: str,
        channel: str | None = None,
        user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Deliver a scheduled task result as a notification."""
        message = "[Scheduled: %s]\n%s" % (task_name, result)
        return await self.send(
            message,
            priority="normal",
            channel=channel,
            user_id=user_id,
            title="Scheduled Task Complete",
            metadata=metadata,
        )

    @staticmethod
    def _throttle_key(user_id: str | None) -> str:
        return user_id or _LOCAL_THROTTLE_BUCKET

    def _timestamps_for_user(self, user_id: str | None) -> deque[float]:
        key = self._throttle_key(user_id)
        return self._sent_timestamps_by_user.setdefault(key, deque())

    def _prune_timestamps(self, user_id: str | None, now: float) -> deque[float]:
        timestamps = self._timestamps_for_user(user_id)
        while timestamps and (now - timestamps[0]) > self._throttle_window:
            timestamps.popleft()
        return timestamps

    def _is_throttled(self, user_id: str | None = None) -> bool:
        """Check if this user has exceeded the hourly notification limit."""
        now = time.monotonic()
        return len(self._prune_timestamps(user_id, now)) >= self._throttle_max

    def _queued_count_for_user(self, user_id: str | None) -> int:
        key = self._throttle_key(user_id)
        return sum(1 for queued in self._queue if self._throttle_key(queued.user_id) == key)

    def _enqueue(self, notification: Notification) -> bool:
        """Queue without letting one user evict or starve another user's notifications."""
        if self._queued_count_for_user(notification.user_id) >= self._queue_max_per_user:
            logger.warning(
                "Notification queue full for user bucket %s",
                self._throttle_key(notification.user_id),
            )
            return False
        if len(self._queue) >= self._queue_max_total:
            logger.warning("Notification queue full globally")
            return False
        self._queue.append(notification)
        return True

    def _is_duplicate(self, notification: Notification) -> bool:
        """Check if this notification was sent recently."""
        now = time.monotonic()
        expired = [content_hash for content_hash, ts in self._sent_hashes.items() if (now - ts) > _DEDUP_WINDOW]
        for content_hash in expired:
            del self._sent_hashes[content_hash]
        return notification.content_hash in self._sent_hashes

    def _record_sent(self, notification: Notification) -> None:
        """Record a sent notification for throttle and dedup tracking."""
        now = time.monotonic()
        self._timestamps_for_user(notification.user_id).append(now)
        self._sent_hashes[notification.content_hash] = now

    @staticmethod
    def _mark_quiet_hours_queued(notification: Notification) -> None:
        metadata = dict(notification.metadata or {})
        metadata[_QUIET_HOURS_QUEUED_META] = True
        notification.metadata = metadata

    @staticmethod
    def _cloud_surface_active() -> bool:
        try:
            from config.settings import settings as app_settings
        except (ImportError, RuntimeError, OSError, TypeError, ValueError):
            return False

        surface = str(getattr(app_settings, "app_surface", "desktop") or "desktop").strip().lower()
        deployment = str(getattr(app_settings, "deployment_mode", surface) or surface).strip().lower()
        return surface == "cloud" or deployment == "cloud"

    async def _send_urgent(self, notification: Notification) -> bool:
        """Send an urgent notification to every available channel."""
        delivered = 0
        if notification.user_id is None:
            try:
                from messaging.hub import get_messaging_hub

                hub = get_messaging_hub()
                if hub is None:
                    logger.warning("No messaging hub for URGENT notification")
                else:
                    formatted = "[URGENT] %s" % notification.message
                    delivered += await hub.broadcast(formatted)  # mt-ok: MessagingHub — device-scoped channels
            except _DELIVERY_ERRORS as exc:
                logger.warning("URGENT notification delivery failed: %s", exc)

        delivered += int(await self._send_windows_toast(notification, fallback_title="Urgent Viola Alert"))
        delivered += int(await self._send_macos_toast(notification, fallback_title="Urgent Viola Alert"))
        delivered += int(await self._send_linux_toast(notification, fallback_title="Urgent Viola Alert"))
        delivered += await self._send_web_push(notification, fallback_title="Urgent Viola Alert")
        if delivered > 0:
            self._record_sent(notification)
            logger.info("URGENT notification delivered via %d channel(s)", delivered)
            return True
        return False

    async def _send_high(self, notification: Notification) -> bool:
        """Send a high-priority notification immediately."""
        delivered = 0
        if notification.user_id is None:
            try:
                from messaging.hub import get_messaging_hub

                hub = get_messaging_hub()
                if hub is None:
                    logger.warning("No messaging hub for HIGH notification")
                else:
                    formatted = "[Important] %s" % notification.message
                    if notification.channel:
                        delivered += int(await hub.send(notification.channel, formatted))

                    if delivered == 0:
                        channels = hub.active_channels()
                        if channels:
                            try:
                                await channels[0].send(formatted)
                                delivered += 1
                                logger.info(
                                    "HIGH notification sent via %s",
                                    getattr(channels[0], "channel_type", "unknown"),
                                )
                            except _DELIVERY_ERRORS as exc:
                                logger.warning("HIGH notification channel send failed: %s", exc)

                    if delivered == 0:
                        logger.warning("No channels available for HIGH notification")
            except _DELIVERY_ERRORS as exc:
                logger.warning("HIGH notification delivery failed: %s", exc)

        delivered += int(await self._send_windows_toast(notification, fallback_title="Important Viola Alert"))
        delivered += int(await self._send_macos_toast(notification, fallback_title="Important Viola Alert"))
        delivered += int(await self._send_linux_toast(notification, fallback_title="Important Viola Alert"))
        delivered += await self._send_web_push(notification, fallback_title="Important Viola Alert")
        if delivered > 0:
            self._record_sent(notification)
            return True
        return False

    async def _send_normal(self, notification: Notification) -> bool:
        """Send a normal notification to the primary available channel."""
        delivered = 0
        if notification.user_id is None:
            try:
                from messaging.hub import get_messaging_hub

                hub = get_messaging_hub()
                if hub is not None:
                    if notification.channel:
                        delivered += int(await hub.send(notification.channel, notification.message))

                    if delivered == 0:
                        channels = hub.active_channels()
                        if channels:
                            try:
                                await channels[0].send(notification.message)
                                delivered += 1
                            except _DELIVERY_ERRORS as exc:
                                logger.warning("NORMAL notification channel send failed: %s", exc)
            except _DELIVERY_ERRORS as exc:
                logger.warning("NORMAL notification delivery failed: %s", exc)
                delivered = 0

        delivered += int(await self._send_windows_toast(notification))
        delivered += int(await self._send_macos_toast(notification))
        delivered += int(await self._send_linux_toast(notification))
        delivered += await self._send_web_push(notification)
        if delivered > 0:
            self._record_sent(notification)
            return True
        return False

    async def _send_windows_toast(
        self,
        notification: Notification,
        fallback_title: str = "Viola",
    ) -> bool:
        """Deliver to the local Windows notification surface when available.

        The isolation boundary here is the **surface**, not whether the
        notification carries a ``user_id`` (#4790). A cloud process serves many
        independent accounts, so a desktop toast raised there would show one
        customer's content on the operator's machine -- that is what
        ``_cloud_surface_active`` refuses, and what the C2 drill asserts.

        A desktop install is one-user-per-install: the signed-in account *is*
        the human at the keyboard. Refusing on ``user_id is not None`` instead
        therefore silenced the desktop rather than the cloud, because every
        production caller (the scheduler's ``notify`` action, the MCP
        ``send_notification`` alias, calendar reminders) is user-scoped. The
        only surviving leg was web push, which needs a browser subscription a
        desktop-only user never has -- so a scheduled reminder fired into
        nothing.
        """
        if self._cloud_surface_active():
            logger.debug("Skipping Windows toast delivery on cloud notification surface")
            return False
        try:
            from services.notifications.windows_toast import send_windows_toast

            return await send_windows_toast(
                notification.title or fallback_title,
                notification.message,
                user_id=notification.user_id,
            )
        except _DELIVERY_ERRORS as exc:
            logger.warning("WINDOWS_TOAST notification delivery failed: %s", exc)
            return False

    async def _send_macos_toast(
        self,
        notification: Notification,
        fallback_title: str = "Viola",
    ) -> bool:
        """Deliver to the local macOS notification surface when available.

        Mirror of :meth:`_send_windows_toast` for the macOS desktop surface,
        including its surface-not-user_id isolation rule (#4790).
        ``send_macos_notification`` self-guards off macOS (returns ``False``), so
        this is safe to call unconditionally on every platform alongside the
        Windows path — exactly one surface delivers per install.
        """
        if self._cloud_surface_active():
            logger.debug("Skipping macOS notification delivery on cloud notification surface")
            return False
        try:
            from services.notifications.macos_notification import send_macos_notification

            return await send_macos_notification(
                notification.title or fallback_title,
                notification.message,
                user_id=notification.user_id,
            )
        except _DELIVERY_ERRORS as exc:
            logger.warning("MACOS notification delivery failed: %s", exc)
            return False

    async def _send_linux_toast(
        self,
        notification: Notification,
        fallback_title: str = "Viola",
    ) -> bool:
        """Deliver to the local Linux notification surface when available.

        Mirror of :meth:`_send_windows_toast` / :meth:`_send_macos_toast` for the
        Linux desktop surface, including their surface-not-user_id isolation
        rule (#4790). ``send_linux_notification`` self-guards off Linux
        (returns ``False``), so this is safe to call unconditionally on every
        platform alongside the Windows and macOS paths — exactly one surface
        delivers per install.
        """
        if self._cloud_surface_active():
            logger.debug("Skipping Linux notification delivery on cloud notification surface")
            return False
        try:
            from services.notifications.linux_notification import send_linux_notification

            return await send_linux_notification(
                notification.title or fallback_title,
                notification.message,
                user_id=notification.user_id,
            )
        except _DELIVERY_ERRORS as exc:
            logger.warning("LINUX notification delivery failed: %s", exc)
            return False

    async def _send_web_push(
        self,
        notification: Notification,
        fallback_title: str = "Viola",
    ) -> int:
        """Deliver to Web Push when a cloud user is specified."""
        if not notification.user_id:
            return 0
        try:
            from services.notifications.web_push import get_web_push_service

            service = get_web_push_service()
            return await service.send_notification(
                user_id=notification.user_id,
                title=notification.title or fallback_title,
                body=notification.message,
                data=notification.metadata,
                urgency=notification.priority.value,
            )
        except Exception as exc:
            logger.warning("WEB_PUSH notification delivery failed: %s", exc)
            return 0

    async def _queue_loop(self) -> None:
        """Process queued notifications periodically."""
        while self._running:
            try:
                await self._process_queue()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Queue processing error: %s", exc)

            try:
                await asyncio.sleep(_QUEUE_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break

    async def _process_queue(self) -> None:
        """Send queued notifications while respecting throttling."""
        sent_count = 0
        checked = 0
        queue_len = len(self._queue)
        while self._queue and checked < queue_len:
            checked += 1
            notification = self._queue.popleft()

            if self._is_duplicate(notification):
                continue

            if self._is_throttled(notification.user_id):
                self._enqueue(notification)
                continue

            if is_quiet_hours(user_id=notification.user_id):
                self._mark_quiet_hours_queued(notification)
                self._enqueue(notification)
                continue

            quiet_queued = bool(notification.metadata and notification.metadata.get(_QUIET_HOURS_QUEUED_META))
            if not quiet_queued and (time.monotonic() - notification.created_at) > 3600:
                logger.debug("Dropped stale notification: %s", notification.message[:40])
                continue

            if await self._send_normal(notification):
                sent_count += 1

        if sent_count > 0:
            logger.info("Processed %d queued notification(s)", sent_count)

    @property
    def queue_size(self) -> int:
        """Number of pending notifications in the queue."""
        return len(self._queue)

    @property
    def sent_in_window(self) -> int:
        """Number of notifications sent in the current throttle window."""
        now = time.monotonic()
        total = 0
        for key in list(self._sent_timestamps_by_user):
            user_id = None if key == _LOCAL_THROTTLE_BUCKET else key
            timestamps = self._prune_timestamps(user_id, now)
            total += len(timestamps)
        return total

    def sent_in_window_for_user(self, user_id: str | None) -> int:
        """Number of notifications sent for one user in the throttle window."""
        now = time.monotonic()
        return len(self._prune_timestamps(user_id, now))


async def send_notification_handler(
    message: str,
    priority: str = "normal",
) -> Any:
    """MCP tool handler: send a push notification."""
    from intent.tool_types import ToolResult

    if not message.strip():
        return ToolResult(ok=False, data=None, error="Message cannot be empty.")

    try:
        svc = get_push_service()
        sent = await svc.send(message.strip(), priority=priority)
        if sent:
            return ToolResult(
                ok=True,
                data="Notification sent (priority=%s): %s" % (priority, message[:100]),
            )
        return ToolResult(
            ok=False,
            data=None,
            error="Notification could not be delivered (no channels available or throttled).",
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="Notification failed: %s" % exc)


def get_push_service(
    throttle_max: int = _DEFAULT_THROTTLE_MAX,
) -> PushNotificationService:
    """Return the process-wide PushNotificationService singleton."""
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PushNotificationService(throttle_max=throttle_max)
    return _SINGLETON


def reset_push_service_for_tests() -> None:
    """Dispose of the singleton. For test suites only."""
    global _SINGLETON
    with _LOCK:
        _SINGLETON = None
