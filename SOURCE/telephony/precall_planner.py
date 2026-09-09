"""Pre-call context planning for outbound phone calls.

This module runs before Telnyx dialing to gather stable context from the user's
profile and lightweight web search. Missing details are exposed through the
phone prompt's ``INFO YOU HAVE`` / ``INFO YOU DO NOT HAVE`` block so the model
can resolve them live with the recipient or ``consult_user``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from core.logging_config import get_logger
from core.user_profile import build_phone_info_manifest
from telephony.caller_validation import is_assistant_caller_name, validate_caller_name
from telephony.user_settings_lookup import first_setting_value, load_cloud_user_settings_blob
from ui.settings_manager import get_settings_manager

logger = get_logger(__name__)

_ADDRESS_RE = re.compile(
    r"\b\d{1,6}\s+"
    r"[A-Za-z0-9.'#\- ]+\s+"
    r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Way|Ct|Court|"
    r"Pkwy|Parkway|Hwy|Highway|Pl|Place|Cir|Circle)\b"
    r"[^,\n]*(?:,\s*[A-Za-z.'\- ]+)?(?:,\s*[A-Z]{2})?(?:\s+\d{5}(?:-\d{4})?)?",
    re.IGNORECASE,
)

_FOOD_ORDER_TERMS = frozenset(
    {
        "burger",
        "coffee",
        "domino",
        "food",
        "meal",
        "pizza",
        "pizzeria",
        "restaurant",
        "sandwich",
        "sushi",
        "taco",
    }
)
_ORDER_TERMS = frozenset({"order", "pickup", "pick up", "takeout", "delivery", "deliver"})
_APPOINTMENT_TERMS = frozenset({"appointment", "book", "booking", "schedule", "reschedule"})
_SERVICE_TERMS = frozenset({"repair", "service", "plumber", "electrician", "hvac", "technician"})
_URGENT_TERMS = frozenset({"urgent", "today", "asap", "emergency", "immediately", "right away"})

_KNOWN_BUSINESSES = (
    (re.compile(r"\bdomino'?s?\b", re.IGNORECASE), "Domino's"),
    (re.compile(r"\bpizza hut\b", re.IGNORECASE), "Pizza Hut"),
    (re.compile(r"\bpapa john'?s?\b", re.IGNORECASE), "Papa Johns"),
)
DISCOVERABLE_APPOINTMENT_NOTE = (
    "Appointment timing: no explicit date/time preference was provided. Discover available slots from the "
    "recipient; if the offered slot requires the user's preference or availability, use consult_user before "
    "choosing or committing."
)


@dataclass(frozen=True)
class PreCallPlan:
    """Resolved and missing context facts for one phone call."""

    info_manifest: dict[str, list[str]]
    resolved: dict[str, str] = field(default_factory=dict)


def merge_info_manifests(*manifests: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """Merge phone prompt manifests while preserving order and removing duplicates."""

    have: list[str] = []
    dont_have: list[str] = []
    for manifest in manifests:
        if not manifest:
            continue
        _extend_unique(have, manifest.get("have", []) or [])
        _extend_unique(dont_have, manifest.get("dont_have", []) or [])
    return {"have": have, "dont_have": dont_have}


async def plan_phone_call_prerequisites(
    *,
    user_id: str,
    task: str,
    caller_name: str,
) -> PreCallPlan:
    """Resolve model-visible context before an outbound call starts."""

    base_manifest = build_phone_info_manifest(user_id=user_id, task=task)
    task_lower = (task or "").lower()
    category = _task_category(task_lower)
    profile = await _load_profile_context(user_id)
    have: list[str] = []
    dont_have: list[str] = []
    resolved: dict[str, str] = {}

    owner_name = _resolve_owner_caller_name(profile=profile, caller_name=caller_name)
    if owner_name:
        resolved["owner_caller_name"] = owner_name

    callback = profile.get("callback")
    if callback:
        resolved["callback"] = callback
        have.append("Reachable callback phone: %s" % callback)
    else:
        dont_have.append("Reachable callback phone")

    if category == "general":
        return PreCallPlan(
            info_manifest=merge_info_manifests(base_manifest, {"have": have, "dont_have": dont_have}),
            resolved=resolved,
        )

    business_name = _extract_business_name(task)
    if business_name:
        resolved["business_name"] = business_name
        have.append("Business name: %s" % business_name)
    elif category in {"pickup_food_order", "delivery", "appointment", "service_request"}:
        dont_have.append("Business/provider name")

    name_on_order = owner_name or caller_name.strip()
    if name_on_order:
        label = "Name on order" if category in {"pickup_food_order", "delivery"} else "Name"
        resolved["name"] = name_on_order
        have.append("%s: %s" % (label, name_on_order))
    else:
        dont_have.append("Name")

    if category in {"pickup_food_order", "delivery"}:
        have.append(
            "Payment boundary: ask for pay-at-pickup or pay-on-delivery; if card or prepayment is required, call consult_user before sharing or committing payment details."
        )

    if category == "pickup_food_order":
        store_address = _extract_address(task)
        if not store_address and business_name:
            store_address = await _resolve_nearest_store_address(
                business_name=business_name,
                profile_address=profile.get("address", ""),
            )
        if store_address:
            resolved["store_address"] = store_address
            have.append("Store/location address: %s" % store_address)
        else:
            dont_have.append("Store/location address")

    if category == "delivery":
        delivery_address = profile.get("delivery_address") or profile.get("address")
        if delivery_address:
            resolved["delivery_address"] = delivery_address
            have.append("Delivery address: %s" % delivery_address)
        else:
            dont_have.append("Delivery address")

    if category == "appointment":
        preferred_time = _extract_time_preference(task)
        if preferred_time:
            resolved["date_time_preference"] = preferred_time
            have.append("Date/time preference: %s" % preferred_time)
        else:
            have.append(DISCOVERABLE_APPOINTMENT_NOTE)
            dont_have.append("Date/time preference")

    if category == "service_request":
        service_type = _extract_service_type(task)
        urgency = _extract_urgency(task)
        if service_type:
            resolved["service_type"] = service_type
            have.append("Service type: %s" % service_type)
        else:
            dont_have.append("Service type")
        if urgency:
            resolved["urgency"] = urgency
            have.append("Urgency: %s" % urgency)
        else:
            dont_have.append("Urgency")

    plan_manifest = merge_info_manifests(base_manifest, {"have": have, "dont_have": dont_have})
    return PreCallPlan(info_manifest=plan_manifest, resolved=resolved)


def _resolve_owner_caller_name(*, profile: dict[str, str], caller_name: str) -> str:
    profile_name = validate_caller_name(profile.get("name", ""))
    if profile_name and is_assistant_caller_name(profile_name):
        profile_name = None
    candidate = validate_caller_name(caller_name)
    if candidate and is_assistant_caller_name(candidate):
        candidate = None
    return profile_name or candidate or ""


def _task_category(task_lower: str) -> str:
    is_order = any(term in task_lower for term in _ORDER_TERMS)
    is_food = any(term in task_lower for term in _FOOD_ORDER_TERMS)
    if is_order and "delivery" in task_lower:
        return "delivery"
    if is_order and (is_food or "pickup" in task_lower or "pick up" in task_lower):
        return "pickup_food_order"
    if any(term in task_lower for term in _APPOINTMENT_TERMS):
        return "appointment"
    if any(term in task_lower for term in _SERVICE_TERMS):
        return "service_request"
    return "general"


async def _load_profile_context(user_id: str) -> dict[str, str]:
    cloud_settings = await load_cloud_user_settings_blob(user_id)
    if cloud_settings is not None:
        name = _clean_text(first_setting_value(cloud_settings, ("user_name", "full_name")))
        address = _format_address(first_setting_value(cloud_settings, ("home_address", "delivery_address")))
        delivery_address = _format_address(first_setting_value(cloud_settings, ("delivery_address", "home_address")))
        callback = _clean_text(first_setting_value(cloud_settings, ("callback_phone",)))
        return {
            "name": name,
            "address": address,
            "delivery_address": delivery_address,
            "callback": callback,
        }

    settings_manager = get_settings_manager()
    name = _clean_text(_first_setting(settings_manager, user_id, ("user_name", "full_name")))
    address = _format_address(_first_setting(settings_manager, user_id, ("home_address", "delivery_address")))
    delivery_address = _format_address(_first_setting(settings_manager, user_id, ("delivery_address", "home_address")))
    callback = _clean_text(_first_setting(settings_manager, user_id, ("callback_phone",)))
    return {
        "name": name,
        "address": address,
        "delivery_address": delivery_address,
        "callback": callback,
    }


async def _resolve_nearest_store_address(*, business_name: str, profile_address: str) -> str:
    if not profile_address:
        return ""
    query = "nearest %s pickup store address near %s" % (business_name, profile_address)
    try:
        from intent.tools.web_search import web_search

        result = await web_search(query)
    except Exception as exc:
        logger.warning("Pre-call store search failed for %s: %s", business_name, exc)
        return ""
    if not getattr(result, "ok", False):
        logger.warning("Pre-call store search returned no usable result for %s: %s", business_name, result.error)
        return ""
    return _extract_address_from_search_data(getattr(result, "data", None))


def _extract_business_name(task: str) -> str:
    for pattern, label in _KNOWN_BUSINESSES:
        if pattern.search(task):
            return label

    match = re.search(r"\b(?:[Cc]all|at|from|with|for)\s+([A-Z][A-Za-z0-9'&.\- ]{1,60})", task)
    if not match:
        return ""
    candidate = match.group(1).strip(" .")
    candidate = re.split(
        r"\b(?:and|for|to|on|ready|pickup|delivery|tomorrow|today)\b",
        candidate,
        maxsplit=1,
    )[0].strip()
    return candidate


def _extract_address(text: str) -> str:
    match = _ADDRESS_RE.search(text or "")
    return match.group(0).strip(" .,") if match else ""


def _extract_address_from_search_data(data: Any) -> str:
    candidates: list[str] = []
    if isinstance(data, dict):
        for key in ("address", "formatted_address", "store_address"):
            value = _clean_text(data.get(key))
            if value:
                candidates.append(value)
        results = data.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict):
                    candidates.extend(_clean_text(result.get(key)) for key in ("address", "snippet", "title"))
                else:
                    candidates.append(str(result))
    elif isinstance(data, list):
        candidates.extend(str(item) for item in data)

    for candidate in candidates:
        address = _extract_address(candidate)
        if address:
            return address
    return ""


def _extract_time_preference(task: str) -> str:
    text = task.strip()
    match = re.search(
        r"\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b[^.]*",
        text,
        re.IGNORECASE,
    )
    if match:
        return match.group(0).strip(" .")
    match = re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", text, re.IGNORECASE)
    return match.group(0) if match else ""


def _extract_service_type(task: str) -> str:
    for term in _SERVICE_TERMS:
        if term in task.lower():
            return term
    return ""


def _extract_urgency(task: str) -> str:
    task_lower = task.lower()
    for term in _URGENT_TERMS:
        if term in task_lower:
            return term
    return ""


def _first_setting(settings_manager: Any, user_id: str, keys: Iterable[str]) -> object:
    for key in keys:
        value = settings_manager.get(key, None, user_id=user_id)
        if value not in (None, "", {}):
            return value
    return None


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


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _extend_unique(target: list[str], values: Iterable[str]) -> None:
    seen = {item.lower() for item in target}
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        target.append(item)
        seen.add(key)
