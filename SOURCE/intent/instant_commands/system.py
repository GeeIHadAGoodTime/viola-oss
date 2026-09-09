"""System, device, display, and safety instant command handlers."""

from __future__ import annotations

# ruff: noqa: F405
from ._base import *

_MACOS_OPEN_BINARY = "/usr/bin/open"
_MACOS_OPEN_TIMEOUT_SECONDS = 15

# What a spoken app name resolves to, per platform. The keys are the same
# everywhere so "open the task manager" works on any desktop; only the target
# differs. Windows targets are executables/shell verbs that ``os.startfile``
# resolves; macOS targets are application display names that ``open -a`` hands
# to LaunchServices; Linux targets are the usual PATH binaries.
_APP_TARGETS_WINDOWS: dict[str, str] = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "firefox": "firefox",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "spotify": "spotify",
    "discord": "discord",
    "slack": "slack",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "file explorer": "explorer",
    "explorer": "explorer",
    "settings": "ms-settings:",
    "task manager": "taskmgr",
    "taskmgr": "taskmgr",
    "paint": "mspaint",
    "mspaint": "mspaint",
    "terminal": "wt",
    "wt": "wt",
    "cmd": "cmd",
    "command prompt": "cmd",
    "powershell": "powershell",
    "word": "winword",
    "winword": "winword",
    "excel": "excel",
    "vscode": "code",
    "visual studio code": "code",
    "code": "code",
}

_APP_TARGETS_DARWIN: dict[str, str] = {
    "chrome": "Google Chrome",
    "google chrome": "Google Chrome",
    "firefox": "Firefox",
    "edge": "Microsoft Edge",
    "microsoft edge": "Microsoft Edge",
    "safari": "Safari",
    "spotify": "Spotify",
    "discord": "Discord",
    "slack": "Slack",
    "notepad": "TextEdit",
    "textedit": "TextEdit",
    "notes": "Notes",
    "calculator": "Calculator",
    "calc": "Calculator",
    "file explorer": "Finder",
    "explorer": "Finder",
    "finder": "Finder",
    "settings": "System Settings",
    "system settings": "System Settings",
    "system preferences": "System Settings",
    "task manager": "Activity Monitor",
    "taskmgr": "Activity Monitor",
    "activity monitor": "Activity Monitor",
    "paint": "Preview",
    "preview": "Preview",
    "terminal": "Terminal",
    "wt": "Terminal",
    "cmd": "Terminal",
    "command prompt": "Terminal",
    "powershell": "Terminal",
    "word": "Microsoft Word",
    "winword": "Microsoft Word",
    "excel": "Microsoft Excel",
    "vscode": "Visual Studio Code",
    "visual studio code": "Visual Studio Code",
    "code": "Visual Studio Code",
    "mail": "Mail",
    "messages": "Messages",
    "music": "Music",
    "photos": "Photos",
    "calendar": "Calendar",
}

_APP_TARGETS_LINUX: dict[str, str] = {
    "chrome": "google-chrome",
    "google chrome": "google-chrome",
    "firefox": "firefox",
    "edge": "microsoft-edge",
    "microsoft edge": "microsoft-edge",
    "spotify": "spotify",
    "discord": "discord",
    "slack": "slack",
    "notepad": "gedit",
    "text editor": "gedit",
    "calculator": "gnome-calculator",
    "calc": "gnome-calculator",
    "file explorer": "nautilus",
    "explorer": "nautilus",
    "files": "nautilus",
    "settings": "gnome-control-center",
    "task manager": "gnome-system-monitor",
    "taskmgr": "gnome-system-monitor",
    "terminal": "x-terminal-emulator",
    "cmd": "x-terminal-emulator",
    "command prompt": "x-terminal-emulator",
    "vscode": "code",
    "visual studio code": "code",
    "code": "code",
}


def _app_target_for_platform(app_lower: str, app_raw: str) -> str:
    """Resolve a spoken app name to this platform's launch target."""
    if sys.platform == "win32":
        targets = _APP_TARGETS_WINDOWS
    elif sys.platform == "darwin":
        targets = _APP_TARGETS_DARWIN
    else:
        targets = _APP_TARGETS_LINUX
    return targets.get(app_lower, app_raw)


