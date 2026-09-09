"""User profile helpers for phone-call prompt context."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from core.logging_config import get_logger
from ui.settings_manager import get_settings_manager

logger = get_logger(__name__)


class _SettingsGetter(Protocol):
    def get(self, key: str, default: object = None, user_id: str | None = None) -> object: ...


_MEDICAL_TASK_KEYWORDS = frozenset(
    {
        "appointment",
        "clinic",
        "dental",
        "dentist",
        "doctor",
        "health",
        "insurance",
        "medical",
        "orthodont",
        "pharmacy",
        "prescription",
        "provider",
        "vet",
        "veterinarian",
    }
)
_ADDRESS_TASK_KEYWORDS = frozenset(
    {
        "address",
        "dental",
        "delivery",
        "dentist",
        "doctor",
        "home",
        "house",
        "medical",
        "order",
        "pharmacy",
        "pickup",
        "pizza",
        "plumber",
        "repair",
        "restaurant",
        "service",
        "vet",
    }
)
_NAME_TASK_KEYWORDS = frozenset(
    {
        "appointment",
        "booking",
        "delivery",
        "order",
        "reservation",
        "reserve",
        "schedule",
    }
)


def _task_mentions(task: str, keywords: Iterable[str]) -> bool:
    task_lower = (task or "").lower()
    return any(keyword in task_lower for keyword in keywords)


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _format_address(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""

    full_text = _clean_text(value.get("full_text"))
    if full_text:
        return full_text

    parts = [
        _clean_text(value.get("street")),
        _clean_text(value.get("city")),
        _clean_text(value.get("state")),
        _clean_text(value.get("zip")),
    ]
    return ", ".join(part for part in parts if part)


def _first_setting(settings_manager: _SettingsGetter, user_id: str, keys: Iterable[str]) -> object:
    for key in keys:
        value = settings_manager.get(key, None, user_id=user_id)
        if value not in (None, "", {}):
            return value
    return None


def build_phone_info_manifest(user_id: str, task: str) -> dict[str, list[str]]:
    """Build a compact phone prompt manifest from per-user settings.

    Only set values are surfaced, and sensitive file paths stay out of the
    prompt. The caller still needs the real user_id so settings remain
    per-user scoped.
    """
    if not user_id:
        raise ValueError("user_id is required")

    have: list[str] = []
    settings_manager = get_settings_manager()

    try:
        if _task_mentions(task, _NAME_TASK_KEYWORDS):
            name = _clean_text(_first_setting(settings_manager, user_id, ("user_name", "full_name")))
            if name:
                have.append("Name on file: %s" % name)

        if _task_mentions(task, _ADDRESS_TASK_KEYWORDS):
            address = _format_address(_first_setting(settings_manager, user_id, ("home_address", "delivery_address")))
            if address:
                have.append("Home address: %s" % address)

        if _task_mentions(task, _MEDICAL_TASK_KEYWORDS):
            date_of_birth = _clean_text(
                _first_setting(settings_manager, user_id, ("date_of_birth", "birth_date", "dob"))
            )
            if date_of_birth:
                have.append("Date of birth: %s" % date_of_birth)

            insurance_card = _first_setting(
                settings_manager,
                user_id,
                (
                    "insurance_card_path",
                    "medical_insurance_card_path",
                    "dental_insurance_card_path",
                ),
            )
            if insurance_card not in (None, "", {}):
                have.append("Insurance card on file")
    except Exception as exc:
        logger.warning("Phone info manifest unavailable for user %s: %s", user_id, exc)
        return {"have": [], "dont_have": []}

    return {"have": have, "dont_have": []}
