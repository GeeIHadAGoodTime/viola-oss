"""
audio/backends/vlc.py
VLC backend using python-vlc bindings.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

from config import settings
from core.constants import TIMEOUT_MEDIUM
from core.logging_config import get_logger

vlc: Any = None

try:
    import vlc as _vlc

    vlc = _vlc
except ImportError:  # pragma: no cover - optional dependency
    pass

if TYPE_CHECKING:
    # Type stubs for VLC when available
    pass

from music.backends.base import BackendCapabilities, BackendProgress, BaseBackend
from music.exceptions import BackendError, ConfigurationError
from music.providers.checker import is_youtube_url, require_provider_linked

if TYPE_CHECKING:
    from logging import Logger


class VLCBackend(BaseBackend):
    def __init__(self, logger: Logger | None = None, enable_video: bool = False):
        super().__init__()
        self._logger = logger or get_logger("viola.backend.vlc")
        self._enable_video = enable_video
        self._test_mode = bool(getattr(settings, "test_mode", False))
        self._vlc_available = not self._test_mode and vlc is not None

        self._instance: Any = None
        self._player: Any = None
        self._is_playing = False
        self._volume = 50
        self._last_error: str | None = None
        self._video_widget: Any | None = None  # For video output
        self._old_instance_for_cleanup: Any | None = None
        self._progress_thread: threading.Thread | None = None
        self._progress_stop = threading.Event()

        # Gapless playback prebuffering support
        self._preloaded_media: Any = None
        self._preloaded_url: str | None = None
        self._preload_lock = threading.Lock()

        if self._vlc_available:
            # Type guard: vlc is available when _vlc_available is True
            assert vlc is not None, "VLC should be available when _vlc_available is True"

            vlc_options = [
                "--quiet",
                "--network-caching=3000",  # 3s buffer for streaming
                "--http-user-agent=Mozilla/5.0",
                "--http-referrer=https://www.youtube.com/",
            ]

            if enable_video:
                vlc_options.extend(["--intf", "dummy"])
            else:
                vlc_options.append("--no-video")

            try:
                self._instance = vlc.Instance(*vlc_options)
                self._player = self._instance.media_player_new()

                mode_label = "VIDEO" if enable_video else "audio only"
                self._logger.debug("VLC backend initialized with %s support", mode_label)

                em = self._player.event_manager()
                em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end)
                em.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_error)
            except Exception as exc:
                self._logger.warning(
                    "VLC library loaded but instance creation failed: %s. " "Falling back to simulated mode.",
                    exc,
                )
                self._vlc_available = False
                self._instance = None
                self._player = None
        if not self._vlc_available:
            mode = "test mode" if self._test_mode else "VLC unavailable"
            self._logger.info("VLC backend running in %s; playback operations are simulated", mode)

    def play(self, source: str) -> None:
        self.play_url(source)

    def play_url(self, url: str, video_widget=None) -> None:
        """
        Play URL with optional video output.

        Args:
            url: Media URL to play
            video_widget: Optional widget for video output (must be QWidget with winId() for VLC)
                          If None, uses previously set video widget (from set_video_output())
        """
        self._last_error = None

        # SAFETY: Block YouTube/YouTube Music URLs if provider not linked
        if is_youtube_url(url):
            try:
                require_provider_linked(
                    "youtube_music",
                    error_message=(
                        "YouTube Music provider is not linked. "
                        "Cannot stream from YouTube/YouTube Music without a linked provider."
                    ),
                )
            except ConfigurationError as exc:
                self._logger.warning(
                    "Blocked YouTube URL playback - provider not linked: %s",
                    url[:80],
                )
                raise BackendError(f"Cannot play YouTube URL without linked provider: {exc}") from exc

        if self._test_mode or self._player is None or self._instance is None or vlc is None:
            self._logger.info("VLC backend (simulated) would play: %s", url[:80])
            self._is_playing = True
            self._start_progress_pump()
            return

        # If video_widget provided, set it now (overrides any previous setting)
        if video_widget is not None:
            self.set_video_output(video_widget)
        elif self._video_widget:
            # No widget provided, but we have a previously set video widget
            # Make sure it's still set (might have been cleared)
            self.set_video_output(self._video_widget)
        elif self._enable_video:
            # Video enabled but no widget - VLC will use default window
            self._logger.debug("Video enabled but no widget provided - using default output")

        # LEGACY: This code path should not be reached for YouTube Music tracks.
        # YouTube Music uses the embedded player engine, not direct stream URLs.
        # This check is for legacy compatibility only - direct googlevideo.com URLs
        # are rejected upstream by ProviderRouter._validate_metadata_url().
        # VLC limitation: media.add_option() doesn't work for HTTP headers - must be set at Instance level
        is_youtube_stream = "googlevideo.com" in url or ("youtube.com" in url and "/watch" not in url)

        self._logger.debug(
            "VLCBackend.play_url() called - is_youtube_stream=%s, url=%s...",
            is_youtube_stream,
            url[:80],
        )

        try:
            if not self._vlc_available:
                raise BackendError("VLC backend not available")

            # Type guard: vlc is available when _vlc_available is True
            assert vlc is not None, "VLC should be available when _vlc_available is True"

            if is_youtube_stream:
                youtube_vlc_options = [
                    "--quiet",
                    "--network-caching=3000",
                    "--http-user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "--http-referrer=https://www.youtube.com/",
                ]

                if self._enable_video:
                    youtube_vlc_options.extend(["--intf", "dummy"])
                else:
                    youtube_vlc_options.append("--no-video")

                temp_instance = vlc.Instance(*youtube_vlc_options)
                temp_player = temp_instance.media_player_new()

                em = temp_player.event_manager()
                em.event_attach(vlc.EventType.MediaPlayerEndReached, self._on_end)
                em.event_attach(vlc.EventType.MediaPlayerEncounteredError, self._on_error)

                media = temp_instance.media_new(url)

                old_instance = self._instance
                self._instance = temp_instance
                self._player = temp_player
                self._old_instance_for_cleanup = old_instance
            else:
                media = self._instance.media_new(url)

            self._player.set_media(media)

            video_enabled = self._enable_video or video_widget or self._video_widget is not None
            if url.endswith(".m3u8"):
                self._logger.debug("Playing M3U8 stream (may have issues)")
            elif "manifest" in url.lower():
                self._logger.debug("Playing DASH manifest (may have issues)")
            else:
                stream_type = "VIDEO" if video_enabled else "AUDIO"
                self._logger.debug("Playing %s stream: %s", stream_type, url[:100])

            self._player.play()
            self._is_playing = True
            self._start_progress_pump()
            time.sleep(0.05)
            time.sleep(0.1)
            state = self._player.get_state()
            if state == vlc.State.Error:
                self._last_error = "vlc_error_state"
                raise BackendError("VLC entered error state after play()")
            if state == vlc.State.Ended:
                self._last_error = "vlc_ended_immediately"
                raise BackendError("VLC ended immediately after play()")
            if state not in (vlc.State.Opening, vlc.State.Buffering, vlc.State.Playing):
                self._logger.warning("Unexpected VLC state after play: %s", state)
        except BackendError:
            self._is_playing = False
            self._stop_progress_pump()
            self._cleanup_old_instance()
            raise
        except Exception as exc:
            self._is_playing = False
            self._stop_progress_pump()
            self._last_error = str(exc)
            self._logger.error("VLC play failed: %s", exc)
            self._cleanup_old_instance()
            raise BackendError("vlc_play_failed") from exc
        else:
            self._cleanup_old_instance()

    def set_video_output(self, widget_or_id):
        """
        Set the video output widget for VLC player.

        Args:
            widget_or_id: Qt widget object (with winId()) or window ID integer
        """
        if self._player is None or vlc is None:
            self._video_widget = widget_or_id
            return

        if widget_or_id is None:
            # Clear video output
            try:
                # On Windows, set_hwnd(None) clears video output
                # VLC will revert to default or no video
                self._video_widget = None
                self._logger.debug("Video output cleared")
            except Exception as e:
                self._logger.warning("Could not clear video output: %s", e)
            return

        self._video_widget = widget_or_id

        # Extract window ID from widget or use directly if it's an ID
        window_id = None
        if hasattr(widget_or_id, "winId"):
            # It's a Qt widget
            try:
                window_id = widget_or_id.winId()
            except Exception as e:
                self._logger.warning("Could not get widget window ID: %s", e)
        elif isinstance(widget_or_id, int):
            # It's already a window ID
            window_id = widget_or_id

        if window_id:
            try:
                # Set video output to this window
                self._player.set_hwnd(window_id)
                self._logger.info("Video output set to window ID: %s", window_id)
            except Exception as e:
                self._logger.warning("Could not set video output: %s", e)
        else:
            self._logger.warning("Invalid widget or window ID: %s", widget_or_id)

    def pause(self) -> None:
        self._is_playing = False
        self._stop_progress_pump()
        if self._player is None or self._test_mode or vlc is None:
            return
        try:
            self._player.set_pause(1)
        except Exception as exc:
            self._logger.error("VLC pause failed: %s", exc)
            raise BackendError("vlc_pause_failed") from exc

    def resume(self) -> None:
        self._is_playing = True
        self._start_progress_pump()
        if self._player is None or self._test_mode or vlc is None:
            return
        try:
            self._player.set_pause(0)
        except Exception as exc:
            self._logger.error("VLC resume failed: %s", exc)
            raise BackendError("vlc_resume_failed") from exc

    def stop(self) -> None:
        self._is_playing = False
        self._stop_progress_pump()
        if self._player is None or self._test_mode or vlc is None:
            return
        try:
            self._player.stop()
        except Exception as exc:
            self._logger.debug("VLC stop failed: %s", exc)

    def cleanup(self) -> None:
        """Cleanup VLC resources on shutdown"""
        try:
            self._logger.info("Cleaning up VLC backend...")
            self.stop()
            self.cancel_preload()  # Release any preloaded media
            self._cleanup_old_instance()
            if self._player and vlc is not None:
                try:
                    self._player.release()
                except Exception as e:  # Silent OK: VLC player release during shutdown
                    self._logger.exception("Failed to release VLC player: %s", e)
                    pass
            if self._instance and vlc is not None:
                try:
                    self._instance.release()
                except Exception as e:  # Silent OK: VLC instance release during shutdown
                    self._logger.exception("Failed to release VLC instance: %s", e)
                    pass
            self._logger.info("VLC backend cleaned up")
        except Exception as e:
            self._logger.warning("Error cleaning up VLC: %s", e)

    def is_playing(self) -> bool:
        # Use VLC state when possible
        if self._player is None or vlc is None or self._test_mode:
            return self._is_playing

        try:
            st = self._player.get_state()
            # Consider Opening, Buffering, and Playing as valid "playing" states
            # Opening means VLC is starting to load the media
            # Buffering means VLC is buffering the stream
            # Playing means VLC is actively playing
            is_playing_result = st in (
                vlc.State.Opening,
                vlc.State.Buffering,
                vlc.State.Playing,
            )
            # Add detailed logging for debugging
            if st == vlc.State.Error:
                self._logger.error("🔥 VLC state check: ERROR state detected")
            elif st == vlc.State.Ended:
                self._logger.error("🔥 VLC state check: ENDED state detected")
            elif st == vlc.State.Stopped:
                self._logger.debug("🔥 VLC state check: STOPPED state")
            elif st == vlc.State.Opening:
                self._logger.debug("🔥 VLC state check: OPENING state (media loading)")
            return is_playing_result
        except Exception as exc:
            # If we can't get state, fall back to internal flag
            self._logger.debug("VLC state check failed: %s, using internal flag", exc)
            return self._is_playing

    def set_volume(self, level: int) -> int:
        self._volume = max(0, min(100, int(level)))
        if self._player is None or self._test_mode or vlc is None:
            return self._volume
        try:
            self._player.audio_set_volume(self._volume)
        except Exception as exc:
            self._logger.debug("VLC set_volume failed: %s", exc)
        return self._volume

    def get_position(self) -> int:
        """Get current playback position in seconds."""
        if self._player is None or vlc is None:
            return 0
        try:
            pos_ms = self._player.get_time()
            return int(pos_ms / 1000) if pos_ms >= 0 else 0
        except Exception as exc:
            self._logger.debug("VLC get_position failed: %r", exc)
            return 0

    def get_duration(self) -> int:
        """Get track duration in seconds."""
        if self._player is None or vlc is None:
            return 0
        try:
            dur_ms = self._player.get_length()
            return int(dur_ms / 1000) if dur_ms > 0 else 0
        except Exception as exc:
            self._logger.debug("VLC get_duration failed: %r", exc)
            return 0

    def get_position_percentage(self) -> float:
        """Get playback position as percentage (0.0 to 1.0)."""
        if self._player is None or vlc is None:
            return 0.0
        try:
            pos = self._player.get_position()
            return max(0.0, min(1.0, pos)) if pos >= 0 else 0.0
        except Exception as exc:
            self._logger.debug("VLC get_position_percentage failed: %r", exc)
            return 0.0

    def seek(self, position_seconds: float | int) -> None:
        """Seek to position in seconds."""
        if self._player is None or self._test_mode or vlc is None:
            return
        try:
            position_ms = int(position_seconds * 1000)
            self._player.set_time(position_ms)
            self._logger.debug("Seeked to %ss (%sms)", position_seconds, position_ms)
        except Exception as e:
            self._logger.error("Seek failed: %s", e)

    # ----- events -----
    def _on_end(self, event):
        self._is_playing = False
        self._stop_progress_pump()

    def _on_error(self, event):
        self._is_playing = False
        self._last_error = "VLC playback error"
        self._logger.error("VLC MediaPlayerEncounteredError event - playback failed")
        if self._player is not None:
            self._logger.error("VLC state: %s", self._player.get_state())
        self._stop_progress_pump()

    def health_check(self) -> dict[str, Any]:
        """Return a lightweight health snapshot for monitoring."""
        try:
            if self._player is not None:
                state = self._player.get_state()
            else:
                state = None
        except Exception as exc:
            self._logger.exception("VLC state query failed: %s", exc)
            return {
                "status": "error",
                "reason": "state_unavailable",
                "exception_type": exc.__class__.__name__,
                "error": repr(exc),
            }

        state_name = getattr(state, "name", str(state))
        if state == vlc.State.Error or self._last_error:
            return {
                "status": "error",
                "reason": "vlc_state_error",
                "state": state_name,
                "last_error": self._last_error,
            }

        return {"status": "ok", "state": state_name}

    def set_audio_filter(self, filter_string: str | None) -> bool:
        """
        Set audio filter chain for VLC player.

        Args:
            filter_string: VLC audio filter string (e.g., "equalizer=f=300:width_type=o:width=2000:g=-10db")
                          Use ":" to chain multiple filters.
                          Pass None to clear all filters.

        Returns:
            True if filter was applied successfully, False otherwise
        """
        if self._player is None:
            return False

        try:
            if filter_string is None:
                # Clear all filters
                self._player.set_equalizer(None)
                self._logger.debug("Cleared audio filters")
                return True

            # VLC uses equalizer API for some filters, but for complex filters
            # we need to set them via media options or audio filter string
            # Note: VLC's python bindings have limited filter support
            # We'll use the audio_equalizer API where possible

            # Try to apply filter via equalizer if it's a simple EQ
            # For complex filters, we'll need to set them when creating media
            # This is a limitation of python-vlc bindings

            # For now, log the filter request
            # Full implementation would require modifying play_url to accept filters
            self._logger.debug("Audio filter requested: %s", filter_string)
            self._logger.warning(
                "VLC audio filter setting via python-vlc is limited. "
                "Filters should be applied when creating media in play_url_with_filters()."
            )
            return True  # Return True to allow graceful degradation

        except Exception as e:
            self._logger.error("Failed to set audio filter: %s", e)
            return False

    def play_url_with_filters(self, url: str, audio_filters: str | None = None) -> None:
        """
        Play URL with optional audio filters.

        Args:
            url: Audio stream URL
            audio_filters: VLC audio filter string (e.g., "--audio-filter=equalizer")
        """
        self._last_error = None

        if not self._vlc_available:
            self._logger.info(
                "VLC backend not available, simulating playback with filters: %s",
                url[:80],
            )
            self._is_playing = True
            self._start_progress_pump()
            return

        # Type guard: vlc is available when _vlc_available is True
        assert vlc is not None, "VLC should be available when _vlc_available is True"

        # Create VLC instance with audio filter options if provided
        if audio_filters:
            # Note: VLC filters need to be set via instance arguments or media options
            # python-vlc limitations require workarounds
            opts = ["--no-video", "--quiet"]
            if audio_filters:
                opts.append(f"--audio-filter={audio_filters}")

            # Create new instance with filter options
            temp_instance = vlc.Instance(opts)
            temp_player = temp_instance.media_player_new()
            media = temp_instance.media_new(url)
            temp_player.set_media(media)
            temp_player.play()

            # Replace current player if successful
            self._instance = temp_instance
            self._player = temp_player
            self._logger.debug("Playing with audio filters: %s", audio_filters)
        else:
            # Normal playback
            assert self._instance is not None, "Instance should be available when VLC is enabled"
            assert self._player is not None, "Player should be available when VLC is enabled"
            media = self._instance.media_new(url)
            self._player.set_media(media)
            self._logger.debug("Playing without filters")

        assert self._player is not None, "Player should be available when VLC is enabled"
        self._player.play()
        self._is_playing = True
        time.sleep(0.05)

        # Check if playback started
        time.sleep(0.1)
        state = self._player.get_state()
        if state == vlc.State.Error:
            self._logger.error("VLC Error state - URL likely invalid")
        elif state == vlc.State.Ended:
            self._logger.error("VLC Ended immediately - URL failed to load")
        elif state in (vlc.State.Opening, vlc.State.Buffering, vlc.State.Playing):
            self._logger.debug("VLC state: %s", state)

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True,
            pause=True,
            resume=True,
            seek=True,
            volume=True,
            position=True,
            duration=True,
        )

    # ---------- gapless playback prebuffering ----------

    def preload_next_track(self, source: str) -> bool:
        """
        Preload the next track for gapless playback transition.

        VLC implementation: Creates a media object and parses its metadata,
        which triggers network prefetching for streaming sources.

        Args:
            source: Media URL to preload

        Returns:
            True if preloading initiated successfully
        """
        if not self._vlc_available or self._instance is None or vlc is None:
            self._logger.debug("Preload skipped - VLC not available")
            return False

        with self._preload_lock:
            # Cancel any existing preload
            self._cancel_preload_internal()

            try:
                # Create media object for the next track
                media = self._instance.media_new(source)

                # Parse metadata asynchronously - this triggers network prefetch
                # for streaming sources (VLC will buffer the initial data)
                media.parse_with_options(
                    vlc.MediaParseFlag.network,
                    timeout=5000,  # 5 second timeout for metadata parsing
                )

                self._preloaded_media = media
                self._preloaded_url = source
                self._logger.info("Preloading next track: %s...", source[:60])
                return True

            except Exception as exc:
                self._logger.warning("Failed to preload track: %s", exc)
                self._preloaded_media = None
                self._preloaded_url = None
                return False

    def has_preloaded_track(self) -> bool:
        """Check if a track has been preloaded."""
        with self._preload_lock:
            return self._preloaded_media is not None

    def play_preloaded(self) -> bool:
        """
        Start playback of the preloaded track for gapless transition.

        Returns:
            True if preloaded track started playing
        """
        if not self._vlc_available or self._player is None:
            return False

        with self._preload_lock:
            if self._preloaded_media is None:
                return False

            try:
                # Stop current playback
                self._player.stop()

                # Set the preloaded media and play immediately
                self._player.set_media(self._preloaded_media)
                self._player.play()

                self._is_playing = True
                self._start_progress_pump()

                self._logger.info(
                    "Gapless transition to preloaded track: %s...",
                    (self._preloaded_url or "")[:60],
                )

                # Clear preloaded state
                self._preloaded_media = None
                self._preloaded_url = None

                return True

            except Exception as exc:
                self._logger.error("Failed to play preloaded track: %s", exc)
                self._preloaded_media = None
                self._preloaded_url = None
                return False

    def cancel_preload(self) -> None:
        """Cancel any pending preload operation."""
        with self._preload_lock:
            self._cancel_preload_internal()

    def _cancel_preload_internal(self) -> None:
        """Internal preload cancellation (must hold lock)."""
        if self._preloaded_media is not None:
            try:
                # Release the preloaded media
                self._preloaded_media.release()
            except Exception as exc:
                self._logger.debug("Error releasing preloaded media: %s", exc)
            finally:
                self._preloaded_media = None
                self._preloaded_url = None

    def current_position_ms(self) -> int | None:
        try:
            if self._player is not None:
                pos_ms = self._player.get_time()
                return int(pos_ms) if pos_ms and pos_ms >= 0 else None
            return None
        except Exception as exc:
            self._logger.debug("VLC current_position_ms query failed: %r", exc)
            return None

    def current_duration_ms(self) -> int | None:
        try:
            if self._player is not None:
                dur_ms = self._player.get_length()
                return int(dur_ms) if dur_ms and dur_ms > 0 else None
            return None
        except Exception as exc:
            self._logger.debug("VLC current_duration_ms query failed: %r", exc)
            return None

    def _start_progress_pump(self) -> None:
        self._stop_progress_pump()
        self._progress_stop.clear()
        self._progress_thread = threading.Thread(target=self._progress_loop, name="VLCProgress", daemon=True)
        self._progress_thread.start()

    def _stop_progress_pump(self) -> None:
        self._progress_stop.set()
        if self._progress_thread and self._progress_thread.is_alive():
            self._progress_thread.join(timeout=TIMEOUT_MEDIUM)
        self._progress_thread = None

    def _cleanup_old_instance(self) -> None:
        if not self._old_instance_for_cleanup:
            return
        try:
            self._old_instance_for_cleanup.release()
        except Exception as exc:
            self._logger.debug("Failed to release old VLC instance: %s", exc)
        finally:
            self._old_instance_for_cleanup = None

    def _progress_loop(self) -> None:
        interval = 0.25
        while not self._progress_stop.wait(interval):
            position_ms = self.current_position_ms()
            duration_ms = self.current_duration_ms()
            if position_ms is None and duration_ms is None:
                continue
            self._emit_progress(
                BackendProgress(
                    position_ms=position_ms or 0,
                    duration_ms=duration_ms,
                )
            )
            if not self.is_playing():
                break
