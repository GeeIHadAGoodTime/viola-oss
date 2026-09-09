"""Deprecated custom-session privilege rotation hooks.

GoTrue now owns cloud auth session lifecycle. This module remains as a
compatibility import surface for privilege-change callers until those call
sites migrate to a GoTrue admin revocation design.
"""

from __future__ import annotations

from typing import Any

from core.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Privilege change reasons
# ---------------------------------------------------------------------------


class PrivilegeChangeReason:
    """Constants for privilege change reasons."""

    MFA_ENABLED = "mfa_enabled"
    MFA_DISABLED = "mfa_disabled"
    PASSWORD_CHANGED = "password_changed"  # pragma: allowlist secret
    PLAN_UPGRADED = "plan_upgraded"
    PLAN_DOWNGRADED = "plan_downgraded"
    ROLE_CHANGED = "role_changed"
    ACCOUNT_RECOVERY = "account_recovery"


# ---------------------------------------------------------------------------
# Core rotation logic
# ---------------------------------------------------------------------------


async def rotate_sessions_on_privilege_change(
    user_id: str,
    reason: str,
    exclude_session_id: str | None = None,
    session_service: Any | None = None,
) -> int:
    """Deprecated no-op until GoTrue admin session revocation is designed."""
    logger.warning(
        "custom_session_privilege_rotation_deprecated",
        extra={
            "user_id": user_id,
            "privilege_change_reason": reason,
            "exclude_session_id": exclude_session_id,
            "custom_session_service_supplied": session_service is not None,
        },
    )
    return 0
