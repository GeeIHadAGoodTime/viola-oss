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


def assert_local_user(user_id: str | None) -> None:
    """Reject GUI control for non-local desktop principals."""
    from core.user_context import get_device_user_id, is_legacy_local_user_id

    expected_user_id = get_device_user_id()  # mt-ok: desktop GUI control is bound to this device.
    if user_id == expected_user_id or is_legacy_local_user_id(user_id):
        return
    if user_id != expected_user_id:
        reason = "GUI control is scoped to the active local desktop user only"
        raise PermissionError(
            {
                "ok": False,
                "error_category": "COMPUTER_USE_FORBIDDEN_USER",
                "reason": reason,
                "expected_user_id": expected_user_id,
                "actual_user_id": user_id or "",
            }
        )
