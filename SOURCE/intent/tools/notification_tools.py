"""Agent tool handlers for user notifications."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from intent.tool_types import ToolResult
from intent.tools._schedule_time import parse_schedule_time
from services.scheduler.actions import decode_tool_action, encode_tool_action


def _resolve_user_id(user_id: str | None = None) -> str:
    if user_id:
        return user_id
    try:
        from core.user_context import get_current_user_id

        return get_current_user_id()
    except LookupError as exc:
        raise PermissionError(
            "Notification tool requires an explicit user_id or an authenticated request context"
        ) from exc


def _normalize_channel(channel: str | None) -> str | None:
    value = (channel or "").strip().lower()
    if not value:
        return None
    if value in {"phone", "text", "sms"}:
        return "sms"
    return value


def _sms_transport_configured() -> bool:
    """Whether a real SMS/text transport (e.g. Twilio) is wired up.

    None exists today: ``services/notifications/push_service.py`` has no
    SMS-specific delivery path, so a ``channel="sms"`` request just falls
    through to an OS toast / web-push notification and used to report
    ``notification_sent: True`` under the "sms" label -- delivering a desktop
    popup while claiming a text was sent (#2774). Fail closed instead of
    silently mislabeling the delivery channel.
    """
    return False


def _parse_when(when: str | None, *, now: datetime | None = None) -> datetime | None:
    """Parse `when` into an aware UTC datetime, or None if unparseable.

    Delegates to the shared ``alarm``/``notify`` parser (#2774) so "remind me
    tomorrow at 8am" schedules correctly instead of only understanding ISO
    timestamps and durations.
    """
    return parse_schedule_time(when, now=now)


async def notify_handler(
    message: str,
    when: str | None = None,
    channel: str | None = None,
    user_id: str = "",
) -> ToolResult:
    """Send or schedule a notification to the user."""
    body = " ".join((message or "").strip().split())
    if not body:
        return ToolResult(ok=False, data=None, error="Notification message cannot be empty.")

    uid = _resolve_user_id(user_id)
    normalized_channel = _normalize_channel(channel)

    # No SMS/text transport exists (#2774) -- fail closed up front rather than
    # scheduling a reminder that promises a text and can only ever deliver a
    # desktop/push notification (or nothing) when it fires.
    if normalized_channel == "sms" and not _sms_transport_configured():
        return ToolResult(
            ok=False,
            data={"message": body, "channel": normalized_channel},
            error=(
                "SMS/text delivery is not available: no SMS transport is configured. "
                "The message was not sent or scheduled as a text; use a different channel."
            ),
        )

    scheduled_at = _parse_when(when)

    # A caller who supplied a delivery time expects the notification to be
    # SCHEDULED, not delivered now. If that time can't be parsed we must NOT
    # silently fall through to immediate delivery (that fires "remind me at 5pm"
    # instantly and reports it as a success). Surface the parse failure instead,
    # matching how the alarm tool rejects an unparseable time.
    if when and when.strip() and scheduled_at is None:
        return ToolResult(
            ok=False,
            data=None,
            error="Could not parse the notification time %r. Use ISO-8601 (e.g. "
            "'2026-03-22T17:00') or a duration like 'in 30 minutes'." % when,
        )

    if scheduled_at is not None:
        try:
            from services.scheduler.service import get_scheduler_service

            scheduler = get_scheduler_service()
            action = encode_tool_action(
                "notify",
                {
                    "message": body,
                    "channel": normalized_channel,
                },
            )
            schedule_id = scheduler.add(
                uid,
                label="Notification",
                action=action,
                one_shot_at=scheduled_at.isoformat(),
            )
            return ToolResult(
                ok=True,
                data={
                    "notification_scheduled": True,
                    "schedule_id": schedule_id,
                    "message": body,
                    "channel": normalized_channel,
                    "when": scheduled_at.isoformat(),
                },
            )
        except Exception as exc:
            return ToolResult(ok=False, data=None, error="notify schedule failed: %s" % exc)

    return await deliver_notification_handler(body, channel=normalized_channel, user_id=uid)


async def list_notify_reminders_handler(enabled_only: bool = True, user_id: str = "") -> ToolResult:
    """List scheduled reminders created through notify."""
    uid = _resolve_user_id(user_id)
    try:
        from services.scheduler.service import get_scheduler_service

        scheduler = get_scheduler_service()
        schedules = scheduler.list_schedules(uid, enabled_only=enabled_only)
        reminders: list[dict[str, Any]] = []
        for schedule in schedules:
            decoded = decode_tool_action(str(getattr(schedule, "action", "") or ""))
            if not decoded or decoded.get("tool") != "notify":
                continue

            args = decoded.get("args")
            notify_args = args if isinstance(args, dict) else {}
            message = str(notify_args.get("message") or schedule.label)
            channel = notify_args.get("channel")
            reminders.append(
                {
                    "schedule_id": schedule.id,
                    "message": message,
                    "channel": channel if isinstance(channel, str) and channel else None,
                    "when": schedule.one_shot_at or schedule.next_run_at,
                    "next_run_at": schedule.next_run_at,
                    "enabled": bool(schedule.enabled),
                    "last_status": schedule.last_status,
                    "created_at": schedule.created_at,
                }
            )

        return ToolResult(ok=True, data={"count": len(reminders), "reminders": reminders})
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="list_notify_reminders failed: %s" % exc)


async def deliver_notification_handler(
    message: str,
    channel: str | None = None,
    user_id: str = "",
    metadata: dict[str, Any] | None = None,
    user_requested: bool = False,
) -> ToolResult:
    """Deliver a notification immediately through the notification service.

    ``user_requested`` is set by the scheduler when a reminder the user
    personally scheduled comes due (#4790). It makes the push service deliver
    now instead of parking the notification in the background queue, and stops
    quiet hours from deferring a notification the user asked to receive at this
    exact time. See ``PushNotificationService.send``.
    """
    body = " ".join((message or "").strip().split())
    if not body:
        return ToolResult(ok=False, data=None, error="Notification message cannot be empty.")

    uid = _resolve_user_id(user_id)
    normalized_channel = _normalize_channel(channel)

    # No SMS/text transport exists (#2774): without this, a "sms" request
    # falls through to an OS toast / web-push notification below and reports
    # `notification_sent: True` under the "sms" label -- a false success that
    # tells the caller a text was sent when only a desktop popup fired.
    if normalized_channel == "sms" and not _sms_transport_configured():
        return ToolResult(
            ok=False,
            data={"notification_sent": False, "message": body, "channel": normalized_channel},
            error=(
                "SMS/text delivery is not available: no SMS transport is configured. "
                "The message was not sent as a text."
            ),
        )

    priority = "high" if normalized_channel == "sms" else "normal"
    try:
        from services.notifications.push_service import get_push_service

        svc = get_push_service()
        queue_size_before = int(getattr(svc, "queue_size", 0) or 0)
        sent = await svc.send(
            body,
            priority=priority,
            channel=normalized_channel,
            user_id=uid,
            title="Viola Notification",
            metadata={
                "tool": "notify",
                "delivery_channel": normalized_channel,
                **(metadata or {}),
            },
            user_requested=user_requested,
        )
        if sent:
            queue_size = int(getattr(svc, "queue_size", 0) or 0)
            # `sent=True` only tells us the push service ACCEPTED the
            # notification -- for NORMAL priority (and HIGH under throttle),
            # PushNotificationService.send() always just enqueues it and
            # `_queue_loop` delivers it later (and can still drop it on
            # dedup/throttle/staleness). A grown queue means "queued", not
            # "delivered right now" -- report that honestly instead of
            # claiming delivery just because enqueueing succeeded (#2774).
            queued_now = queue_size > queue_size_before
            quiet_hours_queued = False
            if queued_now:
                try:
                    queue = list(getattr(svc, "_queue", []) or [])
                    for notification in reversed(queue):
                        if (
                            getattr(notification, "message", None) == body
                            and getattr(notification, "user_id", None) == uid
                            and isinstance(getattr(notification, "metadata", None), dict)
                            and notification.metadata.get("quiet_hours_queued") is True
                        ):
                            quiet_hours_queued = True
                            break
                except (AttributeError, TypeError):
                    quiet_hours_queued = False

            if quiet_hours_queued:
                return ToolResult(
                    ok=True,
                    data={
                        "notification_sent": False,
                        "notification_queued": True,
                        "notification_deferred": True,
                        "quiet_hours_queued": True,
                        "defer_reason": "quiet_hours",
                        "message": body,
                        "channel": normalized_channel,
                        "priority": priority,
                        "queue_size": queue_size,
                    },
                )
            if queued_now:
                return ToolResult(
                    ok=True,
                    data={
                        "notification_sent": False,
                        "notification_queued": True,
                        "message": body,
                        "channel": normalized_channel,
                        "priority": priority,
                        "queue_size": queue_size,
                    },
                )
            return ToolResult(
                ok=True,
                data={
                    "notification_sent": True,
                    "notification_queued": False,
                    "message": body,
                    "channel": normalized_channel,
                    "priority": priority,
                    "queue_size": queue_size,
                },
            )
        return ToolResult(
            ok=False,
            data={
                "notification_sent": False,
                "message": body,
                "channel": normalized_channel,
            },
            error="Notification could not be delivered by any configured channel.",
        )
    except Exception as exc:
        return ToolResult(ok=False, data=None, error="notify delivery failed: %s" % exc)


__all__ = ["deliver_notification_handler", "list_notify_reminders_handler", "notify_handler"]
