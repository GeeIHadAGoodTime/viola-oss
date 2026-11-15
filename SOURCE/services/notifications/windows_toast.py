"""Windows desktop toast delivery for local Viola notifications."""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import sys
from pathlib import Path

from core.logging_config import get_logger
from scripts import proc_tree

logger = get_logger(__name__)

APP_ID = "Viola.Desktop"
APP_NAME = "Viola"
_TOAST_TIMEOUT_SECONDS = 10
_TOAST_TITLE_MAX = 160
_TOAST_BODY_MAX = 360
# The notification text reaches PowerShell only as data: three environment
# variables set for that one child process. The script below is a constant and
# never contains any of the text. Before, the text was pasted into the script
# between single quotes with only the ASCII apostrophe doubled; PowerShell also
# ends a quoted string at the typographic quotes U+2018 to U+201B, so a reply
# with a typographic apostrophe failed to show and the words after such a quote
# ran as PowerShell.
_ENV_APP_ID = "VIOLA_TOAST_APP_ID"
_ENV_TITLE = "VIOLA_TOAST_TITLE"
_ENV_BODY = "VIOLA_TOAST_BODY"
_TOAST_SCRIPT = """
$ErrorActionPreference = 'Stop'
$appId = [string]$env:VIOLA_TOAST_APP_ID
$title = [string]$env:VIOLA_TOAST_TITLE
$body = [string]$env:VIOLA_TOAST_BODY
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$textNodes = $xml.GetElementsByTagName('text')
$textNodes.Item(0).AppendChild($xml.CreateTextNode($title)) > $null
$textNodes.Item(1).AppendChild($xml.CreateTextNode($body)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.Tag = 'viola-notification'
$toast.Group = 'viola'
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId)
$notifier.Show($toast)
"""


def _is_windows() -> bool:
    return platform.system().lower() == "windows"


def _show_notifications_enabled(user_id: str | None = None) -> bool:
    try:
        from ui.settings_manager import get_settings_manager

        return bool(get_settings_manager().get("show_notifications", True, user_id=user_id))
    except (ImportError, RuntimeError, OSError, TypeError, ValueError) as exc:
        logger.debug("Could not read show_notifications setting: %s", exc)
        return True


def windows_toast_permission_state(app_id: str = APP_ID) -> str:
    """Return the OS notification permission state for the app id."""
    if not _is_windows():
        return "unsupported"
    import winreg

    def read_dword(path: str, name: str) -> int | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                try:
                    value, _value_type = winreg.QueryValueEx(key, name)
                    return int(value)
                except FileNotFoundError:
                    return None
        except FileNotFoundError:
            return None

    global_enabled = read_dword(
        r"Software\Microsoft\Windows\CurrentVersion\PushNotifications",
        "ToastEnabled",
    )
    if global_enabled == 0:
        return "denied"

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings\%s" % app_id
    enabled = read_dword(key_path, "Enabled")
    if enabled is None:
        return "default"
    return "denied" if int(enabled) == 0 else "granted"


def windows_toast_enabled(user_id: str | None = None, app_id: str = APP_ID) -> bool:
    """Return True when both Viola settings and Windows allow toast delivery."""
    if not _is_windows():
        return False
    if not _show_notifications_enabled(user_id):
        return False
    return windows_toast_permission_state(app_id) != "denied"


def _powershell_executable() -> str:
    system_root = Path(os.environ.get("SYSTEMROOT") or r"C:\Windows")
    candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate if candidate.exists() else "powershell.exe")


def _shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Viola.lnk"


def _ensure_app_shortcut(app_id: str = APP_ID) -> None:
    """Create/update the Start Menu shortcut Windows uses for desktop toast identity."""
    if not _is_windows():
        return

    try:
        import pythoncom
        import pywintypes
        import win32com.client
        from win32com.propsys import propsys, pscon
        from win32com.shell import shellcon
    except ImportError as exc:
        raise RuntimeError("pywin32 is required for Windows toast shortcut registration") from exc

    shortcut_path = _shortcut_path()
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    launcher = root / "run_viola.py"
    target = Path(sys.executable)
    args = _quote_windows_arg(str(launcher)) if launcher.exists() else ""

    try:
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortCut(str(shortcut_path))
            shortcut.Targetpath = str(target)
            shortcut.Arguments = args
            shortcut.WorkingDirectory = str(root)
            shortcut.IconLocation = str(target)
            shortcut.Description = APP_NAME
            shortcut.save()

            store = propsys.SHGetPropertyStoreFromParsingName(
                str(shortcut_path),
                None,
                shellcon.GPS_READWRITE,
                propsys.IID_IPropertyStore,
            )
            store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(app_id))
            store.Commit()
        finally:
            pythoncom.CoUninitialize()
    except pywintypes.com_error as exc:
        # A local COM registration failure must not abort other delivery legs.
        raise RuntimeError("Windows toast shortcut registration failed") from exc


def _quote_windows_arg(value: str) -> str:
    return subprocess.list2cmdline([value])


def _env_value(value: str) -> str:
    # An environment block cannot hold a NUL character; nothing else is changed.
    return str(value).replace("\x00", "")


def _toast_environment(title: str, body: str, app_id: str) -> dict[str, str]:
    """The child's environment: this process's own, plus the three toast values."""
    env = dict(os.environ)
    env[_ENV_APP_ID] = _env_value(app_id)
    env[_ENV_TITLE] = _env_value(title[:_TOAST_TITLE_MAX])
    env[_ENV_BODY] = _env_value(body[:_TOAST_BODY_MAX])
    return env


def _send_windows_toast_sync(title: str, body: str, *, app_id: str = APP_ID) -> bool:
    _ensure_app_shortcut(app_id)
    creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    # PowerShell is a full interpreter that can spawn grandchildren, so this is routed
    # through proc_tree.run's tree-killing runner (nosec B603 - fixed executable and
    # argument list, no shell). Default raise_on_timeout=False is fine: the caller
    # (send_windows_toast) already treats a False return as a delivery failure
    # regardless of whether it came from a non-zero returncode or a raised
    # subprocess.SubprocessError, so a timeout falling through to the returncode
    # check below is behavior-equivalent.
    completed = proc_tree.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _TOAST_SCRIPT,
        ],
        env=_toast_environment(title, body, app_id),
        creationflags=creationflags,
        timeout=_TOAST_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        # PowerShell's own output can quote the notification text, so it is never logged.
        logger.warning("Windows toast delivery failed: PowerShell exit code %s", completed.returncode)
        return False
    logger.info("Windows toast shown: PowerShell exit code 0")
    return True


async def send_windows_toast(
    title: str,
    body: str,
    *,
    user_id: str | None = None,
    app_id: str = APP_ID,
) -> bool:
    """Send a Windows toast when supported and enabled."""
    if not windows_toast_enabled(user_id=user_id, app_id=app_id):
        return False
    try:
        return await asyncio.to_thread(_send_windows_toast_sync, title or APP_NAME, body, app_id=app_id)
    except (OSError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        logger.warning("Windows toast delivery unavailable: %s", exc)
        return False
