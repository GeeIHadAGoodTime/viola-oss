"""Per-request resolution of the signed-in user's IANA timezone.

Viola's cloud containers run in UTC. Every calendar path that turns a naive
wall-clock time ("tomorrow at 2pm") into a stored instant used to stamp it with
the *process* timezone, so a user in ``America/Chicago`` who asked for 2pm got
an event stored at ``14:00Z`` -- 9:00 AM their time, five hours early (#3557).
Desktop never showed the defect because the process timezone there IS the
user's timezone; the cloud has no such luck, which made this a cloud-only
parity gap rather than a shared bug.

Two ambient sources, both scoped to one request/turn:

* **client zone** -- ``X-Viola-Timezone``, the browser's own
  ``Intl.DateTimeFormat().resolvedOptions().timeZone``, bound by the auth
  middleware. It costs no round trip, stores nothing (so it needs no
  cloud-sync consent, unlike a Tier-2 preference at rest), and follows the
  user when they travel.
* **user preference** -- the stored ``timezone`` / ``calendar_timezone``
  setting, read per user from RLS-scoped cloud Postgres on the server or from
  SettingsManager on desktop. An explicit choice outranks the browser's guess,
  and it is what serves paths with no live client request behind them.

Resolution order is therefore: stored preference, then client zone, then the
deployment's configured ``calendar_timezone`` (the machine zone on desktop,
UTC in a container).

There is deliberately no cross-user fallback. Both values live in
``ContextVar``s scoped to a single request and always reset afterwards, so an
unresolvable timezone degrades to the deployment default and never to another
tenant's zone. ``resolve_user_timezone_name`` refuses to run without a
``user_id``.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import re
from collections.abc import AsyncIterator, Iterator, Mapping

from core.logging_config import get_logger

logger = get_logger(__name__)

#: Request header carrying the client's own IANA zone (browser + LAN clients).
TIMEZONE_HEADER = "X-Viola-Timezone"

#: Preference keys checked, in order, when reading a stored per-user zone.
USER_TIMEZONE_SETTING_KEYS: tuple[str, ...] = ("timezone", "calendar_timezone")

# "auto" is the AppConfig sentinel meaning "detect from the machine", which is
# exactly the answer that is wrong on a cloud box. "user.timezone" is the
# settings-schema placeholder. Neither is a real zone.
_AUTO_TIMEZONE_SENTINELS = frozenset({"auto", "user.timezone"})

# A zoneinfo key is a slash-joined set of identifier-ish segments. Validating
# the shape before touching ``ZoneInfo`` keeps hostile header values away from
# its filesystem lookup entirely (it rejects traversal itself, but a header is
# attacker-controlled input and gets an explicit allowlist).
_TIMEZONE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+_-]*(?:/[A-Za-z0-9+_-]+)*$")
_MAX_TIMEZONE_NAME_LEN = 64

_client_timezone_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "viola_client_timezone",
    default=None,
)
_user_timezone_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "viola_user_timezone",
    default=None,
)


def normalize_timezone_name(value: object) -> str | None:
    """Return *value* as a usable IANA zone name, or ``None``.

    Rejects the "auto"/placeholder sentinels, anything that is not shaped like
    a zoneinfo key, and anything ``zoneinfo`` cannot actually load.
    """

    text = str(value or "").strip()
    if not text or len(text) > _MAX_TIMEZONE_NAME_LEN:
        return None
    if text.lower() in _AUTO_TIMEZONE_SENTINELS:
        return None
    if not _TIMEZONE_NAME_RE.match(text):
        return None
    if load_timezone(text) is None:
        return None
    return text


def load_timezone(name: str) -> datetime.tzinfo | None:
    """Load an IANA zone by name, returning ``None`` when unavailable."""

    if name.upper() == "UTC":
        return datetime.UTC
    try:
        from zoneinfo import ZoneInfo
    except ImportError:  # pragma: no cover - stdlib since 3.9
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        logger.debug("Unknown timezone name %r: %s", name, exc)
        return None


def client_timezone_from_headers(headers: Mapping[str, str] | None) -> str | None:
    """Extract the client-declared zone from request headers."""

    if headers is None:
        return None
    try:
        raw = headers.get(TIMEZONE_HEADER)
        if raw is None:
            raw = headers.get(TIMEZONE_HEADER.lower())
    except (AttributeError, TypeError, ValueError) as exc:  # pragma: no cover - exotic containers
        logger.debug("Timezone header read failed: %s", exc)
        return None
    return normalize_timezone_name(raw)


def _configured_timezone_name() -> str | None:
    """The deployment default (machine zone on desktop, UTC in a container)."""

    try:
        from config.settings import settings

        return normalize_timezone_name(getattr(settings, "calendar_timezone", None))
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Configured calendar timezone lookup failed: %s", exc)
        return None


async def resolve_user_timezone_name(user_id: str) -> str | None:
    """Read *user_id*'s explicitly stored timezone preference, if any.

    Never falls back to another user's value, and never to a process global:
    a caller with no ``user_id`` is a bug, not a reason to guess.
    """

    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required to resolve a user timezone")

    cloud_settings = None
    try:
        # Generic async-safe reader: returns the RLS-scoped cloud preference
        # blob on the server and ``None`` when the active auth DB is the
        # desktop SQLite backend, so the desktop branch below still runs.
        from telephony.user_settings_lookup import load_cloud_user_settings_blob

        cloud_settings = await load_cloud_user_settings_blob(uid)
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Cloud timezone preference lookup failed for user %s: %s", uid, exc)

    if cloud_settings is not None:
        for key in USER_TIMEZONE_SETTING_KEYS:
            resolved = normalize_timezone_name(cloud_settings.get(key))
            if resolved is not None:
                return resolved
        return None

    try:
        from ui.settings_manager import get_settings_manager

        settings_manager = get_settings_manager()
        for key in USER_TIMEZONE_SETTING_KEYS:
            resolved = normalize_timezone_name(settings_manager.get(key=key, user_id=uid, default=None))
            if resolved is not None:
                return resolved
    except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("Desktop timezone preference lookup failed for user %s: %s", uid, exc)
    return None


@contextlib.contextmanager
def client_timezone_scope(name: object) -> Iterator[str | None]:
    """Bind the client-declared zone for the enclosing block (auth middleware)."""

    resolved = normalize_timezone_name(name)
    token = _client_timezone_name.set(resolved)
    try:
        yield resolved
    finally:
        _client_timezone_name.reset(token)


@contextlib.contextmanager
def timezone_scope(name: object) -> Iterator[str | None]:
    """Bind *name* as the user's zone for the enclosing block."""

    resolved = normalize_timezone_name(name)
    token = _user_timezone_name.set(resolved)
    try:
        yield resolved
    finally:
        _user_timezone_name.reset(token)


