"""
Embedded Backend JavaScript Communication.

This module contains JavaScript API communication logic
extracted from the main EmbeddedPlayerBackend class to comply with code constraints.
"""

from __future__ import annotations

import threading

from core.logging_config import get_logger

logger = get_logger(__name__)


class EmbeddedBackendJSCommunicator:
    """Handles JavaScript API communication for the embedded backend."""

    def __init__(self, backend_instance):
        """
        Initialize JS communicator.

        Args:
            backend_instance: The EmbeddedPlayerBackend instance
        """
        self.backend = backend_instance

    def inject_player_api(self) -> None:
        """Inject YouTube Player API and custom JavaScript."""
        if not self.backend._webview:
            logger.warning("No webview available for API injection")
            return

        try:
            # Inject YouTube IFrame Player API
            api_script = """
            // YouTube IFrame Player API injection
            if (!window.YT) {
                var tag = document.createElement('script');
                tag.src = "https://www.youtube.com/iframe_api";
                var firstScriptTag = document.getElementsByTagName('script')[0];
                firstScriptTag.parentNode.insertBefore(tag, firstScriptTag);
            }

            // Global player reference
            window.youtubePlayer = null;

            // Player ready callback
            window.onYouTubeIframeAPIReady = function() {
                if (window.youtubePlayer) return;

                var playerElement = document.getElementById('youtube-player');
                if (playerElement) {
                    window.youtubePlayer = new YT.Player('youtube-player', {
                        events: {
                            'onReady': function(event) {
                                window.playerReady = true;
                                console.log('YouTube player ready');
                            },
                            'onStateChange': function(event) {
                                window.playerState = event.data;
                                console.log('Player state changed:', event.data);
                            },
                            'onError': function(event) {
                                console.error('YouTube player error:', event.data);
                                window.playerError = event.data;
                            }
                        }
                    });
                }
            };

            // Utility functions for Qt to call
            window.getPlayerState = function() {
                return window.playerState || -1;
            };

            window.getCurrentTime = function() {
                return window.youtubePlayer ? window.youtubePlayer.getCurrentTime() : 0;
            };

            window.getDuration = function() {
                return window.youtubePlayer ? window.youtubePlayer.getDuration() : 0;
            };

            window.seekTo = function(seconds) {
                if (window.youtubePlayer) {
                    window.youtubePlayer.seekTo(seconds);
                }
            };

            window.setVolume = function(volume) {
                if (window.youtubePlayer) {
                    window.youtubePlayer.setVolume(volume);
                }
            };
            """

            # Execute the JavaScript
            self.backend._webview.page().runJavaScript(api_script)

            # Mark as injected
            self.backend._api_injected = True
            logger.debug("YouTube Player API injected")

        except Exception as e:
            logger.exception("Failed to inject player API: %s", e)

    def get_player_state(self) -> str | None:
        """
        Get current player state from JavaScript.

        Returns:
            Player state string or None if unavailable
        """
        if not self.backend._webview:
            return None

        try:
            # Run JavaScript to get player state
            result = None

            def callback(value):
                nonlocal result
                result = value

            self.backend._webview.page().runJavaScript("window.getPlayerState()", callback)

            # Wait a bit for callback (simplified - in real implementation would use proper async)
            import time

            from config.constants import UI_UPDATE_INTERVAL_MS

            time.sleep(UI_UPDATE_INTERVAL_MS / 1000.0)  # Convert ms to seconds

            return result

        except Exception as e:
            logger.debug("Failed to get player state: %s", e)
            return None

    def get_position_from_player(self) -> int | None:
        """
        Get current playback position from player.

        Returns:
            Position in milliseconds or None if unavailable
        """
        if not self.backend._webview:
            return None

        try:
            result = None

            def callback(value):
                nonlocal result
                result = value

            self.backend._webview.page().runJavaScript("window.getCurrentTime()", callback)

            # Simplified wait
            import time

            from config.constants import UI_UPDATE_INTERVAL_MS

            time.sleep(UI_UPDATE_INTERVAL_MS / 1000.0)  # Convert ms to seconds

            if result is not None:
                return int(result * 1000)  # Convert to milliseconds
            return None

        except Exception as e:
            logger.debug("Failed to get position: %s", e)
            return None

    def get_duration_from_player(self) -> int | None:
        """
        Get video duration from player.

        Returns:
            Duration in milliseconds or None if unavailable
        """
        if not self.backend._webview:
            return None

        try:
            result = None

            def callback(value):
                nonlocal result
                result = value

            self.backend._webview.page().runJavaScript("window.getDuration()", callback)

            # Simplified wait
            import time

            from config.constants import UI_UPDATE_INTERVAL_MS

            time.sleep(UI_UPDATE_INTERVAL_MS / 1000.0)  # Convert ms to seconds

            if result is not None:
                return int(result * 1000)  # Convert to milliseconds
            return None

        except Exception as e:
            logger.debug("Failed to get duration: %s", e)
            return None

    def check_player_ready(self) -> bool:
        """
        Check if the YouTube player is ready.

        Returns:
            True if player is ready, False otherwise
        """
        if not self.backend._webview:
            return False

        try:
            result = False

            def callback(value):
                nonlocal result
                result = bool(value)

            self.backend._webview.page().runJavaScript("window.playerReady === true", callback)

            # Simplified wait
            import time

            from config.constants import UI_UPDATE_INTERVAL_MS

            time.sleep(UI_UPDATE_INTERVAL_MS / 1000.0)  # Convert ms to seconds

            return result

        except Exception as e:
            logger.debug("Failed to check player ready: %s", e)
            return False

    def execute_command(self, command: str, *args) -> None:
        """
        Execute a command on the YouTube player.

        This is the main entry point for playback controller commands.

        THREAD SAFETY: Qt's runJavaScript must be called from the main thread.
        When called from other threads (e.g., wake-detector-thread for audio ducking),
        we use QMetaObject.invokeMethod with QueuedConnection to marshal the call.

        Args:
            command: Command to execute (play, pause, stop, seekTo, setVolume)
            *args: Arguments for the command
        """
        if not self.backend._webview:
            logger.debug("Cannot execute command '%s' - no webview available", command)
            return

        try:
            # Build JavaScript command
            if command == "play":
                js_command = "if (window.youtubePlayer) window.youtubePlayer.playVideo();"
            elif command == "pause":
                js_command = "if (window.youtubePlayer) window.youtubePlayer.pauseVideo();"
            elif command == "stop":
                js_command = "if (window.youtubePlayer) window.youtubePlayer.stopVideo();"
            elif command == "seekTo" and args:
                js_command = f"if (window.youtubePlayer) window.youtubePlayer.seekTo({args[0]});"
            elif command == "setVolume" and args:
                # YouTube API expects 0-100, but caller passes 0-1
                # Convert back to 0-100 range
                volume = args[0]
                if isinstance(volume, (int, float)) and volume <= 1.0:
                    volume = int(volume * 100)
                js_command = f"if (window.youtubePlayer) window.youtubePlayer.setVolume({volume});"
                logger.debug("WEBVIEW_VOLUME: Setting YouTube player volume to %d", volume)
            else:
                logger.warning("Unknown player command: %s", command)
                return

            # Execute command with thread safety
            self._execute_js_threadsafe(js_command)

        except Exception as e:
            logger.exception("Failed to execute player command %s: %s", command, e)

    def _execute_js_threadsafe(self, js_command: str) -> None:
        """
        Execute JavaScript with thread safety.

        Qt's runJavaScript must be called from the main thread. When called from
        other threads (e.g., wake-detector-thread for audio ducking), we use
        QMetaObject.invokeMethod with QueuedConnection to marshal the call.

        Args:
            js_command: The JavaScript code to execute
        """
        current_thread = threading.current_thread().name
        is_main_thread = current_thread == "MainThread"
        page = self.backend._webview.page()

        if is_main_thread:
            page.runJavaScript(js_command)
        else:
            # Marshal to Qt main thread for thread safety
            try:
                from PySide6.QtCore import Q_ARG, QMetaObject, Qt

                QMetaObject.invokeMethod(
                    page,
                    "runJavaScript",
                    Qt.ConnectionType.QueuedConnection,
                    Q_ARG(str, js_command),
                )
                logger.debug(
                    "WEBVIEW_JS_QUEUED thread=%s cmd=%s",
                    current_thread,
                    js_command[:60],
                )
            except ImportError:
                logger.warning("WEBVIEW_JS_MARSHAL_FAILED: PyQt6 not available")
            except Exception as exc:
                logger.warning("WEBVIEW_JS_MARSHAL_FAILED thread=%s error=%r", current_thread, exc)

    def execute_player_command(self, command: str, *args) -> None:
        """
        Legacy alias for execute_command.

        Args:
            command: JavaScript command to execute
            *args: Arguments for the command
        """
        self.execute_command(command, *args)
