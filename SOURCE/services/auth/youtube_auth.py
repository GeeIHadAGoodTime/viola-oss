from __future__ import annotations

from dataclasses import dataclass

from music.consent.vault import EncryptedTokenVault
from music.providers.youtube_music_auth import _get_access_token


@dataclass(frozen=True, slots=True)
class YouTubeAuthManager:
    user_id: str

    def is_authenticated(self) -> bool:
        return _get_access_token(self.user_id) is not None

    def refresh_token(self) -> bool:
        return _get_access_token(self.user_id) is not None

    def logout(self) -> None:
        vault = EncryptedTokenVault(user_id=self.user_id)
        vault.remove_provider("youtube_music", user_id=self.user_id)


def get_youtube_auth_manager(user_id: str) -> YouTubeAuthManager:
    return YouTubeAuthManager(user_id=user_id)
