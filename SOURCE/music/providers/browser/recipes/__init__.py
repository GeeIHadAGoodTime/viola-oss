"""
Browser-native provider recipes.

Each recipe encapsulates URL templates, JS automation snippets, and
detection logic for a launch-supported music streaming provider.  Only
YouTube Music and Spotify are registered here.

Usage::

    from music.providers.browser.recipes import get_recipe, PROVIDER_TEMPLATES

    recipe = get_recipe("youtube_music")
    url    = recipe.get_search_url("bohemian rhapsody")
    js     = recipe.get_play_first_result_js()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.logging_config import get_logger

if TYPE_CHECKING:
    from music.providers.browser.recipes.base import ProviderRecipe

logger = get_logger(__name__)

SUPPORTED_BROWSER_PROVIDER_IDS: tuple[str, ...] = ("youtube_music", "spotify")

PROVIDER_TEMPLATES: dict[str, str] = {
    "youtube_music": "https://www.youtube.com/results?search_query={query}",
    "spotify": "https://open.spotify.com/search/{query}",
}

PROVIDER_HOME_URLS: dict[str, str] = {
    "youtube_music": "https://www.youtube.com",
    "spotify": "https://open.spotify.com",
}

# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def get_recipe(name: str) -> ProviderRecipe:
    """Instantiate and return a recipe by provider name.

    Args:
        name: Provider identifier (must match a key in ``PROVIDER_TEMPLATES``
              **and** have a concrete recipe class).

    Returns:
        A :class:`ProviderRecipe` instance for the requested provider.

    Raises:
        ValueError: If *name* does not have an implemented recipe.
    """
    if name == "youtube_music":
        from music.providers.browser.recipes.youtube_music import YouTubeMusicRecipe

        return YouTubeMusicRecipe()

    if name == "spotify":
        from music.providers.browser.recipes.spotify import SpotifyRecipe

        return SpotifyRecipe()

    raise ValueError("No recipe for provider: %s" % name)


def get_available_providers() -> list[str]:
    """Return provider names users can choose in current product surfaces."""
    return list(SUPPORTED_BROWSER_PROVIDER_IDS)


def get_all_provider_names() -> list[str]:
    """Return all registered browser-native provider names."""
    return list(PROVIDER_TEMPLATES.keys())


__all__ = [
    "PROVIDER_HOME_URLS",
    "PROVIDER_TEMPLATES",
    "SUPPORTED_BROWSER_PROVIDER_IDS",
    "get_all_provider_names",
    "get_available_providers",
    "get_recipe",
]
