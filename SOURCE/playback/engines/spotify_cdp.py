"""
Spotify CDP playback engine.

Controls Spotify's web player through Chrome DevTools Protocol.
Audio plays through the user's own Chrome browser.
State is synced via 2.5s polling of the Spotify DOM.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from core.logging_config import get_logger
from models.player import QueueItem

from .base import (
    PlaybackCapabilities,
    PlaybackError,
    PlaybackHandle,
    ProviderPlaybackEngine,
    QueueContext,
)

_POSITION_RE = re.compile(r"(\d+):(\d+)")
_ARTWORK_SYNC_DELAY_SECONDS = 1.5
_STARTUP_STALE_TRACK_GUARD_SECONDS = 3.0


def _parse_time_str(time_str: str | None) -> float:
    """Parse "M:SS" or "H:MM:SS" time string to seconds."""
    if not time_str:
        return 0.0
    match = _POSITION_RE.search(time_str)
    if not match:
        return 0.0
    parts = time_str.strip().split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (ValueError, IndexError):
        pass
    return 0.0


def _normalize_track_title(title: str | None) -> str:
    if not title:
        return ""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", title.lower()).split())


def _track_titles_match(expected: str | None, observed: str | None) -> bool:
    expected_normalized = _normalize_track_title(expected)
    observed_normalized = _normalize_track_title(observed)
    if not expected_normalized or not observed_normalized:
        return False
    return expected_normalized in observed_normalized or observed_normalized in expected_normalized


def _is_spotify_track_identifier(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    cleaned = value.strip().lower()
    return cleaned.startswith("spotify:track:") or "open.spotify.com/track/" in cleaned


class SpotifyCDPEngine(ProviderPlaybackEngine):
    """
    Playback engine for Spotify via Chrome DevTools Protocol.

    Controls playback through keyboard shortcuts and reads state
    from the Spotify DOM via JS evaluation.
    """

    def __init__(
        self,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        capabilities = PlaybackCapabilities(
            gapless=False,
            hot_buffer=False,
            artwork_sync=True,
            max_bitrate_kbps=256,
            supports_offline=False,
            supports_lyrics=False,
        )
        super().__init__("spotify_cdp", "Spotify (CDP)", capabilities)
        self._logger = logger or get_logger("viola.playback.spotify_cdp")
        self._lock = threading.Lock()
        self._handles: dict[str, PlaybackHandle] = {}
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._last_track: str | None = None
        self._position: float = 0.0
        self._duration: float = 0.0

    def _get_controller(self) -> Any:
        """Get the shared CDP controller."""
        from music.providers.spotify_cdp import get_cdp_controller

        return get_cdp_controller()

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------
    def _start_poll_loop(self, handle: PlaybackHandle) -> None:
        """Start the background state polling loop."""
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            args=(handle,),
            name="SpotifyCDPPoller",
            daemon=True,
        )
        self._poll_thread.start()

    def _stop_poll_loop(self) -> None:
        """Stop the polling loop."""
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
        self._poll_thread = None

    def _poll_loop(self, handle: PlaybackHandle) -> None:
        """Background loop that polls Spotify DOM for state changes."""
        self._logger.info("SpotifyCDPPoller: poll loop started")
        controller = self._get_controller()
        poll_interval = 2.5
        login_check_counter = 0
        poll_count = 0
        consecutive_poll_errors = 0
        connection_notice_sent = False

        while not handle.stop_requested() and not self._poll_stop.is_set():
            try:
                np = controller.get_now_playing()
                if consecutive_poll_errors:
                    self._logger.info(
                        "SpotifyCDPPoller: recovered after %d consecutive poll errors",
                        consecutive_poll_errors,
                    )
                    consecutive_poll_errors = 0
                    if connection_notice_sent:
                        notify_user = getattr(controller, "_notify_user", None)
                        if callable(notify_user):
                            notify_user("Spotify connection restored.")
                        connection_notice_sent = False
                poll_count += 1
                if np:
                    current_track = np.get("track")
                    position = _parse_time_str(np.get("position"))
                    duration = _parse_time_str(np.get("duration"))

                    if self._is_stale_startup_track(handle, current_track):
                        if poll_count <= 5 or poll_count % 10 == 0:
                            self._logger.info(
                                "SpotifyCDPPoller: poll #%d ignored stale startup track=%s expected=%s",
                                poll_count,
                                (current_track or "?")[:40],
                                (getattr(handle, "_spotify_cdp_expected_title", None) or "?")[:40],
                            )
                    else:
                        self._position = position
                        self._duration = duration
                        if poll_count <= 5 or poll_count % 10 == 0:
                            self._logger.info(
                                "SpotifyCDPPoller: poll #%d pos=%.1f dur=%.1f track=%s",
                                poll_count,
                                self._position,
                                self._duration,
                                (current_track or "?")[:40],
                            )

                        if current_track and current_track != self._last_track:
                            # Only a transition between two OBSERVED tracks is a
                            # genuine auto-advance needing a ProcTap rescan.
                            # _last_track is None at play start (play() resets
                            # it), so the first observed track never misfires a
                            # synthetic "track change" rescan — the old shape
                            # did, whenever a raw-URI expected title mismatched
                            # the human DOM title.
                            genuine_change = self._last_track is not None
                            self._last_track = current_track
                            if genuine_change:
                                self._request_proctap_track_rescan(current_track)
                            if np.get("artwork_url"):
                                handle.mark_artwork(np["artwork_url"])

                        # Metadata sync runs EVERY non-stale poll, not only on a
                        # track change: the change-only sync left a raw-URI
                        # title in the emitted state forever when _last_track
                        # already matched the playing track.
                        self._sync_item_metadata(handle, current_track, np.get("artist"))

                        # Broadcast state update via player._emit()
                        self._broadcast_state()
                elif poll_count <= 5:
                    self._logger.info("SpotifyCDPPoller: poll #%d returned None", poll_count)

                # Periodic login check (every ~30 seconds = 12 polls)
                login_check_counter += 1
                if login_check_counter >= 12:
                    login_check_counter = 0
                    if not controller.is_logged_in():
                        self._logger.warning("Spotify session expired, initiating re-auth")
                        try:
                            controller.handle_reauth()
                        except Exception as exc:
                            self._logger.warning(
                                "Spotify re-auth failed; ending active CDP playback: %s",
                                exc,
                            )
                            handle.mark_finished(error=exc)
                            return

            except Exception as exc:
                consecutive_poll_errors += 1
                if consecutive_poll_errors >= 3:
                    self._logger.warning(
                        "SpotifyCDPPoller: %d consecutive poll errors; retrying: %s",
                        consecutive_poll_errors,
                        exc,
                    )
                    if not connection_notice_sent:
                        notify_user = getattr(controller, "_notify_user", None)
                        if callable(notify_user):
                            notify_user("Spotify connection lost - retrying.")
                        connection_notice_sent = True
                else:
                    self._logger.debug("CDP poll error: %s", exc)

            self._poll_stop.wait(poll_interval)

        handle.mark_finished()

    def _sync_item_metadata(self, handle: PlaybackHandle, current_track: Any, artist: Any) -> None:
        """Keep the queue item's title/artist in step with the live Spotify DOM.

        The queue item is the object the player state serializes for the UI;
        for direct-URI plays it is seeded with the raw spotify:track:<id>
        string, so the live DOM is the source of truth for the human name.
        """
        item = handle.item
        if item is None:
            return
        if isinstance(current_track, str) and current_track and item.title != current_track:
            item.title = current_track
        if isinstance(artist, str) and artist and item.artist != artist:
            item.artist = artist

    def _adopt_direct_uri_metadata(self, item: QueueItem, played_meta: Any) -> None:
        """Populate human title/artist on a direct-URI play, at play start.

        The direct-URI resolution path (provider_router._resolve_via_spotify_cdp)
        can only seed the queue item with the raw spotify:track:<id> string;
        the track page the controller navigated to is the first place the
        human metadata exists. Adopting it here means the play-start state
        emit — and the poller's stale-startup expected-title guard — carry
        the real track name instead of the URI (which previously mis-fired
        the guard and a synthetic "track change" ProcTap rescan, and left the
        UI showing the raw URI until the poller's late heal).
        """
        if not isinstance(played_meta, dict):
            return
        title = str(played_meta.get("title") or "").strip()
        artist = str(played_meta.get("artist") or "").strip()
        if title:
            item.title = title
        if artist:
            item.artist = artist
        if title or artist:
            self._logger.info(
                "Spotify CDP direct-URI metadata adopted: title=%s artist=%s",
                title or "?",
                artist or "?",
            )

    def _request_proctap_track_rescan(self, track: str) -> None:
        """Point ProcTap at the KNOWN Chrome PID after a track change, then rescan.

        Notify comes FIRST: rescan PID resolution prefers a notified PID
        over pycaw discovery, and during a Spotify transition Chrome's
        audio session is momentarily INACTIVE — a bare rescan therefore
        either kept a wandered wrong PID ("no new info") or grabbed
        another audible process, costing spokes 10s-2min of silence
        while capture re-acquired (2026-07-01 field report + measured
        runs). Naming the Chrome PID makes the rescan converge on Chrome
        and grants the named-PID silence patience while the new track's
        audio starts — same pattern as the resume() fix (00d53bfd).
        """
        self._notify_proctap_known_chrome_pid("track change")
        try:
            from audio_core.streaming.pipeline_wiring import request_proctap_rescan

            request_proctap_rescan()
            self._logger.info(
                "SpotifyCDPPoller: requested ProcTap rescan after track change to %s",
                track[:40],
            )
        except Exception as exc:
            self._logger.debug("SpotifyCDPPoller: ProcTap track-change rescan skipped: %s", exc)

    def _is_stale_startup_track(self, handle: PlaybackHandle, current_track: str | None) -> bool:
        expected_title = getattr(handle, "_spotify_cdp_expected_title", None)
        started_at = getattr(handle, "_spotify_cdp_started_at", None)
        if not expected_title or not current_track or not isinstance(started_at, float):
            return False
        if time.monotonic() - started_at > _STARTUP_STALE_TRACK_GUARD_SECONDS:
            return False
        return not _track_titles_match(str(expected_title), current_track)

    def _broadcast_state(self) -> None:
        """Sync cached position/duration to player state, then broadcast.

        IMPORTANT: We must NOT call player._emit() on the event loop thread.
        _emit() acquires player._lock, which would block the event loop if
        any worker thread currently holds that lock (e.g. PlayCommandService
        completing a long Spotify CDP search).  This causes the ~30s delayed
        freeze described in SP-BUG-6.

        Instead, we write position/duration attributes (no lock needed for
        simple attribute writes on the player) and then schedule _emit to
        run in a thread pool via asyncio.to_thread(), which keeps the event
        loop free.  If the event loop is unavailable, we skip the broadcast
        entirely (the periodic state broadcaster in lifecycle.py will pick
        up the changes within 5 seconds anyway).
        """
        try:
            from playback.engine_manager import _get_player_for_emit

            player = _get_player_for_emit()
            if player is None or not hasattr(player, "_emit"):
                return

            # Push engine state into player fields (no lock needed
            # for simple attribute writes on the player object).
            player._position_ms = int(self._position * 1000)
            player._duration_ms = int(self._duration * 1000)

            # Schedule _emit in a thread pool via the main event loop.
            # NEVER use call_soon_threadsafe(player._emit) — that runs
            # _emit directly on the event loop thread, which blocks if
            # any worker holds player._lock.
            import asyncio

            main_loop = None

            # Try to find the event loop from the on_state_change
            # callback's hub reference
            on_state_change = getattr(player, "on_state_change", None)
            if on_state_change is not None:
                # The lifecycle module stores _main_loop on the hub
                # Look for it through various paths
                try:
                    # Try through bindings / websocket hub
                    for attr_name in ("_ws_hub", "_hub"):
                        hub = getattr(player, attr_name, None)
                        if hub is not None:
                            loop = getattr(hub, "_main_loop", None)
                            if loop is not None and loop.is_running():
                                main_loop = loop
                                break
                except Exception:
                    pass

            if main_loop is None:
                try:
                    main_loop = asyncio.get_event_loop()
                    if not main_loop.is_running():
                        main_loop = None
                except Exception:
                    main_loop = None

            if main_loop is not None:
                # Run _emit in a thread pool so it never blocks the event
                # loop while waiting for player._lock.
                main_loop.call_soon_threadsafe(lambda: asyncio.ensure_future(asyncio.to_thread(player._emit)))
            # else: skip broadcast — periodic broadcaster will pick it up
        except Exception as exc:
            self._logger.debug("State broadcast failed: %s", exc)

    # ------------------------------------------------------------------
    # ProviderPlaybackEngine interface
    # ------------------------------------------------------------------
    def resolve_track(self, query: str) -> QueueItem:
        """Resolve a track via CDP search."""
        controller = self._get_controller()
        controller.ensure_running()

        results = controller.search(query)
        if not results:
            from .base import PlaybackError

            raise PlaybackError("Spotify CDP: no results for '%s'" % query)

        first = results[0]
        item = QueueItem(
            id="spotify-cdp-%s" % int(time.time()),
            title=first.get("title", "Unknown Track"),
            url="https://open.spotify.com",
            source="spotify",
            video_id=None,  # No video — album art mode
            artist=first.get("artist", "Unknown Artist"),
            provider="spotify",
            resolved_at=time.time(),
            playback_mode="spotify_cdp",
        )
        item.capabilities["search_index"] = first.get("index", 0)
        return item

    def _annotate_account_tier(self, controller: Any) -> None:
        """Log account tier so callers know whether to expect ads.

        Spotify Free plays full tracks with ads between songs — it's NOT a
        hard block on playback. The previous behavior raised PlaybackError
        on Free, which made Viola refuse to play for users who would have
        otherwise heard their music (with ads). We now just log the tier
        for diagnostic purposes; the user's frontend toast surface can
        decide whether to show an "ads will play between songs" notice.
        """
        tier_fn = getattr(controller, "get_account_tier_status", None)
        if not callable(tier_fn):
            return

        try:
            tier = tier_fn()
        except Exception as exc:
            self._logger.debug("Spotify CDP account tier check skipped: %s", exc)
            return
        if not isinstance(tier, dict):
            return

        product = str(tier.get("product") or "unknown").lower()
        is_premium = tier.get("is_premium")
        if is_premium is False or product in {"free", "open"}:
            self._logger.info("Spotify account is Free — full-track playback works, ads will play between songs")
        elif is_premium is True:
            self._logger.debug("Spotify account is Premium — ad-free playback")

    def _sync_artwork_after_start(
        self,
        controller: Any,
        handle: PlaybackHandle,
        item: QueueItem,
        on_artwork: Callable[[QueueItem, str | None], None] | None,
    ) -> None:
        """Fetch artwork after playback start without delaying state promotion."""
        try:
            if _ARTWORK_SYNC_DELAY_SECONDS > 0:
                time.sleep(_ARTWORK_SYNC_DELAY_SECONDS)
            if handle.stop_requested():
                return
            np = controller.get_now_playing()
            if isinstance(np, dict) and np.get("artwork_url"):
                handle.mark_artwork(np["artwork_url"])
                if on_artwork:
                    on_artwork(item, np["artwork_url"])
        except Exception as exc:
            self._logger.debug("Spotify CDP artwork sync failed: %s", exc)

    def play(
        self,
        item: QueueItem,
        *,
        queue_context: QueueContext,
        on_artwork: Callable[[QueueItem, str | None], None] | None = None,
    ) -> PlaybackHandle:
        """Start playback for a Spotify track via CDP.

        If the item has a search_index capability, plays that search result.
        Otherwise, searches and plays the first result.
        """
        controller = self._get_controller()
        controller.ensure_running()
        self._annotate_account_tier(controller)

        handle = PlaybackHandle(item, self.provider_id)

        # Reset per-play poller state. The engine instance is shared across
        # plays, so a stale _last_track/_position/_duration from the previous
        # track would suppress metadata sync (replaying the same track never
        # healed a raw-URI title) or leak the old track's progress into the
        # new play's emitted state.
        self._last_track = None
        self._position = 0.0
        self._duration = 0.0

        # Prefer exact URI playback when available — bypasses search ranking
        # and plays the LLM's actual pick. The provider populates `uri` in
        # capabilities when search_tracks returned a spotify:track:<id>.
        track_uri = item.capabilities.get("uri") or item.capabilities.get("track_uri")
        search_index = item.capabilities.get("search_index")

        if track_uri and isinstance(track_uri, str) and track_uri.strip():
            try:
                played_meta = controller.play_track_by_uri(track_uri, max_wait=18.0)
            except Exception as exc:
                if _is_spotify_track_identifier(track_uri) or _is_spotify_track_identifier(item.title):
                    raise PlaybackError("Spotify CDP direct track playback failed: %s" % exc) from exc
                self._logger.warning(
                    "play_track_by_uri(%s) failed: %s, falling back to search",
                    track_uri,
                    exc,
                )
                self._search_and_play(item, controller)
            else:
                self._adopt_direct_uri_metadata(item, played_meta)
        elif search_index is not None:
            try:
                controller.play_track_from_search(int(search_index))
            except Exception as exc:
                self._logger.warning(
                    "play_track_from_search(%s) failed: %s, trying search",
                    search_index,
                    exc,
                )
                # Fallback: search and play
                self._search_and_play(item, controller)
        else:
            self._search_and_play(item, controller)

        handle.mark_started()
        handle._spotify_cdp_started_at = time.monotonic()
        handle._spotify_cdp_expected_title = item.title or ""
        # Queue-advance/skip lands here as a fresh play() (the poller's
        # track-change hook deliberately does not fire for the first
        # observed track), and no generic play-path rescan exists for
        # spotify_cdp — so a capture that wandered off Chrome during the
        # previous track's tail/pause had NOTHING pointing it back and
        # spokes sat silent through the slow post-audio re-discovery
        # cycle (10s-2min class, 2026-07-01 field report). Name the PID.
        self._notify_proctap_known_chrome_pid("play start")
        self._logger.info(
            "Spotify CDP: playback started for %s - %s",
            item.title,
            item.artist,
        )

        # Source-local sync: register mute/unmute callbacks.
        #
        # NOTE: These callbacks are ACTIVE for Spotify CDP multiroom.
        #
        # Spotify CDP is an EXTERNAL Chrome process (not an embedded source
        # Source-local muting removed — ProcTap captures at full volume,
        # WebSocket broadcasts to devices, no gain compensation needed.

        with self._lock:
            self._handles[item.id] = handle

        # Start state polling
        self._start_poll_loop(handle)
        threading.Thread(
            target=self._sync_artwork_after_start,
            args=(controller, handle, item, on_artwork),
            name="SpotifyCDPArtworkSync",
            daemon=True,
        ).start()

        return handle

    def _search_and_play(self, item: QueueItem, controller: Any) -> None:
        """Search for the track and play the first result."""
        query = item.title or ""
        if item.artist:
            query = "%s %s" % (query, item.artist)
        query = query.strip()

        if not query:
            from .base import PlaybackError

            raise PlaybackError("Spotify CDP: no search query for item")

        results = controller.search(query)
        if results:
            controller.play_track_from_search(0)
        else:
            from .base import PlaybackError

            raise PlaybackError("Spotify CDP: no results for '%s'" % query)

    def pause(self, handle: PlaybackHandle) -> None:
        """Pause playback via Space key (only if playing)."""
        controller = self._get_controller()
        np = controller.get_now_playing()
        if np and np.get("is_playing"):
            controller.play_pause()
            handle.mark_paused()
            self._logger.info("Spotify CDP: paused")

    def resume(self, handle: PlaybackHandle) -> None:
        """Resume playback via Space key (only if paused)."""
        controller = self._get_controller()
        np = controller.get_now_playing()
        if np and not np.get("is_playing"):
            controller.play_pause()
            handle.mark_resumed()
            self._notify_proctap_known_chrome_pid("resume")
            self._logger.info("Spotify CDP: resumed")

    def _notify_proctap_known_chrome_pid(self, reason: str) -> None:
        """Point ProcTap at the KNOWN Chrome PID instead of rescanning.

        During a >15s pause the Chrome audio session goes quiet, ProcTap's
        post-audio rescan wanders off the Chrome PID, and the generic
        resume-time rescan-for-whoever-is-audible races Spotify's audio
        restart — measured 24-79s of spoke silence after resume
        (2026-07-02 live runs, _diag/2026-07-01). A notified PID gets
        ProcTap's extended silence patience, so it waits for Chrome to
        become audible instead of shopping around and grabbing Viola's
        own process.
        """
        try:
            controller = self._get_controller()
            chrome_pid = controller.get_capture_chrome_pid()
            if not chrome_pid:
                self._logger.warning(
                    "Spotify CDP: no Chrome PID known for ProcTap %s notify",
                    reason,
                )
                return
            from audio_core.streaming.pipeline_wiring import notify_proctap_pid

            notify_proctap_pid(chrome_pid)
            self._logger.info(
                "Spotify CDP: notified ProcTap of Chrome PID %d on %s",
                chrome_pid,
                reason,
            )
        except (ImportError, RuntimeError, AttributeError, TypeError, ValueError, OSError) as exc:
            self._logger.debug("Spotify CDP: ProcTap %s notify skipped: %s", reason, exc)

    def stop(self, handle: PlaybackHandle) -> None:
        """Stop playback and clean up.

        request_stop() signals the poll loop to exit.  We call
        _stop_poll_loop() which joins the poll thread.  To avoid
        deadlock, the caller must NOT hold player._lock when
        calling this method (the poll thread may be waiting to
        acquire player._lock via _emit/_broadcast_state).
        """
        handle.request_stop()
        self._stop_poll_loop()

        try:
            controller = self._get_controller()
            np = controller.get_now_playing()
            if np and np.get("is_playing"):
                try:
                    controller.play_pause()
                except Exception as exc:
                    self._logger.debug("Spotify CDP stop-pause failed: %s", exc)
        except Exception as exc:
            self._logger.debug("Spotify CDP stop controller access failed: %s", exc)

        with self._lock:
            self._handles.pop(handle.item.id, None)
        handle.mark_finished()
        self._logger.info("Spotify CDP: stopped")

    def seek(self, handle: PlaybackHandle, position_seconds: float) -> None:
        """Seek to a position by clicking the Spotify progress bar.

        Args:
            handle: Active playback handle.
            position_seconds: Target position in seconds.
        """
        controller = self._get_controller()
        controller.seek(int(position_seconds * 1000))
        self._logger.info("Spotify CDP: seek to %.1fs", position_seconds)

    def next_track(self, handle: PlaybackHandle) -> None:
        """Skip to the next Spotify track."""
        controller = self._get_controller()
        controller.next_track()
        # Same known-PID pattern as play()/resume()/track-change: the skip
        # makes Chrome briefly silent, and the poller's hook may lag the
        # DOM by up to 2.5s — name the PID now so capture never shops.
        self._notify_proctap_known_chrome_pid("next track")
        self._logger.info("Spotify CDP: next track")

    def previous_track(self, handle: PlaybackHandle) -> None:
        """Skip to the previous Spotify track."""
        controller = self._get_controller()
        controller.prev_track()
        self._notify_proctap_known_chrome_pid("previous track")
        self._logger.info("Spotify CDP: previous track")

    def set_volume(self, handle: PlaybackHandle, level: int) -> None:
        """Set volume by clicking the Spotify volume slider at the target position.

        Args:
            handle: Active playback handle.
            level: Volume level 0-100.
        """
        controller = self._get_controller()
        controller.set_volume(level)
        self._logger.info("Spotify CDP: volume set to %d%%", level)

    def current_position(self, handle: PlaybackHandle) -> float:
        """Return cached playback position from last poll."""
        return self._position

    def duration(self, handle: PlaybackHandle) -> float:
        """Return cached duration from last poll."""
        return self._duration
