"""
Queue Discovery System - Enhanced Queue & Music Discovery
Provides inline queue preview, recommendations, and recently played history

Features:
- Mini queue preview (no modal needed)
- Smart recommendations
- Recently played history
- Visual queue progress
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config.settings import settings
from core.json_types import JsonDict, JsonValue, to_json_value
from core.logging_config import get_logger
from models.player import QueueItem

logger = get_logger(__name__)


@dataclass
class QueueItemDisplay:
    """Enhanced queue item for display"""

    id: str
    title: str
    artist: str | None
    thumbnail_url: str | None
    duration: int  # seconds
    position_in_queue: int
    is_current: bool = False
    is_next: bool = False
    video_id: str | None = None
    unavailable: bool = False
    unavailable_reason: str | None = None

    def to_dict(self) -> JsonDict:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "thumbnail_url": self.thumbnail_url,
            "duration": self.duration,
            "position_in_queue": self.position_in_queue,
            "is_current": self.is_current,
            "is_next": self.is_next,
            "video_id": self.video_id,
            "unavailable": self.unavailable,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass
class RecommendedSong:
    """Song recommendation"""

    video_id: str
    title: str
    artist: str | None
    thumbnail_url: str | None
    reason: str  # Why this is recommended
    confidence: float  # 0.0 to 1.0

    def to_dict(self) -> JsonDict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artist": self.artist,
            "thumbnail_url": self.thumbnail_url,
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass
class HistoryItem:
    """Recently played song"""

    video_id: str
    title: str
    artist: str | None
    thumbnail_url: str | None
    played_at: datetime
    is_favorite: bool = False
    play_count: int = 1

    def to_dict(self) -> JsonDict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "artist": self.artist,
            "thumbnail_url": self.thumbnail_url,
            "played_at": self.played_at.isoformat(),
            "is_favorite": self.is_favorite,
            "play_count": self.play_count,
        }


class QueueDiscoverySystem:
    """
    Enhanced queue visualization and music discovery system.
    Provides inline previews, recommendations, and history.
    """

    def __init__(self, music_player=None, settings_manager=None):
        self.music_player = music_player
        self.settings_manager = settings_manager
        self._history: list[HistoryItem] = []
        self._max_history = 50
        self._load_history()

    def _load_history(self):
        """Load history from settings"""
        if self.settings_manager:
            stored = self.settings_manager.get("recently_played_history", [])
            for item in stored:
                try:
                    self._history.append(
                        HistoryItem(
                            video_id=item["video_id"],
                            title=item["title"],
                            artist=item.get("artist"),
                            thumbnail_url=item.get("thumbnail_url"),
                            played_at=datetime.fromisoformat(item["played_at"]),
                            is_favorite=item.get("is_favorite", False),
                            play_count=item.get("play_count", 1),
                        )
                    )
                except Exception as e:
                    logger.warning("Failed to load history item: %s", e)

    def _save_history(self):
        """Save history to settings"""
        if self.settings_manager:
            stored = [item.to_dict() for item in self._history]
            self.settings_manager.set("recently_played_history", stored)

    def get_mini_queue_preview(self, max_items: int = 3) -> JsonDict:
        """
        Get a mini preview of the queue (next few songs).
        This is displayed inline, no modal needed.
        """
        if not self.music_player:
            return {"items": [], "total_items": 0, "has_more": False}

        try:

            def _to_plain_dict(value: object) -> JsonValue:
                """
                Convert Pydantic models or dataclass-like objects to dictionaries.
                Leaves mapping-like objects untouched.
                """
                model_dump = getattr(value, "model_dump", None)
                if callable(model_dump):
                    try:
                        return to_json_value(model_dump())
                    except Exception as exc:
                        logger.debug("Queue discovery model_dump failed: %s", exc)
                dict_method = getattr(value, "dict", None)
                if callable(dict_method):
                    try:
                        return to_json_value(dict_method())
                    except Exception as exc:
                        logger.debug("Queue discovery dict() fallback failed: %s", exc)
                if hasattr(value, "__dict__"):
                    return to_json_value(vars(value))
                return to_json_value(value)

            raw_state = self.music_player.state()
            state_value: JsonValue = raw_state if isinstance(raw_state, dict) else _to_plain_dict(raw_state)
            state_dict: JsonDict = (
                {str(k): to_json_value(v) for k, v in state_value.items()} if isinstance(state_value, dict) else {}
            )
            queue_value = state_dict.get("queue")
            queue = queue_value if isinstance(queue_value, list) else []

            # Mark unavailable YouTube tracks
            from music.providers.checker import (
                get_youtube_unavailable_reason,
                is_provider_linked,
                is_youtube_track_requiring_provider,
            )

            for item in queue:
                item_dict = _to_plain_dict(item) if not isinstance(item, dict) else item
                if is_youtube_track_requiring_provider(item_dict):
                    if not is_provider_linked("youtube_music"):
                        if isinstance(item_dict, dict):
                            item_dict["unavailable"] = True
                            item_dict["unavailable_reason"] = get_youtube_unavailable_reason()
                        elif isinstance(item, QueueItem):
                            # For QueueItem objects, set attributes
                            item.unavailable = True
                            item.unavailable_reason = get_youtube_unavailable_reason()

            # Convert to display items
            items = []
            for i, item in enumerate(queue[:max_items]):
                if not isinstance(item, dict):
                    converted = _to_plain_dict(item)
                    item = converted if isinstance(converted, dict) else {}

                item_id_value = item.get("id")
                item_id = item_id_value if isinstance(item_id_value, str) and item_id_value else f"item_{i}"

                title_value = item.get("title")
                title = title_value if isinstance(title_value, str) and title_value else "Unknown"

                artist_value = item.get("artist")
                artist = artist_value if isinstance(artist_value, str) and artist_value else None

                duration_value = item.get("duration")
                duration = int(duration_value) if isinstance(duration_value, (str, int, float)) else 0

                video_id_value = item.get("video_id")
                video_id = video_id_value if isinstance(video_id_value, str) and video_id_value else None

                unavailable_value = item.get("unavailable")
                unavailable = unavailable_value if isinstance(unavailable_value, bool) else False

                unavailable_reason_value = item.get("unavailable_reason")
                unavailable_reason = (
                    unavailable_reason_value
                    if isinstance(unavailable_reason_value, str) and unavailable_reason_value
                    else None
                )

                items.append(
                    QueueItemDisplay(
                        id=item_id,
                        title=title,
                        artist=artist,
                        thumbnail_url=self._get_thumbnail_url(item),
                        duration=duration,
                        position_in_queue=i + 1,
                        is_current=i == 0,
                        is_next=i == 1,
                        video_id=video_id,
                        unavailable=unavailable,
                        unavailable_reason=unavailable_reason,
                    )
                )

            return {
                "items": [item.to_dict() for item in items],
                "total_items": len(queue),
                "has_more": len(queue) > max_items,
            }
        except Exception as e:
            logger.error("Failed to get mini queue preview: %s", e)
            return {"items": [], "total_items": 0, "has_more": False}

    async def get_recommendations(
        self,
        current_song: JsonDict | None = None,
        max_items: int = 5,
        use_ai: bool = True,
    ) -> list[RecommendedSong]:
        """
        Get smart recommendations based on current song and listening history.

        Uses AI if available, falls back to genre/artist matching.
        """
        recommendations: list[RecommendedSong] = []

        # If no current song, use recently played
        if not current_song and self._history:
            current_song = {
                "video_id": self._history[0].video_id,
                "title": self._history[0].title,
                "artist": self._history[0].artist,
            }

        if not current_song:
            return []

        # Try AI recommendations first (if enabled and available)
        if use_ai and self._is_ai_enabled():
            ai_recommendations = await self._get_ai_recommendations(current_song, max_items)
            recommendations.extend(ai_recommendations)

        # Fallback to simple recommendations if AI unavailable
        if len(recommendations) < max_items:
            recommendations.extend(self._get_simple_recommendations(current_song, max_items))

        return recommendations[:max_items]

    async def _get_ai_recommendations(self, current_song: JsonDict, max_items: int) -> list[RecommendedSong]:
        """Get AI-powered recommendations using GPT"""
        try:
            api_key = settings.openai_api_key
            if not api_key:
                return []

            # Get recent history for context
            recent = self._history[:10]
            history_context = "\n".join([f"- {item.title} by {item.artist or 'Unknown'}" for item in recent])

            # Build prompt
            prompt = f"""Given that the user is currently listening to:
