"""Linux desktop notification delivery for Viola.

The Linux counterpart to ``macos_notification`` / ``windows_toast``. Wired into
the production dispatcher ``services.notifications.push_service`` at every
priority level (urgent / high / normal) via ``PushService._send_linux_toast``.

Primary delivery is D-Bus ``org.freedesktop.Notifications.Notify`` — the
freedesktop.org Desktop Notifications spec that every major Linux desktop
notification daemon implements (GNOME Shell, KDE Plasma, dunst, mako,
xfce4-notifyd, ...), so this one integration point covers them all without
per-desktop-environment special-casing. The D-Bus transport is ``jeepney``, a
pure-Python D-Bus client library already vendored for Linux in
``requirements_linux.txt`` (it backs ``secretstorage``/``keyring`` today), so
this adds zero new dependencies — buy over build.

When no session bus / notification daemon is reachable (headless box, no
notification daemon installed, sandboxing quirk), delivery falls back to the
``notify-send`` CLI (``libnotify-bin``). If that binary is also unavailable,
this reports failure honestly rather than a silent no-op success — mirroring
the fallback discipline in ``macos_notification``'s osascript path.
"""

from __future__ import annotations

import asyncio
import platform
import shutil

from core.logging_config import get_logger
from scripts import proc_tree

logger = get_logger(__name__)

APP_NAME = "Viola"
_DBUS_TIMEOUT_SECONDS = 5.0
_NOTIFY_SEND_TIMEOUT_SECONDS = 8

__all__ = [
    "APP_NAME",
    "linux_notification_enabled",
    "send_linux_notification",
]


def _is_linux() -> bool:
    return platform.system() == "Linux"


def _show_notifications_enabled(user_id: str | None = None) -> bool:
    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("show_notifications", True, user_id=user_id))
    except (ImportError, RuntimeError, OSError, TypeError, ValueError) as exc:
        logger.debug("Could not read show_notifications setting: %s", exc)
        return True


def linux_notification_enabled(user_id: str | None = None) -> bool:
    """Return whether Viola settings allow notification delivery on Linux.

    Unlike macOS/Windows, Linux has no single OS-level per-app permission
    registry to consult up front — whether an actual notification surface
    (a D-Bus notification daemon, or ``notify-send``) exists is only knowable
    by attempting delivery. So this only gates on the Viola-level setting;
    the honest-degrade behavior lives in the two delivery paths below.
    """
    if not _is_linux():
        return False
    return _show_notifications_enabled(user_id)


def _send_via_dbus(title: str, body: str, *, app_name: str = APP_NAME) -> bool:
    """Primary path: ``org.freedesktop.Notifications.Notify`` over the session bus."""
    try:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        from jeepney.wrappers import unwrap_msg
    except ImportError as exc:
        logger.debug("jeepney unavailable; skipping D-Bus notification path: %s", exc)
        return False

    try:
        conn = open_dbus_connection(bus="SESSION", auth_timeout=_DBUS_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001, RUF100 - no session bus reachable; fall back
        logger.debug("D-Bus session bus unreachable: %s", exc)
        return False

    try:
        notifications = DBusAddress(
            "/org/freedesktop/Notifications",
            bus_name="org.freedesktop.Notifications",
            interface="org.freedesktop.Notifications",
        )
        # Notify(app_name:s, replaces_id:u, app_icon:s, summary:s, body:s,
        #        actions:as, hints:a{sv}, expire_timeout:i) -> id:u
        msg = new_method_call(
            notifications,
            "Notify",
            "susssasa{sv}i",
            (app_name, 0, "", title or APP_NAME, body or "", [], {}, -1),
        )
        reply = conn.send_and_get_reply(msg, timeout=_DBUS_TIMEOUT_SECONDS)
        unwrap_msg(reply)  # raises DBusErrorResponse for an error-type reply
        return True
    except Exception as exc:  # noqa: BLE001, RUF100 - daemon rejected/errored; fall back
        logger.debug("D-Bus Notify call failed: %s", exc)
        return False
    finally:
        conn.close()


def _send_via_notify_send(title: str, body: str, *, app_name: str = APP_NAME) -> bool:
    """Fallback delivery via the ``notify-send`` CLI (``libnotify-bin``).

    Used when the D-Bus path can't reach a notification daemon directly (e.g. a
    sandboxing quirk) but ``notify-send`` can still reach one itself. If the
    binary is missing entirely there is no notification surface on this box at
    all, and this returns ``False`` so the caller reports an honest failure
    instead of a silent no-op success.
    """
    if shutil.which("notify-send") is None:
        logger.debug("notify-send binary not found; no Linux notification surface available")
        return False
    try:
        result = proc_tree.run(
            ["notify-send", "--app-name=%s" % app_name, title or APP_NAME, body or ""],
            timeout=_NOTIFY_SEND_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "notify-send fallback failed (rc=%s): %s",
            result.returncode,
            (result.stderr or "").strip(),
        )
        return False
    except Exception as exc:  # noqa: BLE001, RUF100 - best-effort platform guard; must not raise
        logger.warning("notify-send fallback error: %s", exc)
        return False


def _send_linux_notification_sync(title: str, body: str, *, app_name: str = APP_NAME) -> bool:
    """Deliver one Linux notification, D-Bus first with a ``notify-send`` fallback.

    Returns ``False`` only when neither path delivers, or off Linux — never a
    fake success when no notification surface actually exists.
    """
    if not _is_linux():
        return False
    if _send_via_dbus(title, body, app_name=app_name):
        return True
    return _send_via_notify_send(title, body, app_name=app_name)


async def send_linux_notification(
    title: str,
    body: str,
    *,
    user_id: str | None = None,
    app_name: str = APP_NAME,
) -> bool:
    """Send a Linux desktop notification when supported and enabled."""
    if not linux_notification_enabled(user_id=user_id):
        return False
    return await asyncio.to_thread(
        _send_linux_notification_sync,
        title or APP_NAME,
        body,
        app_name=app_name,
    )
