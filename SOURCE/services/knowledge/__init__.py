"""Legacy Knowledge package backed by the Workbench folder.

The old Knowledge store/vault/search implementation was retired after the
markdown-first memory and Workbench rewrite. Keep this package importable only
for callers that still need the legacy folder-open helpers.
"""

from __future__ import annotations

from services.knowledge.folder_open import get_storage_mode, get_user_folder_path, open_user_folder

__all__ = [
    "get_storage_mode",
    "get_user_folder_path",
    "open_user_folder",
]