class SystemHandlersMixin:
    """System, device, display, and safety instant command handlers."""

    @staticmethod
    def _active_executor_scope(params: dict[str, object]) -> tuple[str | None, str | None, str | None]:
        from core.user_context import get_current_user_id, user_id_or_none

        def _text_value(*keys: str) -> str | None:
            for key in keys:
                value = params.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return None

        user_id = user_id_or_none(_text_value("_user_id", "user_id"))
        if user_id is None:
            try:
                user_id = user_id_or_none(get_current_user_id())
            except LookupError:
                user_id = None
        session_id = _text_value("_session_id", "session_id")
        task_id = _text_value("_task_id", "task_id")
        return user_id, session_id, task_id

    async def plugin_install(self, params: dict[str, object]) -> dict[str, object]:
        """Install a plugin by name."""
        original = str(params.get("_original_text", ""))
        # Extract plugin name from "install <name> plugin"
        import re

        m = re.match(r"install\s+(\w[\w-]*)\s+plugin", original, re.I)
        name = m.group(1) if m else ""
        if not name:
            return {"ok": False, "message": "Please specify a plugin name.", "data": {}}

        try:
            from plugins.singleton import get_plugin_manager

            pm = get_plugin_manager()

            if name in pm.loaded_plugins:
                return {
                    "ok": True,
                    "message": "The %s plugin is already installed." % name,
                    "data": {"already_installed": True},
                }

            plugin = pm.install(name)
            return {
                "ok": True,
                "message": "I've installed the %s plugin." % name,
                "data": {"installed": name, "version": plugin.version},
            }
        except Exception:
            log.exception("Command 'plugin_install' failed for plugin: %s", name)
            return {
                "ok": False,
                "message": "I couldn't install the %s plugin. Check your internet connection." % name,
                "data": {},
                "error": "plugin_install_failed",
            }

    async def plugin_remove(self, params: dict[str, object]) -> dict[str, object]:
        """Remove a plugin."""
        original = str(params.get("_original_text", ""))
        import re

        m = re.match(r"remove\s+(\w[\w-]*)\s+plugin", original, re.I)
        name = m.group(1) if m else ""
        if not name:
            return {"ok": False, "message": "Please specify a plugin name.", "data": {}}

        try:
            from plugins.singleton import get_plugin_manager

            pm = get_plugin_manager()
            pm.remove(name)
            return {
                "ok": True,
                "message": "I've removed the %s plugin." % name,
                "data": {"removed": name},
            }
        except Exception:
            log.exception("Command 'plugin_remove' failed for plugin: %s", name)
            return {
                "ok": False,
                "message": "I couldn't remove the %s plugin. It may be in use." % name,
                "data": {},
                "error": "plugin_remove_failed",
            }

    async def plugin_reload(self, params: dict[str, object]) -> dict[str, object]:
        """Reload one or all plugins."""
        original = str(params.get("_original_text", ""))
        import re

        m = re.match(r"reload\s+(?:plugins|(\w[\w-]*)\s+plugin)", original, re.I)
        name = m.group(1) if m else None

        try:
            from plugins.singleton import get_plugin_manager

            pm = get_plugin_manager()
            results = pm.reload(name)

            if name:
                status = results.get(name, "not_found")
                if status == "reloaded":
                    return {
                        "ok": True,
                        "message": "I've reloaded the %s plugin." % name,
                        "data": results,
                    }
                return {
                    "ok": False,
                    "message": "Couldn't reload %s: %s" % (name, status),
                    "data": results,
                }

            reloaded = sum(1 for v in results.values() if v == "reloaded")
            return {
                "ok": True,
                "message": "Reloaded %d plugin%s." % (reloaded, "s" if reloaded != 1 else ""),
                "data": results,
            }
        except Exception:
            log.exception("Command 'plugin_reload' failed")
            return {
                "ok": False,
                "message": "Couldn't reload the plugins. Try again in a moment.",
                "data": {},
                "error": "plugin_reload_failed",
            }

    async def plugin_list(self, params: dict[str, object]) -> dict[str, object]:
        """List installed and available plugins."""
        try:
            from plugins.singleton import get_plugin_manager

            pm = get_plugin_manager()
            installed = pm.list_plugins()

            if installed:
                names = ", ".join(installed)
                message = "You have %d plugin%s installed: %s." % (
                    len(installed),
                    "s" if len(installed) != 1 else "",
                    names,
                )
            else:
                message = "No plugins are installed."

            # Check registry for additional available plugins
            try:
                from plugins.registry import RegistryClient

                registry = RegistryClient()
                available = [e.name for e in registry.list_all() if e.name not in installed]
                if available:
                    message += " Available to install: %s." % ", ".join(available)
            except (ImportError, OSError, RuntimeError, ValueError):
                logger.debug("Registry list failed, omitting available skill hints", exc_info=True)

            return {
                "ok": True,
                "message": message,
                "data": {"installed": installed},
            }
        except Exception:
            log.exception("Command 'plugin_list' failed")
            return {
                "ok": False,
                "message": "Couldn't load the plugin list right now. Try again?",
                "data": {},
                "error": "plugin_list_failed",
            }

    async def launch_app(self, params: dict[str, object]) -> dict[str, object]:
        """Launch an application by name."""
        try:
            original = str(params.get("_original_text", ""))

            # Extract app name from voice text or AI-routed params.
            match = re.match(
                r"^(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)?(?:open|launch|start|run)\s+(.+)$",
                original,
                re.I,
            )
            app_param = params.get("app") or params.get("app_name") or params.get("application") or params.get("name")
            if match:
                app_raw = match.group(1).strip().rstrip(".")
            elif isinstance(app_param, str) and app_param.strip():
                app_raw = app_param.strip().rstrip(".")
            else:
                return {
                    "ok": False,
                    "message": "I didn't catch the app name.",
                    "data": {},
                    "error": "no_app_name",
                }

            # If the target contains a dot, delegate to open_url instead
            if "." in app_raw and " " not in app_raw:
                return await self.open_url(params)

            app_lower = app_raw.lower()

            # The spoken name is the same everywhere ("open the calculator");
            # what it resolves to is not. Picking the map by platform is the
            # whole point -- a single Windows-executable map meant every macOS
            # launch Popen'd a binary that does not exist there (#333).
            executable = _app_target_for_platform(app_lower, app_raw)

            # Friendly display name
            display_name = app_raw.title()

            if sys.platform == "win32":
                os.startfile(executable)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                # LaunchServices is macOS's own launcher and resolves an app by
                # display name, so an unmapped name still has a real chance.
                completed = subprocess.run(  # proc-tree-ok: /usr/bin/open is a leaf binary; the app it starts is reparented to launchd, never a grandchild holding this pipe
                    [_MACOS_OPEN_BINARY, "-a", str(executable)],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=_MACOS_OPEN_TIMEOUT_SECONDS,
                )
                if completed.returncode != 0:
                    log.info(
                        "open -a failed for %s: %s",
                        executable,
                        (completed.stderr or "").strip(),
                    )
                    raise FileNotFoundError(executable)
            else:
                cmd = executable if isinstance(executable, list) else [executable]
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            log.info("Launched application: %s", executable)
            return {
                "ok": True,
                "message": "Opening %s" % display_name,
                "data": {"app": executable},
            }

        except FileNotFoundError:
            return {
                "ok": False,
                "message": "Application '%s' not found on this system." % app_raw,
                "data": {},
                "error": "app_not_found",
            }
        except Exception:
            log.exception("Command 'launch_app' failed")
            return {
                "ok": False,
                "message": "Couldn't open that application. It may not be installed.",
                "data": {},
                "error": "launch_app_failed",
            }

    async def system_volume(self, params: dict[str, object]) -> dict[str, object]:
        """Control the OS system audio volume (not Viola player volume)."""
        try:
            if sys.platform == "darwin":
                original = str(params.get("_original_text", ""))
                if "unmute" in original:
                    subprocess.run(["osascript", "-e", "set volume output muted false"], check=True)
                    return {"ok": True, "message": "System unmuted", "data": {"muted": False}}
                if "mute" in original:
                    subprocess.run(["osascript", "-e", "set volume output muted true"], check=True)
                    return {"ok": True, "message": "System muted", "data": {"muted": True}}
                level_match = re.search(r"(\d+)", original)
                if level_match:
                    level = max(0, min(100, int(level_match.group(1))))
                    subprocess.run(["osascript", "-e", "set volume output volume %d" % level], check=True)
                    return {
                        "ok": True,
                        "message": "System volume set to %d percent" % level,
                        "data": {"volume": level},
                    }
                return {
                    "ok": False,
                    "message": "Please specify a volume level (0-100), mute, or unmute.",
                    "data": {},
                    "error": "invalid_volume",
                }

            try:
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            except ImportError:
                return {
                    "ok": False,
                    "message": "System volume control requires the pycaw package. Install it with: pip install pycaw",
                    "data": {},
                    "error": "missing_dependency",
                }

            original = str(params.get("_original_text", ""))

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = interface.QueryInterface(IAudioEndpointVolume)

            if "unmute" in original:
                volume.SetMute(0, None)
                return {
                    "ok": True,
                    "message": "System unmuted",
                    "data": {"muted": False},
                }

            if "mute" in original:
                volume.SetMute(1, None)
                return {
                    "ok": True,
                    "message": "System muted",
                    "data": {"muted": True},
                }

            # Extract volume level
            level_match = re.search(r"(\d+)", original)
            if level_match:
                level = int(level_match.group(1))
                level = max(0, min(100, level))
                # pycaw uses scalar 0.0-1.0
                volume.SetMasterVolumeLevelScalar(level / 100.0, None)
                return {
                    "ok": True,
                    "message": "System volume set to %d percent" % level,
                    "data": {"volume": level},
                }

            return {
                "ok": False,
                "message": "Please specify a volume level (0-100), mute, or unmute.",
                "data": {},
                "error": "invalid_volume",
            }

        except Exception:
            log.exception("Command 'system_volume' failed")
            return {
                "ok": False,
                "message": "Couldn't change the system volume. Check your audio device.",
                "data": {},
                "error": "system_volume_failed",
            }

    async def take_screenshot(self, params: dict[str, object]) -> dict[str, object]:
        """Take a screenshot and save it to the user's Pictures folder."""
        try:
            try:
                import mss
            except ImportError:
                return {
                    "ok": False,
                    "message": "Screenshot requires the mss package. Install it with: pip install mss",
                    "data": {},
                    "error": "missing_dependency",
                }

            screenshot_dir = Path.home() / "Pictures" / "Viola Screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = "screenshot_%s.png" % timestamp
            filepath = screenshot_dir / filename

            with mss.mss() as sct:
                sct.shot(output=str(filepath))

            log.info("Screenshot saved to %s", filepath)
            return {
                "ok": True,
                "message": "Screenshot saved to your Pictures folder",
                "data": {"path": str(filepath)},
            }

        except Exception:
            log.exception("Command 'take_screenshot' failed")
            return {
                "ok": False,
                "message": "Couldn't capture the screenshot. Check display permissions.",
                "data": {},
                "error": "screenshot_failed",
            }

    async def lock_screen(self, params: dict[str, object]) -> dict[str, object]:
        """Lock the computer screen."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["rundll32.exe", "user32.dll,LockWorkStation"],
                    check=False,
                )
            elif sys.platform == "darwin":
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to keystroke "q" using {command down, control down}',
                    ],
                    check=False,
                )
            else:
                # Linux - try common screen lockers
                for cmd in (
                    "loginctl lock-session",
                    "xdg-screensaver lock",
                    "gnome-screensaver-command -l",
                ):
                    parts = cmd.split()
                    try:
                        subprocess.run(parts, check=True)
                        break
                    except (FileNotFoundError, subprocess.CalledProcessError):
                        continue

            log.info("Screen lock requested")
            return {
                "ok": True,
                "message": "Locking your computer",
                "data": {},
            }

        except Exception:
            log.exception("Command 'lock_screen' failed")
            return {
                "ok": False,
                "message": "Couldn't lock the screen. Try using the keyboard shortcut instead.",
                "data": {},
                "error": "lock_screen_failed",
            }

    async def kill_process(self, params: dict[str, object]) -> dict[str, object]:
        """Kill a running process by name."""
        try:
            try:
                import psutil
            except ImportError:
                return {
                    "ok": False,
                    "message": "Process management requires the psutil package. Install it with: pip install psutil",
                    "data": {},
                    "error": "missing_dependency",
                }

            original = str(params.get("_original_text", ""))

            # Extract target from voice text or AI-routed params.
            match = re.match(r"^(?:close|kill|end|force\s+close)\s+(.+)$", original, re.I)
            target_param = (
                params.get("app")
                or params.get("app_name")
                or params.get("application")
                or params.get("process")
                or params.get("target")
                or params.get("name")
            )
            if match:
                target = match.group(1).strip().rstrip(".").lower()
            elif isinstance(target_param, str) and target_param.strip():
                target = target_param.strip().rstrip(".").lower()
            else:
                return {
                    "ok": False,
                    "message": "Please specify which app to close.",
                    "data": {},
                    "error": "no_target",
                }

            # System-critical process deny list
            deny_list = {
                "explorer.exe",
                "svchost.exe",
                "csrss.exe",
                "lsass.exe",
                "winlogon.exe",
                "services.exe",
                "smss.exe",
                "system",
                "wininit.exe",
                "dwm.exe",
                "runtime broker",
            }

            # Check deny list (match against target and target.exe)
            if target in deny_list or (target + ".exe") in deny_list:
                return {
                    "ok": False,
                    "message": "Closing system processes is blocked for safety. Only user applications can be closed.",
                    "data": {},
                    "error": "denied_process",
                }

            # Find matching processes
            killed = 0
            target_exe = target if target.endswith(".exe") else target + ".exe"
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    if pname == target_exe or pname.startswith(target):
                        proc.terminate()
                        killed += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if killed > 0:
                display = target.replace(".exe", "").title()
                log.info("Killed %d process(es) matching '%s'", killed, target)
                return {
                    "ok": True,
                    "message": "Closed %s" % display,
                    "data": {"killed": killed, "target": target},
                }
            else:
                return {
                    "ok": True,
                    "message": "%s isn't running" % target.title(),
                    "data": {"killed": 0, "target": target},
                }

        except Exception:
            log.exception("Command 'kill_process' failed")
            return {
                "ok": False,
                "message": "Couldn't close that application. It may require admin permissions.",
                "data": {},
                "error": "kill_process_failed",
            }

    async def open_url(self, params: dict[str, object]) -> dict[str, object]:
        """Open a URL in the default web browser."""
        try:
            original = str(params.get("_original_text", ""))

            # Extract URL from various patterns
            match = re.match(r"^(?:open|go\s+to|browse\s+to)\s+(\S+)$", original, re.I)
            if not match:
                return {
                    "ok": False,
                    "message": "I didn't catch the URL.",
                    "data": {},
                    "error": "no_url",
                }

            url = match.group(1).strip().rstrip(".")

            # Add https:// if no scheme present
            if not re.match(r"^https?://", url, re.I):
                url = "https://" + url

            webbrowser.open(url)

            # Display without scheme for cleanliness
            display_url = re.sub(r"^https?://", "", url)
            log.info("Opened URL: %s", url)
            return {
                "ok": True,
                "message": "Opening %s" % display_url,
                "data": {"url": url},
            }

        except Exception:
            log.exception("Command 'open_url' failed")
            return {
                "ok": False,
                "message": "Couldn't open that link. Check that your browser is working.",
                "data": {},
                "error": "open_url_failed",
            }

    async def show_task(self, params: dict[str, object]) -> dict[str, object]:
        """Switch display to agent task view (music continues in background)."""
        try:
            from core.activity_tracker import ACTIVITY_AGENT, get_activity_tracker

            tracker = get_activity_tracker()
            if not tracker.is_active(ACTIVITY_AGENT):
                return {
                    "ok": True,
                    "message": "No agent task is currently running.",
                    "data": {},
                }

            _broadcast_display_priority("agentic_task")

            return {
                "ok": True,
                "message": "Showing the task.",
                "data": {"display_mode": "agentic_task"},
            }
        except Exception:
            log.exception("show_task failed")
            return {
                "ok": False,
                "message": "Couldn't switch display",
                "data": {},
                "error": "display_toggle_failed",
            }

    async def show_music(self, params: dict[str, object]) -> dict[str, object]:
        """Switch display to music view (agent continues in background)."""
        try:
            _broadcast_display_priority("now_playing")

            return {
                "ok": True,
                "message": "Showing the music.",
                "data": {"display_mode": "now_playing"},
            }
        except Exception:
            log.exception("show_music failed")
            return {
                "ok": False,
                "message": "Couldn't switch display",
                "data": {},
                "error": "display_toggle_failed",
            }

    async def smart_stop(self, params: dict[str, object]) -> dict[str, object]:
        """Smart stop: stops the most recently started active activity.

        If an agent task started after music, stops the agent.
        If music started after the agent, stops music.
        Falls back to stopping music if no activity tracker data.
        """
        try:
            from core.activity_tracker import (
                ACTIVITY_AGENT,
                ACTIVITY_MUSIC,
                get_activity_tracker,
            )

            user_id, session_id, task_id = self._active_executor_scope(params)
            tracker = get_activity_tracker()
            most_recent = tracker.most_recent_active(user_id=user_id)

            if most_recent == ACTIVITY_AGENT:
                # Stop the agent task
                try:
                    from intent.agent_executor import get_active_executor

                    executor = get_active_executor(session_id=session_id, user_id=user_id, task_id=task_id)
                    if executor is not None:
                        executor.cancel()
                        tracker.record_stop(ACTIVITY_AGENT, user_id=user_id)
                        return {
                            "ok": True,
                            "message": "Stopping the task.",
                            "data": {"stopped": "agent_task"},
                        }
                except ImportError:
                    pass

            # Default: stop music (either most_recent is music, or no tracker data)
            await _call_maybe_async(self.controller.music, "stop")
            tracker.record_stop(ACTIVITY_MUSIC, user_id=user_id)
            return {
                "ok": True,
                "message": _vary("stopped", "Playback stopped."),
                "data": {"stopped": "music"},
            }
        except Exception:
            # Fallback: stop music the old way
            try:
                await _call_maybe_async(self.controller.music, "stop")
            except (AttributeError, RuntimeError, TypeError, ValueError):
                logger.debug("Fallback stop command failed", exc_info=True)
            log.exception("smart_stop failed")
            return {
                "ok": False,
                "message": "Failed to stop",
                "data": {},
                "error": "stop_failed",
            }

    async def stop_everything(self, params: dict[str, object]) -> dict[str, object]:
        """Stop ALL active activities: cancel agent task AND stop music."""
        stopped = []
        try:
            user_id, session_id, task_id = self._active_executor_scope(params)
            # Stop agent task if running
            try:
                from intent.agent_executor import get_active_executor

                executor = get_active_executor(session_id=session_id, user_id=user_id, task_id=task_id)
                if executor is not None:
                    executor.cancel()
                    stopped.append("agent_task")
            except ImportError:
                pass

            # Stop music playback
            try:
                await _call_maybe_async(self.controller.music, "stop")
                stopped.append("music")
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                log.debug("Music stop failed during stop_everything: %s", exc)

            # Clear all activity tracking
            try:
                from core.activity_tracker import get_activity_tracker

                get_activity_tracker().stop_all(user_id=user_id)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                logger.debug("Activity tracker stop_all failed silently", exc_info=True)

            # Reset display to auto (clear any override)
            _broadcast_display_priority("auto")

            if stopped:
                message = "Everything stopped."
            else:
                message = "Nothing was running."

            return {
                "ok": True,
                "message": message,
                "data": {"stopped": stopped},
            }
        except Exception:
            log.exception("stop_everything failed")
            return {
                "ok": False,
                "message": "Failed to stop everything",
                "data": {},
                "error": "stop_everything_failed",
            }

    async def crisis_safety(self, params: dict[str, object]) -> dict[str, object]:
        """Provide immediate crisis hotline information.

        This handler is intentionally duplicated from HealthHandler so that it
        works via the instant-command path WITHOUT any LLM dependency.
        """
        log.warning("Crisis safety handler triggered via instant command — providing hotline info")
        message = (
            "I hear you, and I want you to know you're not alone. "
            "Please reach out to someone who can help right now:\n\n"
            "Call or text 988 (Suicide & Crisis Lifeline, available 24/7).\n"
            "Text HOME to 741741 (Crisis Text Line).\n"
            "For international resources visit https://www.iasp.info/resources/Crisis_Centres/\n\n"
            "You matter, and help is available."
        )
        return {"ok": True, "message": message, "data": {}}

    async def medical_emergency(self, params: dict[str, object]) -> dict[str, object]:
        """Provide immediate emergency guidance.

        This handler is intentionally duplicated from HealthHandler so that it
        works via the instant-command path WITHOUT any LLM dependency.
        """
        log.warning("Medical emergency handler triggered via instant command — providing 911 guidance")
        message = (
            "This sounds like a medical emergency. "
            "Call 911 (or your local emergency number) right now.\n\n"
            "While waiting for help:\n"
            "- Stay as calm as you can\n"
            "- Don't move unless you're in danger\n"
            "- If someone is unconscious, check their breathing\n\n"
            "Emergency numbers: 911 (US/Canada), 999 (UK), 112 (EU), 000 (Australia)."
        )
        return {"ok": True, "message": message, "data": {}}

    async def system_health_status(self, params: dict[str, object]) -> dict[str, object]:
        """Report system health by calling the health endpoint."""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get("http://127.0.0.1:8756/health/details")
                data = r.json()
            status = data.get("status", "unknown")
            deps = data.get("dependencies", {})
            issues = [k for k, v in deps.items() if isinstance(v, dict) and v.get("status") != "ok"]
            healthy = [k for k, v in deps.items() if isinstance(v, dict) and v.get("status") == "ok"]
            if not issues:
                msg = "All systems operational. %d subsystems healthy." % len(healthy)
            else:
                msg = "Status: %s. Issues: %s. Healthy: %s." % (status, ", ".join(issues), ", ".join(healthy))
            return {"ok": True, "message": msg, "data": {"status": status, "issues": issues}}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {"ok": True, "message": "Health check timed out. Try again in a moment.", "data": {}}

    async def system_resource_info(self, params: dict[str, object]) -> dict[str, object]:
        """Report CPU, RAM, and disk usage."""
        try:
            import psutil

            query = str(params.get("query", "all")).lower()
            parts = []

            if query in ("cpu", "all"):
                cpu = psutil.cpu_percent(interval=0.5)
                cores = psutil.cpu_count()
                parts.append("CPU: %.1f%% across %d cores" % (cpu, cores))

            if query in ("ram", "memory", "all"):
                mem = psutil.virtual_memory()
                parts.append(
                    "RAM: %.1f GB used of %.1f GB (%.0f%%)" % (mem.used / (1024**3), mem.total / (1024**3), mem.percent)
                )

            if query in ("disk", "storage", "all"):
                disk_parts = []
                for part in psutil.disk_partitions():
                    try:
                        usage = psutil.disk_usage(part.mountpoint)
                        disk_parts.append(
                            "%s %.0f GB free of %.0f GB (%.0f%% used)"
                            % (
                                part.mountpoint,
                                usage.free / (1024**3),
                                usage.total / (1024**3),
                                usage.percent,
                            )
                        )
                    except (PermissionError, OSError):
                        continue
                if disk_parts:
                    parts.append("Disk: " + ", ".join(disk_parts))

            msg = ". ".join(parts) if parts else "System info unavailable."
            return {"ok": True, "message": msg, "data": {"query": query}}
        except ImportError:
            return {"ok": False, "message": "System monitoring is not available.", "data": {}}
        except Exception:
            log.exception("system_resource_info failed")
            return {"ok": False, "message": "Couldn't retrieve system info right now.", "data": {}}
