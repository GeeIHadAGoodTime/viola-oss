"""
Auto-start registration for Viola.

Handles adding/removing Viola from system startup:
- Windows: Registry key in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
- macOS: LaunchAgent plist in ~/Library/LaunchAgents/com.useviola.viola.plist
- Linux: ~/.config/autostart/viola.desktop
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from core.logging_config import get_logger

logger = get_logger(__name__)


def _get_viola_launch_command() -> str:
    """Get the command to launch Viola.

    When running from a frozen bundle (PyInstaller .exe on Windows, an AppImage
    or onefile binary on Linux) ``sys.executable`` IS the launcher and there is
    no loose ``viola_qt.py`` next to it — so we launch the executable directly.
    For an AppImage, prefer the ``APPIMAGE`` env var (the path to the .AppImage
    file the user double-clicks) over the extracted-mount ``sys.executable``.
    Otherwise (dev/source run) launch the script with the active interpreter.
    """
    if getattr(sys, "frozen", False):
        appimage = os.environ.get("APPIMAGE")
        target = appimage if appimage else sys.executable
        return shlex.quote(target) if sys.platform != "win32" else '"%s"' % target

    python_exe = sys.executable
    script_path = Path(__file__).resolve().parent.parent / "viola_qt.py"
    if sys.platform != "win32":
        return "%s %s" % (shlex.quote(python_exe), shlex.quote(str(script_path)))
    return f'"{python_exe}" "{script_path}"'


def _linux_autostart_dir() -> Path:
    """Return the freedesktop autostart directory the desktop session reads.

    Per the Desktop Application Autostart Spec this is ``$XDG_CONFIG_HOME/autostart``
    with ``~/.config`` as the default. **Do not** use Viola's own
    ``core.platform.get_config_dir()`` or read ``$XDG_CONFIG_HOME`` directly:
    ``core.platform.configure_environment()`` redirects ``XDG_CONFIG_HOME`` into
    Viola's private ``<data_dir>/xdg-config`` tree at startup, and the desktop
    session does not scan that tree. We resolve the *real* per-user config dir so
    the ``.desktop`` lands where GNOME/KDE actually look for autostart entries.
    """
    home_config = Path.home() / ".config"
    return home_config / "autostart"


def set_autostart(enabled: bool) -> bool:
    """Enable or disable Viola auto-start on boot.

    Returns True if successful.
    """
    import platform

    system = platform.system()

    if system == "Windows":
        return _set_autostart_windows(enabled)
    elif system == "Darwin":
        return _set_autostart_macos(enabled)
    elif system == "Linux":
        return _set_autostart_linux(enabled)
    else:
        logger.warning("Auto-start not supported on %s", system)
        return False


def _set_autostart_windows(enabled: bool) -> bool:
    """Windows: Use registry HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        value_name = "Viola"

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                command = _get_viola_launch_command()
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
                logger.info("Auto-start enabled: %s", command)
            else:
                try:
                    winreg.DeleteValue(key, value_name)
                    logger.info("Auto-start disabled")
                except FileNotFoundError:
                    logger.debug("Auto-start key not found (already disabled)")

        return True
    except Exception:
        logger.exception("Failed to set Windows auto-start")
        return False


def _set_autostart_linux(enabled: bool) -> bool:
    """Linux: write a ``viola.desktop`` into the XDG autostart directory."""
    try:
        autostart_dir = _linux_autostart_dir()
        desktop_file = autostart_dir / "viola.desktop"

        if enabled:
            autostart_dir.mkdir(parents=True, exist_ok=True)
            command = _get_viola_launch_command()
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Viola\n"
                f"Exec={command}\n"
                "Icon=viola\n"
                "Terminal=false\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "StartupNotify=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                "Comment=Viola Voice Assistant\n"
            )
            desktop_file.write_text(content, encoding="utf-8")
            logger.info("Auto-start enabled: %s", desktop_file)
        else:
            if desktop_file.exists():
                desktop_file.unlink()
                logger.info("Auto-start disabled")
            else:
                logger.debug("Auto-start desktop file not found (already disabled)")

        return True
    except Exception:
        logger.exception("Failed to set Linux auto-start")
        return False


# LaunchAgent label — reverse-DNS, matches our useviola.com domain.
_MACOS_LAUNCH_AGENT_LABEL = "com.useviola.viola"


def _macos_launch_agent_path() -> Path:
    """Return the per-user LaunchAgents plist path macOS reads at login."""
    return Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_LAUNCH_AGENT_LABEL}.plist"


def _macos_program_arguments() -> list[str]:
    """Build the argv list the LaunchAgent runs to start Viola.

    Frozen .app bundle: ``sys.executable`` is the binary inside
    ``Viola.app/Contents/MacOS``. Dev/source run: the active interpreter plus
    ``viola_qt.py``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable]
    script_path = Path(__file__).resolve().parent.parent / "viola_qt.py"
    return [sys.executable, str(script_path)]


def _set_autostart_macos(enabled: bool) -> bool:
    """macOS: write a per-user LaunchAgent plist that runs Viola at login."""
    try:
        import plistlib

        plist_path = _macos_launch_agent_path()

        if enabled:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": _MACOS_LAUNCH_AGENT_LABEL,
                "ProgramArguments": _macos_program_arguments(),
                "RunAtLoad": True,
                # Login item, not a daemon: don't relaunch if the user quits Viola.
                "KeepAlive": False,
                "ProcessType": "Interactive",
            }
            with plist_path.open("wb") as handle:
                plistlib.dump(payload, handle)
            logger.info("Auto-start enabled: %s", plist_path)
        else:
            if plist_path.exists():
                plist_path.unlink()
                logger.info("Auto-start disabled")
            else:
                logger.debug("Auto-start LaunchAgent not found (already disabled)")

        return True
    except Exception:
        logger.exception("Failed to set macOS auto-start")
        return False


def is_autostart_enabled() -> bool:
    """Check if Viola auto-start is currently enabled."""
    import platform

    system = platform.system()

    if system == "Windows":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_QUERY_VALUE) as key:
                winreg.QueryValueEx(key, "Viola")
                return True
        except (FileNotFoundError, OSError):
            return False
    elif system == "Darwin":
        return _macos_launch_agent_path().exists()
    elif system == "Linux":
        desktop_file = _linux_autostart_dir() / "viola.desktop"
        return desktop_file.exists()

    return False
