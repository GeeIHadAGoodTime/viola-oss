"""Google external-service OAuth configuration helpers."""

from __future__ import annotations

from core.logging_config import get_logger

logger = get_logger("viola.services.oauth.google")

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

GOOGLE_SIGN_IN_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

GMAIL_SCOPES = [
    GMAIL_SEND_SCOPE,
    GMAIL_READONLY_SCOPE,
]

# Least-privilege: every Workspace scope must be read-only ("*.readonly" suffix)
# or a strictly-read scope (openid, userinfo.profile, userinfo.email). The
# public privacy disclosure tells users Workspace data "may be read only as
# needed" — requesting any write-capable Workspace scope here would put the
# product out of sync with the consent screen and the privacy page.
# Gate: scripts/check_google_oauth_scope_least_privilege.py enforces this rule.
WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]

RESTRICTED_GOOGLE_SCOPES = tuple(sorted({*GMAIL_SCOPES, *WORKSPACE_SCOPES}))


def _load_settings(settings_obj: object | None) -> object:
    if settings_obj is not None:
        return settings_obj
    from config.settings import get_settings

    return get_settings()


def is_google_restricted_features_enabled(settings_obj: object | None = None) -> bool:
    """Return whether restricted-scope Google features are enabled for this build."""
    try:
        settings = _load_settings(settings_obj)
        return bool(getattr(settings, "google_restricted_features_enabled", False))
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.debug("Google restricted features disabled because settings lookup failed: %s", exc)
        return False


def get_enabled_gmail_send_scopes(settings_obj: object | None = None) -> list[str]:
    """Return Gmail send scopes only when the restricted Google gate is enabled."""
    if not is_google_restricted_features_enabled(settings_obj):
        return []
    return [GMAIL_SEND_SCOPE]


def get_enabled_gmail_scopes(settings_obj: object | None = None) -> list[str]:
    """Return Gmail scopes only when the restricted Google gate is enabled."""
    if not is_google_restricted_features_enabled(settings_obj):
        return []
    return list(GMAIL_SCOPES)


def get_enabled_workspace_scopes(settings_obj: object | None = None) -> list[str]:
    """Return Workspace scopes only when the restricted Google gate is enabled."""
    if not is_google_restricted_features_enabled(settings_obj):
        return []
    return list(WORKSPACE_SCOPES)


def has_restricted_google_scope(scopes: list[str] | tuple[str, ...] | set[str] | None) -> bool:
    """Return True when *scopes* includes any CASA/restricted Google scope."""
    if not scopes:
        return False
    return bool(set(scopes) & set(RESTRICTED_GOOGLE_SCOPES))


def is_google_configured() -> bool:
    """Check whether Google external-service OAuth credentials are configured."""
    try:
        from config.settings import get_settings

        settings = get_settings()
        return bool(getattr(settings, "google_client_id", None) and getattr(settings, "google_client_secret", None))
    except (ImportError, AttributeError) as exc:
        logger.debug("Google OAuth not configured: %s", exc)
        return False