"{current_song.get("title", "Unknown")}" by "{current_song.get("artist", "Unknown")}"

And their recent listening history:
{history_context}

Suggest {max_items} similar songs they might enjoy. Return ONLY a JSON array with this exact format:
[
    {{"title": "Song Title", "artist": "Artist Name", "reason": "Brief reason", "video_id": "suggested_id"}},
    ...
]
"""

            # F-003: Enforce per-user rate limit before any LLM call.
            # Prefer the authenticated user from the ambient ContextVar so the
            # rate-limit bucket is per-user; fall back to the stable device-id
            # only when no auth context exists (desktop boot-time helpers).
            from core.exceptions import LLMQuotaExceededError
            from services.llm.rate_limiter import get_rate_limiter

            try:
                try:
                    from core.user_context import get_current_user_id

                    limiter_user_id = get_current_user_id()
                except LookupError:
                    from core.user_context import get_device_user_id

                    limiter_user_id = get_device_user_id()  # mt-ok: anon rate-limit bucket

                limiter = get_rate_limiter()
                await limiter.reserve(
                    user_id=limiter_user_id,
                    estimated_tokens=settings.llm_max_tokens_cap,
                )
            except LLMQuotaExceededError:
                logger.warning("LLM rate limit exceeded for queue recommendations")
                return []

            # Use canonical LLM router
            from intent.pipeline import create_llm_router

            llm_router = create_llm_router(config_or_settings={"openai_api_key": api_key})
            if llm_router:
                response = await llm_router.ask(prompt, history=None)
                answer_payload = response
            else:
                logger.warning("No LLM providers available for queue discovery")
                return []
            if not isinstance(answer_payload, str):
                return []

            # Parse response
            import json

            suggestions = json.loads(answer_payload)

            return [
                RecommendedSong(
                    video_id=s.get("video_id", ""),
                    title=s["title"],
                    artist=s.get("artist"),
                    thumbnail_url=None,
                    reason=s.get("reason", "Similar to what you're listening to"),
                    confidence=0.8,
                )
                for s in suggestions[:max_items]
            ]

        except Exception as e:
            logger.debug("AI recommendations failed: %s", e)
            return []

    def _get_simple_recommendations(self, current_song: JsonDict, max_items: int) -> list[RecommendedSong]:
        """Get simple recommendations based on artist/genre matching"""
        recommendations: list[RecommendedSong] = []
        artist_value = current_song.get("artist")
        current_artist = artist_value.lower() if isinstance(artist_value, str) else ""

        current_video_id_value = current_song.get("video_id")
        current_video_id = current_video_id_value if isinstance(current_video_id_value, str) else None

        # Find similar songs from history by same artist
        for item in self._history:
            if len(recommendations) >= max_items:
                break

            if item.artist and item.artist.lower() == current_artist:
                if current_video_id is None or item.video_id != current_video_id:
                    recommendations.append(
                        RecommendedSong(
                            video_id=item.video_id,
                            title=item.title,
                            artist=item.artist,
                            thumbnail_url=item.thumbnail_url,
                            reason=f"More from {item.artist}",
                            confidence=0.7,
                        )
                    )

        # Add favorites if we need more
        if len(recommendations) < max_items:
            for item in self._history:
                if len(recommendations) >= max_items:
                    break

                if item.is_favorite and (current_video_id is None or item.video_id != current_video_id):
                    recommendations.append(
                        RecommendedSong(
                            video_id=item.video_id,
                            title=item.title,
                            artist=item.artist,
                            thumbnail_url=item.thumbnail_url,
                            reason="One of your favorites",
                            confidence=0.9,
                        )
                    )

        return recommendations[:max_items]

    def add_to_history(self, song: JsonDict) -> None:
        """Add a song to recently played history"""
        video_id_value = song.get("video_id") or song.get("id")
        video_id = video_id_value if isinstance(video_id_value, str) and video_id_value else None
        if not video_id:
            return

        # Check if already in history
        for item in self._history:
            if item.video_id == video_id:
                # Update play count and time
                item.play_count += 1
                item.played_at = datetime.now()
                self._history.remove(item)
                self._history.insert(0, item)
                self._save_history()
                return

        # Add new item
        title_value = song.get("title")
        title = title_value if isinstance(title_value, str) and title_value else "Unknown"

        artist_value = song.get("artist")
        artist = artist_value if isinstance(artist_value, str) and artist_value else None

        new_item = HistoryItem(
            video_id=video_id,
            title=title,
            artist=artist,
            thumbnail_url=self._get_thumbnail_url(song),
            played_at=datetime.now(),
            is_favorite=False,
            play_count=1,
        )

        self._history.insert(0, new_item)

        # Trim history
        if len(self._history) > self._max_history:
            self._history = self._history[: self._max_history]

        self._save_history()

    def get_recently_played(self, max_items: int = 10, time_filter: timedelta | None = None) -> list[HistoryItem]:
        """
        Get recently played songs.

        Args:
            max_items: Maximum number of items to return
            time_filter: Only return items played within this timeframe (e.g., timedelta(days=7))
        """
        items = self._history[:max_items]

        if time_filter:
            cutoff = datetime.now() - time_filter
            items = [item for item in items if item.played_at > cutoff]

        return items

    def toggle_favorite(self, video_id: str) -> bool:
        """Toggle favorite status for a song. Returns new favorite status."""
        for item in self._history:
            if item.video_id == video_id:
                item.is_favorite = not item.is_favorite
                self._save_history()
                return item.is_favorite
        return False

    def get_favorites(self) -> list[HistoryItem]:
        """Get all favorite songs"""
        return [item for item in self._history if item.is_favorite]

    def clear_history(self):
        """Clear all history (except favorites)"""
        self._history = [item for item in self._history if item.is_favorite]
        self._save_history()

    def _get_thumbnail_url(self, song: JsonDict) -> str | None:
        """Extract thumbnail URL from song dict"""
        # Try multiple possible keys
        for key in ["thumbnail_url", "thumbnail", "album_art", "artwork_url"]:
            value = song.get(key)
            if isinstance(value, str) and value:
                return value

        # Generate YouTube thumbnail URL if we have video_id
        video_id_value = song.get("video_id") or song.get("id")
        if isinstance(video_id_value, str) and video_id_value:
            return f"https://img.youtube.com/vi/{video_id_value}/default.jpg"

        return None

    def _is_ai_enabled(self) -> bool:
        """Check if AI features are enabled"""
        if self.settings_manager:
            return bool(self.settings_manager.get("enable_gpt", False))
        return False


# Global singleton
_queue_discovery_system: QueueDiscoverySystem | None = None


def get_queue_discovery_system(music_player=None, settings_manager=None) -> QueueDiscoverySystem:
    """Get or create global queue discovery system"""
    global _queue_discovery_system
    if _queue_discovery_system is None:
        _queue_discovery_system = QueueDiscoverySystem(music_player, settings_manager)
    return _queue_discovery_system
