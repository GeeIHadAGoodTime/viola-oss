"""Account gate shared by the source core and private composition.

Only Viola-managed features are account-gated. User-owned BYOK, Codex, and
local inference remain usable without a Viola account. Paid actions fail
closed when no authenticated account is present.
"""

from __future__ import annotations

import os
from typing import Any

WEBSITE_BASE = "https://useviola.com"
SIGNIN_URL = f"{WEBSITE_BASE}/login"
SIGNUP_URL = f"{WEBSITE_BASE}/login?tab=register"

LOGIN_REQUIRED_FOR_PAID_ACTION = "login_required_for_paid_action"
PHONE_TOS_REQUIRED = "phone_tos_required"
OPEN_ACCOUNT_SETTINGS_ACTION = "open_account_settings"

_ACCOUNT_REQUIRED_MESSAGE = "Account required."
_ACCOUNT_REQUIRED_BODY = "Sign in or create a Viola account to use managed AI."
_PAID_ACTION_LOGIN_MESSAGE = "Sign in to continue."
_PAID_ACTION_LOGIN_BODY = "Sign in to use Viola-paid phone, SMS, recording, or managed AI features."


def user_has_viola_account(user_id: str | None = None) -> bool:
    """Return whether the current principal is an authenticated Viola account."""
    if user_id is None:
        try:
            from core.user_context import get_current_user_id

            user_id = get_current_user_id()
        except LookupError:
            return False

    from core.user_context import is_desktop_local_principal

    return bool(user_id and not is_desktop_local_principal(user_id))


def account_required_card() -> dict[str, Any]:
    """Return the account-required card consumed by the desktop UI."""
    return {
        "type": "account_required",
        "title": "Account required",
        "body": _ACCOUNT_REQUIRED_BODY,
        "cta": {
            "label": "Sign in",
            "action": OPEN_ACCOUNT_SETTINGS_ACTION,
            "url": SIGNIN_URL,
        },
        "secondary_cta": {
            "label": "Create account",
            "action": OPEN_ACCOUNT_SETTINGS_ACTION,
            "url": SIGNUP_URL,
        },
        "dismiss_after_ms": 90000,
    }


def paid_action_login_required_card(*, action: str = "paid_action") -> dict[str, Any]:
    """Return the sign-in card for a Viola-paid action."""
    return {
        "type": LOGIN_REQUIRED_FOR_PAID_ACTION,
        "title": "Sign in required",
        "body": _PAID_ACTION_LOGIN_BODY,
        "action": action,
        "cta": {
            "label": "Sign in",
            "action": OPEN_ACCOUNT_SETTINGS_ACTION,
            "url": SIGNIN_URL,
        },
        "secondary_cta": {
            "label": "Create account",
            "action": OPEN_ACCOUNT_SETTINGS_ACTION,
            "url": SIGNUP_URL,
        },
        "dismiss_after_ms": 90000,
    }


def account_required_envelope_data(*, intent: str = "account_required") -> dict[str, Any]:
    """Return the standard account-required command envelope."""
    return {
        "intent": intent,
        "message": _ACCOUNT_REQUIRED_MESSAGE,
        "action": "open_signup",
        "signin_url": SIGNIN_URL,
        "signup_url": SIGNUP_URL,
        "card": account_required_card(),
    }


def paid_action_login_required_data(
    *,
    action: str = "paid_action",
    message: str | None = None,
    intent: str = LOGIN_REQUIRED_FOR_PAID_ACTION,
) -> dict[str, Any]:
    """Return the standard paid-action sign-in envelope."""
    user_message = message or _PAID_ACTION_LOGIN_MESSAGE
    return {
        "intent": intent,
        "error_code": LOGIN_REQUIRED_FOR_PAID_ACTION,
        "message": user_message,
        "action": action,
        "signin_url": SIGNIN_URL,
        "signup_url": SIGNUP_URL,
        "card": paid_action_login_required_card(action=action),
    }


def require_account_for_paid_actions_enabled(user_id: str | None = None) -> bool:
    """Resolve the protected paid-action account-gate setting."""
    from config.defaults import REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT

    env_override = (os.environ.get("VIOLA_REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_OVERRIDE") or "").strip().lower()
    if env_override in {"true", "1", "yes", "on"}:
        return True
    if env_override in {"false", "0", "no", "off"}:
        return False

    try:
        from ui.settings_manager import get_settings_manager

        raw = get_settings_manager().get(
            "require_account_for_paid_actions",
            REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT,
            user_id=user_id,
        )
        if raw is None:
            return REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT
        if isinstance(raw, str):
            return raw.strip().lower() not in {"0", "false", "no", "off"}
        return bool(raw)
    except Exception:
        return REQUIRE_ACCOUNT_FOR_PAID_ACTIONS_DEFAULT


def paid_action_login_required(user_id: str | None = None) -> bool:
    """Return whether a Viola-paid action must pause for account login."""
    return require_account_for_paid_actions_enabled(user_id) and not user_has_viola_account(user_id)


def requires_account_for_command(user_id: str | None, ai_source: str | None) -> bool:
    """Gate managed AI while leaving BYOK, Codex, and local AI account-free."""
    from core.product import ai_source_uses_viola_managed_llm

    if user_has_viola_account(user_id):
        return False
    if not require_account_for_paid_actions_enabled(user_id):
        return False
    return ai_source_uses_viola_managed_llm(ai_source)
