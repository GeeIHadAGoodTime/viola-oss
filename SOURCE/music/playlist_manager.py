"""
Playlist Manager for Viola.

Saves and manages named playlists across supported music providers.
Includes track caching to avoid provider calls on every playback.
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

from core.logging_config import get_logger
from core.platform import get_data_dir
from music.exceptions import ResolutionError
from music.playlists.playlist_store import PlaylistStore
from music.providers.models import ProviderName

logger = get_logger(__name__)

# Cache TTL: 7 days in seconds
TRACK_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


def _copy_legacy_file_if_missing(legacy_path: Path, new_path: Path) -> None:
    if new_path.exists() or not legacy_path.exists():
        return
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, new_path)
        logger.info("Migrated legacy playlist storage from %s to %s", legacy_path, new_path)
    except OSError as exc:
        logger.warning(
            "Could not migrate legacy playlist storage from %s to %s: %s",
            legacy_path,
            new_path,
            exc,
        )


class _TrackSummary(Protocol):
    id: str
    provider_track_id: str | None
    title: str | None
    extras: dict[str, str]


class PlaylistManager:
    """Manages saved playlists across music providers.

    Multi-user: playlists are stored per-user via a nested dict
    ``{user_id: {name: playlist_data}}``.  The ``"default"`` user maps
    to the legacy flat file for backward-compatible desktop mode.
    """

    def __init__(self, storage_path: Path | None = None) -> None:
        default_storage = storage_path is None
        if storage_path is None:
            storage_path = get_data_dir() / "playlists.json"

        self.storage_path = storage_path
        self.settings_path = self.storage_path.parent / "playlist_settings.json"
        if default_storage:
            legacy_dir = Path.home().joinpath(".viola")
            _copy_legacy_file_if_missing(legacy_dir / "playlists.json", self.storage_path)
            _copy_legacy_file_if_missing(legacy_dir / "playlist_settings.json", self.settings_path)
            _copy_legacy_file_if_missing(
                legacy_dir / "playlists.sqlite3",
                self.storage_path.with_suffix(".sqlite3"),
            )
        self._lock = threading.RLock()
        self._store = PlaylistStore(db_path=self.storage_path.with_suffix(".sqlite3"))
        # Per-user playlist stores: user_id -> {name -> playlist_data}
        self._user_playlists: dict[str, dict[str, dict[str, object]]] = {}
        self._user_default_playlists: dict[str, str | None] = {}
        self._loaded_default_playlists: set[str] = set()
        self._active_playlists_user_id = "default"  # mt-ok: initial sentinel, overwritten on first real request

    @property
    def playlists(self) -> dict[str, dict[str, object]]:
        """Deprecated compatibility shim — use _get_user_playlists(user_id).

        Only kept for tests.  Production code MUST use the per-user path.
        """
        uid = self._resolve_user_id()
        with self._lock:
            return self._user_playlists.get(uid, {})

    @playlists.setter
    def playlists(self, value: dict[str, dict[str, object]]) -> None:
        uid = self._resolve_user_id()
        with self._lock:
            self._user_playlists[uid] = value

    @staticmethod
    def _resolve_user_id(user_id: str | None = None) -> str:
        """Resolve user_id from param or ambient context."""
        if user_id:
            return user_id
        try:
            from core.user_context import get_current_user_id

            return get_current_user_id()
        except Exception:
            from core.user_context import get_device_user_id

            return get_device_user_id()

    def _get_user_playlists(self, user_id: str) -> dict[str, dict[str, object]]:
        """Return playlist dict for a user, loading from the database on first access."""
        with self._lock:
            if user_id not in self._user_playlists:
                self._migrate_legacy_user_if_needed(user_id)
                self._user_playlists[user_id] = self._load_user_playlists_from_store(user_id)
            self._active_playlists_user_id = user_id
            return self._user_playlists[user_id]

    def _save_user_playlists(self, user_id: str) -> bool:
        """Save per-user playlists to the backing store."""
        return self.save(user_id=user_id)

    def _legacy_playlist_path_for_user(self, user_id: str) -> Path:
        # mt-ok: legacy-migration only
        if user_id == "default":  # mt-ok
            return self.storage_path
        return self.storage_path.parent / f"playlists_{user_id}.json"

    def _settings_path_for_user(self, user_id: str) -> Path:
        """Return the legacy JSON settings path for a given user."""
        # mt-ok: legacy-migration only
        if user_id == "default":  # mt-ok
            return self.settings_path
        return self.settings_path.with_name(f"{self.settings_path.stem}_{user_id}{self.settings_path.suffix}")

    def _get_user_default_playlist(self, user_id: str) -> str | None:
        """Return the cached default playlist for a user, loading on first access."""
        with self._lock:
            if user_id not in self._loaded_default_playlists:
                self._migrate_legacy_user_if_needed(user_id)
                self._user_default_playlists[user_id] = self.load_default_playlist(user_id=user_id)
                self._loaded_default_playlists.add(user_id)
            return self._user_default_playlists.get(user_id)

    def _normalize_playlist_mapping(self, data: object) -> dict[str, dict[str, object]]:
        """Normalize playlist mappings loaded from disk for backward compatibility."""
        if not isinstance(data, dict):
            return {}

        migrated: dict[str, dict[str, object]] = {}
        for name, value in data.items():
            if not isinstance(name, str) or not name:
                continue
            if isinstance(value, str):
                migrated[name] = self._normalize_playlist_record({"url": value, "shuffle": True})
            elif isinstance(value, dict):
                migrated[name] = self._normalize_playlist_record(value)
        return migrated

    def _load_legacy_user_playlists(self, user_id: str) -> dict[str, dict[str, object]]:
        legacy_path = self._legacy_playlist_path_for_user(user_id)
        if not legacy_path.exists():
            return {}

        try:
            with legacy_path.open(encoding="utf-8") as handle:
                return self._normalize_playlist_mapping(json.load(handle))
        except Exception as exc:
            logger.error("Failed to load legacy playlists for user %s: %s", user_id, exc)
            return {}

    def _load_legacy_default_playlist(self, user_id: str) -> str | None:
        settings_path = self._settings_path_for_user(user_id)
        if not settings_path.exists():
            return None

        try:
            with settings_path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            logger.error("Failed to load legacy playlist settings for user %s: %s", user_id, exc)
            return None

        default_value = data.get("default_playlist")
        if isinstance(default_value, str) and default_value:
            return default_value
        return None

    def _load_user_playlists_from_store(self, user_id: str) -> dict[str, dict[str, object]]:
        stored_playlists = self._store.load_playlist_metadata_map(user_id)
        return {
            name: self._normalize_playlist_record(playlist_data) for name, playlist_data in stored_playlists.items()
        }

    def _migrate_legacy_user_if_needed(self, user_id: str) -> None:
        if self._store.has_json_migration_marker(user_id):
            return

        stored_playlists = self._store.load_playlist_metadata_map(user_id)
        stored_default = self._store.get_default_playlist_name(user_id)
        if stored_playlists or stored_default is not None:
            self._store.mark_json_migrated(user_id)
            return

        legacy_playlists = self._load_legacy_user_playlists(user_id)
        legacy_default = self._load_legacy_default_playlist(user_id)

        if legacy_playlists:
            self._store.save_playlist_metadata_map(user_id, legacy_playlists)
            logger.info(
                "Migrated %s legacy playlists for user %s into PlaylistStore",
                len(legacy_playlists),
                user_id,
            )

        if legacy_default is not None:
            self._store.set_default_playlist_name(user_id, legacy_default)

        self._store.mark_json_migrated(user_id)

    def load(self) -> dict[str, dict[str, object]]:
        """Load default-user playlists from the backing store."""
        try:
            self._migrate_legacy_user_if_needed("default")
            return self._load_user_playlists_from_store("default")
        except Exception as exc:
            logger.error("Failed to load playlists: %s", exc)
            return {}

    def save(self, user_id: str | None = None) -> bool:
        """Save playlists to the backing store."""
        uid = self._resolve_user_id(user_id)
        try:
            with self._lock:
                user_playlists = self._get_user_playlists(uid)
                return self._store.save_playlist_metadata_map(uid, user_playlists)
        except Exception as exc:
            logger.error("Failed to save playlists for user %s: %s", uid, exc)
            return False

    def _normalize_provider_name(self, provider: object) -> ProviderName:
        """Normalize stored provider values, defaulting old data to YouTube Music."""
        if isinstance(provider, ProviderName):
            return provider

        if isinstance(provider, str):
            try:
                return ProviderName(provider)
            except ValueError:
                logger.warning(
                    "Unknown playlist provider '%s'; defaulting to %s",
                    provider,
                    ProviderName.YOUTUBE_MUSIC.value,
                )

        return ProviderName.YOUTUBE_MUSIC

    def _normalize_playlist_record(self, playlist_data: dict[str, object]) -> dict[str, object]:
        """Normalize playlist records loaded from disk for backward compatibility."""
        normalized = dict(playlist_data)
        normalized["provider"] = self._normalize_provider_name(normalized.get("provider"))
        normalized["shuffle"] = bool(normalized.get("shuffle", True))
        return normalized

    def _get_playlist_provider(self, playlist_data: dict[str, object]) -> ProviderName:
        """Return the normalized provider for a playlist record."""
        provider_name = self._normalize_provider_name(playlist_data.get("provider"))
        playlist_data["provider"] = provider_name
        return provider_name

    def _validate_playlist_url(self, provider: ProviderName, url: str) -> bool:
        """Validate playlist identifiers based on provider rather than YouTube only."""
        if not url or not url.strip():
            logger.error("Playlist URL cannot be empty")
            return False

        url_lower = url.lower()
        if provider in {ProviderName.YOUTUBE_MUSIC, ProviderName.YOUTUBE_IFRAME}:
            if "youtube.com" not in url_lower and "youtu.be" not in url_lower:
                logger.error("Invalid YouTube playlist URL")
                return False
        elif provider == ProviderName.SPOTIFY:
            if "open.spotify.com" not in url_lower and not url_lower.startswith("spotify:"):
                logger.error("Invalid Spotify playlist URL")
                return False

        return True

    def add_playlist(
        self,
        name: str,
        url: str,
        shuffle: bool = True,
        provider: ProviderName | str = ProviderName.YOUTUBE_MUSIC,
        playlist_id: str | None = None,
        display_name: str | None = None,
        starred: bool = False,
        user_id: str | None = None,
    ) -> bool:
        """
        Add or update a playlist.

        Args:
            name: Friendly name for playlist (e.g., "workout", "chill")
            url: Provider playlist URL or local identifier
            shuffle: Whether to shuffle playlist when playing (default True)
            provider: Provider that owns the playlist
            playlist_id: Provider playlist ID (for synced playlists)
            display_name: Custom display name (overrides provider name)
            starred: Whether playlist is starred/favorited

        Returns:
            True if successful
        """
        name = name.lower().strip()
        provider_name = self._normalize_provider_name(provider)

        if not name:
            logger.error("Playlist name cannot be empty")
            return False

        if not self._validate_playlist_url(provider_name, url):
            return False

        playlist_data = {
            "url": url,
            "shuffle": shuffle,
            "provider": provider_name,
        }
        if playlist_id:
            playlist_data["playlist_id"] = playlist_id
        if display_name:
            playlist_data["display_name"] = display_name
        if starred:
            playlist_data["starred"] = True

        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        user_pls[name] = playlist_data
        logger.info(
            "Added playlist '%s': %s (provider=%s, shuffle=%s, starred=%s)",
            name,
            url,
            provider_name.value,
            shuffle,
            starred,
        )
        return self.save(user_id=uid)

    def create_playlist(
        self,
        name: str,
        *,
        provider: ProviderName | str = ProviderName.YOUTUBE_MUSIC,
        url: str | None = None,
        shuffle: bool = True,
        playlist_id: str | None = None,
        display_name: str | None = None,
        starred: bool = False,
        user_id: str | None = None,
    ) -> dict[str, object] | None:
        """Create a playlist through the provider when supported, then persist it."""
        provider_name = self._normalize_provider_name(provider)
        playlist_url = url
        playlist_display_name = display_name
        playlist_identifier = playlist_id
        uid = self._resolve_user_id(user_id)

        if playlist_url is None:
            from music.providers.registry import get_provider_class

            try:
                provider_instance = get_provider_class(provider_name)()
            except Exception as exc:
                logger.exception(
                    "Failed to load provider '%s' for playlist creation: %s",
                    provider_name.value,
                    exc,
                )
                return None

            create_method = getattr(provider_instance, "create_playlist", None)
            if not callable(create_method):
                logger.error(
                    "Provider '%s' does not support playlist creation",
                    provider_name.value,
                )
                return None

            try:
                created_playlist = create_method(name=name, user_id=uid)
            except TypeError:
                try:
                    created_playlist = create_method(name=name)
                except TypeError:
                    try:
                        created_playlist = create_method(name)
                    except Exception as exc:
                        logger.exception(
                            "Provider '%s' failed to create playlist '%s': %s",
                            provider_name.value,
                            name,
                            exc,
                        )
                        return None
                except Exception as exc:
                    logger.exception(
                        "Provider '%s' failed to create playlist '%s': %s",
                        provider_name.value,
                        name,
                        exc,
                    )
                    return None
            except Exception as exc:
                logger.exception(
                    "Provider '%s' failed to create playlist '%s': %s",
                    provider_name.value,
                    name,
                    exc,
                )
                return None

            if not isinstance(created_playlist, dict):
                logger.error(
                    "Provider '%s' returned invalid playlist payload for '%s'",
                    provider_name.value,
                    name,
                )
                return None

            created_url = created_playlist.get("url")
            if not isinstance(created_url, str) or not created_url:
                logger.error(
                    "Provider '%s' returned no playlist URL for '%s'",
                    provider_name.value,
                    name,
                )
                return None

            playlist_url = created_url
            if playlist_identifier is None:
                created_id = created_playlist.get("playlist_id")
                if isinstance(created_id, str) and created_id:
                    playlist_identifier = created_id
            if playlist_display_name is None:
                created_display_name = created_playlist.get("display_name") or created_playlist.get("name")
                if isinstance(created_display_name, str) and created_display_name:
                    playlist_display_name = created_display_name

        if playlist_url is None:
            logger.error("Playlist URL is required to create playlist '%s'", name)
            return None

        if not self.add_playlist(
            name=name,
            url=playlist_url,
            shuffle=shuffle,
            provider=provider_name,
            playlist_id=playlist_identifier,
            display_name=playlist_display_name,
            starred=starred,
            user_id=uid,
        ):
            return None

        created_record = self.get_playlist(name, user_id=uid)
        return dict(created_record) if created_record is not None else None

    def remove_playlist(self, name: str, user_id: str | None = None) -> bool:
        """Remove a playlist by name."""
        name = name.lower().strip()
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)

        if name in user_pls:
            del user_pls[name]
            if self._get_user_default_playlist(uid) == name:
                self._user_default_playlists[uid] = None
                self._loaded_default_playlists.add(uid)
                self.save_default_playlist(user_id=uid)
            logger.info("Removed playlist '%s'", name)
            return self.save(user_id=uid)

        logger.warning("Playlist '%s' not found", name)
        return False

    def get_playlist(self, name: str, user_id: str | None = None) -> dict[str, object] | None:
        """Get playlist info by name. Returns dict with 'url' and 'shuffle' keys."""
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        playlist = user_pls.get(name.lower().strip())
        if playlist is None:
            return None
        return self._normalize_playlist_record(playlist)

    def get_playlist_url(self, name: str) -> str | None:
        """Get just the URL for a playlist by name."""
        playlist = self.get_playlist(name)
        if not isinstance(playlist, dict):
            return None

        url_value = playlist.get("url")
        return url_value if isinstance(url_value, str) else None

    def list_playlists(self, user_id: str | None = None) -> dict[str, dict[str, object]]:
        """Get all playlists."""
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        return {name: self._normalize_playlist_record(playlist_data) for name, playlist_data in user_pls.items()}

    def add_track(self, playlist_name: str, track: dict, user_id: str | None = None) -> bool:
        """Add a single track to a local playlist's track list.

        Args:
            playlist_name: Name of the playlist (case-insensitive).
            track: Dict with keys: title (str), url (str), provider (str, optional).

        Returns:
            True if track was added, False if playlist not found.
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = playlist_name.lower().strip()
        record = user_pls.get(name)
        if record is None:
            logger.warning("add_track: playlist '%s' not found", name)
            return False
        local_tracks = list(record.get("local_tracks") or [])
        local_tracks.append(track)
        user_pls[name]["local_tracks"] = local_tracks
        return self.save(user_id=uid)

    def get_local_tracks(self, playlist_name: str, user_id: str | None = None) -> list[dict]:
        """Return local tracks list for a playlist (empty list if not found)."""
        name = playlist_name.lower().strip()
        record = self.get_playlist(name, user_id=user_id)
        if record is None:
            return []
        return list(record.get("local_tracks") or [])

    def rename_playlist(self, old_name: str, new_name: str, user_id: str | None = None) -> bool:
        """
        Rename a playlist (local name only, doesn't change YouTube name).

        Args:
            old_name: Current playlist name
            new_name: New playlist name
            user_id: User identity (resolved from context if None)

        Returns:
            True if successful
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        old_name = old_name.lower().strip()
        new_name = new_name.lower().strip()

        if not old_name or not new_name:
            logger.error("Playlist names cannot be empty")
            return False

        if old_name not in user_pls:
            logger.warning("Playlist '%s' not found for user %s", old_name, uid)
            return False

        if new_name in user_pls:
            logger.error("Playlist '%s' already exists for user %s", new_name, uid)
            return False

        user_pls[new_name] = user_pls.pop(old_name)

        # Update default playlist if it was the renamed one
        if self._get_user_default_playlist(uid) == old_name:
            self._user_default_playlists[uid] = new_name
            self._loaded_default_playlists.add(uid)
            self.save_default_playlist(user_id=uid)

        logger.info("Renamed playlist '%s' to '%s'", old_name, new_name)
        return self.save(user_id=uid)

    def star_playlist(self, name: str, starred: bool = True, user_id: str | None = None) -> bool:
        """
        Star or unstar a playlist.

        Args:
            name: Playlist name
            starred: True to star, False to unstar

        Returns:
            True if successful
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = name.lower().strip()

        if name not in user_pls:
            logger.warning("Playlist '%s' not found", name)
            return False

        if starred:
            user_pls[name]["starred"] = True
        else:
            user_pls[name].pop("starred", None)

        logger.info("%s playlist '%s'", "Starred" if starred else "Unstarred", name)
        return self.save(user_id=uid)

    def is_starred(self, name: str) -> bool:
        """Check if a playlist is starred."""
        playlist = self.get_playlist(name)
        return bool(playlist and playlist.get("starred", False))

    def update_playlist_url(self, name: str, url: str, user_id: str | None = None) -> bool:
        """
        Update a playlist's URL.

        Args:
            name: Playlist name
            url: New YouTube playlist URL

        Returns:
            True if successful
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = name.lower().strip()

        if name not in user_pls:
            logger.warning("Playlist '%s' not found", name)
            return False

        provider_name = self._get_playlist_provider(user_pls[name])
        if not self._validate_playlist_url(provider_name, url):
            return False

        # Update URL
        user_pls[name]["url"] = url

        # Clear cache since URL changed - tracks will be different
        user_pls[name].pop("cached_tracks", None)
        user_pls[name].pop("cached_at", None)
        user_pls[name].pop("cached_count", None)

        logger.info("Updated URL for playlist '%s': %s", name, url[:80])
        return self.save(user_id=uid)

    def set_default_playlist(self, name: str, user_id: str | None = None) -> bool:
        """
        Set a playlist as the default.

        Args:
            name: Playlist name to set as default (empty string to clear)

        Returns:
            True if successful
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = name.lower().strip()

        # Validate playlist exists (unless clearing default)
        if name and name not in user_pls:
            logger.error("Cannot set default: playlist '%s' not found", name)
            return False

        self._user_default_playlists[uid] = name if name else None
        self._loaded_default_playlists.add(uid)
        return self.save_default_playlist(user_id=uid)

    def get_default_playlist(self, user_id: str | None = None) -> str | None:
        """Get the name of the default playlist."""
        uid = self._resolve_user_id(user_id)
        return self._get_user_default_playlist(uid)

    def sync_from_provider(self, user_id: str) -> int:
        """
        Sync playlists from YouTube account using YouTube Data API v3.
        Adds new playlists and updates existing ones, but preserves local customizations
        (display_name, starred, default status).

        Args:
            user_id: User ID for provider access

        Returns:
            Number of playlists synced
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        try:
            from music.providers.registry import get_active_provider
            from music.providers.youtube_music import YouTubeMusicProvider

            provider = get_active_provider()
            if not isinstance(provider, YouTubeMusicProvider):
                logger.warning("YouTube Music provider not active - cannot sync playlists")
                return 0

            # Fetch playlists from YouTube
            result = provider.list_playlists(user_id=uid, limit=50)
            synced_count = 0

            for playlist_summary in result.items:
                playlist_id = playlist_summary.provider_playlist_id or playlist_summary.id
                youtube_name = playlist_summary.name

                # Generate URL from playlist ID
                url = f"https://www.youtube.com/playlist?list={playlist_id}"

                # Check if playlist already exists (by playlist_id or name)
                existing_name = None
                for name, data in user_pls.items():
                    if data.get("playlist_id") == playlist_id or name.lower() == youtube_name.lower():
                        existing_name = name
                        break

                if existing_name:
                    # Update existing playlist but preserve local customizations
                    existing_data = user_pls[existing_name]
                    # Update URL and playlist_id, but keep display_name, starred, default
                    existing_data["url"] = url
                    existing_data["playlist_id"] = playlist_id
                    existing_data["provider"] = ProviderName.YOUTUBE_MUSIC
                    # Only update name if no custom display_name
                    if not existing_data.get("display_name"):
                        # Keep existing name or update if YouTube name changed significantly
                        pass
                    synced_count += 1
                else:
                    # Add new playlist
                    # Use YouTube name as key (lowercased)
                    name_key = youtube_name.lower().strip()
                    # Ensure unique name
                    base_name = name_key
                    counter = 1
                    while name_key in user_pls:
                        name_key = f"{base_name}_{counter}"
                        counter += 1

                    self.add_playlist(
                        name=name_key,
                        url=url,
                        shuffle=True,
                        provider=ProviderName.YOUTUBE_MUSIC,
                        playlist_id=playlist_id,
                        display_name=youtube_name,  # Store original name as display_name
                        user_id=uid,
                    )
                    synced_count += 1

            if synced_count > 0:
                self.save(user_id=uid)
                logger.info("Synced %s playlists from YouTube account", synced_count)

            return synced_count

        except Exception as exc:
            logger.exception("Failed to sync playlists from provider: %s", exc)
            from music.providers.errors import MusicProviderUnavailableError

            raise MusicProviderUnavailableError(
                f"Failed to sync playlists: {exc}",
                technical_details={"root_cause": str(exc)},
            ) from exc

    def load_default_playlist(self, user_id: str | None = None) -> str | None:
        """Load default playlist setting from the backing store."""
        uid = self._resolve_user_id(user_id)
        try:
            default_value = self._store.get_default_playlist_name(uid)
            if default_value:
                logger.info("Loaded default playlist: '%s'", default_value)
            return default_value
        except Exception as exc:
            logger.error("Failed to load playlist settings for user %s: %s", uid, exc)
        return None

    def save_default_playlist(self, user_id: str | None = None) -> bool:
        """Save default playlist setting to the backing store."""
        uid = self._resolve_user_id(user_id)
        self._loaded_default_playlists.add(uid)
        try:
            self._store.set_default_playlist_name(uid, self._user_default_playlists.get(uid))
            logger.info(
                "Saved default playlist for user %s: '%s'",
                uid,
                self._user_default_playlists.get(uid),
            )
            return True
        except Exception as exc:
            logger.error("Failed to save playlist settings for user %s: %s", uid, exc)
            return False

    # ------------------------------------------------------------------ #
    # Track Caching - Avoids API calls on every playback
    # ------------------------------------------------------------------ #

    def _get_cached_tracks(self, name: str, user_id: str | None = None) -> list[dict[str, object]] | None:
        """
        Get cached tracks for a playlist if cache is fresh.

        Args:
            name: Playlist name

        Returns:
            List of track dicts if cache is valid, None if stale/missing
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = name.lower().strip()
        playlist = user_pls.get(name)
        if not playlist:
            return None

        cached_tracks_raw = playlist.get("cached_tracks")
        if not isinstance(cached_tracks_raw, list):
            return None

        cached_tracks = [dict(track) for track in cached_tracks_raw if isinstance(track, dict)]
        if not cached_tracks:
            return None

        # Check cache freshness
        cached_at = playlist.get("cached_at")
        if not isinstance(cached_at, str) or not cached_at:
            return None

        try:
            cached_time = datetime.fromisoformat(cached_at)
            now = datetime.now(UTC)
            age_seconds = (now - cached_time).total_seconds()

            if age_seconds > TRACK_CACHE_TTL_SECONDS:
                logger.info(
                    "Track cache for '%s' expired (age: %.1f days)",
                    name,
                    age_seconds / 86400,
                )
                return None

            logger.info(
                "Using cached tracks for '%s' (%s tracks, age: %.1f hours)",
                name,
                len(cached_tracks),
                age_seconds / 3600,
            )
            return cached_tracks

        except (ValueError, TypeError) as e:
            logger.warning("Invalid cache timestamp for '%s': %s", name, e)
            return None

    def _cache_tracks(self, name: str, tracks: list[dict[str, object]], user_id: str | None = None) -> bool:
        """
        Cache tracks for a playlist.

        Args:
            name: Playlist name
            tracks: List of track dicts to cache

        Returns:
            True if cached successfully
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = name.lower().strip()
        if name not in user_pls:
            logger.warning("Cannot cache tracks: playlist '%s' not found", name)
            return False

        # Store provider-neutral track data to support cached playback across providers.
        minimal_tracks = []
        for track in tracks:
            track_id = track.get("video_id") or track.get("provider_track_id") or track.get("id")
            track_url = track.get("url")
            if not track_id and not track_url:
                continue

            cache_entry: dict[str, object] = {
                "title": track.get("title", "Unknown"),
            }
            if track_id is not None:
                cache_entry["id"] = str(track_id)
                cache_entry["video_id"] = str(track_id)
                cache_entry["provider_track_id"] = str(track_id)
            if isinstance(track_url, str) and track_url:
                cache_entry["url"] = track_url
            uploader = track.get("uploader")
            if uploader is not None:
                cache_entry["uploader"] = uploader
            minimal_tracks.append(cache_entry)

        user_pls[name]["cached_tracks"] = minimal_tracks
        user_pls[name]["cached_at"] = datetime.now(UTC).isoformat()
        user_pls[name]["cached_count"] = len(minimal_tracks)

        if self.save(user_id=uid):
            logger.info("Cached %s tracks for playlist '%s'", len(minimal_tracks), name)
            return True
        return False

    def clear_track_cache(self, name: str | None = None, user_id: str | None = None) -> bool:
        """
        Clear cached tracks for a playlist or all playlists.

        Args:
            name: Playlist name, or None to clear all caches

        Returns:
            True if cleared successfully
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        if name is not None:
            name = name.lower().strip()
            if name in user_pls:
                user_pls[name].pop("cached_tracks", None)
                user_pls[name].pop("cached_at", None)
                user_pls[name].pop("cached_count", None)
                logger.info("Cleared track cache for '%s'", name)
        else:
            for playlist_name in user_pls:
                user_pls[playlist_name].pop("cached_tracks", None)
                user_pls[playlist_name].pop("cached_at", None)
                user_pls[playlist_name].pop("cached_count", None)
            logger.info("Cleared all playlist track caches")

        return self.save(user_id=uid)

    def get_cache_status(self, name: str, user_id: str | None = None) -> dict[str, object]:
        """
        Get cache status for a playlist.

        Args:
            name: Playlist name

        Returns:
            Dict with cache info (has_cache, track_count, age_hours, is_fresh)
        """
        uid = self._resolve_user_id(user_id)
        user_pls = self._get_user_playlists(uid)
        name = name.lower().strip()
        playlist = user_pls.get(name)
        if not playlist:
            return {"has_cache": False}

        cached_at = playlist.get("cached_at")
        cached_count_obj = playlist.get("cached_count", 0)
        cached_count = cached_count_obj if isinstance(cached_count_obj, int) else 0

        if not isinstance(cached_at, str) or not cached_at:
            return {"has_cache": False, "track_count": 0}

        try:
            cached_time = datetime.fromisoformat(cached_at)
            now = datetime.now(UTC)
            age_seconds = (now - cached_time).total_seconds()

            return {
                "has_cache": True,
                "track_count": cached_count,
                "cached_at": cached_at,
                "age_hours": age_seconds / 3600,
                "is_fresh": age_seconds <= TRACK_CACHE_TTL_SECONDS,
            }
        except (ValueError, TypeError):
            return {"has_cache": False, "track_count": 0}

    def _extract_youtube_playlist_id(self, playlist_data: dict[str, object]) -> str | None:
        """Extract a YouTube playlist ID from stored metadata."""
        playlist_id = playlist_data.get("playlist_id")
        if isinstance(playlist_id, str) and playlist_id:
            return playlist_id

        url = playlist_data.get("url")
        if not isinstance(url, str) or "list=" not in url:
            return None

        import re

        match = re.search(r"[?&]list=([^&]+)", url)
        if match:
            return match.group(1)
        return None

    def _default_track_url(
        self,
        provider_name: ProviderName,
        track_id: str | None,
        extras: object = None,
    ) -> str | None:
        """Build a playable URL or identifier for a provider track."""
        extras_dict = extras if isinstance(extras, dict) else {}

        direct_url = extras_dict.get("url")
        if isinstance(direct_url, str) and direct_url:
            return direct_url

        if provider_name == ProviderName.LOCAL:
            file_path = extras_dict.get("file_path")
            if isinstance(file_path, str) and file_path:
                return file_path

        if track_id is None:
            return None

        if provider_name == ProviderName.SPOTIFY:
            return f"https://open.spotify.com/track/{track_id}"

        if provider_name in {ProviderName.YOUTUBE_MUSIC, ProviderName.YOUTUBE_IFRAME}:
            return f"https://www.youtube.com/watch?v={track_id}"

        return track_id

    def _normalize_playlist_entry(
        self,
        provider_name: ProviderName,
        entry: dict[str, object],
    ) -> dict[str, object] | None:
        """Normalize provider entries to the playlist playback shape used by callers."""
        entry_id = entry.get("id") or entry.get("video_id") or entry.get("provider_track_id")
        track_id = str(entry_id) if entry_id is not None else None
        title_obj = entry.get("title", "Unknown")
        title = title_obj if isinstance(title_obj, str) else str(title_obj)

        url = entry.get("url")
        if not isinstance(url, str) or not url:
            url = self._default_track_url(provider_name, track_id)

        if track_id is None and url is None:
            logger.debug("Skipping track with no identifier or URL: '%s'", title)
            return None

        normalized: dict[str, object] = {
            "title": title,
            "url": url,
            "video_id": track_id,
            "provider_track_id": entry.get("provider_track_id") or track_id,
            "provider": provider_name,
        }
        uploader = entry.get("uploader")
        if uploader is not None:
            normalized["uploader"] = uploader
        return normalized

    def _track_summary_to_entry(
        self,
        provider_name: ProviderName,
        track: _TrackSummary,
    ) -> dict[str, object]:
        """Convert provider TrackSummary objects to the shared playlist entry format."""
        track_id = track.provider_track_id or track.id
        return {
            "id": track_id,
            "provider_track_id": track_id,
            "title": track.title or "Unknown",
            "url": self._default_track_url(provider_name, track_id, getattr(track, "extras", {})),
            "video_id": track_id,
        }

    def _call_provider_get_playlist_items(
        self,
        provider_name: ProviderName,
        get_items: object,
        *,
        playlist_id: str | None,
        playlist_url: str | None,
        limit: int,
        page_token: str | None = None,
        user_id: str | None = None,
    ) -> tuple[list[_TrackSummary], str | None]:
        """Call provider get_playlist_items with compatible keyword sets."""
        if not callable(get_items):
            logger.error(
                "Provider '%s' does not implement get_playlist_items",
                provider_name.value,
            )
            return [], None

        uid = self._resolve_user_id(user_id)
        attempted_kwargs: list[dict[str, object]] = []
        if playlist_id:
            attempted_kwargs.append(
                {
                    "playlist_id": playlist_id,
                    "user_id": uid,
                    "limit": limit,
                    "page_token": page_token,
                }
            )
            attempted_kwargs.append(
                {
                    "playlist_id": playlist_id,
                    "limit": limit,
                    "page_token": page_token,
                }
            )
        if playlist_url:
            attempted_kwargs.append(
                {
                    "playlist_url": playlist_url,
                    "user_id": uid,
                    "limit": limit,
                    "page_token": page_token,
                }
            )
            attempted_kwargs.append(
                {
                    "url": playlist_url,
                    "user_id": uid,
                    "limit": limit,
                    "page_token": page_token,
                }
            )
            attempted_kwargs.append(
                {
                    "playlist_url": playlist_url,
                    "limit": limit,
                    "page_token": page_token,
                }
            )
            attempted_kwargs.append(
                {
                    "url": playlist_url,
                    "limit": limit,
                    "page_token": page_token,
                }
            )

        last_type_error: TypeError | None = None
        for kwargs in attempted_kwargs:
            try:
                result = get_items(**kwargs)
                break
            except TypeError as exc:
                last_type_error = exc
        else:
            if last_type_error is not None:
                logger.error(
                    "Provider '%s' get_playlist_items signature is incompatible: %s",
                    provider_name.value,
                    last_type_error,
                )
            return [], None

        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], list):
            next_page = result[1] if isinstance(result[1], str) or result[1] is None else None
            return cast(list[_TrackSummary], result[0]), next_page

        if isinstance(result, list):
            return cast(list[_TrackSummary], result), None

        result_items = getattr(result, "items", None)
        if isinstance(result_items, list):
            next_cursor = getattr(result, "next_cursor", None)
            next_page = next_cursor if isinstance(next_cursor, str) or next_cursor is None else None
            return cast(list[_TrackSummary], result_items), next_page

        logger.error(
            "Provider '%s' returned unsupported playlist items payload: %s",
            provider_name.value,
            type(result).__name__,
        )
        return [], None

    def _fetch_provider_playlist_entries(
        self,
        provider_name: ProviderName,
        playlist_info: dict[str, object],
        *,
        playlist_name: str,
        fetch_limit: int,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Fetch playlist entries from the provider recorded on the playlist."""
        from music.providers.registry import get_provider_class

        try:
            provider = get_provider_class(provider_name)()
        except Exception as exc:
            logger.exception(
                "Failed to initialize provider '%s' for playlist '%s': %s",
                provider_name.value,
                playlist_name,
                exc,
            )
            return []

        playlist_id_obj = playlist_info.get("playlist_id")
        playlist_id = playlist_id_obj if isinstance(playlist_id_obj, str) and playlist_id_obj else None
        playlist_url_obj = playlist_info.get("url")
        playlist_url = playlist_url_obj if isinstance(playlist_url_obj, str) and playlist_url_obj else None

        entries: list[dict[str, object]] = []
        next_page_token: str | None = None
        remaining = fetch_limit

        while remaining > 0:
            track_summaries, next_page_token = self._call_provider_get_playlist_items(
                provider_name,
                getattr(provider, "get_playlist_items", None),
                playlist_id=playlist_id,
                playlist_url=playlist_url,
                limit=min(remaining, 50),
                page_token=next_page_token,
                user_id=user_id,
            )
            if not track_summaries:
                break

            for track in track_summaries:
                entries.append(self._track_summary_to_entry(provider_name, track))

            if not next_page_token:
                break

            remaining = fetch_limit - len(entries)

        logger.info(
            "Fetched %s tracks from playlist '%s' provider=%s",
            len(entries),
            playlist_name,
            provider_name.value,
        )
        return entries[:fetch_limit]

    def _resolve_youtube_playlist_entries(
        self,
        playlist_info: dict[str, object],
        *,
        playlist_name: str,
        fetch_limit: int,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Resolve YouTube playlists via browser search, or API only when browser search is disabled."""
        playlist_id = self._extract_youtube_playlist_id(playlist_info)
        if not playlist_id:
            url = playlist_info.get("url")
            if isinstance(url, str):
                logger.error("Could not extract playlist ID from URL: %s", url[:80])
            else:
                logger.error("Could not extract playlist ID for '%s'", playlist_name)
            return []

        browser_search_enabled = self._browser_search_enabled()
        entries = self._resolve_playlist_browser(playlist_id, fetch_limit) if browser_search_enabled else None
        if entries is not None:
            logger.info(
                "Resolved %s videos from playlist '%s' method=browser",
                len(entries),
                playlist_name,
            )
            return entries[:fetch_limit]

        if browser_search_enabled:
            logger.warning(
                "Playlist '%s' browser resolution unavailable; falling back to API",
                playlist_name,
            )

        logger.info("Playlist '%s' browser search disabled, method=api", playlist_name)
        return self._fetch_provider_playlist_entries(
            ProviderName.YOUTUBE_MUSIC,
            {
                "playlist_id": playlist_id,
                "url": playlist_info.get("url"),
            },
            playlist_name=playlist_name,
            fetch_limit=fetch_limit,
            user_id=user_id,
        )

    def _browser_search_enabled(self) -> bool:
        """Return whether browser-based YouTube search is enabled."""
        try:
            from config.settings import get_settings

            cfg = get_settings()
            return bool(getattr(cfg, "browser_search_enabled", True))
        except Exception:
            return True

    def _resolve_playlist_browser(self, playlist_id: str, limit: int) -> list[dict[str, object]] | None:
        """Try browser-based playlist resolution. Returns None when unavailable or unsuccessful."""
        try:
            if not self._browser_search_enabled():
                return None
        except Exception:
            pass

        try:
            from music.providers.browser_search import BrowserSearchEngine

            engine = BrowserSearchEngine.get_instance()
            raw = engine.resolve_playlist(playlist_id, limit=limit)
            if not raw:
                logger.warning(
                    "Browser playlist resolution returned empty for playlist_id=%s",
                    playlist_id,
                )
                return None

            # Map to entries format expected by downstream code
            entries: list[dict[str, object]] = []
            for item in raw:
                video_id = item.get("video_id", "")
                if not video_id:
                    continue
                entries.append(
                    {
                        "id": video_id,
                        "title": item.get("title", "Unknown"),
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "video_id": video_id,
                        "uploader": item.get("channel"),
                    }
                )

            return entries if entries else None
        except Exception:
            logger.exception(
                "Browser playlist resolution failed for playlist_id=%s",
                playlist_id,
            )
            return None

    async def get_playlist_videos(
        self,
        name: str,
        limit: int = 10,
        shuffle: bool | None = None,
        progressive: bool = True,
        force_refresh: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, object]]:
        """
        Fetch videos from a saved playlist, using cache when available.

        Args:
            name: Playlist name
            limit: Max videos to fetch for initial batch (default 10)
            shuffle: Override shuffle setting (uses playlist default if None)
            progressive: If True, fetch first batch quickly (default True)
            force_refresh: If True, bypass cache and fetch fresh from API

        Returns:
            List of video dicts with {title, url, video_id}

        Note:
            - Uses cached tracks if available and fresh (< 7 days old)
            - YouTube playlist fetching requires a linked YouTube Music provider
            - This method blocks YouTube playlists if provider is not linked
        """
        playlist_info = self.get_playlist(name, user_id=user_id)

        if not playlist_info:
            logger.error("Playlist '%s' not found", name)
            return []

        provider_name = self._get_playlist_provider(playlist_info)
        stored_shuffle = bool(playlist_info.get("shuffle", True))
        should_shuffle = shuffle if shuffle is not None else stored_shuffle

        if not force_refresh:
            cached_tracks = self._get_cached_tracks(name, user_id=user_id)
            if cached_tracks:
                cached_videos: list[dict[str, object]] = []
                for track in cached_tracks:
                    normalized_entry = self._normalize_playlist_entry(provider_name, track)
                    if normalized_entry is not None:
                        cached_videos.append(normalized_entry)

                if should_shuffle:
                    import random

                    random.SystemRandom().shuffle(cached_videos)
                    logger.info("Shuffled %s cached videos from '%s'", len(cached_videos), name)

                return cached_videos[:limit]

        try:
            if progressive:
                fetch_limit = min(50, limit * 5) if should_shuffle else limit
                logger.info(
                    "Progressive loading: Fetching first %s tracks from '%s'",
                    fetch_limit,
                    name,
                )
            else:
                fetch_limit = limit * 50 if should_shuffle else limit
                fetch_limit = min(fetch_limit, 500)
                logger.info(
                    "Full loading: Fetching up to %s tracks from '%s'",
                    fetch_limit,
                    name,
                )

            if provider_name in {
                ProviderName.YOUTUBE_MUSIC,
                ProviderName.YOUTUBE_IFRAME,
            }:
                entries = self._resolve_youtube_playlist_entries(
                    playlist_info,
                    playlist_name=name,
                    fetch_limit=fetch_limit,
                    user_id=user_id,
                )
            else:
                entries = self._fetch_provider_playlist_entries(
                    provider_name,
                    playlist_info,
                    playlist_name=name,
                    fetch_limit=fetch_limit,
                    user_id=user_id,
                )

            if entries:
                videos: list[dict[str, object]] = []
                for entry in entries:
                    if not entry:
                        continue

                    normalized_entry = self._normalize_playlist_entry(provider_name, entry)
                    if normalized_entry is not None:
                        videos.append(normalized_entry)

                logger.info(
                    "Fetched %s tracks from playlist '%s' provider=%s",
                    len(videos),
                    name,
                    provider_name.value,
                )

                self._cache_tracks(name, videos, user_id=user_id)

                if should_shuffle:
                    import random
                    import time

                    secure_random = random.SystemRandom()
                    secure_random.shuffle(videos)
                    random.seed(time.time() * 1000000)

                    logger.info(
                        "Shuffled %s tracks from playlist '%s' (using secure randomness)",
                        len(videos),
                        name,
                    )

                videos = videos[:limit]
                logger.info("Returning %s tracks after shuffle/limit", len(videos))

                return videos
            return []
        except ResolutionError as exc:
            logger.error(
                "Playlist fetch failed for '%s': %s",
                name,
                getattr(exc, "code", str(exc)),
            )
            return []
        except json.JSONDecodeError as exc:
            logger.error("Failed to parse provider response for playlist '%s': %s", name, exc)
            return []
        except Exception as exc:
            logger.exception(
                "Error fetching playlist videos from '%s': %s",
                name,
                exc,
            )
            return []


# Use standardized singleton pattern
from utils.singleton import create_singleton_getter

get_playlist_manager = create_singleton_getter("playlist_manager", PlaylistManager)
