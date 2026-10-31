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

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"

# What the calendar tools use: the calendar list and events (read), and
# creating, changing and deleting events. The broader "calendar" scope (manage
# calendars themselves) is not used, so it is not requested.
CALENDAR_SCOPES = [
    CALENDAR_READONLY_SCOPE,
    CALENDAR_EVENTS_SCOPE,
]

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# Read, search and organize the mailbox: list and read messages and threads,
# change labels (archive, star, mark read), move to and from the trash, and
# create, edit and send drafts. It never deletes mail permanently.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

# The extra Gmail permission Viola asks for the first time the user wants it to
# read, search or organize their mail (incremental consent, with
# include_granted_scopes). gmail.modify is a *restricted* scope and is not part
# of Google's verification of Viola (see GOOGLE_VERIFIED_SCOPES), so Google
# still shows its "Google hasn't verified this app" screen at this step and
# caps it at 100 users. Keeping it out of the base connection means the first
# "Connect Google" asks only for verified scopes and shows no warning. Fold it
# into GOOGLE_CONNECTION_SCOPES only after Google approves gmail.modify (that
# needs the restricted-scope review, normally with a CASA assessment), and add
# it to GOOGLE_VERIFIED_SCOPES in the same change.
GMAIL_MAILBOX_SCOPES: tuple[str, ...] = (GMAIL_MODIFY_SCOPE,)

GMAIL_SCOPES = [
    GMAIL_SEND_SCOPE,
    *GMAIL_MAILBOX_SCOPES,
]

# Scopes the first desktop "Connect Google" asks for: Calendar read + event
# write, and Gmail send. gmail.send stays in the base connection (and is kept
# alongside gmail.modify) so a user who never grants mailbox access can still
# send mail they ask for. Tokens for this connection live only in the
# desktop's encrypted consent vault (storage tier 3); the cloud never sees them.
GOOGLE_CONNECTION_SCOPES = (
    CALENDAR_READONLY_SCOPE,
    CALENDAR_EVENTS_SCOPE,
    GMAIL_SEND_SCOPE,
)

# Everything the desktop Google connection can hold once the user has also
# allowed mailbox access. This is the full list to register on the Google
# Cloud consent screen (plus the sign-in scopes above).
GOOGLE_CONNECTION_ALL_SCOPES = (*GOOGLE_CONNECTION_SCOPES, *GMAIL_MAILBOX_SCOPES)

# The scopes Google verified for Viola's OAuth app (Google Cloud project
# optimum-octane-478214-d6): the sign-in scopes plus the three sensitive scopes
# submitted on 2026-10-26. A request made only of these scopes gets Google's
# normal consent screen with no "unverified app" warning and no user cap. The
# sign-in flow and the first desktop "Connect Google" must stay inside this
# list (tests enforce it). Any new scope means a new Google review before it
# can go into either flow.
GOOGLE_VERIFIED_SCOPES: tuple[str, ...] = (
    *GOOGLE_SIGN_IN_SCOPES,
    CALENDAR_READONLY_SCOPE,
    CALENDAR_EVENTS_SCOPE,
    GMAIL_SEND_SCOPE,
)


def unverified_google_scopes(scopes: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """Scopes in *scopes* that Google has not verified for Viola (keeps order)."""
    verified = set(GOOGLE_VERIFIED_SCOPES)
    return [scope for scope in dict.fromkeys(scopes or ()) if scope not in verified]

# Scope policy (founder decision 2026-10-30): Gmail gets full mailbox access
# within what Google allows without permanent deletion (gmail.modify, plus
# gmail.send). Every other Workspace scope stays read-only ("*.readonly"
# suffix) or a strictly-read scope (openid, userinfo.profile, userinfo.email),
# matching the public privacy page. Drive/Docs/Sheets/Chat/Admin remain behind
# ``google_restricted_features_enabled``.
WORKSPACE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
]

# Scopes Google classes as *restricted* (CASA security assessment when the
# data touches a server). gmail.send is *sensitive*, not restricted. The Gmail
# mailbox scopes are restricted, but Viola's Gmail data never touches a Viola
# server (desktop-only token, desktop-to-Google calls), and Gmail is not behind
# ``google_restricted_features_enabled``: that flag now gates only the
# Drive/Docs/Sheets/Chat/Admin workspace bridge.
RESTRICTED_GOOGLE_SCOPES = tuple(
    sorted(
        {
            GMAIL_READONLY_SCOPE,
            GMAIL_MODIFY_SCOPE,
            *(s for s in WORKSPACE_SCOPES if not s.endswith("/userinfo.profile")),
        }
    )
)


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
    """Return the Gmail send scope.

    ``gmail.send`` is a sensitive (not restricted) scope and is part of the
    desktop Google connection, so it is not behind the restricted-features
    gate. ``settings_obj`` is accepted for call-site compatibility.
    """
    del settings_obj
    return [GMAIL_SEND_SCOPE]


def get_enabled_gmail_scopes(settings_obj: object | None = None) -> list[str]:
    """Return every Gmail scope Viola uses: send plus full mailbox access.

    Gmail reading and organizing are on by default (founder decision
    2026-10-30); they are not behind the restricted-features gate.
    """
    del settings_obj
    return list(GMAIL_SCOPES)


def is_gmail_mailbox_enabled(settings_obj: object | None = None) -> bool:
    """Whether Viola may ask Google for Gmail mailbox access (gmail.modify).

    On by default (founder decision 2026-10-31: full Gmail in 1.0.5 while
    Google reviews the restricted scope). Set ``google_gmail_mailbox_enabled``
    to False (env ``VIOLA_GOOGLE_GMAIL_MAILBOX_ENABLED=false``) to keep every Google
    request inside ``GOOGLE_VERIFIED_SCOPES``: Viola then never asks for
    gmail.modify, and the read and organize tools say the feature is off.
    """
    try:
        settings = _load_settings(settings_obj)
        return bool(getattr(settings, "google_gmail_mailbox_enabled", True))
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        logger.debug("Gmail mailbox setting lookup failed, keeping the default: %s", exc)
        return True


def get_gmail_mailbox_scopes() -> list[str]:
    """Scopes a read, search or organize Gmail action needs."""
    return list(GMAIL_MAILBOX_SCOPES)


def missing_gmail_mailbox_scopes(granted: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
    """Mailbox scopes not yet in *granted* (empty when mailbox access is allowed)."""
    have = set(granted or ())
    return [scope for scope in GMAIL_MAILBOX_SCOPES if scope not in have]


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
