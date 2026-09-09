from __future__ import annotations

LOCAL_TARGET_URL_PREFIXES = (
    "http://localhost",
    "https://localhost",
    "http://127.0.0.1",
    "https://127.0.0.1",
    "file://",
)

_preferred_target_id: str | None = None


def set_preferred_target_id(target_id: str | None) -> None:
    """Publish the Qt overlay Chromium target id from QWebEnginePage.devToolsId()."""
    global _preferred_target_id
    _preferred_target_id = target_id or None


def get_preferred_target_id() -> str | None:
    """Return the preferred Qt overlay Chromium target id, if published."""
    return _preferred_target_id


def is_local_target_url(url: str) -> bool:
    return any(url.startswith(prefix) for prefix in LOCAL_TARGET_URL_PREFIXES)


def target_id_matches(candidate_id: str, preferred_id: str | None) -> bool:
    if not candidate_id or not preferred_id:
        return False
    return candidate_id == preferred_id or candidate_id.startswith(preferred_id[:16])


__all__ = [
    "LOCAL_TARGET_URL_PREFIXES",
    "get_preferred_target_id",
    "is_local_target_url",
    "set_preferred_target_id",
    "target_id_matches",
]
