"""macOS desktop notification delivery for Viola.

The macOS counterpart to ``windows_toast``. Wired into the production dispatcher
``services.notifications.push_service`` at every priority level (urgent / high /
normal) via ``PushService._send_macos_toast``. Primary delivery is
``UNUserNotificationCenter`` (the signed Developer-ID app's own identity), with
an ``osascript`` fallback so a notice is never silently dropped in degraded
environments (unsigned dev process, or before the first-launch grant).
"""

from __future__ import annotations

import asyncio
import platform

from core.logging_config import get_logger
from scripts import proc_tree

logger = get_logger(__name__)

APP_BUNDLE_ID = "com.useviola.Viola"
APP_NAME = "Viola"

__all__ = [
    "APP_BUNDLE_ID",
    "APP_NAME",
    "macos_notification_enabled",
    "macos_notification_permission_state",
    "send_macos_notification",
]


def _is_macos() -> bool:
    return platform.system() == "Darwin"


def _show_notifications_enabled(user_id: str | None = None) -> bool:
    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("show_notifications", True, user_id=user_id))
    except (ImportError, RuntimeError, OSError, TypeError, ValueError) as exc:
        logger.debug("Could not read show_notifications setting: %s", exc)
        return True


_UN_AUTH = {0: "notDetermined", 1: "denied", 2: "authorized", 3: "provisional", 4: "ephemeral"}


def macos_notification_permission_state(bundle_id: str = APP_BUNDLE_ID) -> str:
    """Return the macOS notification permission state via UserNotifications.framework.

    Reads ``UNUserNotificationCenter.getNotificationSettingsWithCompletionHandler``
    (macOS 10.14+) and maps ``UNAuthorizationStatus`` to a stable string:
    ``notDetermined`` / ``denied`` / ``authorized`` / ``provisional`` /
    ``ephemeral``. Returns ``unsupported`` off macOS and ``unknown`` if the
    framework is unavailable (e.g. running outside an app bundle without the
    UserNotifications entitlement).
    """
    if not _is_macos():
        return "unsupported"
    try:
        import threading

        from UserNotifications import UNUserNotificationCenter

        center = UNUserNotificationCenter.currentNotificationCenter()
        result = {"status": None}
        done = threading.Event()

        def _handler(settings):
            try:
                result["status"] = int(settings.authorizationStatus())
            finally:
                done.set()

        center.getNotificationSettingsWithCompletionHandler_(_handler)
        done.wait(timeout=5)
        if result["status"] is None:
            return "unknown"
        return _UN_AUTH.get(result["status"], "unknown")
    except Exception as exc:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.debug("UN notification settings read failed: %s", exc)
        return "unknown"


def macos_notification_enabled(user_id: str | None = None, bundle_id: str = APP_BUNDLE_ID) -> bool:
    """Return whether Viola settings and macOS allow notification delivery."""
    if not _is_macos():
        return False
    if not _show_notifications_enabled(user_id):
        return False
    return macos_notification_permission_state(bundle_id) not in ("denied", "unsupported")


def _send_via_osascript(title: str, body: str) -> bool:
    """Fallback delivery via ``osascript display notification``.

    The primary path (``UNUserNotificationCenter``) requires the running process
    to have a stable code identity with a granted notification authorization —
    true for the shipped Developer-ID app after its first-launch grant, but not
    for an unsigned dev process or before the grant. This fallback guarantees a
    notification is actually delivered in those degraded environments instead of
    silently no-op'ing. The signed app takes the UN path and never reaches here.
    """
    if not _is_macos():
        return False
    try:
        # Escape embedded double-quotes/backslashes for AppleScript string literals.
        def _esc(s: str) -> str:
            return (s or "").replace("\\", "\\\\").replace('"', '\\"')

        script = f'display notification "{_esc(body)}" ' f'with title "{_esc(title or APP_NAME)}"'
        result = proc_tree.run(
            ["osascript", "-e", script],
            timeout=8,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "osascript notification fallback failed (rc=%s): %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    except Exception as exc:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.warning("osascript notification fallback error: %s", exc)
        return False


def _send_macos_notification_sync(title: str, body: str, *, bundle_id: str = APP_BUNDLE_ID) -> bool:
    """Deliver one macOS notification, native path first with a guaranteed fallback.

    Primary: ``UNUserNotificationCenter`` (the signed app's own identity — no
    ad-hoc helper). Builds a ``UNMutableNotificationContent`` and adds a
    ``UNNotificationRequest`` with an immediate (nil) trigger. When that path is
    unavailable — permission denied, framework not loadable outside an app
    bundle, or add failure — it falls back to ``osascript display notification``
    so a notification is actually delivered rather than silently dropped. Returns
    ``False`` only when neither path delivers, or off macOS.
    """
    if not _is_macos():
        return False
    if macos_notification_permission_state(bundle_id) == "denied":
        return _send_via_osascript(title, body)
    try:
        import threading
        import uuid

        from UserNotifications import (
            UNMutableNotificationContent,
            UNNotificationRequest,
            UNUserNotificationCenter,
        )

        center = UNUserNotificationCenter.currentNotificationCenter()
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title or APP_NAME)
        content.setBody_(body or "")
        request = UNNotificationRequest.requestWithIdentifier_content_trigger_(str(uuid.uuid4()), content, None)
        result = {"ok": True}
        done = threading.Event()

        def _completion(error):
            if error is not None:
                result["ok"] = False
                logger.warning("UN notification add failed: %s", error)
            done.set()

        center.addNotificationRequest_withCompletionHandler_(request, _completion)
        done.wait(timeout=5)
        if result["ok"]:
            return True
        # UN accepted the call but the add failed — deliver via fallback.
        return _send_via_osascript(title, body)
    except Exception as exc:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        # Framework unavailable (e.g. unsigned process outside an app bundle) —
        # deliver via the osascript fallback rather than dropping the notice.
        logger.debug("UN notification path unavailable, using fallback: %s", exc)
        return _send_via_osascript(title, body)


async def send_macos_notification(
    title: str,
    body: str,
    *,
    user_id: str | None = None,
    bundle_id: str = APP_BUNDLE_ID,
) -> bool:
    """Send a macOS notification when supported and enabled."""
    if not macos_notification_enabled(user_id=user_id, bundle_id=bundle_id):
        return False
    return await asyncio.to_thread(
        _send_macos_notification_sync,
        title or APP_NAME,
        body,
        bundle_id=bundle_id,
    )
