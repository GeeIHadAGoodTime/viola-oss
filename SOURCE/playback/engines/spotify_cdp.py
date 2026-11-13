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
# Polls in a row that must see Spotify playing while the player says otherwise
# before the player is corrected. One poll can cross a pause or a new play.
_STATE_HEAL_AFTER_POLLS = 2
# The SPOTIFY_STATE line is written when anything in it changes, and at least
# this often (12 polls = 30 s) so a long session still shows the state.
_STATE_LOG_EVERY_POLLS = 12


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
        # One poller per play, each with its own stop signal. They used to
        # share one: stopping the previous song's handle after the next song
        # had started ended the new song's poller, which reported the new song
        # finished while it was still playing.
        self._pollers: dict[int, tuple[threading.Thread, threading.Event]] = {}
        self._current_handle: PlaybackHandle | None = None
        self._emit_thread: threading.Thread | None = None
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
        """Start the background state polling loop for this play."""
        stop = threading.Event()
        thread = threading.Thread(
            target=self._poll_loop,
            args=(handle, stop),
            name="SpotifyCDPPoller",
            daemon=True,
        )
        with self._lock:
            self._pollers[id(handle)] = (thread, stop)
        self._poll_stop = stop
        self._poll_thread = thread
        thread.start()

    def _stop_poll_loop(self, handle: PlaybackHandle | None = None) -> None:
        """Stop this play's poller (every poller when no handle is given)."""
        with self._lock:
            if handle is None:
                entries = list(self._pollers.values())
                self._pollers.clear()
            else:
                entry = self._pollers.pop(id(handle), None)
                entries = [entry] if entry is not None else []
        if handle is None:
            self._poll_stop.set()
            if self._poll_thread is not None:
                entries.append((self._poll_thread, self._poll_stop))
        for thread, stop in entries:
            stop.set()
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=5)
        if handle is None or not self._pollers:
            self._poll_thread = None

    def _owns_player(self, handle: PlaybackHandle) -> bool:
        """This play is the one the engine is playing now (not a superseded one)."""
        current = self._current_handle
        return current is None or current is handle

    def _poll_loop(self, handle: PlaybackHandle, stop: threading.Event | None = None) -> None:
        """Background loop that polls Spotify DOM for state changes."""
        self._logger.info("SpotifyCDPPoller: poll loop started")
        controller = self._get_controller()
        if stop is None:
            stop = self._poll_stop
        poll_interval = 2.5
        login_check_counter = 0
        poll_count = 0
        consecutive_poll_errors = 0
        connection_notice_sent = False
        watch: dict[str, Any] = {}

        while not handle.stop_requested() and not stop.is_set():
            try:
                if not self._owns_player(handle):
                    # A newer play owns the player; this one only waits to be stopped.
                    stop.wait(poll_interval)
                    continue
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

                        # Every poll, not only on a track change: the cover is
                        # often not on the page yet when the title first shows.
                        artwork_url = np.get("artwork_url")
                        if artwork_url and artwork_url != handle.artwork_url():
                            handle.mark_artwork(artwork_url)

                        # Metadata sync runs EVERY non-stale poll, not only on a
                        # track change: the change-only sync left a raw-URI
                        # title in the emitted state forever when _last_track
                        # already matched the playing track.
                        self._sync_item_metadata(handle, current_track, np.get("artist"))

                        try:
                            self._keep_player_truthful(handle, np, poll_count, watch)
                        except Exception:
                            # Never counted as a lost connection.
                            self._logger.debug("Spotify state check failed", exc_info=True)

                        # Broadcast state update via player._emit()
                        self._broadcast_state()
                elif poll_count <= 5 or poll_count % _STATE_LOG_EVERY_POLLS == 0:
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
                            self._report_play_failure(exc)
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

            stop.wait(poll_interval)

        with self._lock:
            entry = self._pollers.get(id(handle))
            if entry is not None and entry[1] is stop:
                self._pollers.pop(id(handle), None)
        self._logger.info(
            "SpotifyCDPPoller: poll loop ended after %d polls (stop_requested=%s)",
            poll_count,
            handle.stop_requested(),
        )
        handle.mark_finished()

    # ------------------------------------------------------------------
    # Player truth
    # ------------------------------------------------------------------
    def _keep_player_truthful(
        self,
        handle: PlaybackHandle,
        np: dict[str, Any],
        poll_count: int,
        watch: dict[str, Any],
    ) -> None:
        """Log what Spotify and the player each say, and correct the player.

        Build 10 on the laptop: Spotify played for minutes while the player
        reported nothing playing ("DUCK_SKIPPED: nothing is playing"), so
        Viola talked over the music at full volume and the stage, the seek bar
        and "like" all followed the wrong state. Nothing in the log said what
        the player's state was or what the page offered. The engine is the only
        part that can see Spotify, so it now writes both sides
        (SPOTIFY_STATE) and, when Spotify's position is moving while the
        player says it is not playing, puts the player right
        (SPOTIFY_STATE_HEAL). A pause or stop the user asked for is never
        undone: those pause or end this handle first.
        """
        from music.player.playback_state import PlaybackPhase
        from playback.engine_manager import _get_player_for_emit

        position = float(_parse_time_str(np.get("position")))
        previous = watch.get("position")
        watch["position"] = position
        advancing = previous is not None and position > previous
        # The play button, the running media element or the page's Media Session.
        spotify_playing = bool(
            np.get("is_playing") or np.get("media_playing") or np.get("media_session_state") == "playing"
        )

        player = _get_player_for_emit()
        sm = getattr(player, "_playback_sm", None) if player is not None else None
        phase = getattr(sm, "phase", None)
        phase_name = getattr(phase, "name", None) or "unknown"
        shown = getattr(getattr(player, "_state", None), "now_playing", None) if player is not None else None

        summary = (
            phase_name,
            shown is not None,
            spotify_playing,
            np.get("button_playing"),
            np.get("media_playing"),
            np.get("media_elements"),
            np.get("media_session"),
            np.get("media_session_state"),
            np.get("widget"),
            np.get("track_source"),
            np.get("artist_source"),
            np.get("artwork_source"),
            np.get("position_source"),
            np.get("duration_source"),
            np.get("track_id_source") or "none",
            np.get("visibility"),
        )
        if summary != watch.get("summary") or poll_count % _STATE_LOG_EVERY_POLLS == 0:
            watch["summary"] = summary
            self._logger.info(
                "SPOTIFY_STATE poll=%d player_phase=%s player_has_track=%s spotify_playing=%s position_moving=%s "
                "button_playing=%s media_playing=%s media_elements=%s media_session=%s media_session_state=%s "
                "widget=%s title_from=%s artist_from=%s art_from=%s position_from=%s duration_from=%s "
                "track_id_from=%s page=%s position=%s duration=%s",
                poll_count,
                summary[0],
                summary[1],
                spotify_playing,
                advancing,
                *summary[3:],
                np.get("position"),
                np.get("duration"),
            )

        wrong = sm is not None and (phase != PlaybackPhase.PLAYING or shown is None)
        backend = getattr(player, "_backend", None) if player is not None else None
        ours = getattr(backend, "_handle", None) is handle
        user_paused = bool(getattr(player, "_user_paused", False) or getattr(backend, "_paused", False))
        if not (wrong and ours and spotify_playing and advancing) or user_paused or handle.is_paused():
            watch["wrong_polls"] = 0
            return
        watch["wrong_polls"] = int(watch.get("wrong_polls", 0)) + 1
        if watch["wrong_polls"] < _STATE_HEAL_AFTER_POLLS:
            return
        watch["wrong_polls"] = 0

        cv = getattr(player, "_cv", None)
        if cv is None:
            return
        with cv:
            if handle.stop_requested() or getattr(player, "_user_paused", False):
                return
            state = getattr(player, "_state", None)
            shown = getattr(state, "now_playing", None)
            item = handle.item
            if shown is not None and shown is not item and getattr(shown, "id", None) != getattr(item, "id", None):
                return  # the player is showing another track; not this play's to correct
            before = sm.phase
            if before == PlaybackPhase.PLAYING and shown is not None:
                return
            if shown is None:
                set_now_playing = getattr(player, "_set_now_playing_locked", None)
                if callable(set_now_playing):
                    set_now_playing(item)
                elif state is not None:
                    state.now_playing = item
            if before != PlaybackPhase.PLAYING:
                if before not in (PlaybackPhase.LOADING, PlaybackPhase.PAUSED):
                    sm.transition(PlaybackPhase.LOADING, force=True)
                sm.transition(PlaybackPhase.PLAYING, force=True)
            if state is not None:
                state.is_playing = True
            cv.notify_all()
        self._logger.warning(
            "SPOTIFY_STATE_HEAL: Spotify is playing (position moving) but the player said %s with %s; "
            "set the player to PLAYING with this track",
            before.name,
            "a track shown" if shown is not None else "no track shown",
        )

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
                return
            # No event loop is reachable from the poll thread (always so on the
            # desktop: the player holds no hub reference). The broadcast used
            # to be skipped here, leaving the screen to the 5 s periodic
            # broadcaster, which only sends when the track id, play state or
            # position changed: a new title or cover on the same play never
            # reached the stage. Emit from a short-lived thread instead, never
            # from the poll thread itself (stop() joins it), one at a time.
            previous = self._emit_thread
            if previous is not None and previous.is_alive():
                return
            self._emit_thread = threading.Thread(target=player._emit, name="SpotifyCDPEmit", daemon=True)
            self._emit_thread.start()
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
        try:
            return self._play(item, on_artwork=on_artwork)
        except Exception as exc:
            self._report_play_failure(exc)
            raise

    def _report_play_failure(self, exc: Exception) -> None:
        """Spotify was chosen and can't play: say why, on screen and to the tools."""
        from music.spotify import connection

        state = str(getattr(exc, "state", "") or getattr(exc.__cause__, "state", "") or "")
        known = state in (
            connection.NOT_SIGNED_IN,
            connection.SESSION_REJECTED,
            connection.NO_BROWSER,
            connection.PLAYER_UNAVAILABLE,
            connection.DRM_UNAVAILABLE,
            connection.SIGNING_IN,
        )
        if known:
            message = connection.message_for(state)
        else:
            # Signed in and reachable, but the song didn't start. Connected stays
            # true (the next song may play); this play still has to say so.
            state = connection.CONNECTED
            message = connection.PLAY_DID_NOT_START_MESSAGE
        code = "spotify_%s" % (state if known else "play_failed")
        self._logger.warning("Spotify CDP: play failed state=%s error=%s", code, str(exc)[:300])
        connection.record_play_failure(None, state=state, message=message, code=code)
        connection.notify_spotify(None, message, code=code, level="error")
        try:
            from music.spotify.diagnostic import log_diagnostic

            log_diagnostic(self._get_controller(), reason=code)
        except Exception:
            self._logger.debug("Spotify diagnostic after a failed play did not run", exc_info=True)
        if state == connection.SESSION_REJECTED:
            # A player nobody can use has no reason to keep running.
            try:
                self._get_controller().shutdown()
            except Exception:
                self._logger.debug("Spotify player shutdown after a refused sign-in failed", exc_info=True)

    def _play(
        self,
        item: QueueItem,
        *,
        on_artwork: Callable[[QueueItem, str | None], None] | None = None,
    ) -> PlaybackHandle:
        controller = self._get_controller()
        controller.ensure_running()
        self._annotate_account_tier(controller)

        handle = PlaybackHandle(item, self.provider_id)
        # From here this play owns the player: an older play's poller stops
        # writing state, and stopping the older handle no longer touches the
        # transport (it used to press pause on the song that had just started).
        self._current_handle = handle

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
        liked_songs = item.capabilities.get("spotify_context") == "liked_songs"

        if liked_songs:
            shuffle = item.capabilities.get("shuffle")
            try:
                played_meta = controller.play_liked_songs(shuffle=shuffle if isinstance(shuffle, bool) else None)
            except Exception as exc:
                raise PlaybackError("Spotify Liked Songs playback failed: %s" % exc) from exc
            self._adopt_direct_uri_metadata(item, played_meta)
        elif track_uri and isinstance(track_uri, str) and track_uri.strip():
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
        self._record_playing()
        # A collection start plays whatever Spotify picks first, so there is
        # no expected title to guard against a stale startup track.
        handle._spotify_cdp_expected_title = "" if liked_songs else (item.title or "")
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

    def _record_playing(self) -> None:
        """A play that started proves the connection; log the proof a test can read."""
        from music.spotify import connection

        resolved = connection.resolve_user_id()
        if resolved:
            connection.record_verdict(resolved, connection.CONNECTED, detail="playback started")

        def _proof() -> None:
            try:
                from music.spotify.diagnostic import log_diagnostic

                log_diagnostic(self._get_controller(), reason="play_started")
            except Exception:
                self._logger.debug("Spotify proof log failed", exc_info=True)

        threading.Timer(4.0, _proof).start()

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
        self._stop_poll_loop(handle)

        superseded = not self._owns_player(handle)
        if self._current_handle is handle:
            self._current_handle = None
        if superseded:
            # A newer play is on the player. Pausing here would silence it.
            self._logger.info("Spotify CDP: stopped a superseded play; transport left to the current one")
        else:
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
