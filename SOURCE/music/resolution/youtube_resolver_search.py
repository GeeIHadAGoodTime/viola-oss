"""
YouTube Resolver Search Operations - Extracted from youtube_resolver.py

Handles search operations and error handling for YouTube resolution.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from music.providers.errors import (
    MusicProviderUnavailableError,
    MusicTrackNotFoundError,
    ProviderNotConfiguredError,
)
from music.providers.models import SearchResults, TrackSummary
from music.providers.youtube_availability import (
    YouTubeAvailabilityChecker,
    YouTubeVideoAvailability,
    check_youtube_video_playable,
)

if TYPE_CHECKING:
    from core.logging_config import StructuredLogger

YOUTUBE_SEARCH_CANDIDATE_LIMIT = 5


class YouTubeResolverSearchOperations:
    """Handles YouTube search operations and error handling."""

    def __init__(
        self,
        logger: logging.Logger | StructuredLogger,
        *,
        availability_checker: YouTubeAvailabilityChecker | None = None,
    ):
        self._logger = logger
        self._availability_checker = availability_checker or check_youtube_video_playable

    def search_tracks(self, provider: Any, query: str, user_id: str, active_provider_id: str) -> TrackSummary:
        """
        Search for tracks using provider.search_tracks().

        TOS COMPLIANCE: This MUST happen before video_id resolution and playback.
        Browser search must return video_id in the results.

        Args:
            provider: Provider instance
            query: Search query
            user_id: User ID for provider operations
            active_provider_id: Active provider ID for logging

        Returns:
            First playable TrackSummary from search results

        Raises:
            MusicProviderUnavailableError: If search fails or provider is unavailable
            MusicTrackNotFoundError: If no tracks found (provider is healthy but no results)
        """
        self._logger.info(
            "YTM_TOS_SEARCH_START: calling YouTube browser search query=%r user_id=%s limit=%d",
            query,
            user_id,
            YOUTUBE_SEARCH_CANDIDATE_LIMIT,
        )

        try:
            search_results: SearchResults = provider.search_tracks(
                user_id,
                query,
                limit=YOUTUBE_SEARCH_CANDIDATE_LIMIT,
            )
            self._logger.info(
                "YTM_TOS_SEARCH_COMPLETE: YouTube browser search returned n_results=%d query=%r user_id=%s",
                len(search_results.items) if search_results.items else 0,
                query,
                user_id,
            )
        except ProviderNotConfiguredError as exc:
            # Handle more specific error first (subclass of MusicProviderUnavailableError)
            # Convert to MusicProviderUnavailableError with structured technical_details
            error_msg = str(exc)
            root_cause = "provider_not_configured"
            self._logger.warning(
                "youtube_resolver._search_tracks: search_tracks failed query=%r user_id=%s root_cause=%s error=%s",
                query,
                user_id,
                root_cause,
                error_msg[:100],
            )
            user_message = "YouTube Music isn't fully set up yet. Say 'connect youtube' to finish linking your account."
            raise MusicProviderUnavailableError(
                user_message,
                technical_details={
                    "root_cause": root_cause,
                    "kind": "PROVIDER_CONFIG",
                    "provider": "youtube_music",
                    "query": query[:50],
                    "user_id": user_id,
                    "error_message": error_msg,
                    "stage": "search_tracks",
                },
            ) from exc
        except MusicProviderUnavailableError:
            # Re-raise MusicProviderUnavailableError as-is to preserve original root_cause
            # (e.g., quota_exceeded, no_user_token)
            raise
        except Exception as exc:
            # Generic provider error - wrap in MusicProviderUnavailableError
            root_cause = "provider_search_failed"
            self._logger.exception(
                "youtube_resolver._search_tracks: search_tracks unexpected error query=%r user_id=%s error=%s",
                query,
                user_id,
                str(exc)[:200],
            )
            raise MusicProviderUnavailableError(
                "YouTube search is busy right now. Try again in a moment.",
                technical_details={
                    "root_cause": root_cause,
                    "kind": "PROVIDER_ERROR",
                    "provider": "youtube_music",
                    "query": query[:50],
                    "user_id": user_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:100],
                },
            ) from exc

        # Validate search results
        if not search_results or not search_results.items:
            self._logger.warning(
                "YTM_TOS_SEARCH_EMPTY: YouTube browser search returned no results query=%r user_id=%s",
                query,
                user_id,
            )
            raise MusicTrackNotFoundError(
                "No results found for '%s'. Try rephrasing the title or artist name." % query,
                technical_details={
                    "root_cause": "track_not_found",
                    "query": query[:50],
                    "provider": "youtube_music",
                    "user_id": user_id,
                },
            )

        return self._first_playable_track(search_results.items, query, user_id)

    def _first_playable_track(self, tracks: list[TrackSummary], query: str, user_id: str) -> TrackSummary:
        skipped: list[str] = []
        for index, track in enumerate(tracks):
            video_id = self._video_id_from_track(track)
            if not video_id:
                skipped.append(f"{index}:missing_video_id")
                self._logger.warning(
                    "YOUTUBE_AVAILABILITY_SKIP query=%r user_id=%s index=%d reason=missing_video_id title=%r",
                    query,
                    user_id,
                    index,
                    track.title,
                )
                continue

            try:
                availability = self._availability_checker(video_id)
            except Exception as exc:
                skipped.append(f"{index}:{video_id}:availability_check_exception")
                self._logger.exception(
                    "YOUTUBE_AVAILABILITY_SKIP query=%r user_id=%s index=%d video_id=%s reason=availability_check_exception error=%s",
                    query,
                    user_id,
                    index,
                    video_id,
                    exc,
                )
                continue
            if availability.playable and availability.embeddable:
                self._logger.info(
                    "YOUTUBE_AVAILABILITY_ACCEPT query=%r user_id=%s index=%d video_id=%s reason=%s",
                    query,
                    user_id,
                    index,
                    video_id,
                    availability.reason,
                )
                return self._track_with_availability(track, availability)

            skipped.append(f"{index}:{video_id}:{availability.reason}")
            self._logger.warning(
                "YOUTUBE_AVAILABILITY_SKIP query=%r user_id=%s index=%d video_id=%s reason=%s status=%s",
                query,
                user_id,
                index,
                video_id,
                availability.reason,
                availability.status_code,
            )

        raise MusicTrackNotFoundError(
            "No playable YouTube results found for '%s'. Try a different title or artist." % query,
            technical_details={
                "root_cause": "no_playable_youtube_results",
                "query": query[:50],
                "provider": "youtube_iframe",
                "user_id": user_id,
                "candidate_count": len(tracks),
                "skipped_candidates": skipped,
            },
        )

    @staticmethod
    def _video_id_from_track(track: TrackSummary) -> str | None:
        video_id = track.provider_track_id or track.extras.get("video_id") or track.id
        return video_id if isinstance(video_id, str) and video_id else None

    @staticmethod
    def _track_with_availability(track: TrackSummary, availability: YouTubeVideoAvailability) -> TrackSummary:
        extras = dict(track.extras)
        extras.update(availability.as_track_extras())
        return track.model_copy(update={"extras": extras})
