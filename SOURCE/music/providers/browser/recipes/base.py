"""
Base recipe interface for browser-native provider automation.

Each provider recipe encapsulates the knowledge of how to search, play,
detect playback, and detect login/session state for a specific music
streaming service via browser automation (JavaScript injection).

Recipes are intentionally decoupled from Playwright / QWebEngine -- they
produce JS strings that any execution layer can evaluate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import quote_plus


@dataclass(frozen=True)
class RecipeResult:
    """Result of a recipe operation."""

    success: bool
    message: str = ""
    data: dict = field(default_factory=dict)


class ProviderRecipe(ABC):
    """Base class for provider-specific search-and-play recipes.

    Each recipe knows how to:
    1. Construct search URLs for its provider
    2. Generate JS to click the first search result
    3. Detect when playback has started
    4. Detect login status and session expiry
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'youtube_music')."""

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable provider name (e.g., 'YouTube Music')."""

    @property
    @abstractmethod
    def search_url_template(self) -> str:
        """URL template with {query} placeholder."""

    @property
    @abstractmethod
    def home_url(self) -> str:
        """Provider home page URL."""

    def get_search_url(self, query: str) -> str:
        """Build search URL from query.

        The query is URL-encoded before substitution to ensure safe URLs.

        Args:
            query: Raw search query text.

        Returns:
            Fully-formed search URL with encoded query.
        """
        return self.search_url_template.format(query=quote_plus(query))

    @abstractmethod
    def get_play_first_result_js(self) -> str:
        """Return JS that clicks and plays the first search result.

        The JS must be self-contained (no external deps), use try/catch,
        and return a JSON string: ``{success: bool, message: str, strategy: str}``.
        """

    @abstractmethod
    def get_detect_playback_started_js(self) -> str:
        """Return JS that detects whether playback has started.

        Must return a JSON string: ``{playing: bool}``.
        """

    @abstractmethod
    def get_login_detection_js(self) -> str:
        """Return JS that detects whether the user is logged in.

        Must return a JSON string: ``{logged_in: bool}``.
        """

    @abstractmethod
    def get_session_expired_js(self) -> str:
        """Return JS that detects session expiry (e.g., redirect to login).

        Must return a JSON string: ``{expired: bool}``.
        """

    def get_provider_icon_url(self) -> str | None:
        """Optional: URL to provider icon for display in UI.

        Returns:
            Icon URL string, or None if no icon is available.
        """
        return None

    # -- Embed support (for SmartDisplay content ID rendering) ---------------

    def has_embed_support(self) -> bool:
        """Whether this provider has an official embed widget.

        Providers with embed support can display a muted visual widget
        in SmartDisplay's media area (YouTube embed, Spotify player, etc.).
        Providers without fall back to album art display.

        Returns:
            ``True`` if an embed widget is available.
        """
        return False

    def get_content_id_from_url(self, url: str) -> str | None:
        """Extract the provider-specific content ID from a page URL.

        Returns ``None`` if the URL is not a content page (e.g. search
        results, browse pages, user profiles).

        Args:
            url: Current page URL (``window.location.href``).

        Returns:
            Content ID string, or ``None``.
        """
        return None

    def get_watch_url_pattern(self) -> str | None:
        """Regex pattern matching URLs where content is playing.

        Used to detect when the page has navigated from a search/browse
        page to an actual playback page.

        Returns:
            Regex pattern string, or ``None``.
        """
        return None

    def __repr__(self) -> str:
        return "<%s name=%r>" % (self.__class__.__name__, self.name)