@contextlib.asynccontextmanager
async def user_timezone_scope(user_id: object) -> AsyncIterator[str | None]:
    """Bind *user_id*'s stored timezone preference for the enclosing block.

    Resolution failure is never fatal: the block still runs, on the client zone
    or the deployment default, exactly as it did before this seam existed.
    """

    resolved: str | None = None
    uid = str(user_id or "").strip()
    if uid:
        try:
            resolved = await resolve_user_timezone_name(uid)
        except (AttributeError, ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.debug("User timezone resolution failed for user %s: %s", uid, exc)
    with timezone_scope(resolved) as active:
        yield active or _client_timezone_name.get()


def active_timezone_name() -> str | None:
    """The zone for the current user: stored preference, else client-declared."""

    return _user_timezone_name.get() or _client_timezone_name.get()


def active_timezone() -> datetime.tzinfo:
    """The tzinfo for the current user: ambient, else deployment, else UTC."""

    for name in (active_timezone_name(), _configured_timezone_name()):
        if not name:
            continue
        tz = load_timezone(name)
        if tz is not None:
            return tz
    try:
        local_tz = datetime.datetime.now().astimezone().tzinfo
    except (OSError, RuntimeError, ValueError) as exc:  # pragma: no cover - platform dependent
        logger.debug("Local timezone detection failed: %s", exc)
        local_tz = None
    return local_tz or datetime.UTC
