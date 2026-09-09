"""
Embedded Backend HTML Generation.

This module contains HTML generation and YouTube embedding logic
extracted from the main EmbeddedPlayerBackend class to comply with code constraints.
"""

from __future__ import annotations

from config.settings import get_runtime_base_url
from core.logging_config import get_logger
from music.youtube_embed import extract_video_id as canonical_extract_video_id

logger = get_logger(__name__)


class EmbeddedBackendHTMLGenerator:
    """Handles HTML generation for embedded content."""

    def __init__(self, backend_instance):
        """
        Initialize HTML generator.

        Args:
            backend_instance: The EmbeddedPlayerBackend instance
        """
        self.backend = backend_instance

    def extract_video_id(self, url: str) -> str | None:
        """
        Extract YouTube video ID from various URL formats.

        Delegates to canonical implementation in music/youtube_embed.py.

        Args:
            url: YouTube URL

        Returns:
            Video ID or None if not found
        """
        return canonical_extract_video_id(url)

    def get_embed_url(self, url: str) -> str:
        """
        Get embed URL for a given video URL.

        Args:
            url: Source URL

        Returns:
            Embed URL
        """
        try:
            from music.youtube_embed import build_embed_url, extract_video_id

            video_id = extract_video_id(url)
            if video_id:
                return build_embed_url(video_id)

            # Fallback - return original URL if not YouTube
            return url

        except ImportError:
            # Fallback implementation
            video_id = self.extract_video_id(url)
            if video_id:
                return f"https://www.youtube.com/embed/{video_id}?enablejsapi=1&origin={self.get_origin()}"

            return url

    def generate_youtube_html(self, video_id: str) -> str:
        """
        Generate HTML for YouTube iframe player.

        Args:
            video_id: YouTube video ID

        Returns:
            HTML string
        """
        origin = self.get_origin()

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <title>YouTube Player</title>
            <style>
                body {{
                    margin: 0;
                    padding: 0;
                    background: #000;
                    font-family: Arial, sans-serif;
                }}
                #youtube-player {{
                    width: 100%;
                    height: 100vh;
                    border: none;
                }}
                .loading {{
                    position: absolute;
                    top: 50%;
                    left: 50%;
                    transform: translate(-50%, -50%);
                    color: #fff;
                    font-size: 16px;
                }}
            </style>
        </head>
        <body>
            <div class="loading">Loading player...</div>
            <iframe id="youtube-player"
                    src="https://www.youtube.com/embed/{video_id}?enablejsapi=1&origin={origin}&modestbranding=1&rel=0&showinfo=0"
                    frameborder="0"
                    allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
                    allowfullscreen>
            </iframe>

            <script>
                // Hide loading message when iframe loads
                document.addEventListener('DOMContentLoaded', function() {{
                    document.querySelector('.loading').style.display = 'none';
                }});

                // Handle iframe load
                document.getElementById('youtube-player').onload = function() {{
                    document.querySelector('.loading').style.display = 'none';
                }};
            </script>
        </body>
        </html>
        """

        return html

    def build_embed_url(self, video_id: str) -> str:
        """
        Build embed URL for YouTube video.

        Args:
            video_id: YouTube video ID

        Returns:
            Embed URL string
        """
        origin = self.get_origin()
        return (
            f"https://www.youtube.com/embed/{video_id}?enablejsapi=1&origin={origin}&modestbranding=1&rel=0&showinfo=0"
        )

    def get_origin(self) -> str:
        """
        Get the origin URL for embedding.

        Returns:
            Origin URL string
        """
        try:
            from music.youtube_embed import DEFAULT_EMBED_ORIGIN

            return DEFAULT_EMBED_ORIGIN
        except ImportError:
            # Fallback
            return get_runtime_base_url()
