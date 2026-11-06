from __future__ import annotations

from typing import Any

from models.player import QueueItem
from music.player.playback_state import PlaybackPhase
from music.queue_config import QueueLimits

from .queue_resolver import QueueItemResolver


class AutoplayService:
    """Encapsulates autoplay queue bookkeeping."""

    def __init__(
        self,
        *,
        player: MusicPlayer,
        controller,
        resolver: QueueItemResolver,
        playlist,
        queue_config: QueueLimits,
        failure_ttl: float,
        logger,
    ) -> None:
        self._player = player
        self._controller = controller
        self._resolver = resolver
        self._playlist = playlist
        self._queue_config = queue_config
        self._failure_ttl = failure_ttl
        self._logger = logger.getChild("autoplay_service")

    def _stopped_by_user(self) -> bool:
        phase = getattr(getattr(self._player, "_playback_sm", None), "phase", None)
        return phase == PlaybackPhase.STOPPED and bool(getattr(self._player, "_user_paused", False))

    def enqueue(self, query: str, *, metadata: dict[str, Any] | None = None) -> QueueItem | None:
        player = self._player
        # A radio fetch already in flight must not refill the queue after the
        # user stopped: the first item became the current track and its embed
        # started playing by itself a few seconds later.
        if self._stopped_by_user():
            self._logger.info("Autoplay: playback was stopped; not queueing %r", (metadata or {}).get("title"))
            return None
        engine_manager = getattr(player, "_playback_manager", None) or getattr(player, "_engine_manager", None)
        item = self._resolver.resolve(
            query,
            "ytsearch1",
            metadata,
            engine_manager=engine_manager,
        )

        # Block disliked songs from autoplay - user explicitly said they don't want to hear this
        video_id = getattr(item, "video_id", None) or getattr(item, "id", None)
        if video_id:
            try:
                from music.rating_system import get_rating_system

                if get_rating_system().is_disliked(video_id):
                    self._logger.info(
                        "Blocking disliked song from autoplay: %s (%s)",
                        getattr(item, "title", "Unknown"),
                        video_id,
                    )
                    return None
            except Exception as e:
                self._logger.debug("Could not check dislike status: %s", e)

        with player._cv:
            self._playlist.expire_failures(self._failure_ttl)
            if self._playlist.has_recent_failure(item, self._failure_ttl):
                self._logger.debug(
                    "Skipping autoplay addition for recently failed item %s",
                    getattr(item, "id", None),
                )
                return None

            if not self._queue_config.can_add_autoplay_song(
                self._playlist.queue_size(), self._playlist.autoplay_additions()
            ):
                self._playlist.set_backpressure(origin="autoplay", reason="capacity")
                self._logger.info(
                    "Autoplay backpressure triggered: %s",
                    self._playlist.backpressure_notice(),
                )
                return None

            self._controller.append_to_queue(item)
            self._playlist.increment_autoplay_counter()
            # Queue mutations auto-invalidate - no sync needed
            if not self._playlist.current():
                self._playlist.schedule_current()
                player._set_now_playing_locked(self._playlist.current())
            player._cv.notify_all()

        player._emit()
        return item


from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from music.player import MusicPlayer
