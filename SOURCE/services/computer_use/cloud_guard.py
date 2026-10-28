"""Runtime guards for desktop-only computer use."""

from __future__ import annotations

from typing import Any

from config.settings import settings

UNAVAILABLE_ENVELOPE: dict[str, object] = {
    "ok": False,
    "error_category": "COMPUTER_USE_UNAVAILABLE",
    "reason": "GUI control is not available on the cloud surface; this is a desktop-only capability",
}


def is_cloud_surface(settings_obj: Any | None = None) -> bool:
    """Return True when this runtime is the cloud surface."""
    cfg = settings_obj or settings
    surface = str(getattr(cfg, "app_surface", "desktop") or "desktop").strip().lower()
    return surface == "cloud"


def cloud_unavailable_envelope() -> dict[str, object]:
    """Return a fresh structured cloud-refusal envelope."""
    return dict(UNAVAILABLE_ENVELOPE)


def assert_desktop_surface(settings_obj: Any | None = None) -> None:
    """Raise NotImplementedError when computer use is called on cloud."""
    if is_cloud_surface(settings_obj):
        raise NotImplementedError(cloud_unavailable_envelope())


def _local_desktop_principals() -> list[str]:
    """The principals of the person sitting at this desktop install.

    One install is one person: the signed-in Viola account and the bootstrap
    device identity are the same human (see ``core.user_context.get_desktop_local_principals``).
    Comparing against the device id alone refused every signed-in user, whose
    agent runs as the account id (1.0.5 laptop report X8.9). Nothing here is
    another tenant: the list holds at most this install's account and device.
    """
    from core.user_context import get_cached_desktop_account_user_id, get_device_user_id

    principals: list[str] = []
    account = get_cached_desktop_account_user_id()
    if account:
        principals.append(account)
    device = get_device_user_id()  # mt-ok: desktop GUI control is bound to this device.
    if device and device not in principals:
        principals.append(device)
    return principals


def assert_local_user(user_id: str | None) -> None:
    """Reject GUI control for anyone but the person at this desktop.

    Allowed: the install's signed-in account and its device identity (and the
    retired local sentinel used by old fixtures). Refused: a cloud surface,
    and any other principal, such as a remote or relayed caller acting for a
    different account.
    """
    from core.user_context import is_legacy_local_user_id

    if is_cloud_surface():
        raise PermissionError(
            {
                "ok": False,
                "error_category": "COMPUTER_USE_FORBIDDEN_USER",
                "reason": "GUI control is not available on the cloud surface",
                "expected_user_id": "",
                "actual_user_id": user_id or "",
            }
        )
    if is_legacy_local_user_id(user_id):
        return
    principals = _local_desktop_principals()
    normalized = str(user_id or "").strip()
    if normalized and normalized in principals:
        return
    raise PermissionError(
        {
            "ok": False,
            "error_category": "COMPUTER_USE_FORBIDDEN_USER",
            "reason": "GUI control is scoped to the active local desktop user only",
            "expected_user_id": principals[0] if principals else "",
            "actual_user_id": user_id or "",
        }
    )
