"""User Model service -- learned preferences, facts, and interaction patterns.

Builds a persistent profile from observed user behavior and explicit statements,
stored in the auth database ``user_models`` table (per-user).  ``data/user_model.json``
acts as a seed file for first-run migration only.

Also provides ``UserPreferencesStore`` for per-user key-value settings
(delivery_address, weather_location, etc.) stored in ``user_preferences``.
"""

from __future__ import annotations

from services.user_model.preferences_store import (
    USER_SCOPED_KEYS,
    UserPreferencesStore,
    get_user_preferences_store,
)
from services.user_model.profile import (
    UserModelProfile,
    get_user_model,
    migrate_json_to_db,
    reload_user_model,
)

__all__ = [
    "USER_SCOPED_KEYS",
    "UserModelProfile",
    "UserPreferencesStore",
    "get_user_model",
    "get_user_preferences_store",
    "migrate_json_to_db",
    "reload_user_model",
]
